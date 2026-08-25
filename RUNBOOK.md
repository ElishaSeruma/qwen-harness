# qwen-harness — command runbook

Every command needed to set up, run, and manage this harness, in order.
Run these yourself in a terminal — nothing here downloads or starts anything automatically.

## 1. One-time setup (installs Ollama, Python deps, Playwright)

```bash
cd /Applications/qwen-harness
./setup.sh
```

This installs Ollama (if missing), starts `ollama serve`, and pulls the light model
roster below. You can skip it and run the pulls manually instead — see §2.

## 2. Pull the light model roster (8 GB RAM friendly)

One primary model plus four very light models for sub-agents that just route,
extract, or do narrow tool-calling in `orchestrator.py`.

```bash
ollama pull qwen3:4b      # ~2.5 GB — primary model
ollama pull qwen3:0.6b    # ~0.5 GB — lightest agent
ollama pull gemma3:1b     # ~0.8 GB — light agent
ollama pull llama3.2:1b   # ~1.3 GB — light agent
ollama pull gemma2:2b     # ~1.6 GB — light agent
```

Watch progress live — each command prints a download bar and blocks until done.

## 3. Remove models you no longer need

```bash
ollama list                # see what's actually on disk
ollama rm qwen3:8b         # drop a model you've replaced
du -sh ~/.ollama/models    # check total disk usage
```

`ollama rm` garbage-collects the blobs that model was using — no manual blob
cleanup needed. An interrupted `ollama pull` just resumes (or restarts) next
time you pull that same model, so there's nothing to "clean up" from a failed pull.

## 4. Start the harness

```bash
cd /Applications/qwen-harness
./wake-up.sh
```

This starts `ollama serve` (if not already running), starts the GUI backend
(`gui/server.py`) on port 8420, and opens `http://127.0.0.1:8420/gui/` in
your browser.

Pass a different port if 8420 is taken: `./wake-up.sh 9000`.

### Start as a desktop app

```bash
cd /Applications/qwen-harness
./script/build_and_run.sh
```

This builds a native macOS wrapper at `dist/Qwen Harness.app`, starts/reuses
Ollama, starts/reuses the same GUI backend, and opens the existing Control Room
inside a desktop window. The browser flow above still works unchanged.

To build, launch, and confirm the app process started:

```bash
./script/build_and_run.sh --verify
```

### Starting pieces manually instead

```bash
# Terminal 1 — model server
ollama serve

# Terminal 2 — GUI backend + static file server (also serves the API the
# Code page's filesystem browser, terminal, and file editor depend on)
cd /Applications/qwen-harness
python3 gui/server.py 8420

# then open http://127.0.0.1:8420/gui/ in a browser
```

## 5. Check everything is actually up

```bash
curl -s http://127.0.0.1:8420/api/health   # GUI backend
curl -s http://localhost:11434/api/tags    # Ollama + which models are pulled
```

The Control Room's topbar shows the same two checks live (Server / Ollama /
active-model status pills), polled automatically every few seconds.

## 6. Run the raw Python examples (no GUI)

```bash
cd /Applications/qwen-harness
python examples/01_hello.py
python examples/02_with_tools.py
python examples/03_multi_agent.py
python examples/04_computer_use.py
python examples/05_swap_models.py
```

## 7. Swap the active model

One line in [config.py](config.py):

```python
ACTIVE_MODEL: str = "qwen3:4b"   # change to any key in MODELS
```

Or per-agent, in the GUI's Agent Hub page (each agent has its own Model
dropdown), or at runtime in Python: `agent.swap_model("gemma3:1b")`.

## 8. Connect GitHub / use web search

No terminal commands needed — both live in the capability bar above any
composer (Home, Code chat, Agent Hub console):

- **GitHub** — click the GitHub button, paste a [Personal Access
  Token](https://github.com/settings/tokens/new) (`repo` scope covers read +
  issues). Stored locally at `~/.qwen-harness/secrets.json` (chmod 600),
  never in the browser or in this repo. The same token is used by the
  Python agents' `github_status` / `github_list_repos` / `github_list_dir` /
  `github_get_file` tools (read-only for now).
- **Web search** — click the Web search button, type a query. Uses
  DuckDuckGo's HTML search (no API key). Each result has an "Insert
  summary →" button that scrapes the page and drops its text into whichever
  composer you last focused. Agents can also call `web_search` / `scrape_url`
  directly.

### Why a chat model sometimes answered instead of searching

Asking "search the web for X" in a chat composer used to get a text reply
rather than a `web_search` call. Three causes, all fixed:

1. `inferEffort()` scored a short prompt as Quick (1/4), and Quick appended
   *"Avoid unnecessary planning or tool calls"* to the system prompt — the
   harness telling the model not to do what was just asked. `wantsToolUse()`
   in `gui/effort.js` now detects an explicit tool/web request and floors the
   level at Focus (2/4), and at Quick the anti-tool line is swapped for one
   that still permits the call.
2. The "Tool usage guidance" block in `gui/agent-loop.js` only covered file
   and code tools. It now states when to reach for `web_search` /
   `scrape_url`, and that the model does have internet access through them.
3. The `code` and `minimal` tool profiles omit the web tools to keep the
   prompt small, so on the Code surface there was no `web_search` to call.
   They're now spliced back in for any turn whose prompt is plainly about
   the internet.

If a model still won't call a tool, check in order: the "Agent tools"
capability toggle is on; the model reports the `tools` capability
(`curl -s localhost:11434/api/tags | grep -A2 capabilities`); and the tool is
in the surface's profile in `TOOL_PROFILES`.

