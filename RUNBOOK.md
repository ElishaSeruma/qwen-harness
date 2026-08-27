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

## 4b. The Finder PATH trap (read this before debugging "X is not installed")

A macOS app launched from Finder or the Dock does **not** inherit your shell's
PATH. It gets:

    PATH=/usr/bin:/bin:/usr/sbin:/sbin

Homebrew is `/opt/homebrew/bin`, so from inside the app `ollama`, `node` and
`npm` simply do not exist. That is why "Start services" reported *"ollama
binary not found — install it first"* on a machine with ollama installed and
working in Terminal.

Two defences, both in `gui/server.py`:

- `repair_path()` runs first thing in `main()` and appends the usual locations
  (Homebrew bin/sbin, /usr/local, ~/.local/bin, ~/.ollama/bin, the Ollama.app
  bundle) — only ones that actually exist. Every subprocess the server spawns
  inherits it, so this fixes sandbox test commands and git too, not just ollama.
- `find_ollama()` resolves an absolute path independently of PATH, and that
  absolute path is what gets exec'd.

**`from __future__ import annotations` in server.py is load-bearing.** The app
spawns whatever `python3` resolves to; under the bare PATH that is
`/usr/bin/python3` — Python **3.9** — where a `str | None` annotation is a hard
TypeError at import and the server never starts at all. `test_server_env.py`
guards both of these; run the suite under `/usr/bin/python3` before shipping.

## 4c. Permission dialogs

`requestPermission()` in `gui/app.js` renders a real `<dialog>` — identical in
a browser and in the Mac app, with focus trapping and Escape for free. It
shows the tool, the scope, and the actual arguments, and offers three choices
rather than two: **Deny / Allow once / Allow for session**.

Three things that are load-bearing:

- **No `window.confirm` fallback.** WKWebView only shows one if the host app
  implements the confirm-panel delegate. On a host that doesn't, `confirm()`
  returns `false` instantly — so every permission is silently DENIED and no
  dialog ever appears. The loop now fails closed *loudly* instead: with no
  handler it says so in the transcript.
- **A 409 `approval_required` must never toast.** `readJsonResponse()`
  broadcast `qh-api-error` for every non-OK response, so the permission
  request itself threw an error in your face before you had decided anything.
  It is a control-flow signal, not a failure.
- **Everything that isn't an explicit Allow denies.** Escape, the backdrop and
  the close button all resolve `false`.

"Allow once" sends `approved: true` for that one call; "Allow for session"
also grants the scope in memory, and that grant dies with the tab — it is
never written to disk.

## 5. Optional working directory

**Explicit beats ambient.** A working directory set on Home/Code/Hub wins over
whichever project happens to be active on the Projects page. The other way
round — which is how it was first written — meant the Working dir button
showed one path while every tool call ran somewhere else entirely, silently,
until you read a tool result and saw the wrong directory in it.


Every chat surface **except Projects** has a `📂 Working dir` button in the
capability bar, next to Connectors. Projects is excluded deliberately: it
already has an authoritative project path, and a second competing directory
there would only be a way to point the tools somewhere the project isn't.

It is optional. Unset means the harness repo, exactly as before. When set, it
is the `cwd` for hotphrases, AI actions, the terminal and every file tool on
Home, Code and the Agent hub — one shared setting (`getDirectory()`), so the
button shows the current folder on all three and changing it anywhere updates
all of them.

Two details worth keeping:

- **`effectiveWorkingDir()` treats a bare `.` as unset.** The Code file tree
  persists `.` as it browses, and `.` already means "the harness repo" — which
  is what unset means. Displaying "Currently: ." as though it were a chosen
  directory just reads as a bug.
- **The desktop picker resolves an object, not a string.** The bridge returns
  `{ok, path}`; assigning it straight to the input produced a literal
  `[object Object]` in the path. Destructure `.path`, and treat a `cancelled`
  rejection as a no-op rather than an error.
