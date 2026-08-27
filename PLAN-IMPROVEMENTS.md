# Improvement plan — what to take from Qwen-MM-Plugins and qwen-code

Written after reading [QwenLM/Qwen-MM-Plugins](https://github.com/QwenLM/Qwen-MM-Plugins)
and [QwenLM/qwen-code](https://github.com/QwenLM/qwen-code), against what this
harness already does.

This is a plan, not a changelog. Nothing here is implemented yet.

---

## The honest starting position

Both Qwen projects are larger than this harness and solve a different problem
shape: qwen-code is a terminal agent with a monorepo, SDKs in three languages,
IDE plugins and IM bots; Qwen-MM-Plugins is a capability *distribution* system
that installs into eight different harnesses. This harness is a single-user
local Control Room with a GUI, a weighbridge, and a staged-edit sandbox.

So the useful question is not "how do we catch up" — it is **which of their
ideas fix something that is actually weak here**. Three of ours are genuinely
ahead and should not be traded away:

| Ours | Why it is worth keeping |
|---|---|
| **The weighbridge** (`gui/weighbridge.js`) | Pre-flight capability tiering that withholds tools a small model cannot drive, and routes a too-big task to a stronger model *before* the first token. Neither Qwen project does capability-aware tool gating. On sub-2B local models this is the difference between working and not. |
| **The staged sandbox** (`tools/sandbox.py`) | stage → verify → promote, where promotion is *refused* without a green test run and any further edit invalidates it. qwen-code has a sandbox and worktrees; it does not gate promotion on verification. |
| **Measured, reported honesty** | `confinement()` reports the real mode rather than claiming "sandboxed"; the context budget explains its arithmetic; ratings show the exact injected rules. This is a design stance worth defending, and every item below is written to preserve it. |

The gaps below are ordered by (value × how cheaply we can get it).

---

## 1. Skills: adopt qwen-code's discovery model — HIGH value, LOW cost

**Where we are.** `tools/skills.py` reads `SKILL.md` frontmatter from exactly
two roots (`skills/` bundled, `~/.qwen-harness/skills/` user), with fields
`name`, `description`, `source`, `safety`, `tools`. Skills are listed in the UI
and can be created from the GUI. They are **instruction-only by design** — no
bundled executable code, which is a real safety property.

**What qwen-code does that we do not.** Its skill format is a superset that
solves problems we have:

```yaml
---
name: your-skill-name
description: what it does AND when to use it
priority: 10
paths:                       # glob gate: hidden until a matching file is touched
  - "**/*.tsx"
user-invocable: true
disable-model-invocation: false
---
```

Plus a **four-tier discovery order**: personal (`~/.qwen/skills/`), project
(`.qwen/skills/`, checked into git and shared with the team), extension, and
bundled. And a directory layout where `SKILL.md` may sit alongside
`reference.md`, `examples.md`, `scripts/`, `templates/` — loaded progressively
rather than all at once.

**Correction, from auditing the code rather than remembering it.** An earlier
draft of this plan argued that `paths:` was really a context-budget feature —
that it would keep an irrelevant skill's text out of the prompt. That is wrong
here, and the reason it is wrong is the more important finding:

**Our skills are slash-only. They are never model-invoked.**
`activateSlashSkill()` in `gui/agent-loop.js:236` matches `^/name` and nothing
else; on a match it appends the skill's instructions to the system prompt
(line 518) and splices in its requested tools (line 480). With no leading
slash, no skill text reaches the model at all. So skills currently cost **zero**
prompt tokens when unused, and `paths:` would gate a picker, not a budget.

That was a deliberate anti-injection choice — the comment says so: "only a
leading /name activates it, avoiding accidental prompt injection from prose
that happens to mention a skill name." It is a real property worth keeping in
some form.

But it also means **the model cannot reach 20 of the harness's 21 capabilities
on its own.** qwen-code's skills are model-invoked: the model reads each
skill's `description` ("what it does AND *when* to use it") and decides. That
is the actual gap, and it is a capability gap, not a budget one.

The way to have both: advertise skills to the model as a compact *index*
(name + one-line description, ~15 tokens each, ~300 for all 20), and let it
pull the full instructions through a `skill_read(name)` tool — the same
progressive-disclosure shape as `tool_search`. The model gets to choose;
the full text still only enters the context when something actually selects
it; and prose mentioning a skill name still cannot activate it, because
activation goes through a tool call, not a regex over user text.

`paths:` then earns its place for a different reason: keeping the *index*
honest (a Figma skill listed while you are editing Python is noise), and it
is what makes the index scale past 20 skills.

**Proposed change.**
1. Add `priority`, `paths`, `user-invocable`, `disable-model-invocation` to the
   frontmatter parser in `tools/skills.py`. Unknown keys are already dropped by
   `_frontmatter()`, so this is backward compatible with all 20 existing
   skills and needs no edits to them.
2. Add **project skills**: a third root, `.qwen-harness/skills/*/SKILL.md`
   under the active project or working directory. `list_skills()` already
   globs a list of roots, so this is one entry plus a precedence rule
   (project > user > bundled). This is the one that unlocks sharing — a skill
   checked into a repo travels with it.
3. **Make skills model-invocable**, per the correction above: a `skill_index`
   block in the system prompt (name + description only) plus a `skill_read`
   tool that returns the full instructions. Gate the index by `paths` and cap
   it by `priority`.
4. Add progressive disclosure *within* a skill: sibling `reference.md` /
   `examples.md` fetched through the same `skill_read(name, file)` call. Today
   a long skill is all-or-nothing.
5. **Keep instruction-only.** qwen-code allows `scripts/`; we should not, or at
   least not without the same approval gate as `run_shell`. A skill that can
   ship executable code is a supply-chain vector, and "reviewed
   instruction-only skill; no bundled executable code" is a claim
   `tools/skills.py` currently earns — `create_skill()` writes only a
   `SKILL.md`, at mode 0600, and nothing loads code from a skill directory.

**Risk.** `paths` gating can hide a skill the user expected. Mitigate by
showing gated skills in the Skills page marked "inactive here — matches
`**/*.tsx`", never by hiding them from the human.

---

## 2. Auto-Memory: close the loop we already half-built — HIGH value, MEDIUM cost

**Where we are.** We have two separate, disconnected learning systems:

- `tools/memory.py` — a persistent key/value store the model writes to *when it
  chooses to* via `memory_write`. In practice small local models rarely choose
  to.
- `tools/feedback.py` — the ratings system, which is genuinely clever:
  `preference_guidance()` derives *measurable* traits (median length, tool use,
  hedging, code blocks) from 👍/👎 and injects rules into every system prompt,
  with `MIN_SAMPLES_PER_SIDE = 3` so two clicks cannot steer everything.

**What qwen-code does.** Auto-Memory: "the agent learns your preferences and
project conventions as you use it, persisting across sessions... zero
configuration." It is on by default. No explicit `memory_write` call required.

**The gap.** Our memory is opt-in for a model that does not opt in. Our ratings
system already proves we can derive guidance from behaviour rather than
declaration — but it only learns *style*, never *facts*. Nobody has to tell
qwen-code "this project uses pytest, not unittest" twice; here they do.

**Proposed change.** A `project_conventions` extraction pass, reusing the
ratings architecture rather than inventing a second one:

1. After a run finishes, a cheap pass (this is exactly the job the light-tier
   models in `orchestrator.py` exist for — qwen3:0.6b at 70 tok/s) extracts
   candidate facts from what actually happened: which test command succeeded,
   which package manager was used, which files were edited together, which
   corrections the user made to agent output.
2. Store under `~/.qwen-harness/memory.json` with `category="convention"` and a
   `project_root` key, so conventions are scoped to a project rather than
   global.
3. Inject the top *N* by confidence, the way `preference_guidance()` already
   injects style rules, with the same TTL cache and the same
   `qh-profile-saved`-style invalidation.
4. **Adopt the `MIN_SAMPLES` discipline.** A convention observed once is a
   coincidence. Require it to hold across at least two runs before it is
   injected, and show the current set in the Ratings-style topbar badge so
   learned behaviour is never invisible — that principle is already written
   into RUNBOOK §6a and it should extend here unchanged.

**Risk, and it is the serious one.** A wrong learned convention is worse than
no convention, because it is applied silently to every future turn. The
existing tripwire design applies: make the injected set inspectable in one
click, and make each entry individually deletable.

---

## 3. SubAgents and Agent Teams: give `orchestrator.py` a boundary contract — MEDIUM/HIGH value, MEDIUM cost

**Where we are.** `orchestrator.py` runs multi-agent work, and the Agent Hub
gives each agent its own model. The weighbridge already routes by capability
tier. What we do not have is any notion of *isolation* between concurrently
working agents — they share one working directory and one sandbox namespace.

**What qwen-code does.** "Give each agent an owner, file boundary, output
format, and finish condition", with git worktrees for isolated execution and
parallelism for independent tasks (repository mapping, test archaeology,
documentation research).

**Those four fields are the whole idea, and we have none of them.** They are
also cheap: they are a schema, not an engine.

**Proposed change.**
1. Extend the agent record in `tools/agents_store.py` with
   `file_boundary` (glob list), `output_format` (free text or a JSON schema),
   and `finish_condition` (a sentence the agent is told to satisfy).
2. Enforce `file_boundary` in `tools/policy.py`'s `require_path()` — it already
   resolves every agent-supplied path against granted roots, so this is one
   more predicate at the same chokepoint. Enforcement at the existing
   chokepoint is what stops an agent phrasing its way around it, exactly as
   `_apply_sandbox()` does today.
3. **Per-agent sandboxes instead of per-run.** `get_sandbox(run_id)` becomes
   `get_sandbox(run_id, agent_id)` so two agents editing in parallel stage into
   separate trees and cannot silently overwrite each other's staged work. This
   is the real bug that worktrees solve, and we can solve it without worktrees
   because we already stage.
4. Git worktrees are the *lower* priority half of this. They matter when agents
   need to run builds against different branches; per-agent staging covers the
   common case.

**Risk.** Parallel agents on a 4-core CPU-only laptop will thrash — the machine
runs one model at a time by design (`OLLAMA_MAX_LOADED_MODELS=1`). Parallelism
here should mean *interleaved staged work*, not concurrent inference, and the
UI must not imply otherwise.

---

## 4. Multimodal: the honest read on Qwen-MM-Plugins — MEDIUM value, HIGH cost

**What it is.** Eight capabilities (`core`, `api`, `search`, `video-memory`,
`video-edit`, `blender`, `freecad`, `edu-agent`), each installed independently
as "a **Skill** plus an optional **MCP server**", named
`qwen-mm-plugins-<capability>`, sharing config at `~/.qwen-mm-plugins/config`.
Media is read at dynamic resolution, and files are referenced naturally
(`@report.pdf`) with automatic tool selection.

**Where we are.** Text only. `scrape_url` reads pages, `read_file` reads text.
There is a File upload capability in the composer and a Document Dock, but
nothing that lets a model *see* an image.

**The blocker is not architecture, it is the model roster.** Our entire roster
— qwen3:4b, qwen3:0.6b, gemma3:1b, llama3.2:1b, gemma2:2b — is text-only.
`api` (Qwen VL/Omni vision, OCR, grounding, ASR) is a **hosted API** capability:
it needs DashScope credentials and network. That is a real change in what this
harness is, which is "runs on your machine, no API keys". It should be a
deliberate decision, not a drive-by feature.

**Proposed change, staged so the cheap half lands first.**

*Stage A — local, no new dependencies on anyone's API (do this):*
- Adopt the **`@file` reference convention** in the composers. It is good UX
  independent of multimodality, and it is how a user will expect to point at a
  file once they have seen it anywhere else.
- Add `read_pdf` (text extraction) and `read_document` for docx/xlsx. These are
  local, offline, and cover the majority of "read this file" asks that fail
  today. `skills/pdf-workflow/SKILL.md` already exists and currently has no
  tool behind it.
- Add a *local* vision path behind a capability toggle: pull a small VLM
  (`qwen2.5vl:3b` or `moondream`) and add `describe_image(path, question)`.
  On 16 GB this is affordable. On 8 GB it is not, which is precisely why it
  must be a toggle and why the weighbridge should treat "has vision" as a
  capability tier the same way it treats `tools`.

*Stage B — hosted (only on an explicit decision):*
- An `api` connector alongside GitHub and Supabase, using the same
  connect-once, secrets-in-`~/.qwen-harness/secrets.json` (chmod 600) pattern.
  Same UI, same guarantees, and it stays **off by default** so the
  no-API-keys promise holds for anyone who does not opt in.

**What not to copy.** `blender`, `freecad` and `edu-agent` are domain
verticals. They are excellent examples of the plugin *pattern* and irrelevant
as features here.

---

## 5. MCP: the biggest single gap — HIGH value, MEDIUM/HIGH cost

**Where we are.** 40 tools, hard-coded in `tools/__init__.py`'s
`TOOL_DEFINITIONS`, served to both runtimes (the Python loop directly, the GUI
loop over `/api/tools/list` + `/api/tools/call`). Adding a tool means editing
this repo.

**What both Qwen projects do.** MCP everywhere. It is how Qwen-MM-Plugins ships
a capability into eight different harnesses without any of them knowing about
it, and it is why qwen-code can reach any third-party tool server.

**Why it is worth the cost here.** Our tool registry is already *shaped* like
an MCP server: a list of JSON schemas plus a dispatcher. `/api/tools/list` and
`/api/tools/call` are an MCP-ish protocol that is not MCP. Two things fall out
of adopting the real one:

- **Consuming**: every existing MCP server becomes available without writing an
  adapter.
- **Being consumed**: this harness's tools — the sandbox in particular, which
  is genuinely good — become usable from qwen-code, Claude Code, or anything
  else.

**Proposed change.**
1. An MCP *client*: a `mcp_servers` section in a config file, launched as
   subprocesses over stdio, their tools merged into `TOOL_DEFINITIONS` at
   runtime.
2. **The gating question, and it is not optional.** External tools must be
   assigned a `TOOL_MIN_TIER` in the weighbridge, and the default for anything
   unlisted is already `standard` — "a new tool is assumed non-trivial until
   someone decides otherwise, which fails safe". That default is exactly right
   for MCP tools and needs no change. They must also flow through
   `TOOL_APPROVAL_SCOPES` and `_apply_sandbox()`; an MCP tool that writes files
   outside the sandbox would silently break the harness's central guarantee.
3. A `--mcp` mode on `gui/server.py` exposing our registry as a server.
4. **Prompt budget is the hard constraint.** 40 tools already cost ~3k tokens
   unprofiled, which is why `TOOL_PROFILES` exists. Merging an MCP server with
   30 more tools would break a 4B model outright. So: MCP tools are **never**
   added to a profile automatically. They are reachable through `tool_search`
   only, unless explicitly profiled. This is the single most important design
   decision in this item.

---

## 6. Session and process model — MEDIUM value, LOW/MEDIUM cost

**qwen-code's four modes**: interactive (`qwen`), headless (`qwen -p "..."`),
daemon (`qwen serve` — one agent, many clients over HTTP+SSE), and IM bot.

**Where we are.** We have the GUI and `examples/*.py`. We have no headless mode:
there is no way to run this harness in CI, in a script, or from a cron job.

**Proposed change, cheapest first.**
1. **`qh -p "prompt"`** — a headless entry point that runs one turn through the
   *same* agent loop with the same tool profiles and the same sandbox, printing
   the result. This is mostly wiring `agent.py` to a CLI, and it is the item
   with the best value-to-effort ratio on this whole list.
2. `--json` output, so it composes.
3. The daemon mode we effectively already have: `gui/server.py` is an HTTP
   server holding shared state. What is missing is that the *agent loop* lives
   in the browser (`gui/agent-loop.js`), not the server — so two clients cannot
   share one run. Moving the loop server-side is a large refactor with a real
   payoff (headless, daemon and the GUI would share one implementation instead
   of the current two), and it should be costed properly before being started.
4. **IM bots: skip.** They are a distribution feature for a hosted product.
   This is a local single-user harness.

---

## 7. Hooks and Plan Mode — LOW/MEDIUM value, LOW cost

qwen-code has hooks (run a command at lifecycle points) and Plan Mode (produce
a plan, then execute on approval).

Plan Mode maps onto something we already have: the effort ladder and
`[[REQUEST_MAX_EFFORT: ...]]` escalation in `gui/effort.js`. A "plan first" toggle
that forces a plan → approval → execute cycle at Deep and Max would fit the
existing UI without new machinery, and it pairs naturally with the sandbox
(plan → stage → verify → promote is already three quarters of that loop).

Hooks are a smaller win here than in a terminal agent, because the GUI already
surfaces the lifecycle in the activity drawer. Worth doing only after §6's
headless mode exists, since a hook without a scriptable harness has little to
call.

---

## Verified gap table

Every row below was checked against the code on 2026-08-27, not recalled. File
and line references are where the work lands.

### §1 Skills

| | Exists today | Needs building |
|---|---|---|
| Frontmatter keys | `name`, `description`, `source`, `safety`, `tools` (`tools/skills.py:36-42`) | `priority`, `paths`, `user-invocable`, `disable-model-invocation` |
| Discovery roots | 2 — bundled `skills/`, user `~/.qwen-harness/skills/` (`tools/skills.py:7-8`) | 3rd root: project `.qwen-harness/skills/`, with precedence |
| Model invocation | **None.** Slash-only regex (`gui/agent-loop.js:236`) | `skill_index` in the prompt + `skill_read` tool |
| Progressive disclosure | None — the whole `SKILL.md` body or nothing (`gui/agent-loop.js:518`) | `reference.md` / `examples.md` via `skill_read(name, file)` |
| Executable skills | Refused by design | Keep refusing |

**Size:** ~150 lines in `tools/skills.py`, ~60 in `gui/agent-loop.js`, one new
tool in `tools/__init__.py`, plus Skills-page rendering for the gated state.

### §2 Auto-Memory

| | Exists today | Needs building |
|---|---|---|
| Style learning | **Working.** `preference_guidance()` derives measurable traits from ratings and is injected every turn with a 30s TTL (`tools/feedback.py:125`, `gui/agent-loop.js:197-215`) | — |
| Fact/convention learning | **None.** `memory_write` is a tool the model must choose to call, and small local models do not choose to | Post-run extraction pass; `category="convention"`; `project_root` scoping |
| Auto-injection of facts | None | Reuse the `guidanceCache` path — same TTL, same invalidation event |
| Confidence discipline | `MIN_SAMPLES_PER_SIDE = 3` exists for ratings | Same idea for conventions: seen in ≥2 runs before injection |
| Inspectability | Ratings badge shows the exact injected rules | Extend that badge, per-entry deletable |

**Size:** a new `tools/conventions.py` (~200 lines) modelled directly on
`feedback.py`, one extraction prompt for a light-tier model, ~30 lines in the
agent loop, one topbar affordance.

### §3 SubAgents / Agent Teams

| | Exists today | Needs building |
|---|---|---|
| Agent record | `("id", "name", "slug", "nickname", "modelKey")` (`tools/agents_store.py:23`) | `file_boundary`, `output_format`, `finish_condition` |
| Boundary enforcement | Grants only — `require_path()` checks granted roots (`tools/policy.py`) | One more predicate at that same chokepoint |
| Sandbox isolation | **Per-run.** `get_sandbox(run_id)` (`tools/sandbox.py:461`) — two agents in one run share a staging tree and can overwrite each other's work | Key on `(run_id, agent_id)` |
| Worktrees | None | Lower priority than the above |

**Size:** 3 fields + a migration in `agents_store.py`, ~20 lines in `policy.py`,
a signature change in `sandbox.py` and its ~6 call sites in `gui/server.py`.

### §4 Multimodal

| | Exists today | Needs building |
|---|---|---|
| Tool count | 40 | — |
| Anything that *reads* an image | **None.** `screenshot` and `browser_screenshot` save a PNG and return its path; nothing consumes the pixels | `describe_image(path, question)` against a local VLM |
| PDF / docx | **None** — though `skills/pdf-workflow/SKILL.md` ships and has no tool behind it | `read_pdf`, `read_document` (local, offline) |
| `@file` references | None | Composer convention + resolution |
| Model roster | All five models are text-only | `qwen2.5vl:3b` or `moondream`, behind a capability toggle |
| Weighbridge | Tiers on parameter count + `tools` capability (`gui/weighbridge.js`) | Add "has vision" as a tier input |

**Size:** `read_pdf` is ~80 lines plus a dependency. The VLM path is a new
tool, a roster entry, a toggle, and a weighbridge case. Stage B (hosted Qwen
VL/Omni) is a connector on the GitHub/Supabase pattern and a policy decision
about the no-API-keys promise, not a large amount of code.

### §5 MCP

| | Exists today | Needs building |
|---|---|---|
| MCP anywhere in the repo | **Zero occurrences** | Everything |
| Tool registry | Static `TOOL_DEFINITIONS` + `execute_tool()` dispatch (`tools/__init__.py`) | Runtime merge of external servers |
| Transport | `/api/tools/list` + `/api/tools/call` — MCP-shaped, not MCP | stdio client; optional `--mcp` server mode |
| Gating for external tools | `TOOL_MIN_TIER` defaults unlisted tools to `standard`, which already fails safe | Nothing to change — but external tools must also route through `TOOL_APPROVAL_SCOPES` and `_apply_sandbox()` |
| Profile safety | `TOOL_PROFILES` caps the prompt at ~1.4k tokens | MCP tools reachable **only** via `tool_search` unless explicitly profiled |

**Size:** the largest item here. A stdio client, lifecycle management, schema
translation, and the three safety integrations above.

### §6 Session / process model

| | Exists today | Needs building |
|---|---|---|
| Interactive | The Control Room GUI | — |
| Headless | **None.** No CLI, no `argparse` in `agent.py` or `orchestrator.py` | `qh -p "prompt"` + `--json` |
| Where the agent loop lives | `gui/agent-loop.js` — in the **browser** | Server-side, for daemon mode and for one implementation instead of two |
| Daemon | `gui/server.py` holds shared state but not the loop | Follows from the move above |
| IM bots | None | Out of scope — this is a local single-user harness |

**Size:** `qh -p` is small — `agent.py` already has the loop; it needs argument
parsing, profile selection, and output formatting. Moving the loop server-side
is the large one and should be costed separately.

### §7 Plan Mode / hooks

| | Exists today | Needs building |
|---|---|---|
| Effort ladder | 4 levels with time and iteration budgets (`gui/effort.js`) | — |
| Self-escalation | `[[REQUEST_MAX_EFFORT: ...]]` parsed and honoured | — |
| Plan → approve → execute | None | A toggle that forces the cycle at Deep/Max |
| Hooks | None | Needs §6.1 first — a hook with nothing scriptable to call is not worth much |

---

## Sequencing

| Order | Item | Why here |
|---|---|---|
| 1 | §1 Skills frontmatter + project skills | Days, backward compatible, immediately reduces prompt cost |
| 2 | §6.1 Headless `qh -p` | Best value-to-effort; unblocks hooks, CI, and scripted evaluation |
| 3 | §4 Stage A: `@file`, `read_pdf`, local VLM toggle | Uses the 16 GB this port now runs on; no new external dependency |
| 4 | §2 Auto-Memory conventions | Needs §6.1 to evaluate it honestly — you cannot measure learning without a scriptable harness |
| 5 | §3 Per-agent sandboxes + boundary contract | The real parallelism bug, independent of worktrees |
| 6 | §5 MCP client | Highest ceiling, highest risk to the prompt budget; do it once §1 has made room |
| 7 | §7 Plan Mode, hooks | Polish on top of the above |

Two things to hold onto throughout: the weighbridge must gate anything new the
same way it gates what exists, and every added capability must keep reporting
what it actually did rather than what it was asked to do. Those two properties
are what make this harness trustworthy on hardware where the models are small
enough to be wrong.
