from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools import agents_store


def roster(*nicknames, seq=None):
    return {
        "agents": [{"id": f"agent-{i+1}", "name": n.lower(), "slug": n.lower(),
                    "nickname": n, "modelKey": "qwen3:4b"} for i, n in enumerate(nicknames)],
        "agent_seq": seq or len(nicknames),
    }


class AgentsStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self._original = agents_store.AGENTS_PATH
        agents_store.AGENTS_PATH = Path(self.temp.name) / "agents.json"

    def tearDown(self):
        agents_store.AGENTS_PATH = self._original
        self.temp.cleanup()

    def test_starts_empty(self):
        self.assertFalse(agents_store.get()["has_roster"])

    def test_saves_and_reads_back(self):
        agents_store.save({**roster("Cubee", "Freddy"), "updated_at": 100})
        stored = agents_store.get()
        self.assertTrue(stored["has_roster"])
        self.assertEqual([a["nickname"] for a in stored["agents"]], ["Cubee", "Freddy"])

    def test_a_stale_client_cannot_clobber_a_newer_roster(self):
        """The device you actually edited must win over one that loaded days ago."""
        agents_store.save({**roster("Cubee", "Freddy", "Nova"), "updated_at": 2000})
        result = agents_store.save({**roster("Strobi"), "updated_at": 1000})

        self.assertEqual(result["status"], "stale")
        self.assertEqual(len(agents_store.get()["agents"]), 3)

    def test_force_overrides_the_timestamp_check(self):
        agents_store.save({**roster("Cubee", "Freddy"), "updated_at": 2000})
        result = agents_store.save({**roster("Strobi"), "updated_at": 1, "force": True})

        self.assertEqual(result["status"], "ok")
        self.assertEqual([a["nickname"] for a in agents_store.get()["agents"]], ["Strobi"])

    def test_empty_roster_is_refused(self):
        """An empty roster is a client bug, never an intentional state."""
        self.assertIn("error", agents_store.save({"agents": [], "updated_at": 9999}))

    def test_unknown_fields_are_stripped(self):
        payload = roster("Cubee")
        payload["agents"][0]["evil"] = "x"
        agents_store.save({**payload, "updated_at": 1})
        self.assertNotIn("evil", agents_store.get()["agents"][0])
