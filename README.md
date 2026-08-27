# qwen-harness

A local agent harness for Qwen and other Ollama models: a Control Room GUI, a
Python tool loop, staged-and-verified autonomous edits, and a native desktop
app — all running on your own machine, bound to localhost, with no API keys.

Runs natively on **macOS** and **Windows**. One backend, one UI, one state
directory; the platform-specific pieces are the desktop shell and a handful of
system calls.

## Start here

| | macOS | Windows |
|---|---|---|
| Install once | `./setup.sh` | `.\setup.ps1` |
| Launch (browser) | `./wake-up.sh` | `.\wake-up.ps1` |
| Launch (desktop app) | `./script/build_and_run.sh` | `.\script\build_and_run.ps1` |
| Run the checks | `./script/test.sh` | `.\script\test.ps1` |
| Runbook | [RUNBOOK.md](RUNBOOK.md) | [RUNBOOK-WINDOWS.md](RUNBOOK-WINDOWS.md) |

Then open <http://127.0.0.1:8420/gui/>.

## Documentation

- **[RUNBOOK.md](RUNBOOK.md)** — what the harness does and why. The
  weighbridge and model handoff, the sandbox, effort levels and deadlines,
  context budgets, the activity drawer, output ratings, tool profiles,
  permission dialogs. Read this one first regardless of platform.
- **[RUNBOOK-WINDOWS.md](RUNBOOK-WINDOWS.md)** — every Windows command, and
  every place the Windows behaviour genuinely differs (the shell, the sandbox's
  confinement mode, timezone data, the PATH trap, the desktop app).
- **[PLAN-IMPROVEMENTS.md](PLAN-IMPROVEMENTS.md)** — what to build next,
  derived from reading [Qwen-MM-Plugins](https://github.com/QwenLM/Qwen-MM-Plugins)
  and [qwen-code](https://github.com/QwenLM/qwen-code).

## Layout

```
agent.py              the Agent class: tool loop, model swapping
config.py             model registry + ACTIVE_MODEL
orchestrator.py       multi-agent orchestration
tools/                the tool registry every runtime shares
  platform_support.py the one place that knows which OS this is
  sysinfo.py          RAM / CPU / model-runner stats, per platform
  sandbox.py          staged, verified, promoted agent edits
  policy.py           path grants and secret-file blocking
gui/                  the Control Room (static frontend + server.py backend)
Sources/              the macOS desktop app (Swift + WKWebView)
windows/              the Windows desktop app (C# + WebView2)
skills/               portable instruction-only Agent Skills
tests/                unittest suite, run by both test scripts
```

## Requirements

Python 3.9+, [Ollama](https://ollama.com), and — only for the desktop app and
the JS syntax checks — Node.js plus a platform toolchain (Swift on macOS, the
.NET 8 SDK and WebView2 Runtime on Windows).

State lives in `~/.qwen-harness/`: grants, agents, projects, memory, ratings,
profile, and secrets. Secrets are never written into this repo or into browser
storage.
