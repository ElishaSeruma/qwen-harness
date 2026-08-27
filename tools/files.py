"""
tools/files.py – file system and shell tools.

`search_files` and `find_files` are implemented in pure Python rather than by
shelling out to grep/find. That started as a Windows necessity and is now the
behaviour everywhere, because the shell-out was quietly wrong on Windows in the
worst possible way: `find.exe` on Windows is a *string search* utility, so
`find <path> -name "*.py"` does not fail — it prints a usage error to stderr,
exits non-zero, and `find_files` returned `{"status": "ok", "files": []}`. An
agent asking "where are the tests?" was told, authoritatively, that there are
none. grep is likewise absent unless Git's `usr/bin` happens to be on PATH.

One implementation for both platforms also means the exclusions (.git,
node_modules, __pycache__, .build) and the result caps are defined once instead
of drifting between two command lines.
"""

from __future__ import annotations
import fnmatch
import os
import re
import subprocess
import time
from pathlib import Path

from tools.platform_support import blocked_pattern, shell_argv, quiet_kwargs
from tools.policy import PermissionDenied, require_path

# Directories never descended into by search_files / find_files. Walking
# node_modules is what turned a "search the project" call into a 20s timeout.
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".build", ".venv", "venv",
             "dist", "build", ".next", ".tox", ".mypy_cache", ".pytest_cache"}
SEARCH_TIMEOUT_S = 20
MAX_SEARCHED_BYTES = 2 * 1024 * 1024   # skip anything bigger; it is not source


def read_file(path: str, cwd: str | None = None) -> dict:
    """Read and return the contents of a file."""
    try:
        p = require_path(path, cwd)
    except PermissionDenied as exc:
        return {"error": str(exc), "permission_required": True, "path": str(exc.path)}
    if not p.exists():
        return {"error": f"File not found: {path}"}
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
        return {"status": "ok", "path": str(p), "content": content}
    except Exception as e:
        return {"error": str(e)}


def write_file(path: str, content: str, cwd: str | None = None) -> dict:
    """Write content to a file, creating parent directories as needed."""
    try:
        p = require_path(path, cwd, parent=True)
    except PermissionDenied as exc:
        return {"error": str(exc), "permission_required": True, "path": str(exc.path)}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"status": "ok", "path": str(p), "bytes_written": len(content.encode())}
    except Exception as e:
        return {"error": str(e)}


def append_file(path: str, content: str, cwd: str | None = None) -> dict:
    """Append content to a file."""
    try:
        p = require_path(path, cwd, parent=True)
    except PermissionDenied as exc:
        return {"error": str(exc), "permission_required": True, "path": str(exc.path)}
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write(content)
        return {"status": "ok", "path": str(p)}
    except Exception as e:
        return {"error": str(e)}


def list_dir(path: str, cwd: str | None = None) -> dict:
    """List the contents of a directory."""
    try:
        p = require_path(path, cwd)
    except PermissionDenied as exc:
        return {"error": str(exc), "permission_required": True, "path": str(exc.path)}
    if not p.exists():
        return {"error": f"Path not found: {path}"}
    if not p.is_dir():
        return {"error": f"Not a directory: {path}"}
    try:
        entries = []
        for item in sorted(p.iterdir()):
            entries.append({
                "name": item.name,
                "type": "dir" if item.is_dir() else "file",
                "size": item.stat().st_size if item.is_file() else None,
            })
        return {"status": "ok", "path": str(p), "entries": entries}
    except Exception as e:
        return {"error": str(e)}


def run_shell(command: str, timeout: int = 30, cwd: str | None = None) -> dict:
    """
    Run a shell command. Returns stdout, stderr, and return code.
    Restricted: refuses the platform's destructive command patterns.

    The shell is /bin/sh on macOS/Linux and PowerShell on Windows (override
    with the QH_SHELL environment variable).
    """
    # Basic safety guard. The pattern list is platform-aware: "rm -rf" never
    # appears in the Windows command that does the same damage, so a Windows
    # user previously had no guard at all.
    blocked = blocked_pattern(command)
    if blocked:
        return {"error": f"Blocked dangerous command pattern: '{blocked}'"}

    try:
        working_dir = require_path(cwd or ".")
        if not working_dir.is_dir():
            return {"error": f"Working directory is not a directory: {working_dir}"}
        # Explicit argv rather than shell=True: on Windows shell=True means
        # cmd.exe, which is neither what a Windows developer types nor able to
        # report a faithful exit code for a chained command.
        result = subprocess.run(
            shell_argv(command),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=working_dir,
            **quiet_kwargs(),
        )
        return {
            "status": "ok",
            "returncode": result.returncode,
            "stdout": result.stdout[:5000],
            "stderr": result.stderr[:2000],
        }
    except PermissionDenied as exc:
        return {"error": str(exc), "permission_required": True, "path": str(exc.path)}
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {timeout}s"}
    except Exception as e:
        return {"error": str(e)}


