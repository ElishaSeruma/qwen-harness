"""
orchestrator.py – multi-agent coordinator.

The Orchestrator manages a pool of named Agents and provides:
  • Parallel execution   – run multiple agents at the same time
  • Sequential pipelines – chain agents, each seeing the previous output
  • Delegation           – one agent can hand a subtask to another by name
  • Global model swap    – switch every agent to a different model at once

Quick example:
    from orchestrator import Orchestrator

    orch = Orchestrator()

    orch.add("researcher", role="You research topics and return concise summaries.")
    orch.add("writer",     role="You turn research notes into polished prose.")
    orch.add("critic",     role="You critique text and suggest improvements.")

    result = orch.pipeline([
        ("researcher", "Research quantum computing breakthroughs in 2025"),
        ("writer",     lambda prev: f"Write a 200-word article from: {prev}"),
        ("critic",     lambda prev: f"Critique this article: {prev}"),
    ])
    print(result)
"""

from __future__ import annotations
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional, Union

from agent import Agent
from config import ACTIVE_MODEL
from tools import TOOL_DEFINITIONS


# Type alias for pipeline steps
Step = tuple[str, Union[str, Callable[[str], str]]]


class Orchestrator:
    """Manages a pool of named agents."""

    def __init__(
        self,
        default_model: Optional[str] = None,
        max_workers: int = 8,
        verbose: bool = True,
    ):
        self.default_model = default_model or ACTIVE_MODEL
        self.verbose = verbose
        self.agents: dict[str, Agent] = {}
        self._executor = ThreadPoolExecutor(max_workers=max_workers)

    # ── Agent management ──────────────────────────────────────────────────────

    def add(
        self,
        name: str,
        role: str,
        model_key: Optional[str] = None,
        tools: Optional[list] = None,
        thinking: bool = False,
    ) -> Agent:
        """
        Create and register a named agent.

        Parameters
        ----------
        name      : Unique name used to address this agent
        role      : System prompt / persona for this agent
        model_key : Model key from config.MODELS (defaults to orchestrator default)
        tools     : List of tool schemas; None = all tools, [] = no tools
        thinking  : Enable Qwen3 chain-of-thought reasoning
        """
        agent = Agent(
            name=name,
            model_key=model_key or self.default_model,
            system_prompt=role,
            tools=tools if tools is not None else TOOL_DEFINITIONS,
            thinking=thinking,
            verbose=self.verbose,
        )
        self.agents[name] = agent
        if self.verbose:
            print(f"[Orchestrator] Registered agent '{name}'")
        return agent

    def remove(self, name: str):
        """Remove a named agent."""
        self.agents.pop(name, None)

    def get(self, name: str) -> Agent:
        if name not in self.agents:
            raise KeyError(f"No agent named '{name}'. Available: {list(self.agents)}")
        return self.agents[name]

    # ── Messaging ─────────────────────────────────────────────────────────────

    def ask(self, agent_name: str, message: str) -> str:
        """Send a message to a named agent and return its response."""
        return self.get(agent_name).chat(message)

    # ── Parallel execution ────────────────────────────────────────────────────

    def parallel(
        self, tasks: list[tuple[str, str]]
    ) -> dict[str, str]:
        """
        Run multiple agent tasks concurrently.

        Parameters
        ----------
        tasks : list of (agent_name, message)

        Returns
        -------
        dict mapping agent_name → response (in completion order)
        """
        if self.verbose:
            names = [t[0] for t in tasks]
            print(f"[Orchestrator] Running {len(tasks)} tasks in parallel: {names}")

        futures = {
            self._executor.submit(self.ask, name, msg): name
            for name, msg in tasks
        }
        results: dict[str, str] = {}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as e:
                results[name] = f"[ERROR] {e}"
            if self.verbose:
                print(f"[Orchestrator] '{name}' finished")

        return results

    # ── Sequential pipeline ───────────────────────────────────────────────────

    def pipeline(self, steps: list[Step]) -> str:
        """
        Run agents sequentially, passing each output as input to the next.

        Each step is (agent_name, message_or_callable).
        If message is a callable, it receives the previous agent's output.

        Example:
            orch.pipeline([
                ("researcher", "Find facts about X"),
                ("writer", lambda prev: f"Write an article from: {prev}"),
            ])
        """
        result = ""
        for i, (agent_name, message) in enumerate(steps, 1):
            if callable(message):
                message = message(result)
            if self.verbose:
                print(f"[Orchestrator] Pipeline step {i}/{len(steps)}: '{agent_name}'")
            result = self.ask(agent_name, message)
        return result

    # ── Debate / critique loop ────────────────────────────────────────────────

    def debate(
        self,
        topic: str,
        agent_a: str,
        agent_b: str,
        rounds: int = 2,
    ) -> list[dict[str, str]]:
        """
        Run a structured debate between two agents.
        Returns a transcript of all exchanges.
        """
        transcript = []
        message = topic
        for r in range(rounds):
            a_response = self.ask(agent_a, message)
            transcript.append({"round": r + 1, "speaker": agent_a, "text": a_response})

            b_response = self.ask(agent_b, a_response)
            transcript.append({"round": r + 1, "speaker": agent_b, "text": b_response})

            message = b_response  # carry forward

        return transcript

    # ── Global operations ─────────────────────────────────────────────────────

    def swap_all(self, model_key: str):
        """Swap every registered agent to a different model."""
        print(f"[Orchestrator] Swapping all agents to '{model_key}'")
        for agent in self.agents.values():
            agent.swap_model(model_key)

    def reset_all(self):
        """Reset conversation history for all agents."""
        for agent in self.agents.values():
            agent.reset()
        if self.verbose:
            print("[Orchestrator] All agents reset")

    def shutdown(self):
        """Shut down the thread pool."""
        self._executor.shutdown(wait=False)

    # ── Introspection ─────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            name: {
                "model": agent.model,
                "turns": len(agent.messages),
            }
            for name, agent in self.agents.items()
        }

    def __repr__(self):
        return f"Orchestrator(agents={list(self.agents)}, default={self.default_model})"
