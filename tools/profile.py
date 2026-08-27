"""
tools/profile.py — who the user is, so models stop saying "user".

Stored at ~/.qwen-harness/profile.json and injected into every system prompt.
Two things this buys:

  * The models address you by name and can reason about your role, stack, and
    timezone instead of guessing.
  * It becomes a **hallucination tripwire**. If a model calls you anything but
    your stated name, it is not working from context — and that is visible
    immediately rather than buried in a plausible-sounding answer.

Everything is optional. Only fields you fill in are sent; empty ones are never
mentioned, so the model is never handed a blank to invent a value for.
"""

from __future__ import annotations

import json
from typing import Any

from tools.policy import STATE_DIR, atomic_write_json

PROFILE_PATH = STATE_DIR / "profile.json"

# (key, label, hint) — the label is what the model is told, so it is phrased for
# the model rather than for the form.
FIELDS: tuple[tuple[str, str, str], ...] = (
    ("name", "Name", "What to call you. The model must use this, never 'user'."),
    ("pronouns", "Pronouns", "e.g. she/her, he/him, they/them"),
    ("age", "Age", "Optional; helps pitch explanations"),
    ("location", "Location", "City or country — also used for local time"),
    ("timezone", "Timezone", "IANA name, e.g. Europe/Dublin. Blank uses your machine's."),
    ("role", "Role", "e.g. founder, backend engineer, student"),
    ("company", "Company or project", "What you're building"),
    ("expertise", "Expertise level", "e.g. senior in Python, new to Rust"),
    ("stack", "Tech stack", "Languages, frameworks and tools you use"),
    ("goals", "Current goals", "What you're trying to get done right now"),
    ("communication_style", "How you like answers", "e.g. terse, no preamble, show code first"),
    ("avoid", "Things to avoid", "e.g. don't suggest cloud services, no emoji"),
    ("notes", "Anything else", "Free text the models should know about you"),
)
FIELD_KEYS = tuple(key for key, _, _ in FIELDS)
MAX_FIELD_CHARS = 2000


def load() -> dict[str, Any]:
    try:
        payload = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        return payload.get("profile", {}) if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save(profile: dict[str, Any]) -> dict[str, Any]:
    """Store the profile, keeping only known fields."""
    clean = {}
    for key in FIELD_KEYS:
        value = profile.get(key)
        if value is None:
            continue
        text = str(value).strip()[:MAX_FIELD_CHARS]
        if text:
            clean[key] = text
    atomic_write_json(PROFILE_PATH, {"version": 1, "profile": clean})
    return {"status": "ok", "profile": clean, "filled": len(clean), "total": len(FIELD_KEYS)}


def describe() -> str:
    """The system-prompt block. Empty string when nothing is filled in."""
    profile = load()
    if not profile:
        return ""

    lines = []
    name = profile.get("name")
    if name:
        lines.append(
            f"You are talking to {name}. Address them as {name} — never as "
            f'"user", "the user", or any other name.'
        )
    label_by_key = {key: label for key, label, _ in FIELDS}
    for key in FIELD_KEYS:
        if key == "name" or key not in profile:
            continue
        lines.append(f"- {label_by_key[key]}: {profile[key]}")

    if len(lines) <= 1 and not name:
        return ""
    head = "About the person you are working with:"
    return head + "\n" + "\n".join(lines)


def status() -> dict[str, Any]:
    profile = load()
    return {
        "status": "ok",
        "profile": profile,
        "fields": [{"key": k, "label": l, "hint": h} for k, l, h in FIELDS],
        "filled": len(profile),
        "total": len(FIELD_KEYS),
        "has_name": bool(profile.get("name")),
        "prompt_block": describe(),
    }
