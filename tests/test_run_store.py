from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gui.run_store import RunStore


class RunStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runs.json"
        self.store = RunStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_cancel_settles_unfinished_subtasks(self):
        """A stopped run must not leave its subtasks spinning as 'running'."""
        run = self.store.create({"surface": "project", "instruction": "create a file"})
        self.store.update(run["id"], {"status": "running", "tasks": [
            {"id": "a", "title": "create the file", "status": "completed"},
            {"id": "b", "title": "verify creation", "status": "running"},
        ]})
        self.store.cancel(run["id"])
        cancelled = self.store.get(run["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(
            [task["status"] for task in cancelled["tasks"]],
            ["completed", "cancelled"],
            "finished subtasks keep their result; unfinished ones are settled",
        )

    def test_stale_updates_cannot_resurrect_a_cancelled_run(self):
        """The browser keeps unwinding after Stop; those writes must not win."""
        run = self.store.create({"surface": "project", "instruction": "x"})
        self.store.update(run["id"], {"status": "running"})
        self.store.cancel(run["id"])
        self.store.update(run["id"], {"status": "running", "tasks": [
            {"id": "a", "title": "still going", "status": "running"},
        ]})
        final = self.store.get(run["id"])
        self.assertEqual(final["status"], "cancelled")
        self.assertEqual([task["status"] for task in final["tasks"]], ["cancelled"])

    def test_terminal_runs_stay_terminal(self):
        run = self.store.create({"surface": "home", "instruction": "x"})
        self.store.update(run["id"], {"status": "completed", "result": "done"})
        self.store.update(run["id"], {"status": "running"})
        self.assertEqual(self.store.get(run["id"])["status"], "completed")

    def test_clear_finished_keeps_runs_still_in_flight(self):
        done = self.store.create({"surface": "home", "instruction": "done"})
        self.store.update(done["id"], {"status": "completed"})
        live = self.store.create({"surface": "home", "instruction": "live"})
        self.store.update(live["id"], {"status": "running"})

        result = self.store.clear_finished()

        self.assertEqual(result, {"cleared": 1, "kept": 1})
        remaining = [r["id"] for r in self.store.list()]
        self.assertEqual(remaining, [live["id"]])

    def test_run_lifecycle_is_durable(self):
        run = self.store.create({"surface": "project", "instruction": "test", "agent_ids": ["a"]})
        self.store.append_event(run["id"], "agent.thinking", {"agent_id": "a"})
        self.store.update(run["id"], {"status": "completed", "result": "done"})
        restored = RunStore(self.path).get(run["id"])
        self.assertEqual(restored["status"], "completed")
        self.assertEqual(restored["result"], "done")
        self.assertGreaterEqual(len(restored["events"]), 3)

    def test_retry_links_attempts(self):
        original = self.store.create({"instruction": "again"})
        retried = self.store.retry(original["id"])
        self.assertEqual(retried["parent_run_id"], original["id"])
        self.assertEqual(retried["attempt"], 2)

    def test_active_run_recovers_as_interrupted(self):
        run = self.store.create({"instruction": "resume"})
        self.store.update(run["id"], {"status": "running"})
        restored = RunStore(self.path).get(run["id"])
        self.assertEqual(restored["status"], "interrupted")

    def test_effort_is_durable_and_clamped(self):
        run = self.store.create({"instruction": "deep work", "effort_level": 3, "effort_source": "manual"})
        self.assertEqual(run["effort_level"], 3)
        self.store.update(run["id"], {"effort_level": 9, "effort_source": "model", "effort_reason": "more verification"})
        restored = RunStore(self.path).get(run["id"])
        self.assertEqual(restored["effort_level"], 4)
        self.assertEqual(restored["effort_source"], "model")
        self.assertEqual(restored["effort_reason"], "more verification")


if __name__ == "__main__":
    unittest.main()
