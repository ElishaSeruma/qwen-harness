"""Guards on per-agent sandbox isolation and the agent boundary contract.

The isolation half is a regression test for a real bug, not a new feature:
sandboxes used to be keyed on `run_id` alone, so every agent in a run shared
one staging tree. Two agents editing the same file each staged it, the second
silently won, and `changes()` reported one entry as though one edit had
happened. Worse, `record()` clears `verified` globally — so agent B's edit
invalidated agent A's green test run and neither could promote.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools import agents_store
from tools.sandbox import (
    DEFAULT_AGENT_ID,
    Sandbox,
    SandboxError,
    drop_sandbox,
    get_sandbox,
    sandboxes_for_run,
)


class PerAgentSandboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name).resolve() / "proj"
        self.project.mkdir(parents=True)
        (self.project / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        self.run_id = "test-run-isolation"

    def tearDown(self):
        drop_sandbox(self.run_id)
        self.temp.cleanup()

    def test_two_agents_get_separate_trees(self):
        a = get_sandbox(self.run_id, self.project, agent_id="alpha")
        b = get_sandbox(self.run_id, self.project, agent_id="beta")
        self.assertNotEqual(a.root, b.root)
        self.assertNotEqual(a.work, b.work)

    def test_the_same_agent_gets_the_same_tree(self):
        first = get_sandbox(self.run_id, self.project, agent_id="alpha")
        second = get_sandbox(self.run_id, self.project, agent_id="alpha")
        self.assertIs(first, second)

    def test_one_agents_edit_does_not_overwrite_anothers(self):
        """The actual bug. Both agents edit calc.py; each must keep its own."""
        a = get_sandbox(self.run_id, self.project, agent_id="alpha")
        b = get_sandbox(self.run_id, self.project, agent_id="beta")

        a.materialize(self.project / "calc.py").write_text("# alpha\n")
        a.record(self.project / "calc.py")
        b.materialize(self.project / "calc.py").write_text("# beta\n")
        b.record(self.project / "calc.py")

        self.assertEqual(a.staged_path(self.project / "calc.py").read_text(), "# alpha\n")
        self.assertEqual(b.staged_path(self.project / "calc.py").read_text(), "# beta\n")
        # And the real file is still untouched by either.
        self.assertIn("a + b", (self.project / "calc.py").read_text())

    def test_one_agents_edit_does_not_invalidate_anothers_green_run(self):
        """record() clears `verified`. Shared state made that cross-agent."""
        a = get_sandbox(self.run_id, self.project, agent_id="alpha")
        b = get_sandbox(self.run_id, self.project, agent_id="beta")
        a.verified = True

        b.materialize(self.project / "calc.py").write_text("# beta\n")
        b.record(self.project / "calc.py")

        self.assertTrue(a.verified, "another agent's edit must not invalidate this one")
        self.assertFalse(b.verified)

    def test_run_without_an_agent_id_uses_one_default_tree(self):
        """Single-agent runs — every existing caller — must not change shape."""
        plain = get_sandbox(self.run_id, self.project)
        named = get_sandbox(self.run_id, self.project, agent_id=DEFAULT_AGENT_ID)
        self.assertIs(plain, named)

    def test_sandboxes_for_run_lists_every_agent(self):
        get_sandbox(self.run_id, self.project, agent_id="alpha")
        get_sandbox(self.run_id, self.project, agent_id="beta")
        self.assertEqual(
            sorted(box.agent_id for box in sandboxes_for_run(self.run_id)),
            ["alpha", "beta"],
        )

    def test_dropping_one_agent_leaves_the_others(self):
        get_sandbox(self.run_id, self.project, agent_id="alpha")
        get_sandbox(self.run_id, self.project, agent_id="beta")
        drop_sandbox(self.run_id, "alpha")
        self.assertEqual([box.agent_id for box in sandboxes_for_run(self.run_id)], ["beta"])

    def test_a_hostile_run_id_cannot_escape_the_sandbox_root(self):
        """run_id and agent_id come from the client, and promote() copies out
        of whatever tree they name."""
        from tools.sandbox import SANDBOX_ROOT
        box = get_sandbox("../../escape", self.project, agent_id="../../also")
        try:
            self.assertTrue(box.root.resolve().is_relative_to(SANDBOX_ROOT.resolve()))
        finally:
            drop_sandbox("../../escape")


class FileBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        self.project = base / "proj"
        (self.project / "src").mkdir(parents=True)
        (self.project / "docs").mkdir(parents=True)
        self.sandbox = Sandbox(run_id="b", project_root=self.project, root=base / "sb",
                               agent_id="alpha", file_boundary=("src/**", "*.md"))
        self.sandbox.work.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_a_write_inside_the_boundary_is_allowed(self):
        self.sandbox.materialize(self.project / "src" / "main.py")
        self.sandbox.record(self.project / "src" / "main.py")
        self.assertIn("src/main.py", [c["path"].replace("\\", "/") for c in self.sandbox.changes()])

    def test_a_slash_free_pattern_matches_the_basename_at_any_depth(self):
        """gitignore semantics, chosen deliberately: someone who writes `*.md`
        means "markdown files", not "top-level markdown files"."""
        self.sandbox.record(self.project / "README.md")
        self.sandbox.record(self.project / "docs" / "guide.md")   # must not raise

    def test_a_single_star_does_not_cross_a_separator(self):
        """The bug the explicit matcher exists to prevent: fnmatch's `*` does
        cross `/`, so `src/*.py` silently also matched `src/a/b.py`."""
        from tools.sandbox import _boundary_match
        self.assertTrue(_boundary_match("src/b.py", "src/*.py"))
        self.assertFalse(_boundary_match("src/a/b.py", "src/*.py"))

    def test_a_subtree_pattern_includes_the_directory_itself(self):
        from tools.sandbox import _boundary_match
        self.assertTrue(_boundary_match("src", "src/**"))
        self.assertTrue(_boundary_match("src/deep/main.py", "src/**"))
        self.assertFalse(_boundary_match("srcalike/main.py", "src/**"))

    def test_a_write_outside_the_boundary_is_refused(self):
        with self.assertRaises(SandboxError) as caught:
            self.sandbox.record(self.project / "docs" / "notes.txt")
        self.assertIn("file boundary", str(caught.exception))

    def test_the_refusal_happens_before_anything_lands_on_disk(self):
        """record() runs after the write, so enforcing only there would let
        out-of-boundary content exist and merely go untracked."""
        target = self.project / "docs" / "notes.txt"
        with self.assertRaises(SandboxError):
            self.sandbox.materialize(target)
        self.assertFalse(self.sandbox.staged_path(target).exists())

    def test_an_empty_boundary_allows_everything(self):
        """Existing single-agent rosters have no boundary and must not break."""
        box = Sandbox(run_id="c", project_root=self.project,
                      root=Path(self.temp.name) / "sb2")
        box.work.mkdir(parents=True, exist_ok=True)
        box.record(self.project / "docs" / "anything.txt")   # must not raise
        self.assertEqual(len(box.changes()), 1)

    def test_the_boundary_is_reported_in_the_summary(self):
        summary = self.sandbox.summary()
        self.assertEqual(summary["agent_id"], "alpha")
        self.assertEqual(summary["file_boundary"], ["src/**", "*.md"])


class AgentContractStorageTests(unittest.TestCase):
    def test_boundary_accepts_a_list_or_a_textarea_string(self):
        self.assertEqual(agents_store._clean_boundary(["src/**", "tests/**"]),
                         ["src/**", "tests/**"])
        self.assertEqual(agents_store._clean_boundary("src/**\ntests/**, docs/*.md"),
                         ["src/**", "tests/**", "docs/*.md"])

    def test_absolute_patterns_are_stripped_not_kept(self):
        """A project-relative match can never fire against an absolute pattern,
        so keeping one would deny every write for no visible reason."""
        self.assertEqual(agents_store._clean_boundary("/src/**"), ["src/**"])

    def test_junk_becomes_an_empty_boundary(self):
        for junk in (None, 42, {}, "", "   ,  , "):
            self.assertEqual(agents_store._clean_boundary(junk), [])

    def test_clean_fills_the_contract_fields_for_a_legacy_agent(self):
        rows = agents_store._clean([{"id": "a1", "name": "Nova"}])
        self.assertEqual(rows[0]["fileBoundary"], [])
        self.assertEqual(rows[0]["outputFormat"], "")
        self.assertEqual(rows[0]["finishCondition"], "")

    def test_contract_fields_are_length_capped(self):
        rows = agents_store._clean([{"id": "a1", "outputFormat": "x" * 9000}])
        self.assertLessEqual(len(rows[0]["outputFormat"]), agents_store.MAX_CONTRACT_CHARS)

    def test_boundary_pattern_count_is_capped(self):
        rows = agents_store._clean([{"id": "a1", "fileBoundary": [f"d{i}/**" for i in range(200)]}])
        self.assertLessEqual(len(rows[0]["fileBoundary"]), agents_store.MAX_BOUNDARY_PATTERNS)


if __name__ == "__main__":
    unittest.main()
