"""Durable run, task, event, and checkpoint storage for the desktop client."""

from __future__ import annotations

import copy
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.policy import STATE_DIR, atomic_write_json


RUNS_PATH = STATE_DIR / "runs.json"
ACTIVE_STATES = {"queued", "planning", "running", "thinking", "working", "verifying", "waiting_permission"}
TERMINAL_STATES = {"completed", "failed", "cancelled", "interrupted"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunStore:
    def __init__(self, path: Path = RUNS_PATH):
        self.path = path
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self._runs: list[dict[str, Any]] = []
        self._load()
        self._recover_interrupted()

    def _load(self) -> None:
        try:
            import json
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self._runs = payload.get("runs", []) if isinstance(payload, dict) else []
        except (OSError, ValueError, TypeError):
            self._runs = []

    def _save(self) -> None:
        self._runs = self._runs[-200:]
        atomic_write_json(self.path, {"version": 1, "runs": self._runs})

    def _recover_interrupted(self) -> None:
        changed = False
        for run in self._runs:
            if run.get("status") in ACTIVE_STATES:
                run["status"] = "interrupted"
                run["updated_at"] = utc_now()
                self._append_event_unlocked(run, "run.interrupted", {"reason": "backend restarted"})
                changed = True
        if changed:
            self._save()

    def _find(self, run_id: str) -> dict[str, Any] | None:
        return next((run for run in self._runs if run.get("id") == run_id), None)

    def list(self, project_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self.lock:
            rows = self._runs
            if project_id:
                rows = [run for run in rows if run.get("project_id") == project_id]
            return copy.deepcopy(list(reversed(rows[-max(1, min(limit, 200)):])) )

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self.lock:
            run = self._find(run_id)
            return copy.deepcopy(run) if run else None

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            now = utc_now()
            run = {
                "id": str(uuid.uuid4()),
                "status": "queued",
                "surface": payload.get("surface") or "home",
                "instruction": str(payload.get("instruction") or "")[:20000],
                "project_id": payload.get("project_id"),
                "cwd": payload.get("cwd"),
                "model_key": payload.get("model_key"),
                "effort_level": max(1, min(4, int(payload.get("effort_level") or 1))),
                "effort_source": payload.get("effort_source") or "auto",
                "effort_reason": payload.get("effort_reason"),
                "agent_ids": list(payload.get("agent_ids") or []),
                "parent_run_id": payload.get("parent_run_id"),
                "attempt": int(payload.get("attempt") or 1),
                "cancel_requested": False,
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
                "tasks": list(payload.get("tasks") or []),
                "checkpoints": list(payload.get("checkpoints") or []),
                "events": [],
                "result": None,
                "error": None,
            }
            self._append_event_unlocked(run, "run.created", {
                "surface": run["surface"], "agent_ids": run["agent_ids"], "attempt": run["attempt"],
                "effort_level": run["effort_level"], "effort_source": run["effort_source"],
            })
            self._runs.append(run)
            self._save()
            self.changed.notify_all()
            return copy.deepcopy(run)

    def _append_event_unlocked(self, run: dict[str, Any], event_type: str, data: dict[str, Any] | None) -> dict[str, Any]:
        events = run.setdefault("events", [])
        event = {
            "seq": int(events[-1]["seq"] if events else 0) + 1,
            "type": event_type,
            "data": data or {},
            "created_at": utc_now(),
        }
        events.append(event)
        run["events"] = events[-500:]
        run["updated_at"] = event["created_at"]
        return event

    def append_event(self, run_id: str, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any] | None:
        with self.lock:
            run = self._find(run_id)
            if not run:
                return None
            event = self._append_event_unlocked(run, event_type, data)
            self._save()
            self.changed.notify_all()
            return copy.deepcopy(event)

    def update(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        with self.lock:
            run = self._find(run_id)
            if not run:
                return None
            status = payload.get("status")
            if status:
                run["status"] = status
                if status in TERMINAL_STATES:
                    run["completed_at"] = utc_now()
            for key in ("result", "error", "tasks", "checkpoints", "effort_level", "effort_source", "effort_reason"):
                if key in payload:
                    if key == "effort_level":
                        run[key] = max(1, min(4, int(payload[key] or 1)))
                    elif key == "effort_source":
                        run[key] = payload[key] if payload[key] in {"auto", "manual", "model"} else "auto"
                    else:
                        run[key] = payload[key]
            self._append_event_unlocked(run, f"run.{status}" if status else "run.updated", {
                key: payload[key] for key in ("status", "error") if key in payload
            })
            self._save()
            self.changed.notify_all()
            return copy.deepcopy(run)

    def cancel(self, run_id: str) -> dict[str, Any] | None:
        with self.lock:
            run = self._find(run_id)
            if not run:
                return None
            run["cancel_requested"] = True
            if run.get("status") not in TERMINAL_STATES:
                run["status"] = "cancelled"
                run["completed_at"] = utc_now()
            self._append_event_unlocked(run, "run.cancelled", {"reason": "user request"})
            self._save()
            self.changed.notify_all()
            return copy.deepcopy(run)

    def retry(self, run_id: str) -> dict[str, Any] | None:
        original = self.get(run_id)
        if not original:
            return None
        return self.create({
            "surface": original.get("surface"),
            "instruction": original.get("instruction"),
            "project_id": original.get("project_id"),
            "cwd": original.get("cwd"),
            "model_key": original.get("model_key"),
            "agent_ids": original.get("agent_ids"),
            "effort_level": original.get("effort_level"),
            "effort_source": original.get("effort_source"),
            "parent_run_id": original.get("id"),
            "attempt": int(original.get("attempt") or 1) + 1,
        })

    def events_after(self, run_id: str, seq: int) -> list[dict[str, Any]]:
        with self.lock:
            run = self._find(run_id)
            if not run:
                return []
            return copy.deepcopy([event for event in run.get("events", []) if int(event.get("seq", 0)) > seq])

    def wait_for_events(self, run_id: str, seq: int, timeout: float = 15.0) -> list[dict[str, Any]]:
        with self.changed:
            events = self.events_after(run_id, seq)
            if events:
                return events
            self.changed.wait(timeout)
            return self.events_after(run_id, seq)


RUN_STORE = RunStore()
