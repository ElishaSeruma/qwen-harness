"""
tools/context_budget.py — how much context this machine can actually afford.

Ollama defaults to a 4k context on any machine with under 24 GiB of VRAM
(https://docs.ollama.com/context-length). On an 8 GB Mac that is what you get,
regardless of the 262,144 tokens a model like qwen3:4b advertises.

4k is not enough to hold a system prompt with tool schemas AND a source file
AND the conversation. When the prompt overflows, Ollama does not error — it
drops the oldest content. The model then answers a question it can no longer
see, which reads as a bizarre hallucination rather than as truncation. That is
the failure this module exists to prevent.

The coefficients below are fitted to measurements taken on this project's
8 GB machine with qwen3:4b (q4_K_M), reading total resident size from
`/api/ps` after loading at each context size:

    num_ctx   4096 -> 3.17 GB
    num_ctx  16384 -> 5.29 GB
    num_ctx  32768 -> 8.45 GB   (over 8 GB — would swap)

That gives ~0.173 GB per 1k tokens of KV cache and ~2.46 GB of weights for a
4B q4 model, which the per-billion constants below reproduce.
"""

from __future__ import annotations

import re
from typing import Any

from tools.platform_support import IS_WINDOWS
from tools.sysinfo import total_memory_gb

# Fitted from the measurements in the docstring, expressed per billion params
# so they extrapolate to the rest of the roster.
WEIGHTS_GB_PER_B = 0.62      # q4_K_M weights
KV_GB_PER_B_PER_1K = 0.043   # KV cache, per 1k tokens of context

# Left for the OS, the harness, a browser, and whatever else you are doing —
# the machine is a laptop you are also using, not a dedicated inference box.
#
# Windows needs more headroom than macOS for the same felt experience: a
# freshly booted Windows 11 desktop sits around 4-5 GB resident before a
# browser is open, where macOS compresses aggressively and reports far less.
# Reserving the macOS 2.5 GB on Windows sized a context that fit the fitted
# formula and then swapped in practice.
RESERVED_GB = 4.0 if IS_WINDOWS else 2.5

MIN_CONTEXT = 4096           # never go below Ollama's own default
MAX_CONTEXT = 65536          # beyond this, prompt-eval latency hurts more than it helps
CONTEXT_STEPS = (4096, 8192, 12288, 16384, 24576, 32768, 49152, 65536)


def total_ram_gb() -> float:
    """Physical RAM in GB. Falls back to a conservative 8 GB if unknown.

    `os.sysconf` does not exist on Windows and `sysctl` is macOS-only, so this
    used to fall all the way through to the 8 GB default on a Windows machine
    — sizing every context as if half the RAM were missing. tools/sysinfo.py
    reads the real figure from GlobalMemoryStatusEx there.
    """
    return total_memory_gb(default=8.0)


def parse_param_billions(parameter_size: str | None) -> float | None:
    """Ollama reports mixed units — "4.0B" but also "751.63M"."""
    if not isinstance(parameter_size, str):
        return None
    match = re.match(r"^([\d.]+)\s*([BbMm])?$", parameter_size.strip())
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return value / 1000 if (match.group(2) or "B").upper() == "M" else value


def estimate_gb(param_billions: float, context: int) -> float:
    """Approximate resident size for a model at a given context."""
    return param_billions * (WEIGHTS_GB_PER_B + KV_GB_PER_B_PER_1K * context / 1024)


def recommend_context(
    param_billions: float | None,
    model_max: int | None = None,
    ram_gb: float | None = None,
) -> dict[str, Any]:
    """Largest context this machine can hold for a model, with the reasoning.

    Returns the chosen value plus every input that produced it, because a
    silently-chosen context is exactly the kind of invisible setting that makes
    truncation impossible to diagnose.
    """
    ram = ram_gb if ram_gb is not None else total_ram_gb()
    budget = max(1.5, ram - RESERVED_GB)

    if not param_billions or param_billions <= 0:
        # Unknown model size: stay at the default rather than guess high and
        # push the machine into swap.
        return {
            "context": MIN_CONTEXT,
            "reason": "model size unknown — staying at Ollama's default",
            "ram_gb": round(ram, 1), "budget_gb": round(budget, 2),
            "estimated_gb": None, "model_max": model_max,
        }

    ceiling = min(MAX_CONTEXT, model_max or MAX_CONTEXT)
    chosen = MIN_CONTEXT
    for step in CONTEXT_STEPS:
        if step > ceiling:
            break
        if estimate_gb(param_billions, step) <= budget:
            chosen = step
        else:
            break

    return {
        "context": chosen,
        "reason": (
            f"{param_billions:g}B model, {ram:.0f} GB RAM, {RESERVED_GB} GB reserved "
            f"for the rest of the system"
        ),
        "ram_gb": round(ram, 1),
        "budget_gb": round(budget, 2),
        "estimated_gb": round(estimate_gb(param_billions, chosen), 2),
        "model_max": model_max,
        "capped_by": (
            "model" if chosen == model_max
            else "policy" if chosen == MAX_CONTEXT
            else "memory"
        ),
    }


def plan_for_models(models: list[dict[str, Any]], ram_gb: float | None = None) -> dict[str, Any]:
    """A context recommendation per model, for the whole roster."""
    ram = ram_gb if ram_gb is not None else total_ram_gb()
    plan = {}
    for model in models or []:
        name = model.get("name")
        if not name:
            continue
        details = model.get("details") or {}
        billions = parse_param_billions(details.get("parameter_size"))
        model_max = details.get("context_length")
        plan[name] = recommend_context(billions, model_max, ram)
    return {"ram_gb": round(ram, 1), "reserved_gb": RESERVED_GB, "models": plan}
