"""
tools/feedback.py — output ratings, and what the harness actually does with them.

Three ratings per response: dislike (-1), like (1), love (2).

Being straight about what this is and is not. Thumbs cannot finetune a GGUF
model at runtime; nothing here changes model weights. What ratings genuinely
buy you, in increasing order of ambition:

  1. **Preference conditioning** (live, automatic). Derive measurable traits
     from what you rated well vs badly — length, tool use, whether it hedged —
     and state those as rules in the system prompt. This is real behavioural
     adaptation and it takes effect on the very next turn.
  2. **Model/effort routing evidence.** Which model at which effort actually
     earns good ratings for your work, rather than a guess.
  3. **A real finetuning dataset.** `export_preference_pairs()` emits DPO-style
     {prompt, chosen, rejected} JSONL and `export_sft()` emits supervised
     examples — the actual input formats for a LoRA run when you want one.

Only (1) and (2) happen automatically. (3) is a file you take elsewhere.
"""

from __future__ import annotations

import json
import re
import statistics
import time
import uuid
from pathlib import Path
from typing import Any

from tools.policy import STATE_DIR, atomic_write_json

RATINGS_PATH = STATE_DIR / "ratings.json"

DISLIKE, LIKE, LOVE = -1, 1, 2
RATING_LABELS = {DISLIKE: "disliked", LIKE: "liked", LOVE: "loved"}

# Below this many rated samples on each side, a difference is noise. Asserting
# a "preference" from two ratings would teach the model something you never
# meant, and it would be very hard to notice going wrong.
MIN_SAMPLES_PER_SIDE = 3
# Ratio by which one side's median must differ before it counts as a signal.
LENGTH_SIGNAL_RATIO = 1.6
# Never state a target shorter than this; below it the number is an artifact of
# a few terse samples rather than a preference worth instructing on.
MIN_STATED_LENGTH = 300
MAX_STORED = 2000


def _load() -> list[dict[str, Any]]:
    try:
        payload = json.loads(RATINGS_PATH.read_text(encoding="utf-8"))
        return payload.get("ratings", []) if isinstance(payload, dict) else []
    except (OSError, ValueError, TypeError):
        return []


def _save(rows: list[dict[str, Any]]) -> None:
    atomic_write_json(RATINGS_PATH, {"version": 1, "ratings": rows[-MAX_STORED:]})


def _traits(text: str) -> dict[str, Any]:
    """Measurable properties of a response, so preferences are derived not guessed."""
    body = text or ""
    return {
        "chars": len(body),
        "has_code": "```" in body,
        "hedged": bool(re.search(
            r"\b(I'?m sorry|I apologi[sz]e|as an AI|I cannot|unfortunately|it seems that)\b",
            body, re.I)),
        "listy": body.count("\n- ") + body.count("\n* ") + len(re.findall(r"\n\d+\. ", body)),
    }


def rate(
    *,
    rating: int,
    response: str,
    prompt: str = "",
    model: str = "",
    surface: str = "",
    effort: int | None = None,
    tools_used: list[str] | None = None,
    message_id: str | None = None,
) -> dict[str, Any]:
    """Record one rating. Re-rating the same message replaces the old value."""
    if rating not in RATING_LABELS:
        return {"error": f"rating must be one of {sorted(RATING_LABELS)}"}

    rows = _load()
    row = {
        "id": message_id or str(uuid.uuid4()),
        "rating": int(rating),
        "label": RATING_LABELS[rating],
        "model": model,
        "surface": surface,
        "effort": effort,
        "tools_used": list(tools_used or []),
        "prompt": (prompt or "")[:2000],
        "response": (response or "")[:8000],
        "traits": _traits(response),
        "created_at": time.time(),
    }
    rows = [r for r in rows if r.get("id") != row["id"]]
    rows.append(row)
    _save(rows)
    return {"status": "ok", "id": row["id"], "label": row["label"], "total": len(rows)}


def unrate(message_id: str) -> dict[str, Any]:
    rows = _load()
    kept = [r for r in rows if r.get("id") != message_id]
    _save(kept)
    return {"status": "ok", "removed": len(rows) - len(kept)}


def _split(rows: list[dict[str, Any]]) -> tuple[list, list]:
    good = [r for r in rows if r.get("rating", 0) > 0]
    bad = [r for r in rows if r.get("rating", 0) < 0]
    return good, bad


def preference_guidance(model: str = "", max_rules: int = 4) -> str:
    """System-prompt rules derived from what you actually rated, or "" if unclear.

    Returns nothing until there is enough evidence on both sides. A confident
    instruction built from three ratings is worse than no instruction.
    """
    rows = _load()
    if model:
        scoped = [r for r in rows if r.get("model") == model]
        # Fall back to all models rather than saying nothing: style preferences
        # ("be brief") transfer across models; they are about you, not the model.
        rows = scoped if len(scoped) >= MIN_SAMPLES_PER_SIDE * 2 else rows

    good, bad = _split(rows)
    if len(good) < MIN_SAMPLES_PER_SIDE or len(bad) < MIN_SAMPLES_PER_SIDE:
        return ""

    rules: list[str] = []

    good_len = statistics.median(r["traits"]["chars"] for r in good if r.get("traits"))
    bad_len = statistics.median(r["traits"]["chars"] for r in bad if r.get("traits"))
    if good_len and bad_len:
        if bad_len > good_len * LENGTH_SIGNAL_RATIO:
            # Only quote a target length when it is a sane one. A handful of
            # very short ratings can put the median at "32 characters", and
            # handing a model that number produces uselessly clipped answers.
            if good_len >= MIN_STATED_LENGTH:
                rules.append(f"Keep answers concise — around {int(round(good_len, -2))} characters. "
                             "Longer answers are consistently rated worse here.")
            else:
                rules.append("Keep answers brief and to the point. Long answers are "
                             "consistently rated worse here.")
        elif good_len > bad_len * LENGTH_SIGNAL_RATIO:
            rules.append("Give fuller answers with the reasoning shown — terse replies "
                         "are consistently rated worse here.")

    def share(rows_: list, key) -> float:
        return sum(1 for r in rows_ if key(r)) / len(rows_) if rows_ else 0.0

    good_tools = share(good, lambda r: bool(r.get("tools_used")))
    bad_tools = share(bad, lambda r: bool(r.get("tools_used")))
    if good_tools - bad_tools > 0.3:
        rules.append("Use tools to check things rather than answering from memory — "
                     "answers backed by tool calls are rated better here.")
    elif bad_tools - good_tools > 0.3:
        rules.append("Answer directly when you already know the answer; unnecessary "
                     "tool calls are rated worse here.")

    if share(bad, lambda r: r.get("traits", {}).get("hedged")) - \
       share(good, lambda r: r.get("traits", {}).get("hedged")) > 0.3:
        rules.append("Skip apologies and hedging. State what you did or could not do.")

    good_code = share(good, lambda r: r.get("traits", {}).get("has_code"))
    bad_code = share(bad, lambda r: r.get("traits", {}).get("has_code"))
    if good_code - bad_code > 0.3:
        rules.append("Include concrete code examples — answers with code are rated better here.")

    if not rules:
        return ""
    head = (f"Learned from {len(good)} well-rated and {len(bad)} poorly-rated "
            "responses in this workspace:")
    return head + "\n" + "\n".join(f"- {rule}" for rule in rules[:max_rules])


def summary() -> dict[str, Any]:
    """Counts, per-model scores, and the guidance currently in effect."""
    rows = _load()
    by_model: dict[str, dict[str, Any]] = {}
    for row in rows:
        bucket = by_model.setdefault(row.get("model") or "unknown",
                                     {"disliked": 0, "liked": 0, "loved": 0, "score": 0})
        bucket[row["label"]] += 1
        bucket["score"] += row["rating"]
    for bucket in by_model.values():
        total = bucket["disliked"] + bucket["liked"] + bucket["loved"]
        bucket["rated"] = total
        bucket["average"] = round(bucket["score"] / total, 2) if total else 0.0

    counts = {label: sum(1 for r in rows if r["label"] == label) for label in RATING_LABELS.values()}
    return {
        "status": "ok",
        "total": len(rows),
        "counts": counts,
        "by_model": by_model,
        "guidance": preference_guidance(),
        "guidance_active": bool(preference_guidance()),
        "min_samples_per_side": MIN_SAMPLES_PER_SIDE,
    }


# ── export for real finetuning ───────────────────────────────────────────
def export_preference_pairs(path: str | Path | None = None) -> dict[str, Any]:
    """DPO-style {prompt, chosen, rejected} JSONL, for prompts rated both ways."""
    rows = _load()
    by_prompt: dict[str, dict[str, list]] = {}
    for row in rows:
        key = (row.get("prompt") or "").strip()[:400]
        if not key:
            continue
        side = "chosen" if row["rating"] > 0 else "rejected"
        by_prompt.setdefault(key, {"chosen": [], "rejected": []})[side].append(row)

    pairs = []
    for prompt, sides in by_prompt.items():
        if not sides["chosen"] or not sides["rejected"]:
            continue
        # Best-rated positive against any negative.
        best = max(sides["chosen"], key=lambda r: r["rating"])
        for worst in sides["rejected"]:
            pairs.append({"prompt": prompt, "chosen": best["response"],
                          "rejected": worst["response"], "model": best.get("model", "")})

    target = Path(path) if path else STATE_DIR / "finetune-preference-pairs.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for pair in pairs:
            handle.write(json.dumps(pair, ensure_ascii=False) + "\n")
    return {"status": "ok", "pairs": len(pairs), "path": str(target),
            "note": "DPO format. Needs pairs where the same prompt was rated both ways."}


def export_sft(path: str | Path | None = None, min_rating: int = LOVE) -> dict[str, Any]:
    """Supervised examples from your best-rated responses, as chat JSONL."""
    rows = [r for r in _load() if r.get("rating", 0) >= min_rating and r.get("prompt")]
    target = Path(path) if path else STATE_DIR / "finetune-sft.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps({"messages": [
                {"role": "user", "content": row["prompt"]},
                {"role": "assistant", "content": row["response"]},
            ]}, ensure_ascii=False) + "\n")
    return {"status": "ok", "examples": len(rows), "path": str(target),
            "min_rating": min_rating}
