#!/usr/bin/env python3
"""
qh — the harness, headless.

    qh -p "summarise config.py"
    qh -p "add a test for add()" --profile code --effort deep --json
    qh -p "fix the failing test" --sandbox --approve write_file,edit_file

Why this exists. Every capability in this harness lived behind the Control
Room: to run one turn you opened a browser. That made three things impossible
— running it in CI, running it from a script, and *measuring* it. The last one
matters most: you cannot honestly claim that learned conventions (§2 of
PLAN-IMPROVEMENTS.md) improve anything without a way to run the same prompt
twice under different settings and diff the result.

What it shares with the GUI, deliberately, rather than reimplementing:

  * `tools/profiles.py` — the same surface profiles and the same capability
    tiering, so a light model is withheld the same tools here as there.
  * `tools/sandbox.py` — the same stage/verify/promote guarantee. `--sandbox`
    stages writes exactly as an autonomous GUI run does.
  * `tools/policy.py` — the same path grants and secret-file blocking.
  * `tools/feedback.py` / `tools/conventions.py` — the same learned guidance.
  * `tools/skills.py` — the same skills, invoked the same way.

What it does NOT share: the browser agent loop in `gui/agent-loop.js`. That
remains the GUI's, and this uses `agent.py`'s Python loop. The two are
different implementations of the same contract, which is a real duplication —
`PLAN-IMPROVEMENTS.md` §6.3 costs the work of collapsing them.

**Approvals fail closed.** A GUI run asks you before a write; a headless run
has nobody to ask. Anything in TOOL_APPROVAL_SCOPES is refused unless named in
`--approve` (or all of them with `--approve all`). Refusing is reported to the
model as a tool error, so it can adapt rather than silently doing nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from tools import TOOL_DEFINITIONS, execute_tool                    # noqa: E402
from tools.profiles import TOOL_PROFILES, select_tools, tier_for, tier_name  # noqa: E402

# Mirrors EFFORT_LEVELS in gui/effort.js. Time is the primary limit, not
# tokens: at ~7 tok/s a token cap of any useful size bounds nothing real, and
# iteration caps are what actually stop a loop.
EFFORT = {
    "quick": {"level": 1, "seconds": 120,  "iterations": 6},
    "focus": {"level": 2, "seconds": 300,  "iterations": 10},
    "deep":  {"level": 3, "seconds": 720,  "iterations": 14},
    "max":   {"level": 4, "seconds": 1500, "iterations": 18},
}

# Same scopes the server gates on. Duplicated as a literal rather than
# imported, because importing gui/server.py starts a whole HTTP stack.
# tests/test_qh.py asserts the two lists agree.
APPROVAL_SCOPES = {
    "write_file", "edit_file", "append_file", "run_shell",
    "click", "double_click", "type_text", "press_key", "scroll",
    "browser_navigate", "browser_click", "browser_type", "browser_evaluate",
    "fetch_data", "web_search", "scrape_url",
    "memory_write", "memory_forget", "create_display", "sandbox_promote",
}


class ApprovalDenied(RuntimeError):
    pass


def build_system_prompt(args, tier: int, withheld: list[str]) -> str:
    """The same layered prompt the GUI builds, in the same order.

    Order is load-bearing and matches gui/agent-loop.js: who the user is first
    (it frames everything else), then learned guidance, then the working
    directory, then the tool rules.
    """
    parts: list[str] = []

    if not args.no_profile:
        try:
            from tools.profile import describe
            who = describe()
            if who:
                parts.append(who)
        except Exception:
            pass

    parts.append(args.system or "You are a capable local coding assistant.")

    if not args.no_guidance:
        for source in ("feedback", "conventions"):
            try:
                if source == "feedback":
                    from tools.feedback import preference_guidance
                    text = preference_guidance()
                else:
                    from tools.conventions import convention_guidance
                    text = convention_guidance(args.cwd)
                if text:
                    parts.append(text)
            except Exception:
                continue

    parts.append(f"Your working directory is {args.cwd}. Paths are relative to it.")
    parts.append(
        "Never state a date, day, or time without calling date_time first — a "
        "fabricated timestamp looks entirely plausible, which is what makes it "
        "dangerous."
    )
    if withheld:
        parts.append(
            f"{len(withheld)} tool(s) are unavailable at this model's capability "
            f"tier ({tier_name(tier)}): {', '.join(sorted(withheld))}. Do not "
            "claim to have used them."
        )
    if args.sandbox:
        parts.append(
            "Your file writes are STAGED, not applied. Edit, then call "
            "sandbox_verify, then sandbox_promote. Promotion is refused until "
            "verification passes."
        )
    return "\n\n".join(part for part in parts if part)


def resolve_model(requested: str | None) -> tuple[str, int]:
    """(model_key, capability_tier). Tier is read from Ollama, never assumed."""
    from config import ACTIVE_MODEL, MODELS
    key = requested or ACTIVE_MODEL
    if key not in MODELS:
        raise SystemExit(f"unknown model '{key}'. Available: {', '.join(MODELS)}")

    # The tier decides which tools are withheld, so getting it from the live
    # server beats hard-coding a table that drifts as models are re-pulled.
    try:
        import urllib.request
        from tools.context_budget import parse_param_billions
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as res:
            models = json.load(res).get("models", [])
        for entry in models:
            if entry.get("name") in (key, f"{key}:latest"):
                billions = parse_param_billions((entry.get("details") or {}).get("parameter_size"))
                return key, tier_for(billions, entry.get("capabilities") or [])
    except Exception:
        pass
    # Unknown means light, which withholds the dangerous tools. Failing safe
    # here costs a capable model some tools; failing open hands a 0.6B model
    # `run_shell`.
    return key, tier_for(None, ["tools"])


def run(args) -> dict:
    from agent import Agent, _strip_thinking   # noqa: F401

    model_key, tier = resolve_model(args.model)
    budget = EFFORT[args.effort]

    tools, withheld = select_tools(
        TOOL_DEFINITIONS, args.profile, None if args.no_gating else tier
    )
    if args.no_tools:
        tools, withheld = [], []

    run_id = args.run_id or f"qh-{int(time.time())}"
    agent_id = args.agent or "main"
    approved = set()
    if args.approve:
        approved = APPROVAL_SCOPES if "all" in args.approve else set(args.approve)

    events: list[dict] = []

    def emit(kind: str, **fields):
        event = {"type": kind, "at": round(time.monotonic() - started, 2), **fields}
        events.append(event)
        if args.stream and not args.json:
            print(f"  · {kind}: {fields.get('name') or fields.get('text') or ''}",
                  file=sys.stderr, flush=True)

    started = time.monotonic()
    system = build_system_prompt(args, tier, withheld)

    agent = Agent(
        name="qh", model_key=model_key, system_prompt=system,
        tools=tools, thinking=args.think, verbose=False,
    )

    # The tool dispatcher: approvals, then the sandbox, then the real call.
    # Wrapping execute_tool rather than editing agent.py keeps the Python
    # examples in examples/*.py behaving exactly as before.
    def dispatch(name: str, tool_args: dict):
        emit("tool_call", name=name, args=tool_args)
        if name in APPROVAL_SCOPES and name not in approved:
            emit("tool_denied", name=name)
            return {
                "error": f"'{name}' needs approval and this is a headless run. "
                         f"Re-run with --approve {name} (or --approve all) if you "
                         f"intend it.",
                "approval_required": True,
            }
        call_args = dict(tool_args)
        call_args.setdefault("cwd", str(args.cwd))
        if args.sandbox:
            call_args["run_id"] = run_id
            call_args["agent_id"] = agent_id
            staged = _stage(name, call_args, run_id, agent_id, args.cwd)
            if staged is not None:
                emit("tool_result", name=name, staged=True)
                return staged
        try:
            result = execute_tool(name, call_args)
        except TypeError:
            # A tool that does not take cwd/run_id — call it with what it
            # declared rather than failing on an argument we added.
            result = execute_tool(name, tool_args)
        emit("tool_result", name=name, ok="error" not in (result or {}))
        return result

    import agent as agent_module
    original = agent_module.execute_tool
    agent_module.execute_tool = dispatch
    try:
        deadline = started + budget["seconds"]
        answer = agent.chat(args.prompt, max_iterations=budget["iterations"])
        timed_out = time.monotonic() > deadline
    finally:
        agent_module.execute_tool = original

    elapsed = round(time.monotonic() - started, 2)
    result = {
        "ok": True,
        "answer": answer,
        "model": model_key,
        "tier": tier_name(tier),
        "profile": args.profile,
        "effort": args.effort,
        "tools_offered": len(tools),
        "tools_withheld": sorted(withheld),
        "elapsed_s": elapsed,
        "timed_out": timed_out,
        "run_id": run_id if args.sandbox else None,
        "events": events,
    }
    if args.sandbox:
        from tools.sandbox import get_sandbox
        box = get_sandbox(run_id, args.cwd, agent_id=agent_id, create=False)
        result["sandbox"] = box.summary() if box else {"changes": []}
    return result


def _stage(name: str, call_args: dict, run_id: str, agent_id: str, cwd: Path):
    """Route a write through the sandbox, mirroring _apply_sandbox in server.py."""
    from tools.sandbox import SandboxError, get_sandbox
    WRITE_TOOLS = {"write_file", "edit_file", "append_file"}
    if name not in WRITE_TOOLS and name not in {"run_shell", "read_file"}:
        return None
    try:
        box = get_sandbox(run_id, cwd, agent_id=agent_id)
        if name == "run_shell":
            return {**box.run(call_args.get("command", "")), "sandboxed": True}
        target = Path(str(call_args.get("path", ""))).expanduser()
        target = target if target.is_absolute() else (Path(cwd) / target)
        if name == "read_file":
            staged = box.staged_path(target)
            if not staged.exists():
                return None      # nothing staged yet: read the real file
            return {**execute_tool("read_file", {"path": str(staged), "cwd": str(box.work)}),
                    "sandboxed": True, "staged": True}
        staged = box.materialize(target)
        out = execute_tool(name, {**call_args, "path": str(staged), "cwd": str(box.work)})
        if isinstance(out, dict) and out.get("error"):
            return {**out, "sandboxed": True}
        box.record(target)
        return {**out, "sandboxed": True, "staged_path": str(staged),
                "note": "Staged only. Run sandbox_verify, then sandbox_promote."}
    except SandboxError as exc:
        return {"error": str(exc), "sandboxed": True}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qh",
        description="Run one turn of the qwen-harness agent loop without the GUI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  qh -p 'what does config.py do?'\n"
            "  qh -p 'add a test for add()' --profile code --effort deep\n"
            "  qh -p 'fix the failing test' --sandbox --approve edit_file,run_shell\n"
            "  qh -p 'summarise the repo' --json | jq .answer\n"
        ),
    )
    parser.add_argument("-p", "--prompt", help="the instruction. Reads stdin if omitted.")
    parser.add_argument("-m", "--model", help="model key from config.MODELS (default: ACTIVE_MODEL)")
    parser.add_argument("--profile", default="home", choices=sorted(TOOL_PROFILES),
                        help="tool profile, as per surface in the GUI (default: home)")
    parser.add_argument("--effort", default="focus", choices=sorted(EFFORT),
                        help="time and iteration budget (default: focus)")
    parser.add_argument("-C", "--cwd", default=None, help="working directory (default: current)")
    parser.add_argument("--system", help="override the base system prompt")
    parser.add_argument("--think", action="store_true", help="allow the model to reason first")
    parser.add_argument("--json", action="store_true", help="emit the full result as JSON")
    parser.add_argument("--stream", action="store_true", help="print tool activity to stderr as it happens")
    parser.add_argument("--sandbox", action="store_true",
                        help="stage writes instead of applying them (stage/verify/promote)")
    parser.add_argument("--approve", type=lambda v: [x.strip() for x in v.split(",") if x.strip()],
                        default=[], metavar="TOOLS",
                        help="comma-separated tools to pre-approve, or 'all'. "
                             "Without this, write/shell tools are refused.")
    parser.add_argument("--run-id", dest="run_id", help="reuse an existing sandbox run")
    parser.add_argument("--agent", help="agent id, for a per-agent sandbox and boundary")
    parser.add_argument("--no-tools", action="store_true", help="plain chat, no tools")
    parser.add_argument("--no-gating", action="store_true",
                        help="offer the whole profile regardless of model capability tier")
    parser.add_argument("--no-guidance", action="store_true",
                        help="skip learned ratings/convention guidance")
    parser.add_argument("--no-profile", action="store_true", help="skip the user profile block")
    parser.add_argument("--list-profiles", action="store_true", help="print the tool profiles and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_profiles:
        for name, tools in sorted(TOOL_PROFILES.items()):
            print(f"{name:9s} {len(tools):2d} tools: {', '.join(tools)}")
        return 0

    if not args.prompt:
        # Reading stdin is what makes this composable: `cat issue.md | qh`.
        if sys.stdin.isatty():
            build_parser().print_help()
            return 2
        args.prompt = sys.stdin.read().strip()
    if not args.prompt:
        print("qh: empty prompt", file=sys.stderr)
        return 2

    args.cwd = Path(args.cwd).expanduser().resolve() if args.cwd else Path.cwd()
    if not args.cwd.is_dir():
        print(f"qh: not a directory: {args.cwd}", file=sys.stderr)
        return 2

    # The working directory has to be granted before any tool touches it, for
    # the same reason the GUI grants before validating: the policy layer
    # refuses ungranted paths before checking whether they exist, so an
    # ungranted cwd reports as a permissions problem rather than a real one.
    try:
        from tools.policy import PERMISSIONS_PATH, atomic_write_json, granted_roots, is_granted
        if not is_granted(args.cwd):
            roots = sorted({str(p) for p in granted_roots()} | {str(args.cwd)})
            atomic_write_json(PERMISSIONS_PATH, {"roots": roots})
    except Exception as exc:
        print(f"qh: could not grant {args.cwd}: {exc}", file=sys.stderr)
        return 1

    try:
        result = run(args)
    except ApprovalDenied as exc:
        print(f"qh: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("qh: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            print(f"qh: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(result["answer"])
        if result["tools_withheld"]:
            print(f"\n[{len(result['tools_withheld'])} tool(s) withheld at "
                  f"{result['tier']} tier: {', '.join(result['tools_withheld'])}]",
                  file=sys.stderr)
        if result.get("sandbox", {}).get("changes"):
            changed = ", ".join(c["path"] for c in result["sandbox"]["changes"])
            print(f"[staged, not applied: {changed} — run id {result['run_id']}]",
                  file=sys.stderr)
        if result["timed_out"]:
            print(f"[the {args.effort} budget ({EFFORT[args.effort]['seconds']}s) "
                  f"elapsed before this finished]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
