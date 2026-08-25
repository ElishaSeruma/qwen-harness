"""
tools/files.py – file system and shell tools.
"""

from __future__ import annotations
import os
import subprocess
from pathlib import Path


def read_file(path: str) -> dict:
    """Read and return the contents of a file."""
    p = Path(path).expanduser()
    if not p.exists():
        return {"error": f"File not found: {path}"}
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
        return {"status": "ok", "path": str(p), "content": content}
    except Exception as e:
        return {"error": str(e)}


def write_file(path: str, content: str) -> dict:
    """Write content to a file, creating parent directories as needed."""
    p = Path(path).expanduser()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"status": "ok", "path": str(p), "bytes_written": len(content.encode())}
    except Exception as e:
        return {"error": str(e)}


def append_file(path: str, content: str) -> dict:
    """Append content to a file."""
    p = Path(path).expanduser()
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write(content)
        return {"status": "ok", "path": str(p)}
    except Exception as e:
        return {"error": str(e)}


def list_dir(path: str) -> dict:
    """List the contents of a directory."""
    p = Path(path).expanduser()
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


def run_shell(command: str, timeout: int = 30) -> dict:
    """
    Run a shell command. Returns stdout, stderr, and return code.
    Restricted: no sudo, no rm -rf.
    """
    # Basic safety guard
    dangerous = ["sudo", "rm -rf", "mkfs", ":(){:|:&};:"]
    for d in dangerous:
        if d in command:
            return {"error": f"Blocked dangerous command pattern: '{d}'"}

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "status": "ok",
            "returncode": result.returncode,
            "stdout": result.stdout[:5000],
            "stderr": result.stderr[:2000],
        }
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {timeout}s"}
    except Exception as e:
        return {"error": str(e)}


def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> dict:
    """
    Surgical in-place edit: replace old_string with new_string in a file.
    Far safer than write_file for changing existing code — the rest of the
    file is untouched. old_string must match exactly (including whitespace)
    and, unless replace_all, must be unique in the file.
    """
    p = Path(path).expanduser()
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


def search_files(query: str, path: str = ".", glob: str | None = None, max_results: int = 30) -> dict:
    """
    Search file contents recursively for a string/regex (like grep -rn).
    Returns matching lines with file:line locations. Optional glob filters
    filenames (e.g. "*.py").
    """
    p = Path(path).expanduser()
    if not p.exists():
        return {"error": f"Path not found: {path}"}
    args = ["grep", "-rn", "--binary-files=without-match",
            "--exclude-dir=.git", "--exclude-dir=node_modules", "--exclude-dir=__pycache__",
            "--exclude-dir=.build", "-E", query]
    if glob:
        args.insert(-2, f"--include={glob}")
    args.append(str(p))
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return {"error": "search timed out after 20s — narrow the path or query"}
    if result.returncode == 2:
        return {"error": result.stderr.strip()[:500] or "grep failed"}
    lines = result.stdout.splitlines()
    matches = []
    for line in lines[:max_results]:
        file_part, _, rest = line.partition(":")
        lineno, _, text = rest.partition(":")
        matches.append({"file": file_part, "line": lineno, "text": text.strip()[:200]})
    return {"status": "ok", "query": query, "matches": matches, "truncated": len(lines) > max_results}


def find_files(pattern: str, path: str = ".", max_results: int = 50) -> dict:
    """Find files by name glob (e.g. "*.py", "test_*") recursively under path."""
    p = Path(path).expanduser()
    if not p.exists():
        return {"error": f"Path not found: {path}"}
    try:
        result = subprocess.run(
            ["find", str(p), "-name", pattern, "-not", "-path", "*/.git/*",
             "-not", "-path", "*/node_modules/*", "-not", "-path", "*/__pycache__/*"],
            capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        return {"error": "find timed out after 20s — narrow the path"}
    files = result.stdout.splitlines()
    return {"status": "ok", "pattern": pattern, "files": files[:max_results], "truncated": len(files) > max_results}
