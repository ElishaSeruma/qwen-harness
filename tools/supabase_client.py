"""
tools/supabase_client.py – Supabase connector (REST/PostgREST, service_role key).

Same pattern as tools/github.py: connect once, credentials stored locally
outside the repo at ~/.qwen-harness/secrets.json (chmod 600), never sent to
the browser. All requests go through gui/server.py's backend, using the
service_role key — this bypasses RLS by design, since these tables (see
supabase/schema.sql) are only ever reached from this trusted local backend.

Connect with:
    from tools.supabase_client import sb_connect
    sb_connect("https://xxxx.supabase.co", "eyJ...service_role_key")
"""

from __future__ import annotations
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from tools.policy import atomic_write_json

SECRETS_PATH = Path.home() / ".qwen-harness" / "secrets.json"
EMBEDDING_MODEL = "nomic-embed-text"
EMBEDDING_DIM = 768
COMPACTION_THRESHOLD_PCT = 0.75


def _load_secrets() -> dict:
    if not SECRETS_PATH.exists():
        return {}
    try:
        return json.loads(SECRETS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_secrets(data: dict) -> None:
    atomic_write_json(SECRETS_PATH, data)


def _creds() -> tuple[str, str] | None:
    sb = _load_secrets().get("supabase")
    if not sb or not sb.get("url") or not sb.get("service_role_key"):
        return None
    return sb["url"].rstrip("/"), sb["service_role_key"]


def sb_connect(url: str, service_role_key: str) -> dict:
    """Validate credentials against the REST root and store them locally."""
    url = url.rstrip("/")
    try:
        req = urllib.request.Request(f"{url}/rest/v1/", headers={
            "apikey": service_role_key,
            "Authorization": f"Bearer {service_role_key}",
        })
        urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"error": "Invalid Supabase URL or service_role key."}
        # PostgREST returns 404 for the bare root with no tables defined yet — still a valid connection.
        if e.code != 404:
            return {"error": f"Supabase API error: {e.code}"}
    except Exception as e:
        return {"error": str(e)}

    secrets = _load_secrets()
    secrets["supabase"] = {"url": url, "service_role_key": service_role_key}
    _save_secrets(secrets)
    return {"status": "ok", "url": url}


def sb_disconnect() -> dict:
    secrets = _load_secrets()
    secrets.pop("supabase", None)
    _save_secrets(secrets)
    return {"status": "ok"}


def sb_status() -> dict:
    creds = _creds()
    return {"connected": creds is not None, "url": creds[0] if creds else None}


def _request(path: str, method: str = "GET", body: Any = None, params: dict | None = None, prefer: str | None = None) -> Any:
    creds = _creds()
    if not creds:
        raise RuntimeError("Supabase is not connected. Call sb_connect(url, service_role_key) first.")
    url, key = creds
    query = ""
    if params:
        from urllib.parse import urlencode
        query = "?" + urlencode(params)
    req_url = f"{url}/rest/v1/{path}{query}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    req = urllib.request.Request(req_url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


# ── embeddings (via Ollama's local nomic-embed-text) ────────────────────────

