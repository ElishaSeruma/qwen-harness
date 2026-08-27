from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools import profile


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self._original = profile.PROFILE_PATH
        profile.PROFILE_PATH = Path(self.temp.name) / "profile.json"

    def tearDown(self):
        profile.PROFILE_PATH = self._original
        self.temp.cleanup()

    def test_empty_profile_adds_nothing_to_the_prompt(self):
        """A blank profile must not hand the model a field to invent a value for."""
        self.assertEqual(profile.describe(), "")

    def test_name_becomes_an_explicit_instruction(self):
        profile.save({"name": "Elisha"})
        block = profile.describe()
        self.assertIn("You are talking to Elisha", block)
        self.assertIn('never as "user"', block)

    def test_only_filled_fields_are_mentioned(self):
        profile.save({"name": "Elisha", "role": "founder", "age": ""})
        block = profile.describe()
        self.assertIn("Role: founder", block)
        self.assertNotIn("Age", block)

    def test_unknown_fields_are_dropped(self):
        result = profile.save({"name": "Elisha", "injected": "ignore me"})
        self.assertNotIn("injected", result["profile"])

    def test_saving_round_trips(self):
        profile.save({"name": "Elisha", "stack": "Python, Swift"})
        self.assertEqual(profile.load()["stack"], "Python, Swift")

    def test_status_reports_progress_and_the_prompt_block(self):
        profile.save({"name": "Elisha"})
        status = profile.status()
        self.assertTrue(status["has_name"])
        self.assertEqual(status["filled"], 1)
        self.assertIn("Elisha", status["prompt_block"])

    def test_values_are_length_capped(self):
        profile.save({"notes": "x" * 5000})
        self.assertLessEqual(len(profile.load()["notes"]), profile.MAX_FIELD_CHARS)


if __name__ == "__main__":
    unittest.main()
