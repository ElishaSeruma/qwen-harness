"""Guards on the resource monitor's data source.

The whole point of these numbers is that they are measured, not estimated, and
that a field the OS cannot answer comes back as None rather than a plausible
fabrication. Before this module, `sysctl hw.memsize` failed silently on Windows
and every RAM-derived decision — most importantly the context size in
tools/context_budget.py — ran as if the machine had 8 GB.
"""

from __future__ import annotations

import unittest

from tools import sysinfo
from tools.platform_support import IS_WINDOWS


class MemoryTests(unittest.TestCase):
    def test_total_memory_is_a_plausible_machine_size(self):
        total = sysinfo.total_memory_mb()
        self.assertIsNotNone(total, "RAM must be detectable on a supported platform")
        self.assertGreater(total, 500)          # no modern machine has 0.5 GB
        self.assertLess(total, 4 * 1024 * 1024)  # nor 4 TB

    def test_total_memory_gb_agrees_with_mb(self):
        self.assertAlmostEqual(sysinfo.total_memory_gb(), sysinfo.total_memory_mb() / 1024, places=3)

    def test_the_fallback_is_only_used_when_detection_fails(self):
        """A machine with real RAM must never silently report the 8 GB default."""
        if sysinfo.total_memory_mb() is not None:
            self.assertNotEqual(sysinfo.total_memory_gb(default=1.0), 1.0)


class SnapshotTests(unittest.TestCase):
    def test_shape_is_identical_on_every_platform(self):
        snapshot = sysinfo.system_snapshot()
        self.assertEqual(set(snapshot), {"system", "processes"})
        self.assertEqual(
            set(snapshot["system"]),
            {"cpu_pct", "mem_used_mb", "mem_total_mb", "load_avg", "cpu_count"},
        )
        self.assertIsInstance(snapshot["processes"], list)

    def test_cpu_percent_is_a_percentage_once_it_has_a_baseline(self):
        # The Windows reading is a delta between calls, so the first one is
        # honestly None rather than a fake zero.
        sysinfo.system_snapshot()
        cpu = sysinfo.system_snapshot()["system"]["cpu_pct"]
        if cpu is None:
            self.skipTest("no CPU reading available on this platform")
        self.assertGreaterEqual(cpu, 0.0)
        self.assertLessEqual(cpu, 100.0)

    def test_load_average_is_none_on_windows_rather_than_invented(self):
        """Windows has no load average. A number derived from CPU percent would
        put something meaningless in the UI under a label that means something
        specific."""
        if not IS_WINDOWS:
            self.skipTest("POSIX has a real load average")
        self.assertIsNone(sysinfo.system_snapshot()["system"]["load_avg"])

    def test_process_rows_carry_a_pid_and_memory(self):
        for row in sysinfo.system_snapshot()["processes"]:
            self.assertIn("pid", row)
            self.assertIn("mem_mb", row)
            self.assertIn("cpu_pct", row)   # may be None; the key must exist

    def test_an_explicit_total_overrides_the_measured_one(self):
        """server.py reads total RAM once at startup and passes it in; the
        snapshot must not re-measure and disagree with itself."""
        snapshot = sysinfo.system_snapshot(total_mb=1234.5)
        self.assertEqual(snapshot["system"]["mem_total_mb"], 1234.5)


if __name__ == "__main__":
    unittest.main()