- **Grant before validating.** The policy layer refuses ungranted paths before
  checking whether they exist, so validating first reported a typo'd path as
  "permission required" and sent you looking for a permissions problem.
  Granting first surfaces the real reason ("not a directory: …").

## 5a. Static files must revalidate

`_serve_static()` sends `ETag` + `Last-Modified` + `Cache-Control: no-cache`.
This is not an optimisation — it is a correctness fix. With **no** cache
headers a browser applies *heuristic* caching and may serve `app.js` from its
own cache for hours without ever asking the server. That is how a browser tab
ends up running last week's JS — stale agent avatars, a blank Profile page,
tools the model cannot see — while a freshly launched WKWebView loads current
files and behaves like a different application entirely.

`no-cache` means "revalidate", not "do not store": an unchanged file still
costs only a 304.

**If the web and the Mac app ever disagree again, suspect a stale asset first.**

## 5b. Shared agent roster

Agents used to live only in `localStorage`, which is per-origin — so the Mac
app and a browser tab kept entirely separate rosters and drifted apart.
`tools/agents_store.py` holds one roster at `~/.qwen-harness/agents.json`;
clients reconcile on boot via `syncAgentRoster()` before the first paint.

Last write wins by `updated_at`, so the device you actually edited on survives
and a stale client cannot clobber it. `force: true` is the explicit "this
device is right" override.

## 5c. Your profile (rail → Profile)

`tools/profile.py` stores who you are at `~/.qwen-harness/profile.json` and
`describe()` prepends it to **every** system prompt — before the tool and
effort rules, because who the person is frames everything else.

Its second job is a **hallucination tripwire**: once your name is set, a model
calling you "user" means it stopped reading its context. That is visible
immediately instead of buried in a plausible-sounding answer. Verified —
qwen3:0.6b answers "Elisha." with the block, and "I don't have access to your
name" without it.

Only filled fields are sent; a blank is never mentioned, so the model is never
handed an empty slot to invent a value for. Add a field by adding it to
`FIELDS` — the form renders from the server's list.

**Saving invalidates the guidance cache** (`qh-profile-saved`). Without that,
the 30s TTL in `agent-loop.js` meant you set your name, sent a message, and
were still called "user" — indistinguishable from the feature being broken.

## 6. The date_time tool

**Models will invent a time if they cannot look one up.** Observed: six agents
all reporting "12:34" with zero tool calls, unchanged five minutes later. The
guidance now says explicitly never to state a date, day, or time without
calling `date_time` first — a fabricated timestamp looks entirely plausible,
which is exactly what makes it dangerous.


Models have no clock and answer date questions from a stale training cutoff.
`date_time` gives them the real answer, defaulting to your machine's region
(read from `/etc/localtime`, so it is the IANA name and not an ambiguous
abbreviation like "IST").

Stdlib and offline by design: `zoneinfo` carries the full IANA database with
DST rules, so there is no API key and no network — which matters because the
sandbox denies network, and a tool needing one would fail exactly where agents
run. An unresolvable place is an error, never a guess.

It is in **every** tool profile including `minimal`. Its absence there is why a
model reported having no way to find the date.

## 6a. Output ratings (👎 👍 ❤️)

Every assistant message carries three rating buttons. `tools/feedback.py`
stores them in `~/.qwen-harness/ratings.json` with the prompt, model, effort,
and which tools ran — a preference is only meaningful against what was asked.

**What ratings actually do.** They do not finetune anything; nothing here
touches model weights, and no local Ollama model can be updated at runtime.
What they do:

1. **Condition later prompts, automatically.** `preference_guidance()` derives
   *measurable* traits from what you rated well vs badly — median length, tool
   use, hedging, code blocks — and emits rules that `agent-loop.js` appends to
   every system prompt (30s TTL, so a rating takes effect on about the next
   turn). This is real behavioural change.
2. **Give routing evidence.** `summary()["by_model"]` is per-model average
   score — which model actually earns good ratings for your work.
