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
# Apple Silicon RAM guide:
#   8B  → needs ~5 GB  → safe on 8 GB Mac
#  14B  → needs ~9 GB  → good on 16 GB Mac
#  30B  → needs ~20 GB → good on 32 GB Mac (MoE: only 3B active per token)
MODELS: dict[str, ModelConfig] = {

    # ── Qwen3 local ───────────────────────────────────────────
    "qwen3:8b": ModelConfig(
        name="qwen3:8b",
        base_url="http://localhost:11434/v1",
        api_key="ollama",
    ),
    "qwen3:14b": ModelConfig(
        name="qwen3:14b",
        base_url="http://localhost:11434/v1",
        api_key="ollama",
    ),
    "qwen3:30b-a3b": ModelConfig(  # MoE – fastest large model on Mac
        name="qwen3:30b-a3b",
        base_url="http://localhost:11434/v1",
        api_key="ollama",
    ),

    # ── DeepSeek local ────────────────────────────────────────
    "deepseek-r1:8b": ModelConfig(
        name="deepseek-r1:8b",
        base_url="http://localhost:11434/v1",
        api_key="ollama",
    ),
    "deepseek-r1:14b": ModelConfig(
        name="deepseek-r1:14b",
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
ACTIVE_MODEL: str = "qwen3:14b"


def get_config(key: Optional[str] = None) -> ModelConfig:
    """Return config for the given key, or the active model."""
    key = key or ACTIVE_MODEL
    if key not in MODELS:
        raise ValueError(f"Unknown model key '{key}'. Available: {list(MODELS)}")
    return MODELS[key]
