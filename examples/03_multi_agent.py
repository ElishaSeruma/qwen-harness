"""
03_multi_agent.py – orchestrate several agents working together.

Demonstrates:
  1. A sequential pipeline (researcher → writer → critic)
  2. Parallel execution of independent tasks
  3. Global model swap mid-session
"""
import sys
sys.path.insert(0, "..")

from orchestrator import Orchestrator

orch = Orchestrator(default_model="qwen3:14b")

# ── Register agents ───────────────────────────────────────────────────────────

orch.add(
    "researcher",
    role=(
        "You are a research assistant. When given a topic, produce a structured "
        "set of bullet-point facts. Be accurate and concise (max 300 words)."
    ),
    tools=[],  # chat-only
)

orch.add(
    "writer",
    role=(
        "You are a skilled writer. Transform research notes into a polished, "
        "engaging 200-word article. Use a professional but accessible tone."
    ),
    tools=[],
)

orch.add(
    "critic",
    role=(
        "You are a sharp editor. Review the article and return: "
        "(1) 3 specific strengths, (2) 2 concrete improvements."
    ),
    tools=[],
)

# ── Pipeline example ──────────────────────────────────────────────────────────

print("=" * 60)
print("PIPELINE: researcher → writer → critic")
print("=" * 60)

topic = "The impact of open-weight AI models on the software industry in 2025"

final = orch.pipeline([
    ("researcher", f"Research this topic: {topic}"),
    ("writer",     lambda prev: f"Write a 200-word article from these notes:\n\n{prev}"),
    ("critic",     lambda prev: f"Review this article:\n\n{prev}"),
])

print("\n── Final output (critic's review) ──────────────────────\n")
print(final)


# ── Parallel example ──────────────────────────────────────────────────────────

print("\n" + "=" * 60)
print("PARALLEL: 3 independent research tasks at once")
print("=" * 60)

results = orch.parallel([
    ("researcher", "What is Qwen3?"),
    ("researcher", "What is DeepSeek V4?"),   # re-uses the same agent sequentially
    ("writer",     "Write a one-sentence tagline for an AI harness tool."),
])

for agent_name, result in results.items():
    print(f"\n[{agent_name}]: {result[:300]}")


# ── Model swap example ────────────────────────────────────────────────────────

print("\n" + "=" * 60)
print("MODEL SWAP: switch all agents to qwen3:8b")
print("=" * 60)

orch.swap_all("qwen3:8b")
print(orch.status())

response = orch.ask("writer", "Write a haiku about AI agents.")
print(f"\nWriter (on qwen3:8b): {response}")