3. **Build a real finetuning dataset.** `POST /api/feedback/export` writes
   DPO-style `{prompt, chosen, rejected}` pairs, or `{"kind":"sft"}` for
   supervised examples from loved responses. Those are the actual input formats
   for a LoRA run — this is the path to genuine finetuning, offline.

Two guards that exist for a reason:

- **`MIN_SAMPLES_PER_SIDE` (3).** Guidance stays empty until there are at
  least three ratings on *each* side. A confident rule derived from two clicks
  would silently steer every future turn on noise, and be very hard to notice
  going wrong.
- **`MIN_STATED_LENGTH` (300).** A few terse ratings can put the median at
  "32 characters"; handing a model that number produces uselessly clipped
  answers. Below the floor the rule is stated qualitatively instead.

The topbar `Ratings` badge highlights when guidance is actually in effect, and
its tooltip shows the exact rules currently being injected — so learned
behaviour is never invisible.

## 6b. Reading JavaScript-rendered pages

`scrape_url` reads static HTML, which is an empty shell for any modern web
app. It now routes to a headless browser automatically:

- **Known SPA domains** (`SPA_DOMAINS` in `tools/web.py` — Instagram, X,
  Reddit, TikTok, LinkedIn, …) skip the pointless static fetch entirely.
- **Everything else** takes the cheap static path first and only renders when
  extraction actually comes back empty. That fallback is why the known list
  never has to be complete — it is an optimisation, not the mechanism.
- `allow_render=False` forces static-only.

The result says which path produced it: `via: "static-html"` or
`via: "headless-browser"`.

`render_url()` in `tools/browser.py` is a **separate, headless** browser from
the interactive `browser_*` session. Both properties matter: headless so an
automatic fallback never pops a window open, and separate so rendering a page
mid-task cannot navigate away from whatever an agent is driving.

**Requires Playwright + Chromium** (`requirements.txt`, installed by
`setup.sh`). Without it, scrape_url reports the failure and the install
command rather than pretending — and still tells the model not to guess. Note
the app's server runs under **Xcode's Python 3.9**, not Homebrew's, so the
install has to land in the interpreter that actually runs `gui/server.py`.

A page that renders but still shows nothing is reported as login/bot-walled,
never as success.

## 6c. Keyboard shortcuts in the Mac app

The desktop app previously had **no Edit menu**, only App and View. On macOS
⌘A/⌘C/⌘V/⌘X are delivered to the first responder through the Edit menu's key
equivalents, so without it copy, paste and select-all were simply dead
everywhere in the WKWebView. `buildMenus()` now installs a standard Edit menu
with nil-targeted selectors, which AppKit routes to whatever has focus.

Deliberately no Find item: `performTextFinderAction:` needs a tag and WKWebView
does not honour it, so the entry would look functional and do nothing.

In a normal browser these shortcuts were never broken — this was app-only.
Rebuild with `script/build_and_run.sh` to pick it up.

## 6d. The weighbridge: tool gating and model handoff

`gui/weighbridge.js` weighs the load and decides whether the model can carry
it. Two mechanisms, both driven by capability **tiers** derived from parameter
count plus Ollama's reported capabilities:

    none     no tools capability at all (gemma2:2b, gemma3:1b)
    light    < 1.5B  (qwen3:0.6b, llama3.2:1b)
    standard < 3.5B
    strong   >= 3.5B (qwen3:4b)

**Tool gating.** `TOOL_MIN_TIER` sets a minimum tier per tool, and anything
unlisted defaults to `standard` — a new tool is assumed non-trivial until
someone decides otherwise, which fails safe. `light` gets tools whose arguments
are one obvious string (`date_time`, `read_file`, `web_search`); it never gets
`edit_file`, `run_shell` or `sandbox_promote`. The boundaries are where
behaviour actually changed in testing: below ~1.5B tool arguments come back
malformed, and below ~3B a long system prompt stops surviving alongside a big
tool list.

**Handoff**, two ways:

