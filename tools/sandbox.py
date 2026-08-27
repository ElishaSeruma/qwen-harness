"""
tools/sandbox.py — staged, confined workspaces for autonomous agent edits.

Agents never write straight to your files. Every run gets a sandbox:

    1. stage   — writes land in a temp copy of the project, not the project
    2. verify  — tests run *inside* the sandbox, confined
    3. promote — only verified changes are copied into the real tree

Confinement (macOS) uses sandbox-exec, which gives three real properties for
anything the agent runs via the shell:

  * writes are restricted to the sandbox directory
  * network is denied outright
  * credential files are unreadable — this one is not free. A naive profile
    with `(allow file-read*)` happily reads .env, ~/.ssh, and ~/.aws; the
    deny rules below are what actually stop a leak.

Honest limits, so nobody trusts this further than it goes:
  * sandbox-exec is macOS-only and Apple has deprecated it. `confinement()`
    reports which mode is live; on other platforms the staging guarantee
    still holds but shell confinement does not.
  * On Windows there is no equivalent that is available on every edition.
    AppContainer needs a signed manifest and per-capability ACLs; Windows
    Sandbox needs Pro/Enterprise plus Hyper-V. Rather than ship something that
    only confines on some machines and reports "sandboxed" on all of them,
    Windows runs in `staging-only` and says so — the mode is in every tool
    result, and the GUI shows it. What Windows *does* still get: writes are
    staged, tests run against the staged copy, promotion needs a green run,
    credentials are stripped from the child environment, and TMPDIR/TEMP/TMP
    point inside the sandbox.
  * Staging protects against accidental damage — the overwhelmingly common
    failure. It is not a defence against code that is actively trying to
    escape, because promote() copies files back out by design.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from tools.platform_support import IS_WINDOWS, python_command, quiet_kwargs, shell_argv
from tools.policy import STATE_DIR, is_secret_path, resolve_path

SANDBOX_ROOT = STATE_DIR / "sandboxes"
# A test command that hasn't finished in this long is hung, not slow.
TEST_TIMEOUT_S = 300
SHELL_TIMEOUT_S = 120
MAX_STAGED_BYTES = 32 * 1024 * 1024

# Read-denied inside a confined shell. These are the paths an autonomous agent
# has no business reading and every reason to leak.
SECRET_SUBPATHS = (
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.config/gh",
    "~/.kube",
    "~/.docker",
    "~/.npmrc",
    "~/.netrc",
    "~/.qwen-harness/secrets.json",
)
# Matched against the whole path, so this catches .env anywhere in the tree.
SECRET_PATTERNS = (
    r"^.*/\.env(\..*)?$",
    r"^.*\.pem$",
    r"^.*\.key$",
    r"^.*/credentials\.json$",
    r"^.*/service-account\.json$",
    r"^.*/id_(rsa|ed25519|ecdsa)$",
)


def _boundary_match(posix_path: str, pattern: str) -> bool:
    """One gitignore-style boundary pattern against one relative POSIX path.

    Split out so the semantics are testable on their own, and so the `**`
    handling is written once. `fnmatch.translate` is not used directly because
    it gives `*` the power to cross `/`, which is exactly the behaviour that
    made the first version of this wrong.
    """
    pattern = pattern.strip()
    if not pattern:
        return False
    if "/" not in pattern:
        # Basename match at any depth, as gitignore does for a slash-free
        # pattern. `*` here cannot cross a separator because a basename has
        # none to cross.
        return fnmatch.fnmatch(PurePosixPath(posix_path).name, pattern)

    # "src/**" should include "src/main.py" AND bare "src" itself, so that a
    # boundary can be expressed the obvious way.
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        if posix_path == prefix or posix_path.startswith(prefix + "/"):
            return True

    # Path match, where `**` crosses separators and `*` does not. Build the
    # regex explicitly rather than post-processing fnmatch's output.
    regex, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern[i:i + 2] == "**":
                regex.append(".*")
                i += 2
                if pattern[i:i + 1] == "/":
                    i += 1          # "**/" may also match zero directories
                    regex[-1] = "(?:.*/)?"
                continue
            regex.append("[^/]*")
        elif ch == "?":
            regex.append("[^/]")
        else:
            regex.append(re.escape(ch))
        i += 1
    return re.fullmatch("".join(regex), posix_path) is not None


class SandboxError(RuntimeError):
    pass


def _sb_quote(value: str) -> str:
    """Quote a string for a sandbox profile literal."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_profile(write_root: Path) -> str:
    """A sandbox-exec profile: read broadly, write only here, no network, no secrets."""
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec process-fork)",
        "(allow sysctl-read mach-lookup)",
        "(allow file-read-metadata)",
        "(allow file-read*)",
        # Order matters: later rules win in sandbox-exec, so the secret denials
        # must come after the broad read allow.
        f"(allow file-write* (subpath {_sb_quote(str(write_root))}))",
        # Deliberately NOT allowing /tmp. Build tools do need scratch space, but
        # /tmp is shared: allowing it let a test command write anywhere under
        # it, which is not "writes are confined to the sandbox". The sandbox
        # gets its own tmp/ and TMPDIR points there instead.
        "(allow file-write-data (literal \"/dev/null\") (literal \"/dev/stdout\") (literal \"/dev/stderr\"))",
        "(allow file-write* (literal \"/dev/dtracehelper\"))",
        "(deny network*)",
    ]
    for raw in SECRET_SUBPATHS:
        expanded = Path(raw).expanduser()
        lines.append(f"(deny file-read* (subpath {_sb_quote(str(expanded))}))")
    for pattern in SECRET_PATTERNS:
        lines.append(f"(deny file-read* (regex {_sb_quote(pattern)}))")
    return "\n".join(lines) + "\n"


