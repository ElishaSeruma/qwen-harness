from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import files
from tools import policy


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
        # The dangerous staleness direction: a cache must never keep allowing
        # a path after it is revoked.
        outside = Path(tempfile.mkdtemp()).resolve()
        try:
            policy.atomic_write_json(self.permissions, {"roots": [str(self.root), str(outside)]})
            self.assertTrue(policy.is_granted(outside))
            policy.atomic_write_json(self.permissions, {"roots": [str(self.root)]})
            self.assertFalse(policy.is_granted(outside))
        finally:
            outside.rmdir()

    def test_shell_executes_in_explicit_working_directory(self):
        result = files.run_shell("pwd", cwd=str(self.root))
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["stdout"].strip(), str(self.root))


if __name__ == "__main__":
    unittest.main()