- *Pre-flight.* `weighTask()` estimates prompt tokens against usable context
  (75% of num_ctx, leaving room to answer) and looks for multi-step signals.
  Verdict `stretch` or `overloaded` routes to the strongest available model
  before the turn starts — catching it here costs one routing decision instead
  of a wasted turn plus a wrong answer.
- *Model-initiated.* `request_stronger_model` lets a model say the task is
  beyond it. Only offered when somewhere stronger actually exists.

**Verified end to end** on qwen3:0.6b with "refactor the auth module across
every file and add tests":

    qwen3:0.6b is a light-tier model — 5 tool(s) it can't reliably drive were withheld.
    Handing off to qwen3:4b: multi-step task on a light model.
    -> exactly ONE request went out, to qwen3:4b, num_ctx 16384, 20 tools

The pre-flight weigh means no inference is wasted on the weak model at all —
the handoff happens before the first token. The same prompt sent directly to
qwen3:4b produces no handoff and no gating note, because it is already the
strongest model available.

`handoffDepth` caps this at one hop, and a handoff that would land on an
equally weak model is declined — it would cost a whole extra turn for nothing.

## 6e. Metrics panel (bottom right)

Collapsed to one live line by default; click to expand into **This session**
and **This chat**. It updates while agents work, not only when they finish,
because during a four-minute local inference the useful question is "is this
going anywhere?".

The numbers are measured, not estimated: since the loop moved to native
`/api/chat`, every call returns `prompt_eval_count`, `eval_count` and real
eval durations. Usage is emitted for **every** call in a tool chain, not just
the last, or a ten-step chain would show nothing until it ended.

One trap worth remembering: on three surfaces the `usage` event branch
`return`s early to run chat compaction. Recording metrics has to happen
*before* that return, or the panel never sees a single token.

## 7. Context and time limits

### num_ctx is the setting that matters

