"""
02_with_tools.py – agent with file system tools enabled.
Try asking it to read a file, list a directory, or run a command.
"""
import sys
sys.path.insert(0, "..")

from agent import Agent
from tools import TOOL_DEFINITIONS

# Only expose file tools (no computer/browser for this demo)
file_tools = [
    t for t in TOOL_DEFINITIONS
    if t["function"]["name"] in ("read_file", "write_file", "list_dir", "run_shell")
]

agent = Agent(
    name="file-agent",
    system_prompt=(
        "You are a helpful assistant with access to the local file system. "
        "Use the tools to answer questions about files and directories."
    ),
    tools=file_tools,
)

# Interactive loop
print("File agent ready. Type 'quit' to exit.\n")
while True:
    user_input = input("You: ").strip()
    if user_input.lower() in ("quit", "exit", "q"):
        break
    if not user_input:
        continue
    response = agent.chat(user_input)
    print(f"\nAgent: {response}\n")