def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False, cwd: str | None = None) -> dict:
    """
    Surgical in-place edit: replace old_string with new_string in a file.
    Far safer than write_file for changing existing code — the rest of the
    file is untouched. old_string must match exactly (including whitespace)
    and, unless replace_all, must be unique in the file.
    """
    try:
        p = require_path(path, cwd)
    except PermissionDenied as exc:
        return {"error": str(exc), "permission_required": True, "path": str(exc.path)}
    if not p.exists():
        return {"error": f"File not found: {path}"}
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"error": str(e)}
    count = content.count(old_string)
    if count == 0:
        return {"error": "old_string not found in file. It must match exactly, including whitespace/indentation."}
    if count > 1 and not replace_all:
        return {"error": f"old_string appears {count} times. Add surrounding context to make it unique, or set replace_all=true."}
    updated = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
    try:
        p.write_text(updated, encoding="utf-8")
    except Exception as e:
        return {"error": str(e)}
    return {"status": "ok", "path": str(p), "replacements": count if replace_all else 1}


def _walk(root: Path, deadline: float):
    """Yield files under `root`, skipping vendor/build dirs and honouring a deadline.

    os.walk with in-place `dirnames` pruning is what keeps node_modules out of
    the traversal entirely rather than filtering it after the fact.
    """
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if time.monotonic() > deadline:
                return
            yield Path(dirpath) / name


def search_files(query: str, path: str = ".", glob: str | None = None, max_results: int = 30, cwd: str | None = None) -> dict:
    """
    Search file contents recursively for a string/regex (like grep -rn).
    Returns matching lines with file:line locations. Optional glob filters
    filenames (e.g. "*.py").
    """
    try:
        p = require_path(path, cwd)
    except PermissionDenied as exc:
        return {"error": str(exc), "permission_required": True, "path": str(exc.path)}
    if not p.exists():
        return {"error": f"Path not found: {path}"}
    try:
        pattern = re.compile(query)
    except re.error as exc:
        return {"error": f"invalid regex: {exc}"}

    deadline = time.monotonic() + SEARCH_TIMEOUT_S
    matches: list[dict] = []
    truncated = False
    targets = [p] if p.is_file() else _walk(p, deadline)
    for candidate in targets:
        if time.monotonic() > deadline:
            truncated = True
            break
        if glob and not fnmatch.fnmatch(candidate.name, glob):
            continue
        try:
            if candidate.stat().st_size > MAX_SEARCHED_BYTES:
                continue
            # errors="replace" rather than skipping undecodable files: a source
            # file with one stray byte should still be searchable.
            with candidate.open("r", encoding="utf-8", errors="replace") as handle:
                for lineno, line in enumerate(handle, 1):
                    if chr(0) in line:    # binary — grep's --binary-files=without-match
                        break
                    if pattern.search(line):
                        if len(matches) >= max_results:
                            truncated = True
                            break
                        matches.append({"file": str(candidate), "line": str(lineno),
                                        "text": line.strip()[:200]})
        except (OSError, ValueError):
            continue
        if truncated:
            break
    return {"status": "ok", "query": query, "matches": matches, "truncated": truncated}


def find_files(pattern: str, path: str = ".", max_results: int = 50, cwd: str | None = None) -> dict:
    """Find files by name glob (e.g. "*.py", "test_*") recursively under path."""
    try:
        p = require_path(path, cwd)
    except PermissionDenied as exc:
        return {"error": str(exc), "permission_required": True, "path": str(exc.path)}
    if not p.exists():
        return {"error": f"Path not found: {path}"}

    deadline = time.monotonic() + SEARCH_TIMEOUT_S
    files: list[str] = []
    truncated = False
    for candidate in _walk(p, deadline):
        # Match on the bare name, as `find -name` does — not the full path, or
        # "*.py" would also need to match the directories above it.
        if fnmatch.fnmatch(candidate.name, pattern):
            if len(files) >= max_results:
                truncated = True
                break
            files.append(str(candidate))
    return {"status": "ok", "pattern": pattern, "files": files, "truncated": truncated}
