from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools import feedback


class FeedbackTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self._original = feedback.RATINGS_PATH
        feedback.RATINGS_PATH = Path(self.temp.name) / "ratings.json"

    def tearDown(self):
        feedback.RATINGS_PATH = self._original
        self.temp.cleanup()

    def add(self, rating, response, prompt="p", **kw):
        return feedback.rate(rating=rating, response=response, prompt=prompt, **kw)


class RatingStorageTests(FeedbackTestCase):
    def test_records_a_rating_with_its_label(self):
        result = self.add(feedback.LOVE, "a great answer")
        self.assertEqual(result["label"], "loved")
        self.assertEqual(feedback.summary()["counts"]["loved"], 1)

    def test_rerating_replaces_rather_than_duplicates(self):
        self.add(feedback.LIKE, "answer", message_id="m1")
        self.add(feedback.DISLIKE, "answer", message_id="m1")
        summary = feedback.summary()
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["counts"]["disliked"], 1)

    def test_unrate_removes_it(self):
        self.add(feedback.LIKE, "answer", message_id="m1")
        feedback.unrate("m1")
        self.assertEqual(feedback.summary()["total"], 0)

    def test_invalid_rating_is_rejected(self):
        self.assertIn("error", feedback.rate(rating=7, response="x"))


class PreferenceLearningTests(FeedbackTestCase):
    """Guidance must stay silent until the evidence justifies it.

    A confident instruction derived from two ratings would quietly steer every
    future turn on noise, and be very hard to notice going wrong.
    """

    def test_says_nothing_without_enough_evidence(self):
        self.add(feedback.LOVE, "short")
        self.add(feedback.DISLIKE, "x" * 4000)
        self.assertEqual(feedback.preference_guidance(), "")

    def test_says_nothing_when_only_one_side_is_rated(self):
        for i in range(6):
            self.add(feedback.LIKE, f"good answer {i}")
        self.assertEqual(feedback.preference_guidance(), "")

    def test_learns_that_shorter_is_preferred(self):
        for i in range(4):
            self.add(feedback.LOVE, f"brief answer {i}")
            self.add(feedback.DISLIKE, "rambling " * 400)
        guidance = feedback.preference_guidance()
        self.assertRegex(guidance, r"Keep answers (concise|brief)")

    def test_does_not_state_an_absurdly_short_target_length(self):
        """A few terse ratings must not become "answer in 32 characters"."""
        for i in range(4):
            self.add(feedback.LOVE, "Done.")
            self.add(feedback.DISLIKE, "rambling " * 400)
        guidance = feedback.preference_guidance()
        self.assertIn("brief and to the point", guidance)
        self.assertNotRegex(guidance, r"around \d{1,2} characters")

    def test_states_a_target_length_when_it_is_a_sane_one(self):
        for i in range(4):
            self.add(feedback.LOVE, "a well-sized answer " * 30)
            self.add(feedback.DISLIKE, "rambling " * 500)
        self.assertRegex(feedback.preference_guidance(), r"around \d{3,} characters")

    def test_learns_that_longer_is_preferred(self):
        for i in range(4):
            self.add(feedback.LOVE, "a thorough explanation " * 200)
            self.add(feedback.DISLIKE, f"nope {i}")
        self.assertIn("fuller answers", feedback.preference_guidance())

    def test_learns_that_tool_backed_answers_are_preferred(self):
        for i in range(4):
            self.add(feedback.LIKE, f"checked it {i}", tools_used=["web_search"])
            self.add(feedback.DISLIKE, f"from memory {i}", tools_used=[])
        self.assertIn("Use tools", feedback.preference_guidance())

    def test_learns_to_drop_hedging(self):
        for i in range(4):
            self.add(feedback.LIKE, f"Done. Result {i}.")
            self.add(feedback.DISLIKE, f"I'm sorry, as an AI I cannot help with {i}.")
        self.assertIn("hedging", feedback.preference_guidance())

    def test_guidance_reports_the_sample_sizes_it_used(self):
        for i in range(4):
            self.add(feedback.LOVE, f"brief {i}")
            self.add(feedback.DISLIKE, "rambling " * 400)
        self.assertIn("4 well-rated", feedback.preference_guidance())


class ExportTests(FeedbackTestCase):
    def test_preference_pairs_need_both_sides_of_the_same_prompt(self):
        self.add(feedback.LOVE, "good", prompt="same question")
        self.add(feedback.DISLIKE, "bad", prompt="same question")
        self.add(feedback.LIKE, "lonely", prompt="unpaired question")

        target = Path(self.temp.name) / "pairs.jsonl"
        result = feedback.export_preference_pairs(target)

        self.assertEqual(result["pairs"], 1)
        line = target.read_text().strip()
        self.assertIn('"chosen": "good"', line)
        self.assertIn('"rejected": "bad"', line)

    def test_sft_export_takes_only_the_best_rated(self):
        self.add(feedback.LOVE, "excellent", prompt="q1")
        self.add(feedback.LIKE, "fine", prompt="q2")
        self.add(feedback.DISLIKE, "bad", prompt="q3")

        target = Path(self.temp.name) / "sft.jsonl"
        result = feedback.export_sft(target)

        self.assertEqual(result["examples"], 1)
        self.assertIn("excellent", target.read_text())


if __name__ == "__main__":
    unittest.main()
