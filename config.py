"""
config.py – model registry and active-model selection.

To swap models, change ACTIVE_MODEL to any key in MODELS,
or call agent.swap_model("key") at runtime.
"""

import os
from dataclasses import dataclass, field
from typing import Optional

# Ollama binds 127.0.0.1 only, and on Windows `localhost` resolves to ::1
# first. Measured on this machine: a urllib request to
# http://localhost:11434/api/tags takes 2133 ms because the ::1 connection has
# to fail before the IPv4 fallback is tried; the same request to
# http://127.0.0.1:11434/api/tags takes 8 ms. A 266x penalty, paid on EVERY
# inference call and on every 3-second poll of the resource monitor.
#
# So the address is the literal IPv4 everywhere, not a hostname. OLLAMA_HOST
# still overrides it for anyone pointing at a remote Ollama.
OLLAMA_BASE_URL: str = os.environ.get(
    "OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/") + "/v1"


@dataclass
class ModelConfig:
    name: str           # model identifier sent to the API
    base_url: str       # OpenAI-compatible endpoint
    api_key: str        # "ollama" for local; real key for cloud APIs
    max_tokens: int = 8192
    temperature: float = 0.6
    # Extra kwargs forwarded to client.chat.completions.create()
    extra: dict = field(default_factory=dict)


# ── Local models (via Ollama) ─────────────────────────────────────────────────
# Install: ollama pull <name>
# This is the harness's canonical light-model roster — one primary model
# (qwen3:4b) plus four very light options for sub-agents that just route,
# extract, or do narrow tool-calling in orchestrator.py.
# RAM guide (same on Apple Silicon and on a Windows box):
#  0.6B/1B  → needs ~1 GB   → trivial anywhere
#   2B      → needs ~2 GB   → safe on 8 GB
#   4B      → needs ~3 GB   → safe on 8 GB
#
# What more RAM buys, and what it does not. The Windows machine this was
# ported to has 16 GB rather than 8, and tools/context_budget.py sizes
# num_ctx from real RAM — so qwen3:4b loads at 49152 tokens of context there
# instead of 16384. That is a real difference: it is the setting that decides
# whether a long file plus the tool schemas plus the conversation all fit, and
# overflow is silent (Ollama drops the oldest content rather than erroring).
#
# It buys no speed at all. Generation rate is bound by memory bandwidth and
# cores, not capacity, and a CPU-only x86 laptop is if anything slower per
# token than the Apple Silicon machine these numbers were measured on. So the
# ACTIVE_MODEL reasoning below still holds unchanged on 16 GB.
MODELS: dict[str, ModelConfig] = {

    # ── Primary ─────────────────────────────────────────────────
    "qwen3:4b": ModelConfig(
        name="qwen3:4b",
        base_url=OLLAMA_BASE_URL,
        api_key="ollama",
    ),

    # ── Light agents (routing / tool-calling / sub-agents) ────────
    "qwen3:0.6b": ModelConfig(
        name="qwen3:0.6b",
        base_url=OLLAMA_BASE_URL,
        api_key="ollama",
    ),
    "gemma3:1b": ModelConfig(
        name="gemma3:1b",
        base_url=OLLAMA_BASE_URL,
        api_key="ollama",
    ),
    "llama3.2:1b": ModelConfig(
        name="llama3.2:1b",
        base_url=OLLAMA_BASE_URL,
        api_key="ollama",
    ),
    # The default llama3.2:1b tag is Q8_0 (~1.3 GB) while everything else here
    # is Q4 — on a machine that is already swapping, the Q4 build is roughly
    # half the size and correspondingly lighter.
    #   ollama pull llama3.2:1b-instruct-q4_K_M
    "llama3.2:1b-instruct-q4_K_M": ModelConfig(
        name="llama3.2:1b-instruct-q4_K_M",
        base_url=OLLAMA_BASE_URL,
        api_key="ollama",
    ),
    "gemma2:2b": ModelConfig(
        name="gemma2:2b",
        base_url=OLLAMA_BASE_URL,
        api_key="ollama",
    ),

    # ── Cloud models (requires API keys) ─────────────────────
    "gpt-4o": ModelConfig(
        name="gpt-4o",
        base_url="https://api.openai.com/v1",
        api_key="YOUR_OPENAI_KEY",   # replace or set OPENAI_API_KEY env var
    ),
    "gpt-4o-mini": ModelConfig(
        name="gpt-4o-mini",
        base_url="https://api.openai.com/v1",
        api_key="YOUR_OPENAI_KEY",
    ),
    # Note: Anthropic uses a different SDK (anthropic), not OpenAI-compatible.
    # See examples/05_anthropic_agent.py for that path.
}

# ── Active model ──────────────────────────────────────────────────────────────
# Change this one line to switch all agents globally.
# Measured on an 8 GB CPU-only Mac (generation speed, same machine/session):
#   qwen3:0.6b    70.4 tok/s
#   llama3.2:1b   63.6 tok/s
#   qwen3:4b       7.3 tok/s   <- ~10x slower
#
# Agentic turns chain many generations (tool call -> result -> next call), so
# that 10x compounds: the same task that finishes in under a minute on 0.6b
# ran past 10 minutes on 4b. qwen3:4b is the better writer, but it is not
# viable as the default for tool-using work on this hardware — switch to it
# per-agent in the Agent Hub when quality matters more than latency.
ACTIVE_MODEL: str = "qwen3:0.6b"


def get_config(key: Optional[str] = None) -> ModelConfig:
    """Return config for the given key, or the active model."""
    key = key or ACTIVE_MODEL
    if key not in MODELS:
        raise ValueError(f"Unknown model key '{key}'. Available: {list(MODELS)}")
    return MODELS[key]
