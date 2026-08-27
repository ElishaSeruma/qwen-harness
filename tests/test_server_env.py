from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = REPO_ROOT / "gui" / "server.py"


def load_server():
    spec = importlib.util.spec_from_file_location("qh_server_under_test", SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PathRepairTests(unittest.TestCase):
    """A Finder-launched app gets PATH=/usr/bin:/bin:/usr/sbin:/sbin.

    Homebrew lives in /opt/homebrew/bin, so without repair `which('ollama')`
    returns None and "Start services" reports it as not installed — the exact
    failure this guards against. It also blinds git, node, npm and every test
    command the sandbox runs.
    """

    # The bare PATH a GUI-launched process is handed. On macOS that is
    # literally what Finder provides; on Windows there is no single such
    # value, so use the two directories that are always present — the point of
    # the test is that repair_path() adds the real install locations back to a
    # minimal PATH, whatever "minimal" means on this OS.
    FINDER_PATH = (
        os.pathsep.join([os.environ.get("SystemRoot", r"C:\Windows") + r"\System32",
                         os.environ.get("SystemRoot", r"C:\Windows")])
        if os.name == "nt" else "/usr/bin:/bin:/usr/sbin:/sbin"
    )

    def setUp(self):
        self.server = load_server()
        self._original_path = os.environ.get("PATH", "")

    def tearDown(self):
        os.environ["PATH"] = self._original_path

    def test_repair_adds_the_usual_binary_locations(self):
        os.environ["PATH"] = self.FINDER_PATH
        repaired = self.server.repair_path()
        self.assertIn(self.FINDER_PATH.split(os.pathsep)[0], repaired)
        for extra in self.server.EXTRA_PATH_DIRS:
            if os.path.isdir(extra):
                self.assertIn(extra, repaired, f"{extra} exists but was not added")

    def test_repair_only_adds_directories_that_exist(self):
        os.environ["PATH"] = self.FINDER_PATH
        for entry in self.server.repair_path().split(os.pathsep):
            self.assertTrue(os.path.isdir(entry), f"added a non-existent dir: {entry}")

    def test_repair_is_idempotent(self):
        os.environ["PATH"] = self.FINDER_PATH
        once = self.server.repair_path()
        twice = self.server.repair_path()
        self.assertEqual(once, twice, "repeated repair must not duplicate entries")

    def test_repair_preserves_existing_entries(self):
        custom = r"C:\some\custom\bin" if os.name == "nt" else "/some/custom/bin"
        os.environ["PATH"] = f"{custom}{os.pathsep}{self.FINDER_PATH}"
        self.assertIn(custom, self.server.repair_path())

    def test_find_ollama_works_without_it_being_on_path(self):
        os.environ["PATH"] = self.FINDER_PATH
        found = self.server.find_ollama()
        if found is None:
            self.skipTest("ollama is not installed on this machine")
        self.assertTrue(os.path.isabs(found))
        self.assertTrue(os.access(found, os.X_OK), "must return an executable path")


@unittest.skipIf(os.name == "nt", "no /usr/bin/python3 on Windows; the desktop "
                                   "app resolves a real python.exe explicitly")
class ServerInterpreterCompatibilityTests(unittest.TestCase):
    def test_server_imports_under_system_python(self):
        """The app spawns whatever `python3` resolves to.

        With the bare PATH a Finder launch provides, that is /usr/bin/python3 —
        Python 3.9 on macOS. A `str | None` annotation there is a hard
        TypeError at import, so the server never starts at all.
        """
        system_python = "/usr/bin/python3"
        if not os.path.exists(system_python):
            self.skipTest("no /usr/bin/python3 on this machine")
        result = subprocess.run(
            [system_python, "-c",
             "import importlib.util,sys;"
             f"sys.path.insert(0, {str(REPO_ROOT)!r});"
             f"spec=importlib.util.spec_from_file_location('s', {str(SERVER_PATH)!r});"
             "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0,
                         f"server.py does not import under {system_python}:\n{result.stderr}")


if __name__ == "__main__":
    unittest.main()
