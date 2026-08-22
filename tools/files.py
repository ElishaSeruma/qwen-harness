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
