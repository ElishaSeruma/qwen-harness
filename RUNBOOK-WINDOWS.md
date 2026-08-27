# qwen-harness on Windows — command runbook

Every command needed to set up, run, and manage this harness on Windows, in
order. Run these yourself in a terminal — nothing here downloads or starts
anything automatically.

[RUNBOOK.md](RUNBOOK.md) remains the reference for **what the harness does** —
the weighbridge, the sandbox, effort levels, the activity drawer, ratings,
context budgets. All of that behaves identically here. This file covers only
what is *different* on Windows, and why.

Everything runs in **PowerShell**. `powershell.exe` (Windows PowerShell 5.1,
present on every Windows install) and `pwsh` (PowerShell 7+) both work.

---

## 0. What is actually different, in one table

| | macOS | Windows |
|---|---|---|
| Desktop app | Swift + WKWebView (`Sources/`) | C# + WebView2 (`windows/`) |
| Launcher scripts | `setup.sh`, `wake-up.sh` | `setup.ps1`, `wake-up.ps1` |
| Build the app | `script/build_and_run.sh` | `script\build_and_run.ps1` |
| Test suite | `script/test.sh` | `script\test.ps1` |
| Agent/terminal shell | `/bin/sh` | PowerShell (`$env:QH_SHELL` overrides) |
| Sandbox confinement | `sandbox-exec` | **staging-only** — see §7 |
| Screenshots | `screencapture` | pyautogui / Pillow `ImageGrab` |
| Scratch files | `/tmp/harness_*` | `%TEMP%\harness_*` |
| Timezone source | `/etc/localtime` | registry + the `tzdata` package |
| RAM / CPU stats | `sysctl`, `vm_stat`, `ps` | `GlobalMemoryStatusEx`, `GetSystemTimes`, `CreateToolhelp32Snapshot` |
| Ollama address | `localhost` or `127.0.0.1` | **`127.0.0.1` only** — see §10a |

State is in the same place on both: `~/.qwen-harness/`
(`C:\Users\<you>\.qwen-harness`). Grants, agents, projects, ratings, memory,
profile and secrets all live there, so the two ports are interchangeable on the
same account.

---

## 1. One-time setup

```powershell
cd C:\Applications\qwen-harness
.\setup.ps1
```

This checks for Ollama (installing it via `winget` if that is available),
starts it, pulls only the models genuinely missing, installs the Python
dependencies, and installs Playwright's Chromium. It ends by verifying three
things that are load-bearing on Windows specifically: `date_time` returns a
real time, the context budget sees your real RAM, and the sandbox reports which
confinement mode is live.

Options:

```powershell
.\setup.ps1 -SkipModels      # Python side only
.\setup.ps1 -SkipBrowsers    # skip the ~150 MB Chromium download
```

### If the script cannot run at all

PowerShell's default execution policy blocks unsigned local scripts. This is
per-user and does not need an administrator:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Or run one script without changing the policy:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

### Prerequisites you must install yourself

| | Where | Why |
|---|---|---|
| **Python 3.9+** | <https://python.org/downloads> — tick *Add python.exe to PATH* | runs `gui/server.py` and every Python tool |
| **Ollama** | <https://ollama.com/download/windows> | the model server |
| **Node.js** | <https://nodejs.org> | only for `script\test.ps1`'s JS syntax checks |
| **.NET 8+ SDK** | <https://dotnet.microsoft.com/download> | only to build the desktop app |
| **WebView2 Runtime** | preinstalled on Windows 11; else <https://developer.microsoft.com/microsoft-edge/webview2/> | the desktop app's browser engine |

**Do not install Python from the Microsoft Store.** The Store build is a PEP 668
"externally managed" interpreter, and its `python3.exe` under
`%LOCALAPPDATA%\Microsoft\WindowsApps` is a *redirector stub*: it exits 9009
and opens the Store rather than running anything. Several things in the harness
generate commands containing an interpreter name, so a stub in that position
fails in confusing ways.

---

## 2. Pull the model roster

```powershell
ollama pull qwen3:4b      # ~2.5 GB - primary model
ollama pull qwen3:0.6b    # ~0.5 GB - lightest agent
ollama pull gemma3:1b     # ~0.8 GB - light agent
ollama pull llama3.2:1b   # ~1.3 GB - light agent
ollama pull gemma2:2b     # ~1.6 GB - light agent
```

`setup.ps1` does this for you and skips anything already present.