def confinement() -> dict[str, Any]:
    """What confinement is actually available here — never guessed, always reported."""
    if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists():
        return {
            "mode": "sandbox-exec",
            "writes_confined": True,
            "network_denied": True,
            "secrets_denied": True,
        }
    note = (
        "No OS sandbox on this platform. Agent writes are still staged and "
        "verified before reaching your files, but a shell command run by the "
        "agent is not confined."
    )
    if IS_WINDOWS:
        note += (
            " On Windows there is no confinement mechanism present on every "
            "edition (AppContainer needs a signed manifest; Windows Sandbox "
            "needs Pro/Enterprise and Hyper-V), so this is reported rather "
            "than approximated. Credentials are still stripped from the "
            "command's environment and its temp directory is inside the "
            "sandbox."
        )
    return {
        "mode": "staging-only",
        "writes_confined": False,
        "network_denied": False,
        "secrets_denied": False,
        "note": note,
    }


@dataclass
class Sandbox:
    """One agent's staging workspace within one run."""

    run_id: str
    project_root: Path
    root: Path
    agent_id: str = "main"
    # Glob patterns this agent may write, relative to project_root. Empty means
    # unrestricted, which is the single-agent default. See `check_boundary`.
    file_boundary: tuple[str, ...] = ()
    staged: dict[str, str] = field(default_factory=dict)  # rel path -> "create"|"modify"
    verified: bool = False
    last_test: dict[str, Any] | None = None

    # ── boundary ──────────────────────────────────────────────────────────
    def check_boundary(self, rel: str) -> None:
        """Refuse a write outside this agent's declared file boundary.

        Enforced here, at the same chokepoint that already resolves every
        agent-supplied path, so an agent cannot phrase its way around it —
        the same reasoning as _apply_sandbox() in gui/server.py.

        An empty boundary allows everything. That is deliberate: a boundary is
        an opt-in contract for parallel work, and defaulting to deny would
        break every existing single-agent run.

        **Pattern semantics are gitignore's, chosen deliberately.** Writing
        this against bare `fnmatch` produced *accidental* semantics instead:
        fnmatch's `*` happily crosses `/`, so `*.md` silently also matched
        `docs/deep/guide.md`. Rather than leave a permission check behaving by
        accident, the two cases are separated:

          no `/` in the pattern   ->  matched against the BASENAME, at any
                                      depth. `*.md` means "markdown files",
                                      which is what someone typing it means.
          `/` in the pattern      ->  matched against the whole relative path,
                                      with `**` crossing directories and a
                                      single `*` not. `src/**` is the subtree;
                                      `src/*.py` is only that one level.
        """
        if not self.file_boundary:
            return
        posix = PurePosixPath(str(rel).replace(os.sep, "/")).as_posix()
        for pattern in self.file_boundary:
            if _boundary_match(posix, pattern):
                return
        raise SandboxError(
            f"{rel} is outside agent {self.agent_id}'s file boundary "
            f"({', '.join(self.file_boundary)})"
        )

    # ── layout ────────────────────────────────────────────────────────────
    @property
    def work(self) -> Path:
        return self.root / "work"

    def _rel(self, target: Path) -> str:
        # Resolve before comparing. Two reasons, both load-bearing:
        #  - macOS hands out /var/... and /tmp/... paths that are symlinks to
        #    /private/..., so an unresolved compare rejects legitimate paths.
        #  - It collapses "..", so a path cannot walk out of the project root
        #    and a symlink cannot point the sandbox somewhere else.
        resolved = Path(target).expanduser()
        resolved = resolved if resolved.is_absolute() else (self.project_root / resolved)
        resolved = resolved.resolve()
        try:
            return str(resolved.relative_to(self.project_root))
        except ValueError as exc:
            raise SandboxError(
                f"{resolved} is outside the sandboxed project root {self.project_root}"
            ) from exc

    def staged_path(self, target: Path) -> Path:
        return self.work / self._rel(target)

    # ── staging ───────────────────────────────────────────────────────────
    def materialize(self, target: Path) -> Path:
        """Sandbox copy of `target`, seeded from the real file on first touch.

        Seeding matters: an edit_file that replaces a unique string has to see
        the file's real content, or every edit would apply to an empty file.
        """
        # Checked before the staged copy is created, not only in record():
        # record() runs *after* the write, so enforcing there alone would let
        # out-of-boundary content land on disk and merely refuse to track it.
        self.check_boundary(self._rel(target))
        staged = self.staged_path(target)
        staged.parent.mkdir(parents=True, exist_ok=True)
        if not staged.exists() and target.exists():
            shutil.copy2(target, staged)
        return staged

    def record(self, target: Path) -> None:
        rel = self._rel(target)
        self.check_boundary(rel)
        if rel not in self.staged:
            self.staged[rel] = "modify" if (self.project_root / rel).exists() else "create"
        # Any new edit invalidates a previous green test run: what was verified
        # is no longer what would be promoted.
        self.verified = False

    def changes(self) -> list[dict[str, Any]]:
        rows = []
        for rel, action in sorted(self.staged.items()):
            staged = self.work / rel
            real = self.project_root / rel
            try:
                new_bytes = staged.stat().st_size if staged.exists() else 0
            except OSError:
                new_bytes = 0
            rows.append({
                "path": rel,
                "action": action,
                "bytes": new_bytes,
                "old_bytes": real.stat().st_size if real.exists() else 0,
            })
        return rows

    # ── hydration ─────────────────────────────────────────────────────────
    # Tests need a whole project, not just the files the agent touched, so the
    # work dir is filled in from the real tree before anything runs. Staged
    # files are never overwritten — the agent's edit is the point.
    SKIP_DIRS = {
        ".git", "node_modules", "__pycache__", ".venv", "venv", ".build",
        "dist", "build", ".next", ".tox", ".mypy_cache", ".pytest_cache",
        ".qwen-harness", "sandboxes",
    }

    def hydrate(self, *, max_bytes: int = MAX_STAGED_BYTES) -> dict[str, Any]:
        copied = skipped = 0
        total = 0
        for source in self.project_root.rglob("*"):
            if any(part in self.SKIP_DIRS for part in source.relative_to(self.project_root).parts):
                continue
            if not source.is_file() or source.is_symlink():
                continue
            rel = source.relative_to(self.project_root)
            # Never copy credentials into the sandbox: the point is that the
            # agent cannot read them, and a copy would hand them over.
            if is_secret_path(source):
                skipped += 1
                continue
            destination = self.work / rel
            if str(rel) in self.staged and destination.exists():
                continue
            try:
                size = source.stat().st_size
                if total + size > max_bytes:
                    skipped += 1
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists() or destination.stat().st_mtime < source.stat().st_mtime:
                    shutil.copy2(source, destination)
                    copied += 1
                total += size
            except OSError:
                skipped += 1
        return {"copied": copied, "skipped": skipped, "bytes": total}

    def purge_caches(self) -> int:
        """Drop compiled artefacts in the work tree so tests see current source."""
        removed = 0
        for cache in list(self.work.rglob("__pycache__")):
            shutil.rmtree(cache, ignore_errors=True)
            removed += 1
        for pyc in list(self.work.rglob("*.pyc")):
            try:
                pyc.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    # ── execution ─────────────────────────────────────────────────────────
    def run(self, command: str, timeout: int = SHELL_TIMEOUT_S) -> dict[str, Any]:
        """Run a shell command inside the sandbox, confined where the OS allows."""
        self.work.mkdir(parents=True, exist_ok=True)
        mode = confinement()
        # Strip the parent's credentials rather than trusting the profile alone:
        # an env var is readable without touching the filesystem at all.
        env = {
            k: v for k, v in os.environ.items()
            if not any(marker in k.upper() for marker in ("TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL"))
        }
        env["QWEN_HARNESS_SANDBOX"] = str(self.work)
        # Scratch space inside the jail, so denying /tmp doesn't break builds.
        scratch = self.root / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        env["TMPDIR"] = env["TMP"] = env["TEMP"] = str(scratch)
        # Belt and braces with purge_caches(): don't create new .pyc files either.
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        if mode["mode"] == "sandbox-exec":
            profile = self.root / "profile.sb"
            profile.write_text(build_profile(self.root), encoding="utf-8")
            argv = ["/usr/bin/sandbox-exec", "-f", str(profile), "/bin/sh", "-c", command]
        else:
            # /bin/sh does not exist on Windows; shell_argv picks PowerShell
            # there (or whatever QH_SHELL names) and /bin/sh everywhere else.
            argv = shell_argv(command)

        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv, cwd=str(self.work), env=env, timeout=timeout,
                capture_output=True, text=True, **quiet_kwargs(),
            )
        except subprocess.TimeoutExpired:
            return {
                "error": f"command exceeded {timeout}s and was killed",
                "timed_out": True, "confinement": mode["mode"],
            }
        except OSError as exc:
            return {"error": str(exc), "confinement": mode["mode"]}

        return {
            "status": "ok",
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-8000:],
            "stderr": proc.stderr[-4000:],
            "duration_s": round(time.monotonic() - started, 2),
            "confinement": mode["mode"],
            "cwd": str(self.work),
        }

    def verify(self, command: str | None = None) -> dict[str, Any]:
        """Run the project's tests in the sandbox. Green here is what unlocks promote()."""
        # Fill in the rest of the project first, or the tests have nothing to
        # import and "no tests ran" would masquerade as a pass.
        self.hydrate()
        # Then purge stale bytecode. CPython invalidates a .pyc on source
        # mtime+size, and two versions of an edit are very often the same size
        # written in the same second ("a * b" vs "a + b"). Verification then
        # runs against the PREVIOUS attempt's bytecode — a green run that
        # certifies code the agent never staged. This is the one cache we
        # cannot afford to trust.
        self.purge_caches()
        command = command or detect_test_command(self.project_root) or detect_test_command(self.work)
        if not command:
            self.last_test = {"status": "no_tests", "command": None}
            self.verified = False
            return {
                "status": "no_tests",
                "detail": (
                    "No test command detected. Promotion needs explicit approval "
                    "because the change could not be verified."
                ),
            }
        result = self.run(command, timeout=TEST_TIMEOUT_S)
        passed = result.get("exit_code") == 0
        self.verified = passed
        self.last_test = {
            "status": "passed" if passed else "failed",
            "command": command,
            "exit_code": result.get("exit_code"),
            "stdout": result.get("stdout", "")[-4000:],
            "stderr": result.get("stderr", "")[-2000:],
        }
        return {**result, "command": command, "verified": passed}

    # ── promotion ─────────────────────────────────────────────────────────
    def promote(self, *, force: bool = False) -> dict[str, Any]:
        """Copy staged files into the real tree. Refuses unverified work unless forced."""
        if not self.staged:
            return {"status": "nothing_to_promote", "promoted": []}
        if not self.verified and not force:
            return {
                "error": "refusing to promote unverified changes",
                "verified": False,
                "last_test": self.last_test,
                "hint": "Run sandbox_verify first, or promote with force=true to accept the risk.",
                "changes": self.changes(),
            }

        promoted, failed = [], []
        for rel in sorted(self.staged):
            source = self.work / rel
            target = self.project_root / rel
            if not source.exists():
                continue
            if is_secret_path(target):
                failed.append({"path": rel, "error": "refusing to write a credential file"})
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                # Keep one .bak of anything overwritten: promotion is the only
                # step that touches real files, so it's the only one that needs
                # an undo.
                if target.exists():
                    shutil.copy2(target, target.with_suffix(target.suffix + ".qh.bak"))
                shutil.copy2(source, target)
                promoted.append(rel)
            except OSError as exc:
                failed.append({"path": rel, "error": str(exc)})

        for rel in promoted:
            self.staged.pop(rel, None)
        return {
            "status": "ok" if not failed else "partial",
            "promoted": promoted,
            "failed": failed,
            "verified": self.verified,
            "forced": bool(force and not self.verified),
        }

    def discard(self) -> dict[str, Any]:
        count = len(self.staged)
        shutil.rmtree(self.root, ignore_errors=True)
        self.staged.clear()
        self.verified = False
        return {"status": "ok", "discarded": count}

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "file_boundary": list(self.file_boundary),
            "root": str(self.root),
            "work": str(self.work),
            "project_root": str(self.project_root),
            "changes": self.changes(),
            "verified": self.verified,
            "last_test": self.last_test,
            "confinement": confinement(),
        }


