"""Shared filesystem and execution policy for every harness tool runtime."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = Path.home() / ".qwen-harness"
PERMISSIONS_PATH = STATE_DIR / "permissions.json"
SECRET_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "secrets.json",
    "credentials.json",
    "service-account.json",
}


class PermissionDenied(RuntimeError):
    def __init__(self, path: Path, reason: str = "permission required"):
        self.path = path
        self.reason = reason
        super().__init__(f"{reason} for {path}")


def atomic_write_json(path: Path, payload: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    # Any write to the permissions file must drop the cached roots, or a
    # freshly granted (or revoked) directory would not take effect until the
    # TTL expired. Cheap and unconditional beats remembering at each caller.
    if path == PERMISSIONS_PATH:
        invalidate_grants()


# Grants are consulted on every path check — 35 call sites in gui/server.py
# alone, several of them per request — and each check previously re-read and
# re-resolved permissions.json from disk. Cache the resolved roots, keyed on
# the file's identity so it self-invalidates when the file changes.
#
# Security properties this cache must preserve:
#  - It caches the ROOTS LIST ONLY, never a per-path allow/deny decision, so
#    each path is still resolved and matched fresh (no symlink-swap window).
#  - The key includes the path string and inode, because tests/test_policy.py
#    monkeypatches PERMISSIONS_PATH to a fresh temp file per test — an
#    mtime-only key could collide across them.
#  - A TTL ceiling bounds staleness even on filesystems with coarse mtime.
#  - Any stat error falls through to a full read: it fails CLOSED, never
#    serving a stale allow.
_GRANTS_LOCK = threading.RLock()
_GRANTS_CACHE: dict[str, Any] = {"key": None, "roots": (), "at": 0.0}
_GRANTS_TTL_S = 1.0


def _grants_cache_key() -> tuple:
    try:
        st = PERMISSIONS_PATH.stat()
        return (str(PERMISSIONS_PATH), st.st_ino, st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(PERMISSIONS_PATH), None, None, None)


def invalidate_grants() -> None:
    """Drop the cached roots. Call after any write to permissions.json."""
    with _GRANTS_LOCK:
        _GRANTS_CACHE["key"] = None


def _read_granted_roots() -> tuple[Path, ...]:
    # The sandbox root is harness-owned scratch space. It has to be writable or
    # every staged edit fails the grant check — which is exactly what happened:
    # writes "succeeded" as staged no-ops and the tests then ran against
    # unmodified code.
    roots = {REPO_ROOT, STATE_DIR / "sandboxes"}
    try:
        payload = json.loads(PERMISSIONS_PATH.read_text(encoding="utf-8"))
        roots.update(Path(value).expanduser() for value in payload.get("roots", []))
    except (OSError, ValueError, TypeError):
        pass
    resolved = []
    for root in roots:
        try:
            candidate = root.resolve()
            if candidate.is_dir():
                resolved.append(candidate)
        except OSError:
            continue
    return tuple(sorted(set(resolved), key=str))


def granted_roots() -> list[Path]:
    """Directories the harness is allowed to touch. Returns a fresh list.

    Callers in gui/server.py mutate the result (`roots.append(...)` before
    saving), so this must never hand back the cached sequence itself.
    """
    now = time.monotonic()
    key = _grants_cache_key()
    with _GRANTS_LOCK:
        if _GRANTS_CACHE["key"] == key and (now - _GRANTS_CACHE["at"]) < _GRANTS_TTL_S:
            return list(_GRANTS_CACHE["roots"])
    roots = _read_granted_roots()
    with _GRANTS_LOCK:
        _GRANTS_CACHE.update({"key": key, "roots": roots, "at": now})
    return list(roots)


def resolve_path(raw_path: str | Path, cwd: str | Path | None = None) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        base = Path(cwd).expanduser() if cwd else Path.cwd()
        candidate = base / candidate
    return candidate.resolve()


def is_granted(path: Path) -> bool:
    for root in granted_roots():
        if path == root:
            return True
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def is_secret_path(path: Path) -> bool:
    name = path.name.lower()
    return name in SECRET_NAMES or name.startswith(".env.") or name.endswith(".pem") or name.endswith(".key")


def require_path(
    raw_path: str | Path,
    cwd: str | Path | None = None,
    *,
    parent: bool = False,
    allow_secret: bool = False,
) -> Path:
    path = resolve_path(raw_path, cwd)
    checked = path.parent if parent and not path.exists() else path
    if not is_granted(checked):
        raise PermissionDenied(checked)
    if not allow_secret and is_secret_path(path):
        raise PermissionDenied(path, "secret-file access blocked")
    return path
