from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from tools.sandbox import Sandbox, SandboxError, build_profile, confinement, detect_test_command


def make_project(root: Path, *, buggy: bool = True) -> None:
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n" if buggy else "def add(a, b):\n    return a + b\n"
    )
    (root / "tests" / "test_calc.py").write_text(
        "import sys; sys.path.insert(0, '.')\n"
        "import unittest\nfrom calc import add\n"
        "class T(unittest.TestCase):\n"
        "    def test_add(self): self.assertEqual(add(2, 3), 5)\n"
    )


class SandboxStagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        self.project = base / "proj"
        make_project(self.project)
        self.sandbox = Sandbox(run_id="t", project_root=self.project, root=base / "sb")
        (self.sandbox.work).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_writes_do_not_touch_the_real_file(self):
        staged = self.sandbox.materialize(self.project / "calc.py")
        self.sandbox.record(self.project / "calc.py")
        staged.write_text("def add(a, b):\n    return 999\n")
        self.assertIn("a - b", (self.project / "calc.py").read_text())
        self.assertEqual(self.sandbox.changes()[0]["action"], "modify")

    def test_materialize_seeds_from_the_real_file(self):
        """edit_file replaces a unique string, so the staged copy must start real."""
        staged = self.sandbox.materialize(self.project / "calc.py")
        self.assertIn("a - b", staged.read_text())

    def test_paths_outside_the_project_are_rejected(self):
        with self.assertRaises(SandboxError):
            self.sandbox.materialize(self.project / ".." / ".." / "etc" / "passwd")

    def test_symlinked_root_still_matches(self):
        """macOS hands out /var/... for /private/var/...; that must not read as an escape."""
        rel = self.sandbox._rel(Path(str(self.project) + "/calc.py"))
        self.assertEqual(rel, "calc.py")

    def test_promotion_is_refused_until_tests_pass(self):
        staged = self.sandbox.materialize(self.project / "calc.py")
        self.sandbox.record(self.project / "calc.py")
        staged.write_text("def add(a, b):\n    return a * b\n")

        result = self.sandbox.verify()
        self.assertFalse(result["verified"])

        refused = self.sandbox.promote()
        self.assertIn("error", refused)
        self.assertIn("a - b", (self.project / "calc.py").read_text(),
                      "a failing verify must leave the real file untouched")

    def test_verified_changes_promote_and_keep_a_backup(self):
        staged = self.sandbox.materialize(self.project / "calc.py")
        self.sandbox.record(self.project / "calc.py")
        staged.write_text("def add(a, b):\n    return a + b\n")

        self.assertTrue(self.sandbox.verify()["verified"])
        result = self.sandbox.promote()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["promoted"], ["calc.py"])
        self.assertIn("a + b", (self.project / "calc.py").read_text())
        self.assertTrue((self.project / "calc.py.qh.bak").exists())

    def test_editing_after_a_green_run_invalidates_it(self):
        staged = self.sandbox.materialize(self.project / "calc.py")
        self.sandbox.record(self.project / "calc.py")
        staged.write_text("def add(a, b):\n    return a + b\n")
        self.assertTrue(self.sandbox.verify()["verified"])

        self.sandbox.record(self.project / "calc.py")  # a further edit
        self.assertFalse(self.sandbox.verified, "verification must not survive a later edit")
        self.assertIn("error", self.sandbox.promote())

    def test_verification_is_not_fooled_by_stale_bytecode(self):
        """Two edits of equal size in the same second must not reuse a .pyc.

        CPython invalidates on source mtime+size, and "a * b" / "a + b" are the
        same length — so a second verify could otherwise certify the first
        attempt's bytecode.
        """
        staged = self.sandbox.materialize(self.project / "calc.py")
        self.sandbox.record(self.project / "calc.py")

        staged.write_text("def add(a, b):\n    return a * b\n")
        self.assertFalse(self.sandbox.verify()["verified"])

        staged.write_text("def add(a, b):\n    return a + b\n")
        self.sandbox.record(self.project / "calc.py")
        result = self.sandbox.verify()

        self.assertTrue(result["verified"], f"stale bytecode reused: {self.sandbox.last_test}")

    def test_promotion_refuses_to_write_credential_files(self):
        secret = self.project / ".env"
        staged = self.sandbox.staged_path(secret)
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text("API_KEY=leaked\n")
        self.sandbox.staged[".env"] = "create"
        self.sandbox.verified = True

        result = self.sandbox.promote()

        self.assertEqual(result["failed"][0]["path"], ".env")
        self.assertFalse(secret.exists())

    def test_hydrate_skips_credentials(self):
        (self.project / ".env").write_text("SECRET=1\n")
        self.sandbox.hydrate()
        self.assertFalse((self.sandbox.work / ".env").exists())
        self.assertTrue((self.sandbox.work / "calc.py").exists())


class SandboxProfileTests(unittest.TestCase):
    def test_profile_denies_network_and_credentials(self):
        profile = build_profile(Path("/tmp/whatever"))
        self.assertIn("(deny network*)", profile)
        self.assertIn(".ssh", profile)
        self.assertIn(r"\.env", profile)

    def test_profile_does_not_grant_shared_tmp(self):
        """Allowing /tmp would mean writes are not actually confined."""
        profile = build_profile(Path("/tmp/whatever"))
        self.assertNotIn('(allow file-write* (subpath "/private/tmp"))', profile)

    def test_confinement_is_reported_not_assumed(self):
        mode = confinement()
        self.assertIn(mode["mode"], {"sandbox-exec", "staging-only"})
        if mode["mode"] == "staging-only":
            self.assertFalse(mode["writes_confined"])


@unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists(),
                     "sandbox-exec confinement is macOS-only")
class SandboxConfinementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        self.project = base / "proj"
        self.project.mkdir(parents=True)
        self.sandbox = Sandbox(run_id="c", project_root=self.project, root=base / "sb")
        self.sandbox.work.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_writes_inside_the_sandbox_succeed(self):
        result = self.sandbox.run("echo staged > note.txt && cat note.txt")
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("staged", result["stdout"])

    def test_writes_outside_the_sandbox_are_denied(self):
        escape = Path(self.temp.name) / "escaped.txt"
        self.sandbox.run(f"echo pwned > {escape}")
        self.assertFalse(escape.exists())

    def test_network_is_denied(self):
        result = self.sandbox.run("curl -s --max-time 5 https://example.com")
        self.assertNotEqual(result["exit_code"], 0)

    def test_home_directory_writes_are_denied(self):
        marker = Path.home() / ".qh-sandbox-escape-test"
        try:
            self.sandbox.run(f"echo pwned > {marker}")
            self.assertFalse(marker.exists())
        finally:
            marker.unlink(missing_ok=True)


class TestCommandDetectionTests(unittest.TestCase):
    def test_detects_python_test_suite(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "tests").mkdir()
            self.assertIn("unittest", detect_test_command(root))

    def test_returns_none_when_there_is_nothing_to_run(self):
        with tempfile.TemporaryDirectory() as name:
            self.assertIsNone(detect_test_command(Path(name)))


if __name__ == "__main__":
    unittest.main()