def detect_test_command(root: Path) -> str | None:
    """Best-guess test command for a project, or None if there's nothing to run."""
    if (root / "package.json").exists():
        try:
            import json
            scripts = json.loads((root / "package.json").read_text(encoding="utf-8")).get("scripts", {})
            if "test" in scripts:
                return "npm test --silent"
        except (OSError, ValueError):
            pass
    # `python3` is not a real executable on a default Windows install: the
    # python3.exe under WindowsApps is a Microsoft Store redirector that exits
    # 9009 and opens the Store. A detected test command that cannot run turns
    # every verify into a failure and blocks every promotion, so the
    # interpreter name comes from the platform layer.
    python = python_command()
    if (root / "tests").is_dir() or list(root.glob("test_*.py")):
        return f"{python} -m unittest discover -s tests -q"
    if (root / "pytest.ini").exists() or (root / "pyproject.toml").exists():
        return f"{python} -m pytest -q"
    if (root / "Cargo.toml").exists():
        return "cargo test --quiet"
    if (root / "go.mod").exists():
        return "go test ./..."
    return None


# ── registry: one sandbox per (run, agent) ───────────────────────────────
# Keyed on the AGENT as well as the run, not the run alone.
#
# The run-only key was a real bug, not a missing feature. A run can drive
# several agents (the Agent Hub broadcasts, and orchestrator.py fans out), and
# they all shared one staging tree. Two agents told to edit the same file each
# materialize() it, each write their own version, and the second silently wins
# — with `changes()` reporting a single entry as though one edit had happened.
# Worse, `record()` sets `verified = False` globally, so agent B's edit
# invalidates agent A's green test run and neither can promote.
#
# Separate trees per agent give each one its own materialize/record/verify/
# promote cycle. That is the property git worktrees buy in qwen-code, obtained
# here without worktrees because the harness already stages.
_SANDBOXES: dict[tuple[str, str], Sandbox] = {}

