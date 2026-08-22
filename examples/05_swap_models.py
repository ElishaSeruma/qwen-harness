"""
05_swap_models.py – demonstrate hot-swapping models mid-conversation.

The agent keeps its full conversation history when you swap models,
so the new model picks up exactly where the last one left off.
"""
import sys
sys.path.insert(0, "..")

from agent import Agent

agent = Agent(
    name="swappable",
    system_prompt="You are a concise assistant. Keep answers under 3 sentences.",
    tools=[],
)

print(f"Starting on: {agent.model}")
r1 = agent.chat("Tell me one interesting fact about the Moon.")
print(f"[{agent.model}] {r1}\n")

# Swap to a smaller model
agent.swap_model("qwen3:8b")
print(f"Swapped to: {agent.model}")
r2 = agent.chat("Now tell me one about Mars.")
print(f"[{agent.model}] {r2}\n")

# Swap back
agent.swap_model("qwen3:14b")
print(f"Swapped back to: {agent.model}")
r3 = agent.chat("Summarise both facts in one sentence.")
print(f"[{agent.model}] {r3}\n")

print(f"Total conversation turns: {len(agent.messages)}")
