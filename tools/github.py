"""
tools/github.py – GitHub connector (Personal Access Token, read + basic write).

The token is stored once, locally, outside the repo — never in git, never in
browser localStorage — at ~/.qwen-harness/secrets.json (chmod 600). Both the
Python agents (via these functions) and the GUI backend (gui/server.py's
/api/github/* routes) read the same file, so connecting once from either side
works everywhere.

Connect with:
    from tools.github import gh_connect
    gh_connect("ghp_...")
or from the GUI's Connectors panel (GitHub card).
"""

from __future__ import annotations
import json
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path

SECRETS_PATH = Path.home() / ".qwen-harness" / "secrets.json"
API_ROOT = "https://api.github.com"
USER_AGENT = "qwen-harness/1.0"


def _load_secrets() -> dict:
    if not SECRETS_PATH.exists():
        return {}
    try:
        return json.loads(SECRETS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_secrets(data: dict) -> None:
    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SECRETS_PATH.write_text(json.dumps(data, indent=2))
    os.chmod(SECRETS_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 600 — owner read/write only


def _token() -> str | None:
    return _load_secrets().get("github", {}).get("token")


def _request(path: str, method: str = "GET", body: dict | None = None) -> dict:
    token = _token()
    if not token:
        raise RuntimeError("GitHub is not connected. Call gh_connect(token) first.")
    url = path if path.startswith("http") else f"{API_ROOT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def gh_connect(token: str) -> dict:
    """Validate a Personal Access Token against the GitHub API and store it locally."""
    try:
        req = urllib.request.Request(f"{API_ROOT}/user", headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            profile = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"error": "Invalid or expired token."}
        return {"error": f"GitHub API error: {e.code}"}
    except Exception as e:
        return {"error": str(e)}

    secrets = _load_secrets()
    secrets["github"] = {
        "token": token,
        "login": profile.get("login"),
        "name": profile.get("name"),
        "avatar_url": profile.get("avatar_url"),
    }
    _save_secrets(secrets)
    return {"status": "ok", "login": profile.get("login"), "name": profile.get("name"), "avatar_url": profile.get("avatar_url")}


def gh_disconnect() -> dict:
    secrets = _load_secrets()
    secrets.pop("github", None)
    _save_secrets(secrets)
    return {"status": "ok"}


def gh_status() -> dict:
    """Return connection status without ever exposing the raw token."""
    gh = _load_secrets().get("github")
    if not gh:
        return {"connected": False}
    return {"connected": True, "login": gh.get("login"), "name": gh.get("name"), "avatar_url": gh.get("avatar_url")}


def gh_token_for_git() -> str | None:
    """Return the stored token for local git transport only."""
    return _token()


def gh_list_repos(max_results: int = 30) -> dict:
    """List repositories the connected account owns or collaborates on."""
    try:
        repos = _request(f"/user/repos?per_page={max_results}&sort=updated")
        return {"status": "ok", "repos": [
            {
                "full_name": r["full_name"],
                "private": r["private"],
                "description": r.get("description"),
                "html_url": r["html_url"],
                "default_branch": r.get("default_branch"),
                "updated_at": r.get("updated_at"),
            }
            for r in repos
        ]}
    except Exception as e:
        return {"error": str(e)}


def gh_get_file(owner: str, repo: str, path: str, ref: str | None = None) -> dict:
    """Read a file's contents from a GitHub repo."""
    try:
        import base64
        q = f"?ref={ref}" if ref else ""
        data = _request(f"/repos/{owner}/{repo}/contents/{path}{q}")
        if isinstance(data, list):
            return {"error": f"{path} is a directory, not a file."}
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        return {"status": "ok", "path": path, "content": content, "sha": data.get("sha")}
    except Exception as e:
        return {"error": str(e)}


def gh_list_dir(owner: str, repo: str, path: str = "", ref: str | None = None) -> dict:
    """List a directory's contents in a GitHub repo."""
    try:
        q = f"?ref={ref}" if ref else ""
        data = _request(f"/repos/{owner}/{repo}/contents/{path}{q}")
        if not isinstance(data, list):
            return {"error": f"{path} is a file, not a directory."}
        return {"status": "ok", "path": path, "entries": [
            {"name": e["name"], "path": e["path"], "type": "dir" if e["type"] == "dir" else "file", "size": e.get("size")}
            for e in data
        ]}
    except Exception as e:
        return {"error": str(e)}


def gh_create_issue(owner: str, repo: str, title: str, body: str = "") -> dict:
    """Open a new issue on a GitHub repo."""
    try:
        result = _request(f"/repos/{owner}/{repo}/issues", method="POST", body={"title": title, "body": body})
        return {"status": "ok", "number": result.get("number"), "html_url": result.get("html_url")}
    except Exception as e:
        return {"error": str(e)}


def gh_create_repo(name: str, private: bool = True, description: str = "", auto_init: bool = False) -> dict:
    """Create a repository for the connected account."""
    try:
        body = {
            "name": name,
            "private": private,
            "auto_init": auto_init,
        }
        if description:
            body["description"] = description
        result = _request("/user/repos", method="POST", body=body)
        return {
            "status": "ok",
            "full_name": result.get("full_name"),
            "clone_url": result.get("clone_url"),
            "ssh_url": result.get("ssh_url"),
            "html_url": result.get("html_url"),
            "default_branch": result.get("default_branch") or "main",
        }
    except Exception as e:
        return {"error": str(e)}