# The agent id used when a run does not name one. Explicit rather than "", so a
# path and a log line both read as something deliberate.
DEFAULT_AGENT_ID = "main"


def _safe_segment(value: str) -> str:
    """A path segment that cannot escape SANDBOX_ROOT.

    run_id and agent_id arrive from the client. Without this a run_id of
    "../.." would put the sandbox — and therefore promote()'s source tree —
    anywhere on disk.
    """
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_.") else "-" for ch in str(value))
    cleaned = cleaned.strip(".-") or "unnamed"
    return cleaned[:64]


def sandbox_key(run_id: str, agent_id: str | None = None) -> tuple[str, str]:
    return (str(run_id), str(agent_id or DEFAULT_AGENT_ID))


def get_sandbox(
    run_id: str,
    project_root: str | Path | None = None,
    *,
    agent_id: str | None = None,
    create: bool = True,
) -> Sandbox | None:
    """The sandbox for one agent within one run, created on first use."""
    if not run_id:
        return None
    key = sandbox_key(run_id, agent_id)
    existing = _SANDBOXES.get(key)
    if existing or not create:
        return existing
    # Nested run/agent directories rather than a joined name, so `ls` on the
    # sandbox root still shows one entry per run.
    root = SANDBOX_ROOT / _safe_segment(run_id) / _safe_segment(key[1])
    (root / "work").mkdir(parents=True, exist_ok=True)
    sandbox = Sandbox(
        run_id=str(run_id),
        agent_id=key[1],
        project_root=resolve_path(project_root or Path.cwd()),
        root=root,
    )
    _SANDBOXES[key] = sandbox
    return sandbox


