"""
01_hello.py – simplest possible agent: just chat, no tools.
"""
import sys
sys.path.insert(0, "..")

from agent import Agent

agent = Agent(
    name="hello",
    system_prompt="You are a friendly assistant. Be concise.",
    tools=[],  # no tools for this example
)

response = agent.chat("What is the capital of France, and what is it known for?")
print(response)
