"""
tools/memory.py – persistent, cross-session memory for agents.

Unlike agent.messages (which lives only for the duration of one Python
process), this survives across runs: ~/.qwen-harness/memory.json.

Entries are {key, value, category, updated_at}. `category` is a free-form
tag — use "user_edit" for corrections the user makes to agent output (the
"memory_user_edits" use case), "fact" for general remembered facts,
"preference" for user preferences, or anything else that's useful to filter
by later. There's no separate "memory_user_edits" tool — it's just this
same store with a category, so reads/writes/search all work uniformly.
"""

from __future__ import annotations
import json
import os
import stat
import time
from pathlib import Path

MEMORY_PATH = Path.home() / ".qwen-harness" / "memory.json"


def _load() -> dict:
    if not MEMORY_PATH.exists():
        return {"entries": {}}
    try:
        return json.loads(MEMORY_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {"entries": {}}


def _save(data: dict) -> None:
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_PATH.write_text(json.dumps(data, indent=2))
    os.chmod(MEMORY_PATH, stat.S_IRUSR | stat.S_IWUSR)


def memory_write(key: str, value: str, category: str = "fact") -> dict:
    """Remember something, keyed for later recall. Overwrites an existing key."""
    data = _load()
    data["entries"][key] = {
        "value": value,
        "category": category,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _save(data)
    return {"status": "ok", "key": key, "category": category}


def memory_read(key: str) -> dict:
    """Recall a specific remembered value by its exact key."""
    entry = _load()["entries"].get(key)
    if not entry:
        return {"error": f"No memory for key '{key}'."}
    return {"status": "ok", "key": key, **entry}


def memory_search(query: str, max_results: int = 10) -> dict:
    """Search remembered keys/values for a substring match (case-insensitive)."""
    q = query.lower()
    matches = []
    for key, entry in _load()["entries"].items():
        haystack = f"{key} {entry.get('value', '')}".lower()
        if q in haystack:
            matches.append({"key": key, **entry})
    matches.sort(key=lambda e: e.get("updated_at", ""), reverse=True)
    return {"status": "ok", "query": query, "results": matches[:max_results]}


def memory_list(category: str | None = None, max_results: int = 50) -> dict:
    """List remembered entries, optionally filtered to one category."""
    entries = _load()["entries"]
    items = [{"key": k, **v} for k, v in entries.items() if category is None or v.get("category") == category]
    items.sort(key=lambda e: e.get("updated_at", ""), reverse=True)
    return {"status": "ok", "category": category, "results": items[:max_results]}


def memory_forget(key: str) -> dict:
    """Delete a remembered entry."""
    data = _load()
    if key not in data["entries"]:
        return {"error": f"No memory for key '{key}'."}
    del data["entries"][key]
    _save(data)
    return {"status": "ok", "key": key}
