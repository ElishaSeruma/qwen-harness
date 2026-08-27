from __future__ import annotations

import unittest

from tools import context_budget as cb


class EstimateTests(unittest.TestCase):
    """Coefficients are fitted to measurements on this project's 8 GB machine.

    qwen3:4b (q4_K_M), resident size read from /api/ps after loading:
        4096 -> 3.17 GB, 16384 -> 5.29 GB, 32768 -> 8.45 GB
    """

    MEASURED = {4096: 3.17, 16384: 5.29, 32768: 8.45}

    def test_reproduces_the_measured_sizes(self):
        for context, measured in self.MEASURED.items():
            predicted = cb.estimate_gb(4.0, context)
            self.assertLess(abs(predicted - measured), 0.6,
                            f"ctx={context}: predicted {predicted:.2f} vs measured {measured}")

    def test_grows_with_context(self):
        self.assertGreater(cb.estimate_gb(4.0, 16384), cb.estimate_gb(4.0, 4096))

    def test_grows_with_model_size(self):
        self.assertGreater(cb.estimate_gb(7.0, 8192), cb.estimate_gb(1.0, 8192))


class RecommendationTests(unittest.TestCase):
    def test_never_goes_below_ollamas_default(self):
        tiny = cb.recommend_context(70.0, 262144, ram_gb=4.0)
        self.assertGreaterEqual(tiny["context"], cb.MIN_CONTEXT)

    def test_respects_the_models_own_maximum(self):
        result = cb.recommend_context(1.0, 8192, ram_gb=64.0)
        self.assertLessEqual(result["context"], 8192)
        self.assertEqual(result["capped_by"], "model")

    def test_fits_within_the_memory_budget(self):
        ram = 8.0
        result = cb.recommend_context(4.0, 262144, ram_gb=ram)
        self.assertLessEqual(result["estimated_gb"], ram - cb.RESERVED_GB + 0.01)

    def test_8gb_machine_gets_more_than_the_4k_default(self):
        """The whole point: 4k cannot hold tool schemas plus a source file."""
        result = cb.recommend_context(4.0, 262144, ram_gb=8.0)
        self.assertGreater(result["context"], 4096)

    def test_a_bigger_machine_gets_more_context(self):
        small = cb.recommend_context(4.0, 262144, ram_gb=8.0)["context"]
        large = cb.recommend_context(4.0, 262144, ram_gb=64.0)["context"]
        self.assertGreater(large, small)

    def test_unknown_model_size_stays_at_the_default(self):
        """Guessing high on an unknown model would push the machine into swap."""
        result = cb.recommend_context(None, None, ram_gb=8.0)
        self.assertEqual(result["context"], cb.MIN_CONTEXT)

    def test_reports_why_it_chose_what_it_did(self):
        result = cb.recommend_context(4.0, 262144, ram_gb=8.0)
        for key in ("reason", "ram_gb", "budget_gb", "estimated_gb", "capped_by"):
            self.assertIn(key, result)


class ParamParsingTests(unittest.TestCase):
    def test_handles_ollamas_mixed_units(self):
        self.assertEqual(cb.parse_param_billions("4.0B"), 4.0)
        self.assertAlmostEqual(cb.parse_param_billions("751.63M"), 0.75163)

    def test_rejects_junk(self):
        for value in (None, "", "banana", 4.0):
            self.assertIsNone(cb.parse_param_billions(value))


class PlanTests(unittest.TestCase):
    def test_plans_the_whole_roster(self):
        models = [
            {"name": "qwen3:4b", "details": {"parameter_size": "4.0B", "context_length": 262144}},
            {"name": "gemma2:2b", "details": {"parameter_size": "2.6B", "context_length": 8192}},
        ]
        plan = cb.plan_for_models(models, ram_gb=8.0)
        self.assertEqual(set(plan["models"]), {"qwen3:4b", "gemma2:2b"})
        self.assertLessEqual(plan["models"]["gemma2:2b"]["context"], 8192)

    def test_skips_entries_without_a_name(self):
        self.assertEqual(cb.plan_for_models([{"details": {}}], ram_gb=8.0)["models"], {})


if __name__ == "__main__":
    unittest.main()
