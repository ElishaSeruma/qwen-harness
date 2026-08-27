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

# Identity, plus the four-part contract qwen-code gives a subagent: an owner,
# a file boundary, an output format, and a finish condition.
#
# The first five fields are identity and were always here. The last three are
# the contract, and they are what make more than one agent working at once
# something other than a race:
#
#   fileBoundary    glob list, enforced in tools/sandbox.py at the same
#                   chokepoint that resolves every agent-supplied path
#   outputFormat    free text or a schema description, appended to the prompt
#   finishCondition a sentence the agent is told it must satisfy to be done
#
# All three are optional and default to empty. An empty boundary allows
# everything, so existing single-agent rosters behave exactly as before —
# a boundary is an opt-in contract, and defaulting to deny would break every
# roster already on disk.
AGENT_FIELDS = (
    "id", "name", "slug", "nickname", "modelKey",
    "fileBoundary", "outputFormat", "finishCondition",
)
CONTRACT_FIELDS = ("fileBoundary", "outputFormat", "finishCondition")
MAX_AGENTS = 50
MAX_BOUNDARY_PATTERNS = 24
MAX_CONTRACT_CHARS = 2000


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


def _clean_boundary(value: Any) -> list[str]:
    """Normalise a file boundary to a list of glob strings.

    Accepts a list or a comma/newline separated string, because the GUI field
    is a textarea and the API is JSON. Anything unusable becomes an empty
    boundary rather than a broken one — a malformed pattern that silently
    matched nothing would deny every write and read as the agent being
    inexplicably unable to edit anything.
    """
    if isinstance(value, str):
        parts = [part.strip() for chunk in value.splitlines() for part in chunk.split(",")]
    elif isinstance(value, (list, tuple)):
        parts = [str(part).strip() for part in value]
    else:
        return []
    seen, out = set(), []
    for part in parts:
        # A leading "/" or a drive letter would be an absolute path, which is
        # meaningless against a project-relative path and would silently never
        # match. Strip it rather than accept a pattern that cannot fire.
        part = part.lstrip("/\\").strip()
        if not part or part in seen:
            continue
        seen.add(part)
        out.append(part)
        if len(out) >= MAX_BOUNDARY_PATTERNS:
            break
    return out


def _clean(agents: Any) -> list[dict[str, Any]]:
    rows = []
    for agent in (agents or [])[:MAX_AGENTS]:
        if not isinstance(agent, dict) or not agent.get("id"):
            continue
        row = {key: agent.get(key) for key in AGENT_FIELDS}
        row["fileBoundary"] = _clean_boundary(row.get("fileBoundary"))
        for key in ("outputFormat", "finishCondition"):
            value = row.get(key)
            row[key] = str(value).strip()[:MAX_CONTRACT_CHARS] if value else ""
        rows.append(row)
    return rows


def contract_for(agent_id: str) -> dict[str, Any]:
    """One agent's boundary/format/finish contract, or empty defaults.

    Read by gui/server.py on every sandboxed tool call, so it reads from the
    same on-disk roster every client syncs against rather than trusting what
    the browser sent — an agent that could declare its own boundary per call
    could widen it per call.
    """
    for agent in get().get("agents", []):
        if agent.get("id") == agent_id:
            return {
                "fileBoundary": _clean_boundary(agent.get("fileBoundary")),
                "outputFormat": (agent.get("outputFormat") or "").strip(),
                "finishCondition": (agent.get("finishCondition") or "").strip(),
            }
    return {"fileBoundary": [], "outputFormat": "", "finishCondition": ""}


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