**A pull is aborted by anything that restarts Ollama** — including
`wake-up.ps1` and launching the desktop app, both of which restart the server
to apply the context settings. Re-running `ollama pull` resumes; nothing needs
cleaning up.

Managing what is on disk:

```powershell
ollama list
ollama rm qwen3:8b
(Get-ChildItem "$env:USERPROFILE\.ollama\models" -Recurse -File | Measure-Object Length -Sum).Sum / 1GB
```

---

## 3. Start the harness

```powershell
cd C:\Applications\qwen-harness
.\wake-up.ps1
```

Starts Ollama with the harness's tuning, starts the Control Room backend on
port 8420, and opens `http://127.0.0.1:8420/gui/`.

```powershell
.\wake-up.ps1 -Port 9000     # different port
.\wake-up.ps1 -NoBrowser     # start everything, open nothing
```

### As a desktop app

```powershell
.\script\build_and_run.ps1
```

Builds `windows\QwenHarnessDesktop`, stamps the repo root next to the
executable, renders the icon, and launches the app. It reuses a healthy backend
if one is already running, so the browser tab and the window share one harness.

```powershell
.\script\build_and_run.ps1 -Mode verify   # launch, then prove it is alive and answering
.\script\build_and_run.ps1 -Mode build    # build only
.\script\build_and_run.ps1 -Mode logs     # launch, then tail the server log
.\script\build_and_run.ps1 -Mode debug    # Debug configuration (DevTools enabled)
```

The executable ends up at:

```
windows\QwenHarnessDesktop\bin\Release\net8.0-windows\QwenHarnessDesktop.exe
```

Pin that to the taskbar and it works standalone — it finds the checkout through
the `repo-root.txt` written beside it.

### Starting the pieces manually

```powershell
# Terminal 1 — model server
ollama serve

# Terminal 2 — GUI backend + static files
cd C:\Applications\qwen-harness
python gui\server.py 8420
```

Then open `http://127.0.0.1:8420/gui/`.

---

## 4. Check everything is up

```powershell
Invoke-RestMethod http://127.0.0.1:8420/api/health
Invoke-RestMethod http://127.0.0.1:8420/api/platform
Invoke-RestMethod http://127.0.0.1:11434/api/tags
```

`/api/platform` is new in the Windows port and is the fastest way to see what
the backend thinks it is running on:

```
platform             : windows
shell                : powershell
example_project_path : C:\Users\you\Projects\my-app
confinement          : @{mode=staging-only; writes_confined=False; ...}
total_memory_mb      : 16067.9
```

The Control Room topbar shows the same Server / Ollama / active-model pills,
polled automatically.

---

## 5. The PATH trap, Windows edition

The macOS runbook (§4b) documents that a Finder-launched app gets
`PATH=/usr/bin:/bin:/usr/sbin:/sbin` and therefore cannot see Homebrew. Windows
has the same failure with different scenery:

- **Ollama installs per-user** into `%LOCALAPPDATA%\Programs\Ollama`. A process
  launched from a shortcut, a scheduled task, or an IDE frequently does not
  have that on PATH, so "Start services" reports *"ollama binary not found"* on
  a machine where `ollama --version` works fine in a terminal.
- **A PATH edit made by an installer does not reach already-running processes.**
  Explorer (and therefore everything launched from it) keeps the environment it
  started with until you sign out.

Two defences, both in `tools/platform_support.py` and used by both the server
and the desktop app:

- `repair_path()` appends the real install locations — Ollama's two, Git's
  `cmd` and `usr\bin`, Node, dotnet, Python and its `Scripts` — but only ones
  that exist. It **adds and never prunes**: a real machine's PATH routinely
  contains directories that do not exist yet, and dropping them would change
  the environment of every subprocess the server spawns.
- `find_binary()` resolves an absolute path independently of PATH, and that
  absolute path is what gets executed.

`tests/test_platform_support.py` and `tests/test_server_env.py` guard both.

---

## 6. The shell is PowerShell

Every agent `run_shell` call, every command typed into the Code page's
terminal, and every sandbox test command runs through **PowerShell** here,
where macOS uses `/bin/sh`.

Override it if you would rather have Git Bash or cmd:

```powershell
$env:QH_SHELL = 'bash'    # or 'cmd', 'pwsh', 'powershell'
.\wake-up.ps1
```

Three things this changes that are worth knowing:

**`cd` persistence works differently.** The terminal runs each command in a
fresh process, so `cd` can only persist if the shell reports where it ended up.
The sh version appends `printf '\n@@QH_CWD@@%s' "$PWD"`. The cmd translation of
that is a trap: `%CD%` inside a one-liner is expanded at **parse** time, so it
reports where the command *started* and the terminal silently never moves.
PowerShell reports `(Get-Location).ProviderPath` after the fact, and the cmd
path uses `/v:on` delayed expansion (`!CD!`) instead.

**The destructive-command guard is platform-aware.** The macOS list is
`sudo`, `rm -rf`, `mkfs`, the fork bomb. Not one of those substrings appears in
the Windows command that does the same damage, so a Windows user previously had
*no* guard at all. `Remove-Item -Recurse -Force`, `rmdir /s`, `del /s`,
`format`, `diskpart`, `vssadmin delete` and `cipher /w` are now refused too, and
matching is case-insensitive because PowerShell is. The POSIX patterns stay
active on Windows as well — Git Bash and WSL are one `bash -c` away.

**`python3` is not a command.** Anywhere the harness generates an interpreter
name for a shell command — most importantly `detect_test_command()`, which is
what `sandbox_verify` runs — it emits the *running interpreter's own path*,
quoted, prefixed with PowerShell's `&` call operator. Without the `&`,
PowerShell parses a quoted path as a string expression and
`"C:\...\python.exe" -m unittest` is a syntax error, not a command. That made
every verify fail, which blocks every promotion.

---

## 7. The sandbox reports `staging-only` — and that is deliberate

On macOS, `sandbox_verify` and any agent `run_shell` inside a run are confined
by `sandbox-exec`: writes restricted to the sandbox, network denied, credential
files unreadable.

**Windows has no equivalent that exists on every edition.** AppContainer needs
a signed manifest and per-capability ACLs; Windows Sandbox needs Pro or
Enterprise plus Hyper-V, which Windows Home does not have. Rather than ship
something that confines on some machines and *reports* "sandboxed" on all of
them, `confinement()` returns `staging-only` here and says so in every tool
result and in `/api/platform`.

What you still get on Windows, unchanged:

- writes are **staged** into `~/.qwen-harness/sandboxes/<run-id>/work`, never
  into your files
- `sandbox_verify` runs the project's tests **against the staged copy**
- `sandbox_promote` is refused unless verify passed, keeps a `.qh.bak` of
  anything it overwrites, and needs explicit user approval
- any further edit invalidates a previous green run
- credentials are stripped from the command's environment (a token in an env
  var needs no filesystem access to leak), and `TMPDIR`/`TEMP`/`TMP` point
  inside the sandbox
- `__pycache__` is purged before every verify, so a stale `.pyc` cannot certify
  code that was never staged

What you do **not** get: a shell command the agent runs is not prevented from
writing outside the sandbox or reaching the network. Treat autonomous runs on
Windows accordingly, and leave 🛡 Auto-approve off unless you are watching.

---

## 8. Context sizing on 16 GB

Ollama defaults to a 4k context under 24 GiB of VRAM, and on overflow it does
not error — it drops the oldest content, so the model answers a question it can
no longer see. `tools/context_budget.py` sizes `num_ctx` per model from real
RAM to avoid that.

Two Windows-specific facts:

- **RAM detection had to be rewritten.** `os.sysconf` does not exist on
  Windows and `sysctl` is macOS-only, so the old code fell all the way through
  to its conservative 8 GB default — sizing every context as if half the
  machine were missing. `tools/sysinfo.py` reads the real figure from
  `GlobalMemoryStatusEx`.
- **The reserve is larger: 4.0 GB, against macOS's 2.5.** A freshly booted
  Windows 11 desktop sits around 4–5 GB resident before a browser is open,
  where macOS compresses aggressively and reports far less. Reserving the macOS
  figure sized a context that fit the fitted formula and then swapped.

Measured on this 16 GB machine:

```
qwen3:4b     49152 tokens   (est. 10.7 GB)     macOS 8 GB gave 16384
qwen3:0.6b   65536 tokens   (est.  2.0 GB)     capped by policy, not memory
gemma2:2b    65536 tokens   (est.  6.7 GB)     capped by policy
```

Check what actually loaded:

```powershell
ollama ps
Invoke-RestMethod http://127.0.0.1:8420/api/context/plan
```

**More RAM buys context, not speed.** Generation rate is bound by memory
bandwidth and cores, not capacity. A CPU-only x86 laptop is if anything slower
per token than the Apple Silicon machine the timings in `config.py` were
measured on, so `ACTIVE_MODEL = "qwen3:0.6b"` remains the right default for
tool-using work; switch to `qwen3:4b` per-agent in the Agent Hub when quality
matters more than latency.

---

## 9. The date_time tool needs `tzdata`

`tzdata` is a **hard requirement on Windows**, not an optional extra. Windows
ships no IANA timezone database, so the stdlib `zoneinfo` reads it from that
package; without it every `ZoneInfo(...)` call raises and `date_time` fails on
every invocation. It is in every tool profile including `minimal`, so the one
tool that exists to stop models inventing a timestamp would itself be
unavailable.

`requirements.txt` pins it behind a `sys_platform == "win32"` marker, so
`setup.ps1` installs it and macOS does not.

Two more Windows-only pieces:

- **The local zone comes from the registry.** There is no `/etc/localtime`.
  `TimeZoneKeyName` under
  `HKLM\SYSTEM\CurrentControlSet\Control\TimeZoneInformation` gives a *Windows*
  zone name ("GMT Standard Time"), which is not an IANA name and cannot be
  handed to `ZoneInfo`. It is mapped through the CLDR windowsZones table in
  `tools/datetime_tool.py`. An unmapped zone falls back to a zone whose current
  UTC offset matches — correct about the *time*, and honest that it is not a
  claim about where you are.
- **`%-d` and `%-I` are not portable.** Those no-leading-zero `strftime` forms
  are glibc/BSD extensions. Windows uses MSVCRT's `strftime`, where they do not
  degrade — the whole call raises `ValueError("Invalid format string")`. The
  human-readable `spoken` field is now formatted arithmetically instead.

Verify:

```powershell
python -c "from tools.datetime_tool import get_datetime; print(get_datetime())"
```

---

## 10. Resource monitor internals

`/api/system/stats` had to be rebuilt: `ps`, `vm_stat` and `sysctl vm.loadavg`
do not exist here.

The Windows path is `ctypes` against Win32 — no `psutil` dependency and no
subprocess of any kind. This endpoint is polled every few seconds *while a
local model is generating at single-digit tokens per second*, and the macOS
version already had to drop `top -l 1` because it cost 25.8s under load and
starved the inference it was watching.

The first Windows version of this used `tasklist` for the per-process rows, on
the assumption that one small subprocess is cheap. It is not:

```
GlobalMemoryStatusEx      0.0 ms
GetSystemTimes            0.1 ms
tasklist /fo csv       1503.7 ms     <- 40% duty cycle on a 3s poll
ollama /api/ps           90.8 ms
```

`tasklist` enumerates and formats every process on the machine.
`CreateToolhelp32Snapshot` walks the same list and opens a handle only for the
two or three whose name matches. Endpoint latency went from **1281 ms to
~32 ms**. The lesson is the same one the `top -l 1` story teaches: measure
before assuming a subprocess is cheap.

One ctypes trap worth knowing if you touch that code: ctypes defaults a
function's `restype` to `c_int`, so a 64-bit `HANDLE` from
`CreateToolhelp32Snapshot` or `OpenProcess` is silently **truncated**. The
truncated value still looks truthy, so it does not raise — it just refers to
nothing, and you leak a handle every poll. `_bind_win32()` declares every
argtype and restype explicitly for exactly this reason.

Differences you will see in the panel:

- **CPU is more truthful here.** Windows reports a delta between polls (real
  instantaneous utilisation); macOS `ps %cpu` is a per-process *lifetime*
  average that lags bursts. The first Windows reading is `null`, not `0` — a
  delta needs a baseline, and a fake zero would be a lie.
- **Load average is `null`.** Windows has no such concept. Deriving one from
  CPU percent would put a meaningless number under a label that means something
  specific.
- **Per-process CPU is `null`.** There is no cheap Windows equivalent of
  `ps %cpu`. Memory per runner process is real, read from `tasklist`.

---

## 10a. `localhost` costs 2 seconds per request — use `127.0.0.1`

The single most expensive Windows-specific bug found during this port, and it
was invisible because nothing failed.

Ollama binds **127.0.0.1 only**. On Windows 11, `localhost` resolves to `::1`
*first*. So every request to `http://localhost:11434` attempts IPv6, waits for
that to fail, and only then falls back to IPv4. Measured on this machine:

```
http://localhost:11434/api/tags    2133 ms
http://127.0.0.1:11434/api/tags       8 ms
```

A 266x penalty — paid on **every inference request**, on every 3-second poll of
the resource monitor, and on every readiness probe. `wake-up.ps1` reported
"Ollama did not come up" on every single run because its 1-second probe could
not finish inside the IPv6 timeout, about a server that was already listening
and serving.

Every Ollama URL in the harness is now the literal IPv4 address —
`config.py`, `gui/server.py`, `gui/agent-loop.js`, `gui/app.js`,
`gui/data/models.json`, `tools/supabase_client.py`, and both PowerShell
scripts. `OLLAMA_HOST` still overrides it for anyone pointing at a remote
Ollama.

If you add a new call to Ollama anywhere, use `127.0.0.1`. On macOS the two
spellings are equivalent, so this costs nothing there.

---

## 11. Files, search, and the `find.exe` trap

`search_files` and `find_files` are now **pure Python on every platform**, not
shell-outs to grep and find.

This started as a Windows necessity and became the behaviour everywhere,
because the shell-out was wrong on Windows in the worst possible way: Windows
ships a `find.exe` that is a **string search utility**, not a file finder. So
`find <path> -name "*.py"` did not fail loudly — it printed a usage error to
stderr, exited non-zero, and `find_files` returned
`{"status": "ok", "files": []}`. An agent asking "where are the tests?" was
told, authoritatively, that there are none. `grep` is likewise absent unless
Git's `usr\bin` happens to be on PATH.

One implementation also means the exclusions (`.git`, `node_modules`,
`__pycache__`, `.build`, …) and the result caps are defined once instead of
drifting between two command lines.

---

## 12. The desktop app

`windows\QwenHarnessDesktop` is a .NET 8 WinForms host around a **WebView2**
control — the Windows counterpart of `Sources/QwenHarnessDesktop/main.swift`.
It loads the same Control Room from the same local server, so there is one UI,
not two.

It matches the Mac app behaviour for behaviour:

| | |
|---|---|
| Toolbar | Reload, Open in Browser, status line |
| Shortcuts | F5 / Ctrl+R reload, Ctrl+B open in browser, F11 full screen |
| Desktop bridge | `chooseDirectory` / `chooseFile`, same `{id, action}` envelope |
| Navigation | only `127.0.0.1:<port>` loads in-window; everything else opens in your real browser |
| Lifecycle | starts Ollama and the server, reuses a healthy one, stops only what it started |
| Window | position and size remembered, and only restored onto a monitor that is still connected |

Windows-specific notes:

- **No menu bar, on purpose.** The Mac app has an Edit menu because ⌘C/⌘V/⌘A
  are delivered to WKWebView through its key equivalents — without it the
  clipboard was simply dead. WebView2 handles Ctrl+C/V/X/A itself, so a menu
  would be decoration.
- **No `window.confirm` workaround needed.** WKWebView shows no dialog for
  `alert`/`confirm`/`prompt` unless the host implements three delegate methods,
  and `confirm()` silently returns `false` without them — which is what made
  every destructive Projects action appear to do nothing on the Mac. WebView2
  shows all three natively (`AreDefaultScriptDialogsEnabled`).
- **The user-data folder is explicit** (`%LOCALAPPDATA%\QwenHarness\WebView2`).
  WebView2's default is next to the executable, which is read-only for an
  installed copy, and it then fails to create its environment at all.
- **The bridge speaks WebView2's channel.** WKWebView exposes a *named*
  message handler; WebView2 has exactly one, `window.chrome.webview`.
  `gui/capabilities.js` detects which host it is in and picks the channel —
  everything above that line is unchanged, and a plain browser tab, which has
  neither, still falls back to the typed-path input.
- **The app icon needs two separate mechanisms**, and getting only one is
  the trap. `<ApplicationIcon>` embeds the icon in the executable's PE
  resource section — that is what Explorer, the file properties dialog and a
  pinned shortcut read. `Form.Icon` is a *runtime property* and is what draws
  the **title bar, the running taskbar button and the Alt-Tab entry**; leaving
  it null gets the generic WinForms window glyph no matter how good the
  embedded icon is. That was the original symptom: Explorer showed the right
  icon, the window showed a stock one.

  `AppIcon.ico` is rendered from `AppLogo.png` by
  `script/render_app_icon_windows.py` — full-bleed square, deliberately *not*
  the rounded macOS silhouette, since Windows draws icons as supplied and a
  rounded tile would be the only one on the taskbar with soft corners.

  It is **committed**, not gitignored, and that is load-bearing: MSBuild
  evaluates `Condition="Exists('AppIcon.ico')"` when the project *loads*,
  before any target runs. Gitignoring it meant a fresh clone evaluated the
  condition as false, the generate target then created the file, and the
  compile proceeded with no `<ApplicationIcon>` at all — a build that succeeds
  and silently produces an iconless executable. The `GenerateAppIcon` target
  still regenerates it whenever `AppLogo.png` is newer, so the two cannot
  drift, and `CopyAppIconToOutput` puts it beside the exe for `Form.Icon`.
- **`longPathAware` is set** in the manifest. Without it a path over 260
  characters fails, and `node_modules` trees inside a granted project folder
  routinely exceed that — the app would report files as missing that plainly
  exist.

---

## 13. Run the tests

```powershell
.\script\test.ps1
.\script\test.ps1 -SkipDesktopBuild
```

Same checks as `script/test.sh` — JS syntax, Python compile, the unittest suite
— plus two Windows additions:

- **A PowerShell parse check on every `.ps1`.** Windows PowerShell 5.1 reads a
  script with **no UTF-8 BOM as ANSI**, which mangles every non-ASCII character
  mid-string and breaks the parse outright. Every `.ps1` here is saved with a
  BOM, and this check is what catches it if one is ever re-saved without.
- **A `dotnet build` of the desktop app.** On the Mac, `swift build` is part of
  `build_and_run.sh` so breakage surfaces immediately; here the app is a
  separate project that a Python-only change can silently break, because the
  bridge protocol lives on both sides of the line.

Two traps that were in the first version of this script, in case you write
another: `$ErrorActionPreference = 'Stop'` turns **any** stderr output from a
native command into a terminating error, and `python -m unittest` writes its
progress dots and its final "OK" to stderr — so a fully passing suite reported
as a failure. And `$LASTEXITCODE` is global and sticky, so a check that runs no
native command inherits whatever the previous one left behind.

---

## 14. Stop everything

```powershell
Get-Process -Name python | Where-Object { $_.CommandLine -like '*gui*server.py*' } | Stop-Process
Stop-Process -Name ollama -Force
Stop-Process -Name QwenHarnessDesktop -Force
```

Closing the desktop app already stops what *it* started. It deliberately leaves
a pre-existing server or Ollama alone — those are shared with any browser tab
you have open, and killing one out from under an in-flight run is exactly the
surprise the activity drawer's Stop semantics exist to avoid.

---

## 15. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `.ps1 cannot be loaded ... not digitally signed` | Execution policy | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| Script fails to parse with mojibake in the error | `.ps1` re-saved without a UTF-8 BOM | Re-save as *UTF-8 with BOM*; `script\test.ps1` catches this |
| "ollama binary not found" but it works in a terminal | Per-user install not on the launched process's PATH | Nothing — `repair_path()` handles it. If it persists, check `Invoke-RestMethod .../api/platform` and restart the app |
| Store opens when something runs Python | `python3.exe` WindowsApps redirector stub | Install Python from python.org and tick *Add to PATH* |
| `date_time` errors on every call | `tzdata` missing | `python -m pip install tzdata` |
| Desktop app shows "WebView2 Runtime is required" | Runtime absent | Install the Evergreen runtime from Microsoft |
| Terminal `cd` does not stick | `$env:QH_SHELL` set to something unsupported | Unset it, or use `powershell` / `pwsh` / `cmd` / `bash` |
| Sandbox says `staging-only` | Expected on Windows | See §7 — it is reported, not a failure |
| `MSB3027 ... locked by "Qwen Harness"` | The app was still holding its own .exe | Fixed: `build_and_run.ps1` now waits for the previous instance to actually exit (Stop-Process only signals) |
| A model pull stops halfway | Something restarted Ollama (`wake-up.ps1`, the desktop app) | Re-run `ollama pull`; it resumes |
| Everything is inexplicably ~2s slower per model call | A `localhost:11434` URL crept back in | Use `127.0.0.1` — see §10a |
| "Ollama did not come up" but it is clearly running | The Ollama tray app respawned the server and won the bind race | Harmless; the warning that follows says the context settings may not have applied |