def sandboxes_for_run(run_id: str) -> list[Sandbox]:
    """Every agent's sandbox within one run, for a combined status view."""
    return [box for (rid, _), box in _SANDBOXES.items() if rid == str(run_id)]


def drop_sandbox(run_id: str, agent_id: str | None = None, *, remove_files: bool = True) -> None:
    """Drop one agent's sandbox, or every sandbox in the run when agent_id is None."""
    if agent_id is None:
        for key in [k for k in _SANDBOXES if k[0] == str(run_id)]:
            box = _SANDBOXES.pop(key, None)
            if box and remove_files:
                shutil.rmtree(box.root, ignore_errors=True)
        # Remove the now-empty run directory too, or the sandbox root fills up
        # with empty husks that cleanup_stale then has to walk forever.
        if remove_files:
            run_dir = SANDBOX_ROOT / _safe_segment(run_id)
            try:
                if run_dir.is_dir() and not any(run_dir.iterdir()):
                    run_dir.rmdir()
            except OSError:
                pass
        return
    box = _SANDBOXES.pop(sandbox_key(run_id, agent_id), None)
    if box and remove_files:
        shutil.rmtree(box.root, ignore_errors=True)


def cleanup_stale(max_age_s: int = 24 * 3600) -> int:
    """Remove sandbox directories left behind by runs that never finished."""
    if not SANDBOX_ROOT.exists():
        return 0
    removed = 0
    cutoff = time.time() - max_age_s
    live_runs = {_safe_segment(rid) for rid, _ in _SANDBOXES}
    for entry in SANDBOX_ROOT.iterdir():
        try:
            # The layout is now SANDBOX_ROOT/<run>/<agent>, so the entries here
            # are runs. Skipping any run with a live sandbox keeps a
            # long-running multi-agent run from having half its trees deleted
            # out from under it.
            if entry.is_dir() and entry.stat().st_mtime < cutoff and entry.name not in live_runs:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed
