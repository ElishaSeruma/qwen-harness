"""
agent.py – core Agent class with tool-use loop and model swapping.

Usage:
    from agent import Agent
    agent = Agent(name="assistant", model_key="qwen3:14b")
    response = agent.chat("Search the web for the best coffee shops in Dublin")
"""

from __future__ import annotations
import json
from typing import Optional

from openai import OpenAI

from config import get_config, ModelConfig
from tools import TOOL_DEFINITIONS, execute_tool


class Agent:
    """
    A single LLM agent with an autonomous tool-use loop.

    Parameters
    ----------
    name        : Human-readable label (shown in logs)
    model_key   : Key from config.MODELS (defaults to ACTIVE_MODEL)
    system_prompt: Role / instructions for this agent
    tools       : Tool schemas to expose; pass [] for a chat-only agent
    thinking    : Enable Qwen3's chain-of-thought thinking mode
    verbose     : Print tool calls and responses
    """

    def __init__(
        self,
        name: str = "agent",
        model_key: Optional[str] = None,
        system_prompt: str = "You are a helpful AI assistant.",
        tools: Optional[list] = None,
        thinking: bool = False,
        verbose: bool = True,
    ):
        self.name = name
        self.verbose = verbose
        self.thinking = thinking
        self._system_prompt_base = system_prompt
        self.tools = tools if tools is not None else TOOL_DEFINITIONS

        self._load_model(model_key)

        # Conversation history
        self.messages: list[dict] = []
        self._reset_messages()

    # ── Model management ──────────────────────────────────────────────────────

    def _load_model(self, model_key: Optional[str]):
        self.model_key = model_key
        config: ModelConfig = get_config(model_key)
        self.config = config
        self.model = config.name
        self.client = OpenAI(
            base_url=config.base_url,
            api_key=config.api_key,
        )
        if self.verbose:
            print(f"[{self.name}] Model: {self.model} @ {config.base_url}")

    def swap_model(self, model_key: str):
        """Hot-swap to a different model. Conversation history is preserved."""
        self._load_model(model_key)
        if self.verbose:
            print(f"[{self.name}] → Swapped to {model_key}")

    def _reset_messages(self):
        system = self._system_prompt_base
        # Qwen3: prepend /no_think to disable reasoning mode for faster tool use
        if not self.thinking and "qwen3" in self.model.lower():
            system = "/no_think\n\n" + system
        self.messages = [{"role": "system", "content": system}]

    def reset(self):
        """Clear conversation history and start fresh."""
        self._reset_messages()

    # ── Core chat loop ────────────────────────────────────────────────────────

    def chat(self, user_message: str, max_iterations: int = 25) -> str:
        """
        Send a message and run the tool-use loop until the model
        produces a final text response (no more tool calls).
        """
        self.messages.append({"role": "user", "content": user_message})

        for i in range(max_iterations):
            # ── API call ──────────────────────────────────────
            kwargs: dict = {
                "model": self.model,
                "messages": self.messages,
                "max_tokens": self.config.max_tokens,
                "temperature": self.config.temperature,
                **self.config.extra,
            }
            if self.tools:
                kwargs["tools"] = self.tools
                kwargs["tool_choice"] = "auto"

            response = self.client.chat.completions.create(**kwargs)
            message = response.choices[0].message

            # Store the raw assistant message
            raw = message.model_dump(exclude_unset=False)
            # Normalize: ensure tool_calls is None if absent (some models omit it)
            raw.setdefault("tool_calls", None)
            self.messages.append(raw)

            # ── No tool calls → final answer ──────────────────
            if not message.tool_calls:
                content = message.content or ""
                # Strip <think>…</think> blocks from visible output
                content = _strip_thinking(content)
                return content

            # ── Execute tool calls ────────────────────────────
            for tc in message.tool_calls:
                fn_name = tc.function.name
                fn_args = json.loads(tc.function.arguments or "{}")

                if self.verbose:
                    print(f"[{self.name}] 🔧 {fn_name}({_pretty_args(fn_args)})")

                result = execute_tool(fn_name, fn_args)

                if self.verbose:
                    print(f"[{self.name}]    → {str(result)[:200]}")

                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result),
                })

        return "[max_iterations reached]"

    # ── Streaming chat (optional) ─────────────────────────────────────────────

    def stream(self, user_message: str):
        """Simple streaming – yields text chunks. Does NOT run tool use."""
        self.messages.append({"role": "user", "content": user_message})
        with self.client.chat.completions.stream(
            model=self.model,
            messages=self.messages,
            max_tokens=self.config.max_tokens,
        ) as stream:
            full = ""
            for chunk in stream.text_stream:
                full += chunk
                yield chunk
        self.messages.append({"role": "assistant", "content": full})

    # ── Introspection ─────────────────────────────────────────────────────────

    def __repr__(self):
        return f"Agent(name={self.name!r}, model={self.model!r}, turns={len(self.messages)})"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strip_thinking(text: str) -> str:
    """Remove <think>…</think> blocks that Qwen3 may include in output."""
    import re
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _pretty_args(args: dict) -> str:
    s = json.dumps(args)
    return s[:120] + "…" if len(s) > 120 else s
