"""Shared filesystem and execution policy for every harness tool runtime."""

from __future__ import annotations

import json
import os
import tempfile
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


def granted_roots() -> list[Path]:
    roots = {REPO_ROOT}
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
    return sorted(set(resolved), key=str)


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
