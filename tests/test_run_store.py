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