def embed_text(text: str) -> list[float] | None:
    """Embed `text` via Ollama's local embedding model. Returns None if unavailable."""
    try:
        req = urllib.request.Request(
            "http://localhost:11434/api/embeddings",
            data=json.dumps({"model": EMBEDDING_MODEL, "prompt": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data.get("embedding")
    except Exception:
        return None


# ── chats ────────────────────────────────────────────────────────────────

def sb_create_chat(agent_id: str, agent_name: str, model_key: str, context_limit: int | None, title: str | None = None) -> dict:
    row = {
        "agent_id": agent_id, "agent_name": agent_name, "model_key": model_key,
        "context_limit": context_limit, "title": title,
    }
    result = _request("chats", method="POST", body=row, prefer="return=representation")
    return {"status": "ok", "chat": result[0]} if result else {"error": "no row returned"}


def sb_list_chats(agent_id: str | None = None, limit: int = 50) -> dict:
    params = {"select": "*", "order": "updated_at.desc", "limit": str(limit)}
    if agent_id:
        params["agent_id"] = f"eq.{agent_id}"
    result = _request("chats", params=params)
    return {"status": "ok", "chats": result or []}


def sb_get_chat(chat_id: str) -> dict:
    result = _request("chats", params={"id": f"eq.{chat_id}", "select": "*"})
    if not result:
        return {"error": "chat not found"}
    return {"status": "ok", "chat": result[0]}


def sb_update_chat_tokens(chat_id: str, current_tokens: int) -> dict:
    _request(f"chats?id=eq.{chat_id}", method="PATCH", body={"current_tokens": current_tokens})
    return {"status": "ok"}


def sb_touch_chat(chat_id: str, title: str | None = None) -> dict:
    body: dict = {"updated_at": "now()"}
    if title:
        body["title"] = title
    _request(f"chats?id=eq.{chat_id}", method="PATCH", body=body)
    return {"status": "ok"}


# ── messages ─────────────────────────────────────────────────────────────

def sb_add_message(chat_id: str, role: str, content: str | None = None,
                    tool_calls: list | None = None, tool_call_id: str | None = None,
                    tool_name: str | None = None, token_count: int | None = None) -> dict:
    row = {
        "chat_id": chat_id, "role": role, "content": content,
        "tool_calls": tool_calls, "tool_call_id": tool_call_id,
        "tool_name": tool_name, "token_count": token_count,
    }
    result = _request("chat_messages", method="POST", body=row, prefer="return=representation")
    return {"status": "ok", "message": result[0]} if result else {"error": "no row returned"}


def sb_get_messages(chat_id: str, after_message_id: str | None = None) -> dict:
    params = {"chat_id": f"eq.{chat_id}", "select": "*", "order": "created_at.asc"}
    result = _request("chat_messages", params=params)
    messages = result or []
    if after_message_id:
        idx = next((i for i, m in enumerate(messages) if m["id"] == after_message_id), -1)
        messages = messages[idx + 1:]
    return {"status": "ok", "messages": messages}


# ── memory matrix ────────────────────────────────────────────────────────

def sb_memory_write(key: str, value: str, category: str = "fact", chat_id: str | None = None) -> dict:
    embedding = embed_text(f"{key}: {value}")
    row = {"key": key, "value": value, "category": category, "chat_id": chat_id, "embedding": embedding}
    result = _request("memory_matrix", method="POST", body=row, prefer="return=representation")
    return {"status": "ok", "entry": result[0], "embedded": embedding is not None} if result else {"error": "no row returned"}


def sb_memory_search(query: str, max_results: int = 10, semantic: bool = True) -> dict:
    if semantic:
        embedding = embed_text(query)
        if embedding:
            try:
                result = _request("rpc/match_memory", method="POST", body={
                    "query_embedding": embedding, "match_count": max_results,
                })
                return {"status": "ok", "query": query, "mode": "semantic", "results": result or []}
            except Exception:
                pass  # fall back to keyword search below
    result = _request("memory_matrix", params={
        "or": f"(key.ilike.*{query}*,value.ilike.*{query}*)",
        "select": "*", "order": "updated_at.desc", "limit": str(max_results),
    })
    return {"status": "ok", "query": query, "mode": "keyword", "results": result or []}


# ── work done ────────────────────────────────────────────────────────────

def sb_log_work_done(agent_id: str, action_type: str, file_path: str,
                      chat_id: str | None = None, summary: str | None = None) -> dict:
    row = {"chat_id": chat_id, "agent_id": agent_id, "action_type": action_type, "file_path": file_path, "summary": summary}
    result = _request("work_done", method="POST", body=row, prefer="return=representation")
    return {"status": "ok", "entry": result[0]} if result else {"error": "no row returned"}


# ── compaction ───────────────────────────────────────────────────────────

def sb_record_compaction(chat_id: str, summary_text: str, covers_up_to_message_id: str,
                          token_count_before: int, token_count_after: int) -> dict:
    row = {
        "chat_id": chat_id, "summary_text": summary_text,
        "covers_up_to_message_id": covers_up_to_message_id,
        "token_count_before": token_count_before, "token_count_after": token_count_after,
    }
    result = _request("compacted_context", method="POST", body=row, prefer="return=representation")
    _request(f"chats?id=eq.{chat_id}", method="PATCH", body={"last_compacted_at": "now()", "current_tokens": token_count_after})
    return {"status": "ok", "entry": result[0]} if result else {"error": "no row returned"}


def sb_get_latest_compaction(chat_id: str) -> dict:
    result = _request("compacted_context", params={
        "chat_id": f"eq.{chat_id}", "select": "*", "order": "created_at.desc", "limit": "1",
    })
    return {"status": "ok", "compaction": (result or [None])[0]}


# ── durable orchestration snapshots ─────────────────────────────────────

def sb_sync_run(run: dict, project: dict | None = None) -> dict:
    """Mirror a local run snapshot. Local storage remains authoritative offline."""
    if project:
        project_row = {
            "id": project["id"], "name": project.get("name") or project["id"],
            "working_directory": project.get("path") or run.get("cwd") or "",
            "repository": project.get("repo") or {}, "settings": project.get("settings") or {},
        }
        _request("projects", method="POST", body=project_row, params={"on_conflict": "id"}, prefer="resolution=merge-duplicates")

    status = run.get("status") or "queued"
    if status in {"thinking", "working"}:
        status = "running"
    row = {
        "id": run["id"], "project_id": run.get("project_id") if project else None,
        "parent_run_id": run.get("parent_run_id"), "surface": run.get("surface") or "home",
        "instruction": run.get("instruction") or "", "status": status,
        "cwd": run.get("cwd"), "model_key": run.get("model_key"),
        "agent_ids": run.get("agent_ids") or [], "attempt": run.get("attempt") or 1,
        "effort_level": run.get("effort_level") or 1, "effort_source": run.get("effort_source") or "auto",
        "effort_reason": run.get("effort_reason"),
        "result": run.get("result"), "error": run.get("error"),
        "created_at": run.get("created_at"), "completed_at": run.get("completed_at"),
        "updated_at": run.get("updated_at"),
    }
    _request("agent_runs", method="POST", body=row, params={"on_conflict": "id"}, prefer="resolution=merge-duplicates")
    for task in run.get("tasks") or []:
        task_key = str(task.get("id") or task.get("task_key") or "task")
        task_row = {
            "run_id": run["id"], "task_key": task_key,
            "title": task.get("title") or task.get("instruction") or task_key,
            "status": task.get("status") or "pending", "agent_id": task.get("agent_id"),
            "depends_on": task.get("depends_on") or [], "result": task.get("result"), "error": task.get("error"),
        }
        _request("run_tasks", method="POST", body=task_row, params={"on_conflict": "run_id,task_key"}, prefer="resolution=merge-duplicates")
    return {"status": "ok", "run_id": run["id"]}
