"""
config.py – model registry and active-model selection.

To swap models, change ACTIVE_MODEL to any key in MODELS,
or call agent.swap_model("key") at runtime.
"""

from dataclasses import dataclass, field
from typing import Optional


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
# This is the harness's canonical light-model roster for 8 GB Macs — one
# primary model (qwen3:4b) plus four very light options for sub-agents that
# just route, extract, or do narrow tool-calling in orchestrator.py.
# Apple Silicon RAM guide:
#  0.6B/1B  → needs ~1 GB   → trivial on any Mac
#   2B      → needs ~2 GB   → safe on 8 GB Mac
#   4B      → needs ~3 GB   → safe on 8 GB Mac
MODELS: dict[str, ModelConfig] = {

    # ── Primary ─────────────────────────────────────────────────
    "qwen3:4b": ModelConfig(
        name="qwen3:4b",
        base_url="http://localhost:11434/v1",
        api_key="ollama",
    ),

    # ── Light agents (routing / tool-calling / sub-agents) ────────
    "qwen3:0.6b": ModelConfig(
        name="qwen3:0.6b",
        base_url="http://localhost:11434/v1",
        api_key="ollama",
    ),
    "gemma3:1b": ModelConfig(
        name="gemma3:1b",
        base_url="http://localhost:11434/v1",
        api_key="ollama",
    ),
    "llama3.2:1b": ModelConfig(
        name="llama3.2:1b",
        base_url="http://localhost:11434/v1",
        api_key="ollama",
    ),
    # The default llama3.2:1b tag is Q8_0 (~1.3 GB) while everything else here
    # is Q4 — on a machine that is already swapping, the Q4 build is roughly
    # half the size and correspondingly lighter.
    #   ollama pull llama3.2:1b-instruct-q4_K_M
    "llama3.2:1b-instruct-q4_K_M": ModelConfig(
        name="llama3.2:1b-instruct-q4_K_M",
        base_url="http://localhost:11434/v1",
        api_key="ollama",
    ),
    "gemma2:2b": ModelConfig(
        name="gemma2:2b",
        base_url="http://localhost:11434/v1",
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