To disconnect GitHub or check status from a terminal instead:

```bash
curl -s http://127.0.0.1:8420/api/github/status
curl -s -X POST http://127.0.0.1:8420/api/github/disconnect
```

## 8b. The Tools button (topbar)

`Tools NN` in the topbar opens a drawer listing every entry in
`TOOL_DEFINITIONS` as **Friendly name → `tool_id`** (Web search →
`web_search`, Read file → `read_file`), grouped by area, with a filter box.
Click a row to copy the tool id.

Two things it tells you that are otherwise invisible:

- **Surface badges** on each row — which of the `TOOL_PROFILES` in
  `gui/agent-loop.js` actually offer that tool. A tool with no badge is
  reachable only via `tool_search`, and a tool missing from your current
  surface simply won't be offered to the model on that screen.
- **The status line at the top** — warns when the "Agent tools" toggle is off
  or when the active model has no tool-calling support, the two conditions
  that stop every tool from working regardless of what the list shows.

The friendly names are derived from the tool ids, so tools added to
`TOOL_DEFINITIONS` appear here without touching `gui/tools-panel.js`.

## 9. Tools available to agents (Python side)

`tools/__init__.py`'s `TOOL_DEFINITIONS` is what gets handed to the model each
turn — the model decides which to call, `execute_tool()` dispatches it. As of
this roster (31 tools) it's worth using `tool_search` once you're at this
scale rather than eyeballing the list. Current ones beyond computer/browser/
file/GitHub/web control:

- **`memory_write` / `memory_read` / `memory_search` / `memory_list` /
  `memory_forget`** — persistent, cross-session store at
  `~/.qwen-harness/memory.json` (unlike `agent.messages`, which resets every
  process run). Use `category="user_edit"` when writing a correction you made
  to agent output, `"preference"` for stated preferences, `"fact"` for
  everything else — there's no separate tool for this, just a category tag
  on the same store.
- **`fetch_data`** — call a URL/JSON API directly (GET/POST/etc.), response
  JSON-parsed when possible. Distinct from `scrape_url`, which is for HTML
  pages you want as readable text.
- **`ask_user`** — pauses and asks a clarifying question. Terminal/CLI only
  (blocks on `input()`) — only call this from an interactive script, not from
  the GUI's browser-side chat (which doesn't run this Python tool loop — see
  the architecture note below).
- **`create_display`** — renders an HTML fragment (table, report, summary) as
  a page under `/tmp/harness_displays/` and opens it, instead of a wall of
  plain text.
- **`tool_search`** — searches the tool list above by keyword instead of
  scanning all of it; returns the best-matching names + descriptions.
- **`edit_file`** — surgical in-place edit (replace an exact `old_string`
  with `new_string`, rest of file untouched). The primary way agents change
  existing code — safer than `write_file` full rewrites. Errors helpfully on
  no-match and on ambiguous matches (`replace_all` to override).
- **`search_files` / `find_files`** — recursive content grep (file:line
  matches, optional `*.py`-style filter) and filename-glob search. How agents
  locate code before editing instead of guessing paths.
- **`append_file`** — append to a file (now registered; previously dead code).

**Approvals.** Write/edit/shell/computer tools are gated server-side
(`TOOL_APPROVAL_SCOPES` in `gui/server.py`). By default each new scope asks
once via a confirm dialog; approving grants that scope for the rest of the
browser session. The **🛡 Auto-approve** capability-bar toggle skips the
prompt entirely — useful for long autonomous coding runs, off by default.
Grants are in-memory per tab and never written to disk.

**Small-model compatibility.** Many local models emit a tool call as plain
text (`{"type":"function","function":{...}}`) instead of the structured
`tool_calls` field — llama3.2 does this consistently. `gui/agent-loop.js`
salvages those back into real calls (handles `<tool_call>` tags, fenced JSON,
and the `arguments`/`parameters`/`_parameters` key variants), so these models
actually work instead of printing JSON at you.

**Prompt budget.** Sending all 35 tool schemas costs ~3k tokens per request —
enough to stall a 4B model on an 8GB CPU-only Mac. Each surface sends only a
relevant profile (`TOOL_PROFILES` in `gui/agent-loop.js`): ~1.4k tokens for
the code profile. `tool_search` is always included so agents can still reach
anything not in their profile.

**Path safety.** `tools/policy.py`'s `require_path()` resolves every
agent-supplied path against the agent's working directory, refuses paths
outside granted roots, and blocks secret files (`.env`, `*.pem`, `*.key`).

When the GUI chat loop runs with tools on, the system prompt automatically
teaches this workflow (read → edit_file → verify; search before guessing) and
tells the model its working directory (active project path, selected
directory, or Code-page tree root).

**Architecture note:** the same registry serves both runtimes. `agent.py`'s
Python tool loop uses it directly (`examples/*.py`, `orchestrator.py`), and
the GUI's chat loop (`gui/agent-loop.js`) uses it over HTTP via
`/api/tools/list` + `/api/tools/call` — so agents chatting in Home, Code,
Projects, or the Agent Hub console can call every tool above, up to 12
chained calls per turn. The "Agent tools" capability-bar toggle turns this
off per session; models that can't do tool-calling (gemma3/gemma2) fall back
to plain chat automatically with a visible note.

## 10. Stop everything

```bash
pkill -f "gui/server.py"
pkill -x ollama
```
