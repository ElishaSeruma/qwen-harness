from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import files
from tools import policy
from tools.platform_support import IS_WINDOWS


class ToolPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.permissions = self.root / "permissions.json"
        self.permissions.write_text(json.dumps({"roots": [str(self.root)]}), encoding="utf-8")
        self.patch = patch.object(policy, "PERMISSIONS_PATH", self.permissions)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_relative_paths_resolve_against_cwd(self):
        result = files.write_file("notes/demo.txt", "hello", cwd=str(self.root))
        self.assertEqual(result["status"], "ok")
        self.assertEqual((self.root / "notes/demo.txt").read_text(), "hello")

    def test_path_outside_grant_is_denied(self):
        result = files.read_file("/etc/hosts", cwd=str(self.root))
        self.assertTrue(result["permission_required"])

    def test_secret_file_is_denied_inside_grant(self):
        secret = self.root / ".env"
        secret.write_text("TOKEN=hidden", encoding="utf-8")
        result = files.read_file(str(secret), cwd=str(self.root))
        self.assertTrue(result["permission_required"])
        self.assertIn("secret-file", result["error"])

    def test_granted_roots_returns_a_fresh_list(self):
        # gui/server.py does `roots = _load_grants(); roots.append(...)` in
        # three places. If the cache handed back its own sequence, those
        # appends would silently corrupt it for every later caller.
        first = policy.granted_roots()
        first.append(Path("/tmp/definitely-not-granted"))
        self.assertNotIn(Path("/tmp/definitely-not-granted"), policy.granted_roots())

    def test_new_grant_is_visible_after_write(self):
        # A directory granted at runtime must take effect immediately, not
        # after the cache TTL expires.
        extra = self.root / "granted-later"
        extra.mkdir()
        self.assertTrue(policy.is_granted(self.root))
        policy.granted_roots()  # prime the cache
        policy.atomic_write_json(self.permissions, {"roots": [str(self.root), str(extra)]})
        self.assertIn(extra.resolve(), policy.granted_roots())

    def test_revoked_grant_stops_being_granted(self):
        """The dangerous staleness direction: the cache must never keep
        allowing a path after the grant is revoked.

        Asserted against the resolved ROOT LIST rather than is_granted().
        is_granted() is an OR across every root, including the two the policy
        grants unconditionally (the repo, and ~/.qwen-harness/sandboxes). A
        temp directory is not reliably outside those: inside a sandbox run
        TMPDIR points into the sandboxes tree, so mkdtemp() lands under a
        permanently-granted root and the revoked path is still "granted" —
        correctly, by a different root. That made this test fail only under
        sandbox_verify, and a permanently red verify blocks every promotion.

        The cache property being guarded is unchanged: after a revoking write,
        the roots list must not still contain the revoked entry.
        """
        outside = Path(tempfile.mkdtemp()).resolve()
        try:
            policy.atomic_write_json(self.permissions, {"roots": [str(self.root), str(outside)]})
            self.assertIn(outside, policy.granted_roots())
            self.assertTrue(policy.is_granted(outside))

            policy.atomic_write_json(self.permissions, {"roots": [str(self.root)]})
            self.assertNotIn(outside, policy.granted_roots(),
                             "the cache served a revoked root")
            # is_granted can still be True if an unconditional root happens to
            # contain this temp path, so only assert it where it is meaningful.
            if not any(outside.is_relative_to(root) for root in policy.granted_roots()):
                self.assertFalse(policy.is_granted(outside))
        finally:
            outside.rmdir()

    def test_shell_executes_in_explicit_working_directory(self):
        # `pwd` prints a bare path in sh; in PowerShell it is an alias for
        # Get-Location, which formats a table. Ask each shell for the string
        # directly instead of parsing its default rendering.
        command = "[Console]::Out.Write((Get-Location).ProviderPath)" if IS_WINDOWS else "pwd"
        result = files.run_shell(command, cwd=str(self.root))
        self.assertEqual(result["returncode"], 0, result)
        self.assertEqual(result["stdout"].strip(), str(self.root))

    def test_shell_refuses_this_platforms_destructive_patterns(self):
        """A Windows user previously had no guard: no Windows command contains
        the substring "rm -rf"."""
        destructive = "Remove-Item -Recurse -Force C:/" if IS_WINDOWS else "rm -rf /"
        result = files.run_shell(destructive, cwd=str(self.root))
        self.assertIn("Blocked dangerous command pattern", result["error"])


if __name__ == "__main__":
    unittest.main()
