"""
tools/platform_support.py — the one place that knows which OS this is.

The harness was written for macOS and hard-coded a lot of it: `/bin/sh -c`,
`/tmp/...`, `open <file>`, `sysctl`, `grep`/`find`, `python3`, Homebrew's
`/opt/homebrew/bin`. None of those exist on Windows, and several of them are
worse than missing — Windows ships a `find.exe` that is a *string search*, not
a file finder, so `find_files` would have silently returned nonsense rather
than failing.

Everything platform-conditional now routes through here so that:

  * there is exactly one definition of "the shell", "the temp dir", "open this
    file", "where binaries live" — not one per call site drifting apart,
  * the macOS behaviour is preserved byte-for-byte (every function returns the
    old value on darwin),
  * a third platform only has to be taught here.

Nothing in this module imports anything outside the standard library, because
it is imported by `gui/server.py` before any dependency is guaranteed present.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

PLATFORM_NAME = "windows" if IS_WINDOWS else "macos" if IS_MAC else "linux" if IS_LINUX else sys.platform


# ── temp space ────────────────────────────────────────────────────────────
# `/tmp` does not exist on Windows: Path("/tmp/x").mkdir() resolves to
# `\tmp\x` on the current drive and raises WinError 3. Importing
# tools/browser.py was enough to crash the whole test suite because of it.

def temp_root() -> Path:
    """The system temp directory. `/tmp` on POSIX, `%TEMP%` on Windows."""
    return Path("/tmp") if not IS_WINDOWS else Path(tempfile.gettempdir())


def harness_temp(name: str) -> Path:
    """A harness-owned scratch directory, created if missing.

    Never raises: a read-only or unusual temp location degrades to a directory
    under the harness state dir rather than taking down the import that asked
    for it.
    """
    for base in (temp_root(), Path.home() / ".qwen-harness" / "tmp"):
        candidate = base / name
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError:
            continue
    return Path(tempfile.mkdtemp(prefix=f"{name}-"))


# ── the shell ─────────────────────────────────────────────────────────────
# On macOS/Linux a command is `/bin/sh -c "<command>"`. On Windows the useful
# equivalent is PowerShell, not cmd.exe: it is what a Windows developer
# actually types, it handles multi-line scripts, and `$LASTEXITCODE` gives a
# faithful exit status for native programs — which `cmd /c a & b` does not.
#
# QH_SHELL overrides the choice (`powershell`, `pwsh`, `cmd`, `sh`) for anyone
# who wants Git Bash or cmd instead.

_SHELL_ENV = "QH_SHELL"


def shell_kind() -> str:
    """Which shell family runs agent/terminal commands here."""
    override = (os.environ.get(_SHELL_ENV) or "").strip().lower()
    if override in {"powershell", "pwsh", "cmd", "sh", "bash"}:
        return override
    if not IS_WINDOWS:
        return "sh"
    return "pwsh" if shutil.which("pwsh") else "powershell"


def _powershell_binary(kind: str) -> str:
    if kind == "pwsh":
        return shutil.which("pwsh") or "pwsh"
    found = shutil.which("powershell")
    if found:
        return found
    # PATH is unreliable in a GUI-launched process; fall back to the known
    # absolute location rather than failing with "file not found".
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    return str(Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def shell_argv(command: str) -> list[str]:
    """argv that runs `command` through this platform's shell.

    PowerShell is invoked with `-Command -` style semantics via `-Command`,
    which accepts an arbitrary script as one argument — subprocess quotes it
    correctly on Windows, so no manual escaping is needed here.
    """
    kind = shell_kind()
    if kind in {"sh", "bash"}:
        return ["/bin/sh", "-c", command] if kind == "sh" else ["/bin/bash", "-c", command]
    if kind == "cmd":
        comspec = os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"
        return [comspec, "/d", "/s", "/c", command]
    return [
        _powershell_binary(kind),
        "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-Command", command,
    ]


def cwd_reporting_script(command: str, marker: str) -> str:
    """`command`, followed by a line that reports the resulting directory.

    The terminal surface runs each command in a fresh subprocess, so `cd` would
    never persist to the next one. Both shells therefore print their final
    working directory after a marker, and the caller feeds that back as the
    next call's cwd.

    PowerShell needs its own form: `%CD%` in a cmd one-liner is expanded at
    *parse* time, so a `cd` earlier on the same line is invisible to it.
    """
    kind = shell_kind()
    if kind in {"sh", "bash"}:
        return f"{command}\nprintf '\\n{marker}%s' \"$PWD\""
    if kind == "cmd":
        # /v:on is added by the caller's argv for this branch; !CD! is the
        # delayed-expansion form, which does see an earlier `cd`.
        return f"{command}\r\necho.\r\necho {marker}!CD!"
    return (
        f"{command}\n"
        f"$__qh_code = if ($null -ne $LASTEXITCODE) {{ $LASTEXITCODE }} elseif ($?) {{ 0 }} else {{ 1 }}\n"
        f"[Console]::Out.Write(\"`n{marker}\" + (Get-Location).ProviderPath)\n"
        f"exit $__qh_code\n"
    )


def shell_argv_for_terminal(command: str, marker: str) -> list[str]:
    """argv for the terminal surface: runs `command` and reports the new cwd."""
    script = cwd_reporting_script(command, marker)
    if shell_kind() == "cmd":
        comspec = os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"
        return [comspec, "/d", "/s", "/v:on", "/c", script]
    return shell_argv(script)


# ── dangerous command patterns ────────────────────────────────────────────
# The POSIX list guarded against `sudo` / `rm -rf`. Those substrings never
# appear in the Windows commands that do the same damage, so a Windows user
# had *no* guard at all. This is a basic footgun filter, not a sandbox.

POSIX_DANGEROUS_PATTERNS = ("sudo", "rm -rf", "mkfs", ":(){:|:&};:")
WINDOWS_DANGEROUS_PATTERNS = (
    "rmdir /s", "rd /s", "del /s", "del /f /s", "format ",
    "remove-item -recurse -force", "remove-item -force -recurse",
    "cipher /w", "diskpart", "vssadmin delete", "bcdedit",
)


def dangerous_patterns() -> tuple[str, ...]:
    """Substrings refused by the terminal and by run_shell, for this platform.

    The POSIX ones are kept on Windows too: Git Bash and WSL are a `bash -c`
    away, and a pasted `rm -rf /` should be refused wherever it is typed.
    """
    if IS_WINDOWS:
        return POSIX_DANGEROUS_PATTERNS + WINDOWS_DANGEROUS_PATTERNS
    return POSIX_DANGEROUS_PATTERNS


def blocked_pattern(command: str) -> str | None:
    """The first dangerous pattern `command` contains, or None."""
    lowered = (command or "").lower()
    for pattern in dangerous_patterns():
        if pattern in lowered:
            return pattern
    return None


# ── opening things for the user ───────────────────────────────────────────

def open_path(path: str | Path) -> bool:
    """Open a file or URL with the OS default handler. True if it was launched."""
    target = str(path)
    try:
        if IS_WINDOWS:
            os.startfile(target)  # noqa: S606 — the documented Windows API for this
            return True
        opener = "open" if IS_MAC else "xdg-open"
        subprocess.run([opener, target], check=False)
        return True
    except (OSError, AttributeError):
        return False


# ── detached background processes ─────────────────────────────────────────
# `start_new_session=True` is POSIX-only. The Windows equivalents keep a
# spawned `ollama serve` alive past this process and stop a console window
# flashing up in a GUI app.

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def detached_kwargs() -> dict:
    """Popen kwargs that outlive this process without opening a console."""
    if IS_WINDOWS:
        return {"creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW}
    return {"start_new_session": True}


def quiet_kwargs() -> dict:
    """Popen/run kwargs that suppress a console window for a short-lived child."""
    if IS_WINDOWS:
        return {"creationflags": CREATE_NO_WINDOW}
    return {}


# ── where binaries live ───────────────────────────────────────────────────
# A GUI-launched process does not inherit the shell's PATH on either platform:
# macOS hands a Finder app `/usr/bin:/bin:/usr/sbin:/sbin`, and a Windows app
# started from a shortcut misses anything a shell profile added. Ollama in
# particular installs per-user under %LOCALAPPDATA% and is routinely absent.

def extra_path_dirs() -> tuple[str, ...]:
    """Directories worth appending to PATH, whether or not they exist."""
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        return (
            str(Path(local) / "Programs" / "Ollama"),
            str(Path(program_files) / "Ollama"),
            str(Path(local) / "Ollama"),
            str(Path(program_files) / "Git" / "cmd"),
            str(Path(program_files) / "Git" / "usr" / "bin"),   # grep, find, sh
            str(Path(program_files) / "nodejs"),
            str(Path(program_files) / "dotnet"),
            str(Path(local) / "Programs" / "Python" / "Python312"),
            str(Path(local) / "Programs" / "Python" / "Python312" / "Scripts"),
            str(Path(local) / "Microsoft" / "WindowsApps"),
            str(Path(program_files_x86) / "GitHub CLI"),
        )
    return (
        "/opt/homebrew/bin", "/opt/homebrew/sbin",      # Apple Silicon Homebrew
        "/usr/local/bin", "/usr/local/sbin",            # Intel Homebrew, manual installs
        str(Path.home() / ".local" / "bin"),
        str(Path.home() / ".ollama" / "bin"),
        "/Applications/Ollama.app/Contents/Resources",  # the Ollama.app bundle
    )


def repair_path() -> str:
    """Put the usual binary locations back on PATH. Returns the new PATH."""
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    lowered = {p.lower() for p in parts} if IS_WINDOWS else set(parts)
    for extra in extra_path_dirs():
        key = extra.lower() if IS_WINDOWS else extra
        if key not in lowered and os.path.isdir(extra):
            parts.append(extra)
            lowered.add(key)
    os.environ["PATH"] = os.pathsep.join(parts)
    return os.environ["PATH"]


def find_binary(name: str) -> str | None:
    """Absolute path to `name`, PATH or not.

    `shutil.which` already appends PATHEXT on Windows, so a bare "ollama"
    resolves to ollama.exe. The manual sweep covers the case where PATH itself
    is the thing that is broken.
    """
    found = shutil.which(name)
    if found:
        return found
    suffixes = (".exe", ".cmd", ".bat", "") if IS_WINDOWS else ("",)
    for directory in extra_path_dirs():
        for suffix in suffixes:
            candidate = os.path.join(directory, name + suffix)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def python_command() -> str:
    """The interpreter name to put inside a generated shell/test command.

    `python3` is not a real executable on a default Windows install — the
    `python3.exe` under WindowsApps is a Microsoft Store redirector that exits
    9009 and opens the Store, and it is missing entirely for some installs.
    Using the running interpreter's own path is both correct and unambiguous.

    It has to be quoted (the path contains spaces on most installs) and, in
    PowerShell, prefixed with the call operator: PowerShell parses a bare
    quoted string as a *string expression*, so a quoted interpreter path
    followed by `-m pytest` is a parse error rather than a command. `&` is
    what makes it a call.
    """
    if not IS_WINDOWS:
        return "python3"
    executable = sys.executable or "python"
    kind = shell_kind()
    if kind in {"powershell", "pwsh"}:
        return f'& "{executable}"'
    return f'"{executable}"'

