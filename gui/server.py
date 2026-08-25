#!/usr/bin/env python3
"""
gui/server.py — local backend for the Control Room GUI.

Serves the static gui/ files (so relative fetches like "../agent.py" from
gui/index.html keep working) AND a small JSON API under /api/* that the
static frontend cannot do on its own:

  GET  /api/health                          -> {ok:true}
  GET  /api/fs/list?path=<abs>               -> directory listing
  GET  /api/fs/read?path=<abs>               -> raw file content
  GET  /api/fs/grants                        -> allowed filesystem roots
  POST /api/fs/grant   {path}                -> grant a folder root
  POST /api/fs/write   {path, content}       -> write a file
  POST /api/fs/mkdir   {path}                -> create a directory
  POST /api/fs/touch   {path, content?}      -> create a file
  POST /api/fs/move    {path, dest}          -> rename/move a file or directory
  POST /api/fs/delete  {path, recursive?}    -> delete a file or empty directory
  POST /api/exec       {command, cwd}        -> run a shell command; response includes
                                                 the resulting `cwd` so `cd` persists
                                                 across calls in the same terminal session
  GET  /api/web/search?q=<query>             -> web search results (no API key)
  GET  /api/web/scrape?url=<url>             -> readable text of a page
  GET  /api/github/status                    -> {connected, login, ...} (never the token)
  POST /api/github/connect  {token}          -> validate + store a GitHub PAT locally
  POST /api/github/disconnect                -> forget the stored token
  GET  /api/github/repos                     -> list the connected account's repos
  GET  /api/projects                         -> persisted project list
  POST /api/projects/create                  -> create project dir + git repo
  POST /api/projects/docs/add                -> save uploaded markdown docs
  GET  /api/projects/docs/read               -> read a project markdown doc
  POST /api/projects/github/publish          -> create GitHub repo + push
  POST /api/projects/delete                  -> remove project record, optionally delete files
  POST /api/projects/settings                -> update project settings / duplicate / delete git
  POST /api/projects/git/action              -> run allowlisted git/github project actions
  GET  /api/tools/list                       -> the same TOOL_DEFINITIONS the Python agents use
  POST /api/tools/call  {name, args}         -> execute one tool, for the GUI's chat tool-loop
  GET  /api/system/stats                     -> resource monitor: per-loaded-model + system CPU/RAM
  GET  /api/supabase/status                  -> {connected, url} (never the key)
  POST /api/supabase/connect {url, service_role_key} -> validate + store Supabase creds locally
  POST /api/supabase/disconnect              -> forget stored Supabase creds
  GET  /api/chats?agent_id=<id>              -> list persisted chats
  POST /api/chats {agent_id, model_key, ...} -> create a chat
  GET  /api/chats/messages?chat_id=<id>      -> a chat's full message history
  POST /api/chats/messages {chat_id, role, content, ...} -> append a message
  POST /api/chats/touch {chat_id, title?}    -> bump updated_at / rename
  POST /api/chats/tokens {chat_id, current_tokens} -> update the running token tally
  POST /api/chats/compact {chat_id, summary_text, ...}   -> record a compaction
  GET  /api/chats/compaction?chat_id=<id>    -> the latest compaction for a chat, if any
  POST /api/memory/write {key, value, ...}   -> write + embed a memory_matrix entry
  GET  /api/memory/search?q=<query>          -> semantic (falls back to keyword) memory search
  POST /api/work-done {agent_id, action_type, file_path, ...} -> log a file CRUD action

This is a single-user local service bound to 127.0.0.1. Filesystem access is
limited to persisted user grants, secrets are blocked from agent tools, and
state-changing tool calls require an explicit approval context.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # so `tools.*` importable, matching the Python agents
from tools.policy import (
    atomic_write_json,
    granted_roots as policy_granted_roots,
    invalidate_grants,
    is_granted as policy_is_granted,
    is_secret_path,
)

MAX_READ_BYTES = 3 * 1024 * 1024  # 3MB cap so the browser doesn't choke
EXEC_TIMEOUT_S = 30
STATE_DIR = Path.home() / '.qwen-harness'
PERMISSIONS_PATH = STATE_DIR / 'permissions.json'
PROJECTS_PATH = STATE_DIR / 'projects.json'
PROJECTS_LOCK = threading.RLock()


def _total_memory_mb():
    """Physical RAM in MB. A machine constant — read once, not per request."""
    try:
        out = subprocess.run(['sysctl', '-n', 'hw.memsize'], capture_output=True, text=True, timeout=5).stdout.strip()
        return round(int(out) / (1024 * 1024), 1)
    except Exception:
        return None


TOTAL_MEMORY_MB = _total_memory_mb()

SKIP_DIR_NAMES = {'.git', 'node_modules', '__pycache__', '.DS_Store'}

# Same substring blocklist as tools/files.py's run_shell, so the browser
# terminal and the Python agent's run_shell tool refuse the same things.
# This is a basic guard, not a sandbox — see module docstring.
DANGEROUS_PATTERNS = ["sudo", "rm -rf", "mkfs", ":(){:|:&};:"]

TOOL_APPROVAL_SCOPES = {
    'write_file': 'write files',
    'edit_file': 'edit files',
    'append_file': 'append to files',
    'run_shell': 'execute a shell command',
    'click': 'control the computer',
    'double_click': 'control the computer',
    'type_text': 'control the computer',
    'press_key': 'control the computer',
    'scroll': 'control the computer',
    'browser_navigate': 'navigate the automated browser',
    'browser_click': 'interact with the automated browser',
    'browser_type': 'interact with the automated browser',
    'browser_evaluate': 'execute code in the automated browser',
    'fetch_data': 'access a network API',
    'web_search': 'search the web',
    'scrape_url': 'read a web page',
    'memory_write': 'write persistent memory',
    'memory_forget': 'delete persistent memory',
    'create_display': 'open a generated display',
}
TOOL_CWD_NAMES = {
    'read_file', 'write_file', 'append_file', 'list_dir', 'run_shell',
    'edit_file', 'search_files', 'find_files',
}

CONTENT_TYPES = {
    '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8', '.json': 'application/json',
    '.py': 'text/plain; charset=utf-8', '.sh': 'text/plain; charset=utf-8',
    '.txt': 'text/plain; charset=utf-8', '.md': 'text/plain; charset=utf-8',
    '.cjs': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
}


class Handler(BaseHTTPRequestHandler):
    server_version = "QwenHarnessGUI/1"
    # HTTP/1.1 enables keep-alive. Without it every poll paid a fresh TCP
    # handshake and a new thread: measured 2.51ms -> 0.69ms per request.
    # Every response path here sends an explicit Content-Length, which
    # HTTP/1.1 requires for connection reuse.
    protocol_version = "HTTP/1.1"

    # ── helpers ──────────────────────────────────────────────────────────
    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, status=200, content_type='text/plain; charset=utf-8'):
        body = text.encode('utf-8', errors='replace')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length) if length else b'{}'
        try:
            return json.loads(raw or b'{}')
        except json.JSONDecodeError:
            return {}

    def _resolve_path(self, raw_path):
        if not raw_path:
            return REPO_ROOT
        cleaned = unquote(str(raw_path)).strip()
        # Paths pasted from a terminal often arrive shell-quoted or with
        # escaped spaces ('/Applications/barn computing/vgv' or
        # /Applications/barn\ computing/vgv). Without this cleanup the
        # leading quote makes the path non-absolute, so it silently resolves
        # relative to the repo root and "not a directory" errors follow.
        while len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in ('"', "'"):
            cleaned = cleaned[1:-1].strip()
        cleaned = cleaned.replace('\\ ', ' ')
        if cleaned.startswith('file://'):
            cleaned = unquote(cleaned[len('file://'):]) or '/'
        if not cleaned:
            return REPO_ROOT
        return Path(cleaned).expanduser().resolve()

    def _load_grants(self):
        # Delegates to tools/policy.py so the HTTP path and the agent-tool
        # path share one implementation AND one cache. Previously each had its
        # own copy, so caching either side alone still left the other reading
        # permissions.json from disk on every check.
        # Returns list[str] — callers here mutate it and JSON-serialize it.
        return [str(p) for p in policy_granted_roots()]

    def _save_grants(self, roots):
        atomic_write_json(PERMISSIONS_PATH, {'roots': sorted(set(roots))})
        # atomic_write_json already invalidates for this path; explicit here so
        # a future revoke endpoint can't forget to.
        invalidate_grants()

    def _is_granted(self, path):
        # policy.is_granted does not resolve its argument, so the resolve
        # (with the OSError fallback) stays here.
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        return policy_is_granted(resolved)

    def _require_grant(self, path):
        if self._is_granted(path):
            return False
        self._send_json({
            'error': f'permission required for {path}',
            'permission_required': True,
            'path': str(path),
            'grants': self._load_grants(),
        }, 403)
        return True

    def _require_parent_grant(self, path):
        parent = path if path.exists() and path.is_dir() else path.parent
        return self._require_grant(parent)

    def _load_projects(self):
        try:
            if PROJECTS_PATH.exists():
                data = json.loads(PROJECTS_PATH.read_text(encoding='utf-8'))
                if isinstance(data.get('projects'), list):
                    return data['projects']
        except (OSError, json.JSONDecodeError):
            pass
        return []

    def _save_projects(self, projects):
        atomic_write_json(PROJECTS_PATH, {'projects': projects})

    def _find_project(self, project_id):
        for project in self._load_projects():
            if project.get('id') == project_id:
                return project
        return None

    def _upsert_project(self, project):
        with PROJECTS_LOCK:
            projects = self._load_projects()
            for i, existing in enumerate(projects):
                if existing.get('id') == project.get('id'):
                    projects[i] = project
                    break
            else:
                projects.append(project)
            self._save_projects(projects)

    def _slugify(self, value):
        slug = re.sub(r'[^a-zA-Z0-9._-]+', '-', value.strip().lower()).strip('-._')
        return slug or 'project'

    def _project_docs_dir(self, project):
        return Path(project['path']) / '.qwen-harness' / 'documents'

    def _safe_doc_name(self, name):
        base = Path(name).name
        if not base.lower().endswith('.md'):
            base = f'{base}.md'
        return re.sub(r'[^a-zA-Z0-9._ -]+', '-', base).strip() or 'document.md'

    def _run_git(self, args, cwd, timeout=60, extra_env=None):
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run(
            ['git', *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return {
            'ok': proc.returncode == 0,
            'code': proc.returncode,
            'stdout': proc.stdout[-20000:],
            'stderr': proc.stderr[-20000:],
        }

    def _push_with_token(self, project_dir, remote, branch):
        from tools.github import gh_token_for_git

        token = gh_token_for_git()
        push_args = ['push', '-u', 'origin', branch]
        auth = None
        if token and remote and remote.startswith('https://github.com/'):
            auth = base64.b64encode(f'x-access-token:{token}'.encode()).decode()
            push_args = ['-c', f'http.https://github.com/.extraheader=AUTHORIZATION: basic {auth}', *push_args]
        result = self._run_git(push_args, project_dir, timeout=120)
        if token:
            result['stdout'] = result.get('stdout', '').replace(token, '[redacted]')
            result['stderr'] = result.get('stderr', '').replace(token, '[redacted]')
        if auth:
            result['stdout'] = result.get('stdout', '').replace(auth, '[redacted]')
            result['stderr'] = result.get('stderr', '').replace(auth, '[redacted]')
        return result

    def _run_git_remote_with_token(self, args, project_dir, remote, timeout=120):
        from tools.github import gh_token_for_git

        token = gh_token_for_git()
        full_args = args
        auth = None
        if token and remote and remote.startswith('https://github.com/'):
            auth = base64.b64encode(f'x-access-token:{token}'.encode()).decode()
            full_args = ['-c', f'http.https://github.com/.extraheader=AUTHORIZATION: basic {auth}', *args]
        result = self._run_git(full_args, project_dir, timeout=timeout)
        if token:
            result['stdout'] = result.get('stdout', '').replace(token, '[redacted]')
            result['stderr'] = result.get('stderr', '').replace(token, '[redacted]')
        if auth:
            result['stdout'] = result.get('stdout', '').replace(auth, '[redacted]')
            result['stderr'] = result.get('stderr', '').replace(auth, '[redacted]')
        return result

    def _ensure_git_identity(self, project_dir):
        name = self._run_git(['config', 'user.name'], project_dir)
        email = self._run_git(['config', 'user.email'], project_dir)
        if not (name.get('stdout') or '').strip():
            self._run_git(['config', 'user.name', 'Qwen Harness'], project_dir)
        if not (email.get('stdout') or '').strip():
            self._run_git(['config', 'user.email', 'qwen-harness@local'], project_dir)

    # ── routing ──────────────────────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/health':
            return self._send_json({'ok': True})
        if parsed.path == '/api/fs/list':
            return self._handle_list(parse_qs(parsed.query))
        if parsed.path == '/api/fs/read':
            return self._handle_read(parse_qs(parsed.query))
        if parsed.path == '/api/fs/grants':
            return self._handle_grants()
        if parsed.path == '/api/web/search':
            return self._handle_web_search(parse_qs(parsed.query))
        if parsed.path == '/api/web/scrape':
            return self._handle_web_scrape(parse_qs(parsed.query))
        if parsed.path == '/api/github/status':
            return self._handle_github_status()
        if parsed.path == '/api/github/repos':
            return self._handle_github_repos()
        if parsed.path == '/api/projects':
            return self._handle_projects()
        if parsed.path == '/api/projects/docs/read':
            return self._handle_project_doc_read(parse_qs(parsed.query))
        if parsed.path == '/api/projects/context':
            return self._handle_project_context(parse_qs(parsed.query))
        if parsed.path == '/api/projects/search':
            return self._handle_project_search(parse_qs(parsed.query))
        if parsed.path == '/api/tools/list':
            return self._handle_tools_list()
        if parsed.path == '/api/system/stats':
            return self._handle_system_stats()
        if parsed.path == '/api/runs':
            return self._handle_runs_list(parsed)
        if parsed.path == '/api/runs/events':
            return self._handle_run_events(parsed)
        if parsed.path == '/api/runs/stream':
            return self._handle_run_stream(parsed)
        if parsed.path.startswith('/api/runs/'):
            return self._handle_run_get(parsed.path.rsplit('/', 1)[-1])
        if parsed.path == '/api/supabase/status':
            return self._handle_supabase_status()
        if parsed.path == '/api/chats':
            return self._handle_chats_list(parse_qs(parsed.query))
        if parsed.path == '/api/chats/messages':
            return self._handle_chat_messages_get(parse_qs(parsed.query))
        if parsed.path == '/api/memory/search':
            return self._handle_memory_search(parse_qs(parsed.query))
        if parsed.path == '/api/chats/compaction':
            return self._handle_chat_compaction_get(parse_qs(parsed.query))
        return self._serve_static()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/fs/write':
            return self._handle_write()
        if parsed.path == '/api/fs/grant':
            return self._handle_grant()
        if parsed.path == '/api/fs/mkdir':
            return self._handle_mkdir()
        if parsed.path == '/api/fs/touch':
            return self._handle_touch()
        if parsed.path == '/api/fs/move':
            return self._handle_move()
        if parsed.path == '/api/fs/delete':
            return self._handle_delete()
        if parsed.path == '/api/exec':
            return self._handle_exec()
        if parsed.path == '/api/github/connect':
            return self._handle_github_connect()
        if parsed.path == '/api/github/disconnect':
            return self._handle_github_disconnect()
        if parsed.path == '/api/projects/create':
            return self._handle_project_create()
        if parsed.path == '/api/projects/docs/add':
            return self._handle_project_docs_add()
        if parsed.path == '/api/projects/docs/delete':
            return self._handle_project_doc_delete()
        if parsed.path == '/api/projects/docs/rename':
            return self._handle_project_doc_rename()
        if parsed.path == '/api/projects/index':
            return self._handle_project_index()
        if parsed.path == '/api/projects/github/publish':
            return self._handle_project_github_publish()
        if parsed.path == '/api/projects/delete':
            return self._handle_project_delete()
        if parsed.path == '/api/projects/settings':
            return self._handle_project_settings()
        if parsed.path == '/api/projects/git/action':
            return self._handle_project_git_action()
        if parsed.path == '/api/tools/call':
            return self._handle_tools_call()
        if parsed.path == '/api/runs/create':
            return self._handle_run_create()
        if parsed.path == '/api/runs/event':
            return self._handle_run_event()
        if parsed.path == '/api/runs/update':
            return self._handle_run_update()
        if parsed.path == '/api/runs/cancel':
            return self._handle_run_cancel()
        if parsed.path == '/api/runs/retry':
            return self._handle_run_retry()
        if parsed.path == '/api/supabase/connect':
            return self._handle_supabase_connect()
        if parsed.path == '/api/supabase/disconnect':
            return self._handle_supabase_disconnect()
        if parsed.path == '/api/chats':
            return self._handle_chats_create()
        if parsed.path == '/api/chats/messages':
            return self._handle_chat_messages_add()
        if parsed.path == '/api/chats/touch':
            return self._handle_chat_touch()
        if parsed.path == '/api/chats/tokens':
            return self._handle_chat_tokens()
        if parsed.path == '/api/chats/compact':
            return self._handle_chat_compact()
        if parsed.path == '/api/memory/write':
            return self._handle_memory_write()
        if parsed.path == '/api/work-done':
            return self._handle_work_done()
        self._send_json({'error': 'not found'}, 404)

    # ── static files (delegates to a plain file server rooted at repo) ────
    def _serve_static(self):
        parsed = urlparse(self.path)
        rel = unquote(parsed.path).lstrip('/')
        if rel == '':
            rel = 'gui/index.html'
        target = (REPO_ROOT / rel).resolve()
        try:
            target.relative_to(REPO_ROOT)
        except ValueError:
            return self._send_json({'error': 'forbidden'}, 403)
        if target.is_dir():
            target = target / 'index.html'
        if not target.exists():
            return self._send_json({'error': 'not found', 'path': str(target)}, 404)
        content_type = CONTENT_TYPES.get(target.suffix, 'application/octet-stream')
        try:
            data = target.read_bytes()
        except OSError as exc:
            return self._send_json({'error': str(exc)}, 500)
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ── filesystem API ──────────────────────────────────────────────────
    def _handle_list(self, query):
        raw = query.get('path', [''])[0]
        show_secrets = query.get('show_secrets', ['0'])[0] == '1'
        path = self._resolve_path(raw)
        denied = self._require_grant(path)
        if denied:
            return denied
        if not path.exists() or not path.is_dir():
            return self._send_json({'error': f'not a directory: {path}'}, 400)
        entries = []
        try:
            for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                if child.name in SKIP_DIR_NAMES:
                    continue
                if not show_secrets and is_secret_path(child):
                    continue
                try:
                    is_dir = child.is_dir()
                    size = None if is_dir else child.stat().st_size
                except OSError:
                    continue
                entries.append({
                    'name': child.name,
                    'path': str(child),
                    'type': 'dir' if is_dir else 'file',
                    'size': size,
                })
        except PermissionError:
            return self._send_json({'error': 'permission denied'}, 403)
        parent = str(path.parent) if path != path.parent else None
        self._send_json({'path': str(path), 'parent': parent, 'entries': entries})

    def _handle_read(self, query):
        raw = query.get('path', [''])[0]
        path = self._resolve_path(raw)
        denied = self._require_grant(path)
        if denied:
            return denied
        if not path.exists() or not path.is_file():
            return self._send_json({'error': f'not a file: {path}'}, 400)
        try:
            size = path.stat().st_size
            if size > MAX_READ_BYTES:
                return self._send_json({'error': f'file too large ({size} bytes)'}, 413)
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError as exc:
            return self._send_json({'error': str(exc)}, 500)
        self._send_text(text)

    def _handle_grants(self):
        self._send_json({'roots': self._load_grants(), 'default': str(REPO_ROOT)})

    def _handle_grant(self):
        body = self._read_json_body()
        raw = body.get('path')
        if not raw:
            return self._send_json({'error': 'missing path'}, 400)
        path = self._resolve_path(raw)
        if path.is_file():
            path = path.parent
        if not path.exists() or not path.is_dir():
            return self._send_json({'error': f'not a directory: {path}'}, 400)
        roots = self._load_grants()
        roots.append(str(path))
        try:
            self._save_grants(roots)
        except OSError as exc:
            return self._send_json({'error': str(exc)}, 500)
        self._send_json({'ok': True, 'roots': self._load_grants(), 'path': str(path)})

    def _handle_write(self):
        body = self._read_json_body()
        raw = body.get('path')
        content = body.get('content', '')
        if not raw:
            return self._send_json({'error': 'missing path'}, 400)
        path = self._resolve_path(raw)
        denied = self._require_parent_grant(path)
        if denied:
            return denied
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        except OSError as exc:
            return self._send_json({'error': str(exc)}, 500)
        self._send_json({'ok': True, 'path': str(path), 'bytes': len(content.encode('utf-8'))})

    def _handle_mkdir(self):
        body = self._read_json_body()
        raw = body.get('path')
        if not raw:
            return self._send_json({'error': 'missing path'}, 400)
        path = self._resolve_path(raw)
        denied = self._require_parent_grant(path)
        if denied:
            return denied
        try:
            path.mkdir(parents=bool(body.get('parents', True)), exist_ok=False)
        except FileExistsError:
            return self._send_json({'error': f'already exists: {path}'}, 409)
        except OSError as exc:
            return self._send_json({'error': str(exc)}, 500)
        self._send_json({'ok': True, 'path': str(path), 'type': 'dir'})

    def _handle_touch(self):
        body = self._read_json_body()
        raw = body.get('path')
        content = body.get('content', '')
        if not raw:
            return self._send_json({'error': 'missing path'}, 400)
        path = self._resolve_path(raw)
        denied = self._require_parent_grant(path)
        if denied:
            return denied
        if path.exists():
            return self._send_json({'error': f'already exists: {path}'}, 409)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        except OSError as exc:
            return self._send_json({'error': str(exc)}, 500)
        self._send_json({'ok': True, 'path': str(path), 'type': 'file'})

    def _handle_move(self):
        body = self._read_json_body()
        raw = body.get('path')
        dest_raw = body.get('dest')
        if not raw or not dest_raw:
            return self._send_json({'error': 'missing path or dest'}, 400)
        path = self._resolve_path(raw)
        dest = self._resolve_path(dest_raw)
        denied = self._require_grant(path) or self._require_parent_grant(dest)
        if denied:
            return denied
        if not path.exists():
            return self._send_json({'error': f'not found: {path}'}, 400)
        if dest.exists():
            return self._send_json({'error': f'already exists: {dest}'}, 409)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            path.rename(dest)
        except OSError as exc:
            return self._send_json({'error': str(exc)}, 500)
        self._send_json({'ok': True, 'path': str(dest), 'from': str(path)})

    def _handle_delete(self):
        body = self._read_json_body()
        raw = body.get('path')
        if not raw:
            return self._send_json({'error': 'missing path'}, 400)
        path = self._resolve_path(raw)
        denied = self._require_grant(path)
        if denied:
            return denied
        if path == REPO_ROOT:
            return self._send_json({'error': 'refusing to delete repo root'}, 400)
        if not path.exists():
            return self._send_json({'error': f'not found: {path}'}, 400)
        try:
            if path.is_dir():
                if body.get('recursive'):
                    shutil.rmtree(path)
                else:
                    path.rmdir()
            else:
                path.unlink()
        except OSError as exc:
            return self._send_json({'error': str(exc)}, 500)
        self._send_json({'ok': True, 'path': str(path)})

    # ── terminal / sandbox exec ─────────────────────────────────────────
    def _handle_exec(self):
        body = self._read_json_body()
        command = body.get('command', '')
        cwd = body.get('cwd') or str(REPO_ROOT)
        if not command:
            return self._send_json({'error': 'missing command'}, 400)
        for pattern in DANGEROUS_PATTERNS:
            if pattern in command:
                return self._send_json({'ok': False, 'error': f"blocked dangerous command pattern: '{pattern}'"}, 400)
        cwd_path = self._resolve_path(cwd)
        denied = self._require_grant(cwd_path)
        if denied:
            return denied
        # `cd` only changes the directory of the subprocess it runs in, which
        # exits immediately after — so a plain per-command subprocess.run()
        # can never make `cd` persist to the *next* command. Fix: run the
        # command and a cwd-reporting `pwd` in the SAME shell process (one
        # script, joined by a newline), then split the real output from the
        # reported cwd on a marker line, and hand that cwd back to the
        # frontend to use as `cwd` on the next call.
        cwd_marker = '@@QH_CWD@@'
        script = f'{command}\nprintf \'\\n{cwd_marker}%s\' "$PWD"'
        try:
            proc = subprocess.run(
                script, shell=True, cwd=str(cwd_path),
                capture_output=True, text=True, timeout=EXEC_TIMEOUT_S,
            )
            stdout = proc.stdout
            new_cwd = str(cwd_path)
            if cwd_marker in stdout:
                stdout, _, marker_tail = stdout.rpartition(cwd_marker)
                new_cwd = marker_tail.strip() or new_cwd
            self._send_json({
                'ok': True, 'code': proc.returncode, 'cwd': new_cwd,
                'stdout': stdout.rstrip('\n')[-20000:], 'stderr': proc.stderr[-20000:],
            })
        except subprocess.TimeoutExpired:
            self._send_json({'ok': False, 'error': f'timed out after {EXEC_TIMEOUT_S}s'}, 408)
        except OSError as exc:
            self._send_json({'ok': False, 'error': str(exc)}, 500)

    # ── web search / scrape ───────────────────────────────────────────────
    def _handle_web_search(self, query):
        from tools.web import web_search
        q = query.get('q', [''])[0]
        if not q:
            return self._send_json({'error': 'missing q'}, 400)
        max_results = int(query.get('max_results', ['5'])[0])
        self._send_json(web_search(q, max_results=max_results))

    def _handle_web_scrape(self, query):
        from tools.web import scrape_url
        url = query.get('url', [''])[0]
        if not url:
            return self._send_json({'error': 'missing url'}, 400)
        self._send_json(scrape_url(url))

    # ── GitHub connector ─────────────────────────────────────────────────
    def _handle_github_status(self):
        from tools.github import gh_status
        self._send_json(gh_status())

    def _handle_github_connect(self):
        from tools.github import gh_connect
        body = self._read_json_body()
        token = body.get('token', '').strip()
        if not token:
            return self._send_json({'error': 'missing token'}, 400)
        result = gh_connect(token)
        self._send_json(result, 400 if 'error' in result else 200)

    def _handle_github_disconnect(self):
        from tools.github import gh_disconnect
        self._send_json(gh_disconnect())

    def _handle_github_repos(self):
        from tools.github import gh_list_repos
        result = gh_list_repos()
        self._send_json(result, 400 if 'error' in result else 200)

    # ── Tool calling (bridges the GUI's chat loop to tools/__init__.py, ──
    # ── the same registry the Python agents use — single source of truth) ──
    def _handle_tools_list(self):
        from tools import TOOL_DEFINITIONS
        self._send_json({'tools': TOOL_DEFINITIONS})

    def _handle_tools_call(self):
        from tools import execute_tool
        body = self._read_json_body()
        name = body.get('name')
        args = dict(body.get('args') or {})
        context = body.get('context') or {}
        if not name:
            return self._send_json({'error': 'missing tool name'}, 400)

        approval_scope = TOOL_APPROVAL_SCOPES.get(name)
        if approval_scope and context.get('approved') is not True:
            return self._send_json({
                'error': f'approval required to {approval_scope}',
                'approval_required': True,
                'scope': approval_scope,
                'tool': name,
                'args': args,
            }, 409)

        if name in TOOL_CWD_NAMES:
            cwd = self._resolve_path(context.get('cwd') or REPO_ROOT)
            if self._require_grant(cwd):
                return
            args['cwd'] = str(cwd)

        if name in {'read_file', 'write_file', 'append_file', 'edit_file'} and args.get('path'):
            raw = Path(str(args['path'])).expanduser()
            target = (Path(args.get('cwd') or REPO_ROOT) / raw).resolve() if not raw.is_absolute() else raw.resolve()
            if is_secret_path(target) and context.get('allow_secret') is not True:
                return self._send_json({
                    'error': f'secret-file access blocked for {target}',
                    'permission_required': True,
                    'secret': True,
                    'path': str(target),
                }, 403)

        result = execute_tool(name, args)
        status = 403 if isinstance(result, dict) and result.get('permission_required') else 200
        self._send_json({'result': result}, status)

    # ── Durable runs and live events ────────────────────────────────────
    def _handle_runs_list(self, parsed):
        from gui.run_store import RUN_STORE
        query = parse_qs(parsed.query)
        project_id = query.get('project_id', [None])[0]
        try:
            limit = int(query.get('limit', ['50'])[0])
        except ValueError:
            limit = 50
        include_events = query.get('include_events', ['0'])[0] not in ('0', '', 'false')
        runs = RUN_STORE.list(project_id=project_id, limit=limit, include_events=include_events)
        self._send_json({'runs': runs})

    def _handle_run_get(self, run_id):
        from gui.run_store import RUN_STORE
        run = RUN_STORE.get(run_id)
        self._send_json({'run': run} if run else {'error': 'run not found'}, 200 if run else 404)

    def _sync_run_background(self, run):
        if not run:
            return
        project = self._find_project(run.get('project_id')) if run.get('project_id') else None
        def sync():
            try:
                from tools.supabase_client import sb_status, sb_sync_run
                if sb_status().get('connected'):
                    sb_sync_run(run, project)
            except Exception as exc:
                sys.stderr.write(f'[gui-server] Supabase run sync skipped: {exc}\n')
        threading.Thread(target=sync, name=f'run-sync-{run.get("id", "unknown")[:8]}', daemon=True).start()

    def _handle_run_create(self):
        from gui.run_store import RUN_STORE
        body = self._read_json_body()
        if not str(body.get('instruction') or '').strip():
            return self._send_json({'error': 'instruction is required'}, 400)
        cwd = self._resolve_path(body.get('cwd') or REPO_ROOT)
        if self._require_grant(cwd):
            return
        body['cwd'] = str(cwd)
        run = RUN_STORE.create(body)
        self._sync_run_background(run)
        self._send_json({'run': run}, 201)

    def _handle_run_event(self):
        from gui.run_store import RUN_STORE
        body = self._read_json_body()
        event = RUN_STORE.append_event(body.get('run_id'), body.get('type') or 'run.event', body.get('data') or {})
        self._send_json({'event': event} if event else {'error': 'run not found'}, 200 if event else 404)

    def _handle_run_update(self):
        from gui.run_store import RUN_STORE
        body = self._read_json_body()
        run = RUN_STORE.update(body.get('run_id'), body)
        self._sync_run_background(run)
        self._send_json({'run': run} if run else {'error': 'run not found'}, 200 if run else 404)

    def _handle_run_cancel(self):
        from gui.run_store import RUN_STORE
        body = self._read_json_body()
        run = RUN_STORE.cancel(body.get('run_id'))
        self._sync_run_background(run)
        self._send_json({'run': run} if run else {'error': 'run not found'}, 200 if run else 404)

    def _handle_run_retry(self):
        from gui.run_store import RUN_STORE
        body = self._read_json_body()
        run = RUN_STORE.retry(body.get('run_id'))
        self._sync_run_background(run)
        self._send_json({'run': run} if run else {'error': 'run not found'}, 201 if run else 404)

    def _handle_run_events(self, parsed):
        from gui.run_store import RUN_STORE
        query = parse_qs(parsed.query)
        run_id = query.get('run_id', [''])[0]
        try:
            after = int(query.get('after', ['0'])[0])
        except ValueError:
            after = 0
        if not RUN_STORE.get(run_id):
            return self._send_json({'error': 'run not found'}, 404)
        self._send_json({'events': RUN_STORE.events_after(run_id, after)})

    def _handle_run_stream(self, parsed):
        from gui.run_store import RUN_STORE
        query = parse_qs(parsed.query)
        run_id = query.get('run_id', [''])[0]
        if not RUN_STORE.get(run_id):
            return self._send_json({'error': 'run not found'}, 404)
        try:
            seq = int(self.headers.get('Last-Event-ID') or query.get('after', ['0'])[0])
        except ValueError:
            seq = 0
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        # This response has no Content-Length and isn't chunked, so its end is
        # delimited by the connection closing. Under HTTP/1.1 (keep-alive by
        # default) the client would otherwise hang waiting for more data.
        self.send_header('Connection', 'close')
        self.close_connection = True
        self.end_headers()
        try:
            for _ in range(8):
                events = RUN_STORE.wait_for_events(run_id, seq, timeout=3.0)
                if not events:
                    self.wfile.write(b': keepalive\n\n')
                    self.wfile.flush()
                    continue
                for event in events:
                    seq = int(event['seq'])
                    payload = json.dumps(event, separators=(',', ':')).encode('utf-8')
                    self.wfile.write(f'id: {seq}\nevent: run-event\ndata: '.encode('utf-8') + payload + b'\n\n')
                    self.wfile.flush()
                run = RUN_STORE.get(run_id)
                if run and run.get('status') in {'completed', 'failed', 'cancelled', 'interrupted'}:
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ── Resource monitor ─────────────────────────────────────────────────
    # "Tasks" = currently-loaded Ollama models (GET /api/ps, Ollama's native
    # endpoint — not the OpenAI-compat one). Each loaded model runs its own
    # llama-server subprocess, so we pair loaded models with those processes
    # for per-task CPU/RAM. With OLLAMA_NUM_PARALLEL=1 (this project's
    # default) there's normally at most one llama-server per loaded model,
    # so list-order pairing is reliable; it's a best-effort match otherwise.
    def _handle_system_stats(self):
        import urllib.request

        loaded = []
        try:
            req = urllib.request.Request('http://localhost:11434/api/ps')
            with urllib.request.urlopen(req, timeout=5) as resp:
                loaded = json.loads(resp.read()).get('models', [])
        except Exception:
            pass

        # One `ps` sweep (~0.05s) yields BOTH the per-llama-server rows and the
        # machine-wide CPU total. This replaces the old `top -l 1` call, which
        # measured 25.8s on an 8GB Mac under load — polled every 3s, it starved
        # the very inference it was monitoring.
        procs = []
        total_cpu = None
        try:
            out = subprocess.run(
                ['ps', '-axo', 'pid,pcpu,rss,comm'],
                capture_output=True, text=True, timeout=5,
            ).stdout
            cpu_sum = 0.0
            for line in out.splitlines()[1:]:
                parts = line.split(None, 3)
                if len(parts) < 4:
                    continue
                pid, pcpu, rss, comm = parts
                try:
                    cpu_sum += float(pcpu)
                except ValueError:
                    continue
                if 'llama-server' in comm:
                    procs.append({'pid': int(pid), 'cpu_pct': float(pcpu), 'mem_mb': round(int(rss) / 1024, 1)})
            # ps %cpu is a per-process lifetime average, not an instantaneous
            # sample, so this lags real utilization — but it is the same metric
            # already shown per-task, so the numbers stay internally consistent.
            cores = os.cpu_count() or 1
            total_cpu = round(min(100.0, cpu_sum / cores), 1)
        except Exception:
            pass

        tasks = []
        for i, model in enumerate(loaded):
            proc = procs[i] if i < len(procs) else None
            tasks.append({
                'model': model.get('name'),
                'size_mb': round(model.get('size', 0) / (1024 * 1024), 1),
                'size_vram_mb': round(model.get('size_vram', 0) / (1024 * 1024), 1),
                'expires_at': model.get('expires_at'),
                'cpu_pct': proc['cpu_pct'] if proc else None,
                'mem_mb': proc['mem_mb'] if proc else None,
            })

        # Load average is the honest saturation signal (ps %cpu is a lifetime
        # average and understates bursts), so ship it alongside rather than
        # instead of cpu_pct — 0.01s, and it's what exposed the pile-up.
        load_avg = None
        try:
            raw = subprocess.run(['sysctl', '-n', 'vm.loadavg'], capture_output=True, text=True, timeout=2).stdout
            load_avg = round(float(raw.strip().strip('{}').split()[0]), 2)
        except Exception:
            pass

        system = {
            'cpu_pct': total_cpu,
            'mem_used_mb': None,
            'mem_total_mb': TOTAL_MEMORY_MB,
            'load_avg': load_avg,
            'cpu_count': os.cpu_count(),
        }
        try:
            vm = subprocess.run(['vm_stat'], capture_output=True, text=True, timeout=5).stdout
            page_size_m = re.search(r'page size of (\d+) bytes', vm)
            page_size = int(page_size_m.group(1)) if page_size_m else 4096
            used_pages = 0
            for label in ('Pages active', 'Pages wired down', 'Pages occupied by compressor'):
                m = re.search(rf'{label}:\s+(\d+)', vm)
                if m:
                    used_pages += int(m.group(1))
            system['mem_used_mb'] = round(used_pages * page_size / (1024 * 1024), 1)
        except Exception:
            pass

        self._send_json({'system': system, 'tasks': tasks})

    # ── Supabase: persisted chats, memory matrix, work-done log ─────────
    # Thin wrappers around tools/supabase_client.py — same connect-once,
    # secrets-stay-local pattern as GitHub. Every handler here degrades to
    # a clean {"error": ...} when not connected, rather than crashing —
    # the frontend treats that as "persistence unavailable, stay local-only".
    def _handle_supabase_status(self):
        from tools.supabase_client import sb_status
        self._send_json(sb_status())

    def _handle_supabase_connect(self):
        from tools.supabase_client import sb_connect
        body = self._read_json_body()
        url, key = body.get('url', '').strip(), body.get('service_role_key', '').strip()
        if not url or not key:
            return self._send_json({'error': 'missing url or service_role_key'}, 400)
        result = sb_connect(url, key)
        self._send_json(result, 400 if 'error' in result else 200)

    def _handle_supabase_disconnect(self):
        from tools.supabase_client import sb_disconnect
        self._send_json(sb_disconnect())

    def _handle_chats_list(self, query):
        from tools.supabase_client import sb_list_chats
        agent_id = query.get('agent_id', [None])[0]
        try:
            result = sb_list_chats(agent_id=agent_id)
            self._send_json(result)
        except Exception as e:
            self._send_json({'error': str(e)}, 400)

    def _handle_chats_create(self):
        from tools.supabase_client import sb_create_chat
        body = self._read_json_body()
        try:
            result = sb_create_chat(
                body.get('agent_id'), body.get('agent_name'),
                body.get('model_key'), body.get('context_limit'), body.get('title'),
            )
            self._send_json(result, 400 if 'error' in result else 200)
        except Exception as e:
            self._send_json({'error': str(e)}, 400)

    def _handle_chat_messages_get(self, query):
        from tools.supabase_client import sb_get_messages
        chat_id = query.get('chat_id', [None])[0]
        if not chat_id:
            return self._send_json({'error': 'missing chat_id'}, 400)
        try:
            self._send_json(sb_get_messages(chat_id))
        except Exception as e:
            self._send_json({'error': str(e)}, 400)

    def _handle_chat_messages_add(self):
        from tools.supabase_client import sb_add_message
        body = self._read_json_body()
        chat_id = body.get('chat_id')
        if not chat_id:
            return self._send_json({'error': 'missing chat_id'}, 400)
        try:
            result = sb_add_message(
                chat_id, body.get('role'), body.get('content'),
                body.get('tool_calls'), body.get('tool_call_id'),
                body.get('tool_name'), body.get('token_count'),
            )
            self._send_json(result, 400 if 'error' in result else 200)
        except Exception as e:
            self._send_json({'error': str(e)}, 400)

    def _handle_chat_touch(self):
        from tools.supabase_client import sb_touch_chat
        body = self._read_json_body()
        chat_id = body.get('chat_id')
        if not chat_id:
            return self._send_json({'error': 'missing chat_id'}, 400)
        try:
            self._send_json(sb_touch_chat(chat_id, body.get('title')))
        except Exception as e:
            self._send_json({'error': str(e)}, 400)

    def _handle_chat_tokens(self):
        from tools.supabase_client import sb_update_chat_tokens
        body = self._read_json_body()
        chat_id = body.get('chat_id')
        if not chat_id:
            return self._send_json({'error': 'missing chat_id'}, 400)
        try:
            self._send_json(sb_update_chat_tokens(chat_id, body.get('current_tokens', 0)))
        except Exception as e:
            self._send_json({'error': str(e)}, 400)

    def _handle_chat_compact(self):
        from tools.supabase_client import sb_record_compaction
        body = self._read_json_body()
        try:
            result = sb_record_compaction(
                body.get('chat_id'), body.get('summary_text'), body.get('covers_up_to_message_id'),
                body.get('token_count_before', 0), body.get('token_count_after', 0),
            )
            self._send_json(result, 400 if 'error' in result else 200)
        except Exception as e:
            self._send_json({'error': str(e)}, 400)

    def _handle_chat_compaction_get(self, query):
        from tools.supabase_client import sb_get_latest_compaction
        chat_id = query.get('chat_id', [None])[0]
        if not chat_id:
            return self._send_json({'error': 'missing chat_id'}, 400)
        try:
            self._send_json(sb_get_latest_compaction(chat_id))
        except Exception as e:
            self._send_json({'error': str(e)}, 400)

    def _handle_memory_write(self):
        from tools.supabase_client import sb_memory_write
        body = self._read_json_body()
        key = body.get('key')
        if not key:
            return self._send_json({'error': 'missing key'}, 400)
        try:
            result = sb_memory_write(key, body.get('value', ''), body.get('category', 'fact'), body.get('chat_id'))
            self._send_json(result, 400 if 'error' in result else 200)
        except Exception as e:
            self._send_json({'error': str(e)}, 400)

    def _handle_memory_search(self, query):
        from tools.supabase_client import sb_memory_search
        q = query.get('q', [''])[0]
        if not q:
            return self._send_json({'error': 'missing q'}, 400)
        semantic = query.get('semantic', ['true'])[0] != 'false'
        try:
            self._send_json(sb_memory_search(q, semantic=semantic))
        except Exception as e:
            self._send_json({'error': str(e)}, 400)

    def _handle_work_done(self):
        from tools.supabase_client import sb_log_work_done
        body = self._read_json_body()
        agent_id, action_type, file_path = body.get('agent_id'), body.get('action_type'), body.get('file_path')
        if not (agent_id and action_type and file_path):
            return self._send_json({'error': 'missing agent_id, action_type, or file_path'}, 400)
        try:
            result = sb_log_work_done(agent_id, action_type, file_path, body.get('chat_id'), body.get('summary'))
            self._send_json(result, 400 if 'error' in result else 200)
        except Exception as e:
            self._send_json({'error': str(e)}, 400)

    # ── Projects ────────────────────────────────────────────────────────
    def _handle_projects(self):
        projects = self._load_projects()
        for project in projects:
            project['exists'] = Path(project.get('path', '')).exists()
            project['git_exists'] = (Path(project.get('path', '')) / '.git').exists()
        self._send_json({'projects': projects})

    def _handle_project_create(self):
        body = self._read_json_body()
        name = (body.get('name') or '').strip()
        parent_raw = body.get('parent_dir')
        init_git_approved = body.get('init_git_approved') is True
        if not name:
            return self._send_json({'error': 'missing project name'}, 400)
        if not parent_raw:
            return self._send_json({'error': 'missing parent_dir'}, 400)
        if not init_git_approved:
            return self._send_json({'error': 'creating .git requires explicit approval', 'approval_required': True}, 400)

        parent = self._resolve_path(parent_raw)
        denied = self._require_grant(parent)
        if denied:
            return denied
        if not parent.exists() or not parent.is_dir():
            return self._send_json({'error': f'not a directory: {parent}'}, 400)

        slug = self._slugify(name)
        project_dir = (parent / slug).resolve()
        denied = self._require_parent_grant(project_dir)
        if denied:
            return denied
        if project_dir.exists() and any(project_dir.iterdir()):
            return self._send_json({'error': f'project directory is not empty: {project_dir}'}, 409)

        try:
            project_dir.mkdir(parents=True, exist_ok=True)
            # -b main: without init.defaultBranch configured, git inits
            # "master", which then mismatches GitHub's "main" default at
            # publish time. Fall back for git < 2.28 (no -b support).
            git_result = self._run_git(['init', '-b', 'main'], project_dir)
            if not git_result['ok']:
                git_result = self._run_git(['init'], project_dir)
                if git_result['ok']:
                    self._run_git(['symbolic-ref', 'HEAD', 'refs/heads/main'], project_dir)
            if not git_result['ok']:
                return self._send_json({'error': 'git init failed', 'git': git_result}, 500)
            readme = project_dir / 'README.md'
            if not readme.exists():
                readme.write_text(f'# {name}\n\nProject workspace managed by qwen-harness.\n', encoding='utf-8')
            docs_dir = project_dir / '.qwen-harness' / 'documents'
            docs_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return self._send_json({'error': str(exc)}, 500)

        roots = self._load_grants()
        roots.append(str(project_dir))
        self._save_grants(roots)

        project = {
            'id': str(uuid.uuid4()),
            'name': name,
            'slug': slug,
            'path': str(project_dir),
            'repo': {'type': 'git', 'initialized': True, 'remote': None, 'github': None},
            'documents': [],
            'settings': {'agent_ids': []},
        }
        self._upsert_project(project)
        self._send_json({'ok': True, 'project': project})

    def _handle_project_docs_add(self):
        body = self._read_json_body()
        project = self._find_project(body.get('project_id'))
        if not project:
            return self._send_json({'error': 'project not found'}, 404)
        project_dir = Path(project['path']).resolve()
        denied = self._require_grant(project_dir)
        if denied:
            return denied

        docs = body.get('documents')
        if not isinstance(docs, list) or not docs:
            return self._send_json({'error': 'missing documents'}, 400)

        docs_dir = self._project_docs_dir(project)
        docs_dir.mkdir(parents=True, exist_ok=True)
        saved = []
        existing = {doc.get('name'): doc for doc in project.get('documents', [])}
        for doc in docs:
            name = self._safe_doc_name(doc.get('name') or 'document.md')
            if not name.lower().endswith('.md'):
                return self._send_json({'error': f'only .md files are supported: {name}'}, 400)
            path = (docs_dir / name).resolve()
            try:
                path.relative_to(docs_dir.resolve())
            except ValueError:
                return self._send_json({'error': f'invalid document name: {name}'}, 400)
            content = str(doc.get('content') or '')
            path.write_text(content, encoding='utf-8')
            meta = {
                'name': name,
                'path': str(path),
                'bytes': len(content.encode('utf-8')),
            }
            existing[name] = meta
            saved.append(meta)
        project['documents'] = sorted(existing.values(), key=lambda d: d['name'].lower())
        self._upsert_project(project)
        self._send_json({'ok': True, 'project': project, 'saved': saved})

    def _handle_project_doc_read(self, query):
        project = self._find_project(query.get('project_id', [''])[0])
        name = query.get('name', [''])[0]
        if not project:
            return self._send_json({'error': 'project not found'}, 404)
        if not name:
            return self._send_json({'error': 'missing name'}, 400)
        docs_dir = self._project_docs_dir(project).resolve()
        path = (docs_dir / self._safe_doc_name(name)).resolve()
        try:
            path.relative_to(docs_dir)
        except ValueError:
            return self._send_json({'error': 'invalid document path'}, 400)
        denied = self._require_grant(Path(project['path']).resolve())
        if denied:
            return denied
        if not path.exists() or not path.is_file():
            return self._send_json({'error': f'document not found: {name}'}, 404)
        self._send_text(path.read_text(encoding='utf-8', errors='replace'))

    def _handle_project_doc_delete(self):
        body = self._read_json_body()
        project = self._find_project(body.get('project_id'))
        if not project:
            return self._send_json({'error': 'project not found'}, 404)
        if body.get('approved') is not True:
            return self._send_json({'error': 'deleting a context document requires approval', 'approval_required': True}, 409)
        name = self._safe_doc_name(body.get('name') or '')
        docs_dir = self._project_docs_dir(project).resolve()
        path = (docs_dir / name).resolve()
        try:
            path.relative_to(docs_dir)
        except ValueError:
            return self._send_json({'error': 'invalid document path'}, 400)
        if self._require_grant(Path(project['path']).resolve()):
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            return self._send_json({'error': str(error)}, 400)
        project['documents'] = [doc for doc in project.get('documents', []) if doc.get('name') != name]
        self._upsert_project(project)
        self._send_json({'ok': True, 'project': project})

    def _handle_project_doc_rename(self):
        body = self._read_json_body()
        project = self._find_project(body.get('project_id'))
        if not project:
            return self._send_json({'error': 'project not found'}, 404)
        old_name = self._safe_doc_name(body.get('name') or '')
        new_name = self._safe_doc_name(body.get('new_name') or '')
        docs_dir = self._project_docs_dir(project).resolve()
        source, target = (docs_dir / old_name).resolve(), (docs_dir / new_name).resolve()
        if self._require_grant(Path(project['path']).resolve()):
            return
        if not source.exists():
            return self._send_json({'error': 'document not found'}, 404)
        if target.exists() and target != source:
            return self._send_json({'error': f'document already exists: {new_name}'}, 409)
        source.rename(target)
        for doc in project.get('documents', []):
            if doc.get('name') == old_name:
                doc.update({'name': new_name, 'path': str(target)})
        project['documents'] = sorted(project.get('documents', []), key=lambda doc: doc['name'].lower())
        self._upsert_project(project)
        self._send_json({'ok': True, 'project': project, 'name': new_name})

    def _handle_project_index(self):
        body = self._read_json_body()
        project = self._find_project(body.get('project_id'))
        if not project:
            return self._send_json({'error': 'project not found'}, 404)
        root = Path(project['path']).resolve()
        if self._require_grant(root):
            return
        allowed_suffixes = {'.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.swift', '.md', '.json', '.toml', '.yaml', '.yml', '.sh', '.css', '.html', '.sql'}
        symbol_pattern = re.compile(r'^\s*(?:def|class|function|func|struct|enum|interface|type|const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)', re.MULTILINE)
        files, symbols, total_bytes = [], [], 0
        for path in root.rglob('*'):
            if len(files) >= 4000:
                break
            if not path.is_file() or path.suffix.lower() not in allowed_suffixes:
                continue
            if any(part in SKIP_DIR_NAMES or part in {'.build', 'dist', '.qwen-harness'} for part in path.relative_to(root).parts):
                continue
            if is_secret_path(path):
                continue
            try:
                stat = path.stat()
                if stat.st_size > 1024 * 1024:
                    continue
                text = path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            relative = str(path.relative_to(root))
            found = symbol_pattern.findall(text)[:80]
            files.append({'path': relative, 'bytes': stat.st_size, 'mtime': stat.st_mtime, 'symbols': found})
            symbols.extend({'name': name, 'path': relative} for name in found)
            total_bytes += stat.st_size
        index = {'version': 1, 'created_at': time.time(), 'root': str(root), 'file_count': len(files), 'symbol_count': len(symbols), 'total_bytes': total_bytes, 'files': files, 'symbols': symbols[:12000]}
        index_path = root / '.qwen-harness' / 'index.json'
        atomic_write_json(index_path, index)
        project['index'] = {key: index[key] for key in ('created_at', 'file_count', 'symbol_count', 'total_bytes')}
        self._upsert_project(project)
        self._send_json({'ok': True, 'project': project, 'index': project['index']})

    def _load_project_index(self, project):
        path = Path(project['path']) / '.qwen-harness' / 'index.json'
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None

    def _handle_project_context(self, query):
        project = self._find_project(query.get('project_id', [''])[0])
        if not project:
            return self._send_json({'error': 'project not found'}, 404)
        index = self._load_project_index(project)
        self._send_json({'project_id': project['id'], 'documents': project.get('documents', []), 'index': ({key: index.get(key) for key in ('created_at', 'file_count', 'symbol_count', 'total_bytes')} if index else None), 'settings': project.get('settings', {})})

    def _handle_project_search(self, query):
        project = self._find_project(query.get('project_id', [''])[0])
        term = query.get('q', [''])[0].strip().lower()
        if not project:
            return self._send_json({'error': 'project not found'}, 404)
        if not term:
            return self._send_json({'error': 'missing q'}, 400)
        index = self._load_project_index(project)
        if not index:
            return self._send_json({'error': 'project has not been indexed'}, 409)
        results = []
        for file in index.get('files', []):
            haystack = f"{file.get('path', '')} {' '.join(file.get('symbols', []))}".lower()
            if term in haystack:
                results.append({'path': file.get('path'), 'symbols': [name for name in file.get('symbols', []) if term in name.lower()]})
            if len(results) >= 50:
                break
        self._send_json({'query': term, 'results': results})

    def _handle_project_github_publish(self):
        from tools.github import gh_create_repo, gh_token_for_git

        body = self._read_json_body()
        project = self._find_project(body.get('project_id'))
        if not project:
            return self._send_json({'error': 'project not found'}, 404)
        if body.get('publish_approved') is not True:
            return self._send_json({'error': 'publishing to GitHub requires explicit approval', 'approval_required': True}, 400)

        project_dir = Path(project['path']).resolve()
        denied = self._require_grant(project_dir)
        if denied:
            return denied

        repo_name = self._slugify(body.get('repo_name') or project.get('slug') or project.get('name'))
        created = gh_create_repo(
            repo_name,
            private=body.get('private', True) is not False,
            description=body.get('description') or f'{project["name"]} project from qwen-harness',
            auto_init=body.get('add_readme') is True,
        )
        if 'error' in created:
            return self._send_json(created, 400)

        remote = created.get('clone_url') or created.get('ssh_url')
        if not remote:
            return self._send_json({'error': 'GitHub did not return a remote URL', 'github': created}, 500)

        current_branch = self._run_git(['branch', '--show-current'], project_dir)
        branch = (current_branch.get('stdout') or '').strip() or 'main'
        if not current_branch['ok'] or not branch:
            self._run_git(['checkout', '-B', 'main'], project_dir)
            branch = 'main'

        self._run_git(['add', '.'], project_dir)
        self._ensure_git_identity(project_dir)
        commit = self._run_git(['commit', '-m', 'Initial project scaffold'], project_dir)
        if not commit['ok'] and 'nothing to commit' not in commit.get('stdout', '') + commit.get('stderr', ''):
            return self._send_json({'error': 'git commit failed', 'git': commit}, 500)

        remotes = self._run_git(['remote'], project_dir)
        if 'origin' in (remotes.get('stdout') or '').splitlines():
            remote_result = self._run_git(['remote', 'set-url', 'origin', remote], project_dir)
        else:
            remote_result = self._run_git(['remote', 'add', 'origin', remote], project_dir)
        if not remote_result['ok']:
            return self._send_json({'error': 'setting git remote failed', 'git': remote_result, 'github': created}, 500)

        push = self._push_with_token(project_dir, remote, branch)
        if not push['ok']:
            return self._send_json({'error': 'git push failed', 'git': push, 'github': created}, 500)

        project['repo']['remote'] = remote
        project['repo']['github'] = created
        self._upsert_project(project)
        self._send_json({'ok': True, 'project': project, 'github': created, 'git': push})

    def _handle_project_settings(self):
        body = self._read_json_body()
        project_id = body.get('project_id')
        action = body.get('action') or 'update'
        projects = self._load_projects()
        project = next((p for p in projects if p.get('id') == project_id), None)
        if not project:
            return self._send_json({'error': 'project not found'}, 404)

        project_dir = Path(project['path']).resolve()
        denied = self._require_grant(project_dir)
        if denied:
            return denied

        if action == 'update':
            settings = project.get('settings') if isinstance(project.get('settings'), dict) else {}
            if 'agent_ids' in body:
                ids = body.get('agent_ids') or []
                if not isinstance(ids, list):
                    return self._send_json({'error': 'agent_ids must be a list'}, 400)
                settings['agent_ids'] = [str(item) for item in ids if str(item).strip()]
            project['settings'] = settings
            self._upsert_project(project)
            return self._send_json({'ok': True, 'project': project})

        if action == 'delete_git':
            if body.get('approved') is not True:
                return self._send_json({'error': 'deleting .git requires explicit approval', 'approval_required': True}, 400)
            git_dir = project_dir / '.git'
            if project_dir == REPO_ROOT:
                return self._send_json({'error': 'refusing to delete repo root git metadata'}, 400)
            try:
                if git_dir.exists():
                    shutil.rmtree(git_dir)
            except OSError as exc:
                return self._send_json({'error': str(exc)}, 500)
            project['repo'] = {'type': 'git', 'initialized': False, 'remote': None, 'github': None}
            self._upsert_project(project)
            return self._send_json({'ok': True, 'project': project})

        if action == 'duplicate':
            if body.get('approved') is not True:
                return self._send_json({'error': 'duplicating a project requires explicit approval', 'approval_required': True}, 400)
            parent_raw = body.get('parent_dir') or str(project_dir.parent)
            name = (body.get('name') or f"{project.get('name', 'Project')} copy").strip()
            parent = self._resolve_path(parent_raw)
            denied = self._require_grant(parent)
            if denied:
                return denied
            if not parent.exists() or not parent.is_dir():
                return self._send_json({'error': f'not a directory: {parent}'}, 400)
            duplicate_dir = (parent / self._slugify(name)).resolve()
            denied = self._require_parent_grant(duplicate_dir)
            if denied:
                return denied
            if duplicate_dir.exists():
                return self._send_json({'error': f'destination already exists: {duplicate_dir}'}, 409)
            try:
                shutil.copytree(project_dir, duplicate_dir, ignore=shutil.ignore_patterns('.git'))
                git_result = self._run_git(['init'], duplicate_dir)
                if not git_result['ok']:
                    return self._send_json({'error': 'git init failed for duplicate', 'git': git_result}, 500)
            except OSError as exc:
                return self._send_json({'error': str(exc)}, 500)
            roots = self._load_grants()
            roots.append(str(duplicate_dir))
            self._save_grants(roots)
            duplicate = {
                **project,
                'id': str(uuid.uuid4()),
                'name': name,
                'slug': self._slugify(name),
                'path': str(duplicate_dir),
                'repo': {'type': 'git', 'initialized': True, 'remote': None, 'github': None},
                'created_from': project.get('id'),
            }
            self._upsert_project(duplicate)
            return self._send_json({'ok': True, 'project': duplicate})

        self._send_json({'error': f'unsupported project settings action: {action}'}, 400)

    def _handle_project_delete(self):
        body = self._read_json_body()
        project_id = body.get('project_id')
        remove_files = body.get('remove_files') is True
        if body.get('delete_approved') is not True:
            return self._send_json({'error': 'deleting a project requires explicit approval', 'approval_required': True}, 400)

        with PROJECTS_LOCK:
            projects = self._load_projects()
            project = next((p for p in projects if p.get('id') == project_id), None)
            if not project:
                return self._send_json({'error': 'project not found'}, 404)

            project_dir = Path(project['path']).resolve()
            if remove_files:
                denied = self._require_grant(project_dir)
                if denied:
                    return denied
                if project_dir == REPO_ROOT:
                    return self._send_json({'error': 'refusing to delete repo root'}, 400)
                try:
                    if project_dir.exists():
                        shutil.rmtree(project_dir)
                except OSError as exc:
                    return self._send_json({'error': str(exc)}, 500)

            self._save_projects([p for p in projects if p.get('id') != project_id])
        self._send_json({'ok': True, 'project_id': project_id, 'removed_files': remove_files})

    def _handle_project_git_action(self):
        from tools.github import (
            gh_create_issue, gh_create_pull_request, gh_create_repo,
            gh_dispatch_workflow, gh_list_workflows,
        )

        body = self._read_json_body()
        project = self._find_project(body.get('project_id'))
        action = body.get('action')
        if not project:
            return self._send_json({'error': 'project not found'}, 404)
        if not action:
            return self._send_json({'error': 'missing action'}, 400)

        project_dir = Path(project['path']).resolve()
        denied = self._require_grant(project_dir)
        if denied:
            return denied
        if not project_dir.exists() or not project_dir.is_dir():
            return self._send_json({'error': f'project directory not found: {project_dir}'}, 404)
        if not (project_dir / '.git').exists():
            return self._send_json({'error': f'not a git repository: {project_dir}'}, 400)

        remote = (project.get('repo') or {}).get('remote')
        branch_result = self._run_git(['branch', '--show-current'], project_dir)
        branch = (branch_result.get('stdout') or '').strip() or 'main'

        def github_repo_parts():
            full_name = ((project.get('repo') or {}).get('github') or {}).get('full_name')
            if full_name and '/' in full_name:
                return full_name.split('/', 1)
            current_remote = remote or self._run_git(['remote', 'get-url', 'origin'], project_dir).get('stdout', '').strip()
            match = re.search(r'github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$', current_remote)
            return match.groups() if match else (None, None)

        def require_approval(label):
            if body.get('approved') is not True:
                self._send_json({'error': f'{label} requires explicit approval', 'approval_required': True}, 400)
                return False
            return True

        result = None
        updated_project = project
        if action == 'status':
            result = self._run_git(['status', '--short', '--branch'], project_dir)
        elif action == 'diff':
            result = self._run_git(['diff', '--stat', '--patch', '--no-ext-diff'], project_dir)
        elif action == 'stage':
            if not require_approval('staging all changes'):
                return
            result = self._run_git(['add', '--all'], project_dir)
        elif action == 'unstage':
            if not require_approval('unstaging changes'):
                return
            result = self._run_git(['reset', '--mixed', 'HEAD'], project_dir)
        elif action == 'log':
            result = self._run_git(['log', '--oneline', '--decorate', '-n', str(int(body.get('limit') or 12))], project_dir)
        elif action == 'branches':
            result = self._run_git(['branch', '-a'], project_dir)
        elif action == 'commit':
            if not require_approval('commit'):
                return
            message = (body.get('message') or 'Project update').strip()
            self._run_git(['add', '.'], project_dir)
            self._ensure_git_identity(project_dir)
            result = self._run_git(['commit', '-m', message], project_dir)
            if not result['ok'] and 'nothing to commit' in result.get('stdout', '') + result.get('stderr', ''):
                result['ok'] = True
        elif action == 'create_remote':
            if not require_approval('GitHub repo creation'):
                return
            repo_name = self._slugify(body.get('repo_name') or project.get('slug') or project.get('name'))
            created = gh_create_repo(
                repo_name,
                private=body.get('private', True) is not False,
                description=body.get('description') or f'{project["name"]} project from qwen-harness',
                auto_init=body.get('add_readme') is True,
            )
            if 'error' in created:
                return self._send_json(created, 400)
            remote = created.get('clone_url') or created.get('ssh_url')
            if not remote:
                return self._send_json({'error': 'GitHub did not return a remote URL', 'github': created}, 500)
            remotes = self._run_git(['remote'], project_dir)
            if 'origin' in (remotes.get('stdout') or '').splitlines():
                result = self._run_git(['remote', 'set-url', 'origin', remote], project_dir)
            else:
                result = self._run_git(['remote', 'add', 'origin', remote], project_dir)
            if not result['ok']:
                return self._send_json({'error': 'setting git remote failed', 'git': result, 'github': created}, 500)
            updated_project['repo']['remote'] = remote
            updated_project['repo']['github'] = created
            self._upsert_project(updated_project)
            return self._send_json({'ok': True, 'project': updated_project, 'github': created, 'git': result})
        elif action == 'push':
            if not require_approval('push'):
                return
            remote = remote or self._run_git(['remote', 'get-url', 'origin'], project_dir).get('stdout', '').strip()
            result = self._push_with_token(project_dir, remote, branch)
        elif action == 'pull':
            if not require_approval('pull'):
                return
            remote = remote or self._run_git(['remote', 'get-url', 'origin'], project_dir).get('stdout', '').strip()
            result = self._run_git_remote_with_token(['pull', '--ff-only'], project_dir, remote)
        elif action == 'fetch':
            remote = remote or self._run_git(['remote', 'get-url', 'origin'], project_dir).get('stdout', '').strip()
            result = self._run_git_remote_with_token(['fetch', '--all', '--prune'], project_dir, remote)
        elif action == 'checkout':
            if not require_approval('checkout'):
                return
            name = (body.get('branch') or '').strip()
            if not re.match(r'^[A-Za-z0-9._/-]+$', name):
                return self._send_json({'error': 'invalid branch name'}, 400)
            args = ['checkout', '-B' if body.get('create') else '', name]
            result = self._run_git([a for a in args if a], project_dir)
        elif action == 'merge':
            if not require_approval('merge'):
                return
            name = (body.get('branch') or '').strip()
            if not re.match(r'^[A-Za-z0-9._/-]+$', name):
                return self._send_json({'error': 'invalid branch name'}, 400)
            result = self._run_git(['merge', name], project_dir)
        elif action == 'worktrees':
            result = self._run_git(['worktree', 'list', '--porcelain'], project_dir)
        elif action == 'worktree_create':
            if not require_approval('worktree creation'):
                return
            name = (body.get('branch') or '').strip()
            if not re.match(r'^[A-Za-z0-9._/-]+$', name):
                return self._send_json({'error': 'invalid branch name'}, 400)
            target = Path(body.get('path') or (project_dir.parent / f'{project_dir.name}-{self._slugify(name)}')).expanduser().resolve()
            if self._require_grant(target.parent):
                return
            if target.exists():
                return self._send_json({'error': f'worktree path already exists: {target}'}, 409)
            args = ['worktree', 'add']
            if body.get('create') is not False:
                args.extend(['-b', name])
            args.extend([str(target), name] if body.get('create') is False else [str(target)])
            result = self._run_git(args, project_dir)
        elif action == 'worktree_remove':
            if not require_approval('worktree removal'):
                return
            target = Path(body.get('path') or '').expanduser().resolve()
            if not body.get('path') or target == project_dir:
                return self._send_json({'error': 'select a linked worktree, not the primary project'}, 400)
            if self._require_grant(target):
                return
            listed = self._run_git(['worktree', 'list', '--porcelain'], project_dir).get('stdout', '')
            if f'worktree {target}' not in listed:
                return self._send_json({'error': 'path is not a linked worktree'}, 400)
            result = self._run_git(['worktree', 'remove', str(target)], project_dir)
        elif action == 'pr_create':
            if not require_approval('pull request creation'):
                return
            owner, repo_name = github_repo_parts()
            if not owner:
                return self._send_json({'error': 'project has no GitHub remote'}, 400)
            response = gh_create_pull_request(owner, repo_name, (body.get('title') or '').strip(), body.get('head') or branch, body.get('base') or 'main', body.get('body') or '')
            if 'error' in response:
                return self._send_json(response, 400)
            return self._send_json({'ok': True, 'project': updated_project, 'github': response})
        elif action == 'issue_create':
            if not require_approval('issue creation'):
                return
            owner, repo_name = github_repo_parts()
            if not owner:
                return self._send_json({'error': 'project has no GitHub remote'}, 400)
            response = gh_create_issue(owner, repo_name, (body.get('title') or '').strip(), body.get('body') or '')
            if 'error' in response:
                return self._send_json(response, 400)
            return self._send_json({'ok': True, 'project': updated_project, 'github': response})
        elif action == 'workflows':
            owner, repo_name = github_repo_parts()
            if not owner:
                return self._send_json({'error': 'project has no GitHub remote'}, 400)
            response = gh_list_workflows(owner, repo_name)
            if 'error' in response:
                return self._send_json(response, 400)
            return self._send_json({'ok': True, 'project': updated_project, 'github': response})
        elif action == 'workflow_dispatch':
            if not require_approval('workflow dispatch'):
                return
            owner, repo_name = github_repo_parts()
            workflow = str(body.get('workflow') or '').strip()
            if not owner or not workflow:
                return self._send_json({'error': 'GitHub remote and workflow are required'}, 400)
            response = gh_dispatch_workflow(owner, repo_name, workflow, body.get('ref') or branch, body.get('inputs') if isinstance(body.get('inputs'), dict) else {})
            if 'error' in response:
                return self._send_json(response, 400)
            return self._send_json({'ok': True, 'project': updated_project, 'github': response})
        else:
            return self._send_json({'error': f'unsupported git action: {action}'}, 400)

        status = 200 if result and result.get('ok') else 500
        self._send_json({'ok': bool(result and result.get('ok')), 'project': updated_project, 'git': result}, status)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[gui-server] {self.address_string()} {fmt % args}\n")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8420
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f"qwen-harness GUI server → http://127.0.0.1:{port}/gui/")
    print(f"(bound to localhost only; serving static files from {REPO_ROOT})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == '__main__':
    main()