Ollama defaults to a **4k context on any machine under 24 GiB of VRAM**
(docs.ollama.com/context-length). 4k cannot hold ~16 tool schemas AND a source
file AND the conversation. On overflow Ollama does **not** error — it drops the
oldest content, so the model answers a question it can no longer see. That
reads as a bizarre hallucination ("whats my name" -> "The current time in
Dublin is 12:34") rather than as truncation, which is what makes it so hard to
diagnose.

**The `/v1/chat/completions` endpoint cannot set num_ctx.** Measured: a /v1
request reloaded a model that had been loaded at 8192 back down to 4096. So
`streamCompletion()` talks to the **native `/api/chat`** endpoint, which does
accept per-request `options.num_ctx`, and normalises the response back to the
OpenAI shape so nothing downstream changed. Two shape differences to remember:
native streams NDJSON (not SSE `data:` frames), and its `tool_calls.arguments`
is an object, not a JSON string.

`tools/context_budget.py` sizes num_ctx per model from real RAM. Its
coefficients are fitted to measurements on this 8 GB machine with qwen3:4b:

    4096 -> 3.17 GB    16384 -> 5.29 GB    32768 -> 8.45 GB (over budget)

Result here: qwen3:4b 16384, qwen3:0.6b 32768, gemma2:2b 8192 (its own max).
Verify with `ollama ps` — the CONTEXT column shows what actually loaded.

**The estimate is approximate.** It is fitted to qwen3:4b and was accurate
there (predicted 5.23 GB, measured 5.41 GB), but it under-predicted qwen3:0.6b
(predicted 1.5 GB, measured 4.34 GB) because KV cache does not scale purely
with parameter count. It stayed within budget, but do not treat the number as a
guarantee — `RESERVED_GB` exists for that reason.

`_services_start_ollama()` also sets `OLLAMA_CONTEXT_LENGTH` when it starts
Ollama, so anything not going through the loop is not left at 4k either.

### Time is the primary limit, not tokens

**The deadline bounds the CALL, not just the gap between calls.** Checking it
only at the top of the loop let one long generation run indefinitely past the
budget — observed: a 12-minute Deep budget still generating at 18 minutes,
because it never finished an iteration. Each request now gets an
AbortController armed with the remaining budget, chained to the caller's own
signal so Stop still works and the two are told apart.


Effort levels now buy **time and iterations**. `maxTokens` per level is gone:
at ~7 tok/s the old caps allowed Quick 28 min, Focus 94 min, Deep 4.4 hours —
so they bounded nothing real, they just truncated long replies mid-sentence.

    Quick 2 min / 6 iter    Focus 5 min / 10 iter
    Deep 12 min / 14 iter   Max 25 min / 18 iter

`RUNAWAY_MAX_TOKENS` (16384) survives only as a guard against a model looping
forever inside one call — high enough never to interrupt legitimate work.
Iteration caps stay: they stop loops, which a token cap never could.

Escalating to Max extends the clock as well as the level; raising effort while
leaving the old deadline would have granted nothing.

## 7a. Token budget on thinking models

Measured, not assumed — qwen3:4b, identical tool-calling prompt, five runs:

    completion tokens: 641, 642, 752, 822, 1025, 1071, 1801
    reasoning chars:  2623 - 7765          (a 2.8x spread)
    tool call emitted: every run, when it had room

Two facts that shape the design:

- **`think: false` does not stop qwen3 reasoning.** It still returned 3319
  chars of reasoning with thinking off. Neither does a `/no_think` prefix
  (measured previously). Do not "fix" a budget problem by turning thinking off;
  it does nothing.
- **An effort level's cap is not an output budget for a reasoning model.** It
  is a combined deliberate-then-answer budget, and the tail of the reasoning
  distribution eats it. 1801 of a 2048 cap is one circular paragraph from
  truncation.

So `tokenBudgetFor()` doubles the level's nominal cap (ceiling 8192) for models
reporting Ollama's `thinking` capability, and truncation is **retried**, not
surrendered to: the loop makes one more pass with a bigger budget and a "stop
deliberating, act now" nudge, deliberately WITHOUT feeding the abandoned
reasoning back in.

Other options considered and rejected: `tool_choice: 'required'` (measured
*slower* — 1025 tokens vs 642-822 — because the model still deliberates first);
raising every cap unconditionally (wall-clock is the real cost, already 250s
for one turn at ~7 tok/s); and truncating reasoning mid-stream (leaves the
model mid-sentence with no way to recover).

**Never persist an incomplete turn.** When the retry also fails, the loop emits
`{type: 'incomplete'}` and the caller skips `persistTurn`. Storing abandoned
chain-of-thought as the assistant's reply is what caused the model to answer
follow-ups by reasoning about tool calls it had only ever *considered* — it
replayed its own musing as history and treated it as an action taken.

## 7b. The sandbox: agents never write straight to your files

Every tool call that carries a `run_id` is staged. `_apply_sandbox()` in
`gui/server.py` is the chokepoint — enforced there, at the one place every
agent tool call passes through, so an agent cannot opt out by phrasing the
call differently.

    write_file / edit_file / append_file  ->  staged in the run's sandbox
    run_shell                            ->  runs confined, inside it
    read_file                            ->  reads the staged copy if present
    sandbox_verify                       ->  runs the project's tests there
    sandbox_promote                      ->  the ONLY tool that writes real files

Promotion is refused unless `sandbox_verify` passed, any further edit
invalidates a previous green run, and a `.qh.bak` is kept of anything
overwritten. `sandbox_promote` also needs user approval.

**Confinement is reported, never assumed.** `confinement()` returns
`sandbox-exec` on macOS (writes confined, network denied, credentials
unreadable) or `staging-only` elsewhere, where the staging guarantee still
holds but a shell command is not confined. Do not print "sandboxed" without
checking which.

Three things that are load-bearing and non-obvious:

- **The profile must deny reads.** `(allow file-read*)` alone happily reads
  `.env`, `~/.ssh`, and `~/.aws` — verified. The deny rules after it are what
  actually stop a credential leak, and they must come after because later
  rules win.
- **`/tmp` is not granted.** Allowing it meant writes were not confined at
  all; the sandbox gets its own `tmp/` and `TMPDIR` points there.
- **`verify()` purges `__pycache__` first.** CPython invalidates a `.pyc` on
  source mtime+size, and two attempts at an edit are very often the same size
  in the same second (`a * b` / `a + b`). Without the purge, verification
  certifies the *previous* attempt's bytecode — a green run for code that was
  never staged. This was observed, not theorised.

Honest limit: staging prevents accidental damage, which is the common failure.
It is not a jail against code actively trying to escape, because `promote()`
copies files out by design.

## 7c. The process transcript

`gui/process-transcript.js` renders the collapsed "what is the model doing"
panel that appears in the feed the moment a prompt is sent. It fills from the
agent loop's events: `reasoning` (streamed thinking), `tool_call`,
`tool_result`, and notes. Tool chatter is routed here instead of into loose
feed bubbles — that noise between prompt and answer is what it replaces.

## 8. Activity drawer: stop, audit trail, clear

**Stop is authoritative in the store, not the browser.** `RunStore.update()`
refuses to move a run out of a terminal state, and `cancel()` settles every
unfinished subtask. Both matter: after you hit Stop the browser keeps
unwinding the aborted turn for a moment and pushes `status: running` writes,
which used to flip the run back to running — where it stayed forever, because
the abort path never reaches `finishRun`. The subtask half is what you
actually saw: the run read "Cancelled" while its subtasks kept spinning.
`_recover_interrupted()` also repairs records written before this fix, and the
drawer renders defensively (a settled run never draws a spinning subtask).

**Don't add a client-side "mark it cancelled" shortcut.** The store is the
only writer that can win a race with in-flight browser updates.

**The audit trail is lazy.** `/api/runs` deliberately strips `events` (up to
500 per run x 40 runs), so it carries `event_count` plus `last_activity` — the
most recent non-bookkeeping event, which is what drives the live "Nova →
read_file (config.py)" line. Full history is fetched from
`/api/runs/events` only when a trail is expanded, and cached per run.
`gui/run-audit.js` maps event types to sentences; add new event types there.

**Clear only removes settled runs.** `clear_finished()` keeps anything
non-terminal, so the button cannot be used to abandon live work. The button
hides itself when there is nothing finished to clear.

## 8a. Chat layout: docked composer, one bubble per send

The chat surfaces use a **fixed viewport**, not a growing page. `.app` is
`height: 100vh; overflow: hidden`, the shell is a flex column, and each view
scrolls inside itself. Consequences worth knowing before editing layout CSS:

- **The composer is docked at the bottom and never moves.** Previously the
  page grew downward as replies arrived, so sending a second prompt meant
  scrolling back up to find the composer. The feed now sits *above* the
  composer in the DOM (this ordering matters) and absorbs the leftover height.
- **The greeting collapses on first message.** `#view-home` carries
  `data-chat="active"`, set by `setHomeChatActive()`; the CSS hides
  `.home-greeting` in that state and gives the space to the transcript.
  `+ New` resets it.
- **Autoscroll is conditional.** `isPinnedToBottom()` is checked *before*
  appending; the feed only follows along if you were already at the bottom, so
  scrolling up to re-read something no longer yanks you back. A
  "Jump to latest" button appears while you're away from the bottom.
- **A broadcast is one bubble, not N.** Sending to five agents used to append
  five identical prompt bubbles. `feedAppendUserPrompt()` (home) and a single
  `appendProjectMainFeed()` row (project) render one bubble carrying a
  `recipients` list, which `renderEntry()` draws as chips. Each agent still
  gets the turn in its own `agent.transcript` — that's its history, and it is
  intentionally not the shared feed.
- **Any new feed gets wrapped.** `ensureFeedWrap()` puts a
  `position: relative` `.feed-wrap` around a scrolling feed the first time it
  needs a jump button; don't assume a feed is a direct child of its view.

If a composer is clipped or a panel overflows, the cause is almost always a
missing `min-height: 0` on a flex child, not a height that needs raising.

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
