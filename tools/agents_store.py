"""
tools/agents_store.py — the agent roster, shared across every client.

Agents used to live only in each browser's localStorage, which is per-origin:
the Mac app's WKWebView and a browser tab kept entirely separate rosters. Edit
an agent's name or avatar in one and the other never hears about it, so the two
drift until they look like different applications.

The roster now lives here, once, and clients sync against it. Last write wins
by `updated_at`, so the client you actually edited on is the one that survives
— with `force` for when you want to say "this device is right" explicitly.
"""

from __future__ import annotations

import json
import time
from typing import Any

from tools.policy import STATE_DIR, atomic_write_json

AGENTS_PATH = STATE_DIR / "agents.json"
AGENT_FIELDS = ("id", "name", "slug", "nickname", "modelKey")
MAX_AGENTS = 50


def load() -> dict[str, Any]:
    try:
        payload = json.loads(AGENTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def get() -> dict[str, Any]:
    stored = load()
    return {
        "status": "ok",
        "agents": stored.get("agents", []),
        "agent_seq": stored.get("agent_seq", 0),
        "active_agent_id": stored.get("active_agent_id"),
        "updated_at": stored.get("updated_at", 0),
        "has_roster": bool(stored.get("agents")),
    }


def _clean(agents: Any) -> list[dict[str, Any]]:
    rows = []
    for agent in (agents or [])[:MAX_AGENTS]:
        if not isinstance(agent, dict) or not agent.get("id"):
            continue
        rows.append({key: agent.get(key) for key in AGENT_FIELDS})
    return rows


def save(payload: dict[str, Any]) -> dict[str, Any]:
    """Store a roster. Refuses an older one unless `force` is set.

    The timestamp check is what makes syncing safe: a stale client that loads
    days later must not overwrite the roster you actually edited.
    """
    agents = _clean(payload.get("agents"))
    if not agents:
        return {"error": "refusing to store an empty roster"}

    incoming_at = float(payload.get("updated_at") or 0)
    existing = load()
    existing_at = float(existing.get("updated_at") or 0)
    if existing.get("agents") and incoming_at < existing_at and not payload.get("force"):
        return {
            "status": "stale",
            "stored_updated_at": existing_at,
            "your_updated_at": incoming_at,
            "agents": existing.get("agents", []),
            "agent_seq": existing.get("agent_seq", 0),
            "active_agent_id": existing.get("active_agent_id"),
            "detail": "A newer roster is already stored. Adopt it, or resend with force.",
        }

    record = {
        "version": 1,
        "agents": agents,
        "agent_seq": int(payload.get("agent_seq") or len(agents)),
        "active_agent_id": payload.get("active_agent_id"),
        "updated_at": time.time() if payload.get("force") else (incoming_at or time.time()),
    }
    atomic_write_json(AGENTS_PATH, record)
    return {"status": "ok", "agents": record["agents"], "agent_seq": record["agent_seq"],
            "active_agent_id": record["active_agent_id"], "updated_at": record["updated_at"]}
