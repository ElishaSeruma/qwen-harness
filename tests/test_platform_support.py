"""Guards on the platform abstraction.

Every test here corresponds to something that was actually broken on Windows
before this layer existed, or to a macOS behaviour that must not regress while
fixing Windows. The two are the same suite on purpose: a change that makes
Windows work by quietly changing what macOS does is a regression, not a fix.
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

from tools import platform_support as ps


class ShellTests(unittest.TestCase):
    def test_shell_kind_matches_the_platform(self):
        kind = ps.shell_kind()
        if ps.IS_WINDOWS:
            self.assertIn(kind, {"powershell", "pwsh", "cmd"})
        else:
            self.assertEqual(kind, "sh")

    def test_shell_argv_never_uses_bin_sh_on_windows(self):
        """/bin/sh does not exist on Windows; the sandbox used it unconditionally."""
        argv = ps.shell_argv("echo hi")
        if ps.IS_WINDOWS:
            self.assertNotIn("/bin/sh", argv)
        self.assertGreaterEqual(len(argv), 2)

    def test_a_command_actually_runs(self):
        proc = subprocess.run(ps.shell_argv("echo qh-marker"), capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("qh-marker", proc.stdout)

    def test_exit_codes_survive(self):
        """A shell that always reports 0 makes sandbox_verify meaningless."""
        proc = subprocess.run(ps.shell_argv("exit 7"), capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 7)

    def test_cd_persists_via_the_reported_working_directory(self):
        """The terminal runs each command in a fresh process, so `cd` can only
        persist by the shell reporting where it ended up.

        The cmd form of this is the subtle one: %CD% in a one-liner expands at
        parse time, so it reports where the command *started*.
        """
        marker = "@@QH_CWD@@"
        target = Path(os.environ.get("SystemRoot", "/usr")).resolve()
        command = f'cd "{target}"'
        proc = subprocess.run(
            ps.shell_argv_for_terminal(command, marker),
            cwd=str(Path.home()), capture_output=True, text=True, timeout=30,
        )
        self.assertIn(marker, proc.stdout, proc.stderr)
        reported = proc.stdout.rpartition(marker)[2].strip()
        self.assertEqual(Path(reported).resolve(), target)


class DangerousPatternTests(unittest.TestCase):
    def test_posix_patterns_are_blocked_everywhere(self):
        """Git Bash and WSL are a `bash -c` away on Windows too."""
        self.assertIsNotNone(ps.blocked_pattern("rm -rf /"))
        self.assertIsNotNone(ps.blocked_pattern("sudo reboot"))

    def test_windows_patterns_are_blocked_on_windows(self):
        if not ps.IS_WINDOWS:
            self.skipTest("Windows-only patterns")
        for command in ("rmdir /s /q C:\\", "Remove-Item -Recurse -Force C:\\", "format C:"):
            self.assertIsNotNone(ps.blocked_pattern(command), command)

    def test_matching_is_case_insensitive(self):
        """PowerShell is case-insensitive, so a guard that is not can be walked past."""
        self.assertIsNotNone(ps.blocked_pattern("SUDO rm -rf /"))

    def test_ordinary_commands_pass(self):
        for command in ("git status", "npm test", "python -m pytest", "Get-ChildItem"):
            self.assertIsNone(ps.blocked_pattern(command), command)


class TempTests(unittest.TestCase):
    def test_harness_temp_exists_and_is_writable(self):
        """Path("/tmp/...").mkdir() raised WinError 3 at import time, which took
        down the entire tool registry."""
        directory = ps.harness_temp("qh_test_scratch")
        self.assertTrue(directory.is_dir())
        probe = directory / "probe.txt"
        probe.write_text("ok", encoding="utf-8")
        self.assertEqual(probe.read_text(encoding="utf-8"), "ok")
        probe.unlink()


class PathRepairTests(unittest.TestCase):
    def setUp(self):
        self._original = os.environ.get("PATH", "")

    def tearDown(self):
        os.environ["PATH"] = self._original

    def test_repair_is_idempotent(self):
        once = ps.repair_path()
        self.assertEqual(once, ps.repair_path())

    def test_repair_only_adds_existing_directories(self):
        # Start from a minimal PATH whose entries all exist. repair_path()
        # deliberately never prunes what was already there — a real machine's
        # PATH routinely contains directories that do not exist yet (~/.dotnet
        # /tools before a first tool install), and dropping them would change
        # behaviour for every subprocess the server spawns.
        base = (os.environ.get("SystemRoot", r"C:\Windows") + r"\System32") if ps.IS_WINDOWS else "/usr/bin"
        os.environ["PATH"] = base
        for entry in ps.repair_path().split(os.pathsep):
            if entry:
                self.assertTrue(os.path.isdir(entry), entry)

    def test_repair_preserves_entries_it_did_not_add(self):
        marker = r"C:\qh\nonexistent" if ps.IS_WINDOWS else "/qh/nonexistent"
        os.environ["PATH"] = marker
        self.assertIn(marker, ps.repair_path(),
                      "repair must add, never prune — pruning changes every subprocess's PATH")

    def test_find_binary_resolves_the_running_interpreter(self):
        import sys
        name = Path(sys.executable).stem
        found = ps.find_binary(name)
        if found is None:
            self.skipTest(f"{name} is not on PATH under that name")
        self.assertTrue(os.path.isabs(found))


class PythonCommandTests(unittest.TestCase):
    def test_the_generated_interpreter_command_actually_runs(self):
        """`python3` is a Store redirector stub on Windows, and a quoted path
        without the `&` call operator is a PowerShell parse error. Both made
        every sandbox_verify fail, which blocks every promotion."""
        command = f"{ps.python_command()} -c \"print('qh-python-ok')\""
        proc = subprocess.run(ps.shell_argv(command), capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("qh-python-ok", proc.stdout)


if __name__ == "__main__":
    unittest.main()
