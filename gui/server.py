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
  POST /api/exec       {command, cwd}        -> run a shell command, return output
  GET  /api/web/search?q=<query>             -> web search results (no API key)
  GET  /api/web/scrape?url=<url>             -> readable text of a page
  GET  /api/github/status                    -> {connected, login, ...} (never the token)
  POST /api/github/connect  {token}          -> validate + store a GitHub PAT locally
  POST /api/github/disconnect                -> forget the stored token
  GET  /api/github/repos                     -> list the connected account's repos

This is intentionally unauthenticated and unsandboxed: it's a personal,
single-user, local dev tool — it binds to 127.0.0.1 only and is not meant
to be exposed beyond your own machine.
"""
import json
import os
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # so `tools.*` importable, matching the Python agents
MAX_READ_BYTES = 3 * 1024 * 1024  # 3MB cap so the browser doesn't choke
EXEC_TIMEOUT_S = 30
STATE_DIR = Path.home() / '.qwen-harness'
PERMISSIONS_PATH = STATE_DIR / 'permissions.json'

SKIP_DIR_NAMES = {'.git', 'node_modules', '__pycache__', '.DS_Store'}

# Same substring blocklist as tools/files.py's run_shell, so the browser
# terminal and the Python agent's run_shell tool refuse the same things.
# This is a basic guard, not a sandbox — see module docstring.
DANGEROUS_PATTERNS = ["sudo", "rm -rf", "mkfs", ":(){:|:&};:"]

CONTENT_TYPES = {
    '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8', '.json': 'application/json',
    '.py': 'text/plain; charset=utf-8', '.sh': 'text/plain; charset=utf-8',
    '.txt': 'text/plain; charset=utf-8', '.md': 'text/plain; charset=utf-8',
    '.cjs': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
}


class Handler(BaseHTTPRequestHandler):
    server_version = "QwenHarnessGUI/1"

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
        return Path(unquote(raw_path)).expanduser().resolve()

    def _load_grants(self):
        roots = {str(REPO_ROOT)}
        try:
            if PERMISSIONS_PATH.exists():
                data = json.loads(PERMISSIONS_PATH.read_text(encoding='utf-8'))
                roots.update(data.get('roots', []))
        except (OSError, json.JSONDecodeError):
            pass
        valid = []
        for root in roots:
            try:
                path = Path(root).expanduser().resolve()
                if path.exists() and path.is_dir():
                    valid.append(str(path))
            except OSError:
                continue
        return sorted(set(valid))

    def _save_grants(self, roots):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        PERMISSIONS_PATH.write_text(json.dumps({'roots': sorted(set(roots))}, indent=2), encoding='utf-8')

    def _is_granted(self, path):
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        for root in self._load_grants():
            root_path = Path(root)
            if resolved == root_path:
                return True
            try:
                resolved.relative_to(root_path)
                return True
            except ValueError:
                continue
        return False

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
        try:
            proc = subprocess.run(
                command, shell=True, cwd=str(cwd_path),
                capture_output=True, text=True, timeout=EXEC_TIMEOUT_S,
            )
            self._send_json({
                'ok': True, 'code': proc.returncode,
                'stdout': proc.stdout[-20000:], 'stderr': proc.stderr[-20000:],
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
