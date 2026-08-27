from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from tools.datetime_tool import get_datetime, resolve_timezone


class TimezoneResolutionTests(unittest.TestCase):
    def test_blank_location_uses_the_machines_region(self):
        zone, error = resolve_timezone("")
        self.assertIsNone(error)
        self.assertTrue(zone)
        ZoneInfo(zone)  # must be a loadable IANA name, not an abbreviation

    def test_iana_names_pass_through(self):
        self.assertEqual(resolve_timezone("Europe/London")[0], "Europe/London")

    def test_common_city_names_resolve(self):
        for name, expected in [("tokyo", "Asia/Tokyo"), ("New York", "America/New_York"),
                               ("kampala", "Africa/Kampala"), ("UTC", "UTC")]:
            self.assertEqual(resolve_timezone(name)[0], expected, name)

    def test_unknown_places_error_rather_than_guess(self):
        """A wrong timezone is worse than admitting the place is unknown."""
        zone, error = resolve_timezone("Atlantis")
        self.assertIsNone(zone)
        self.assertIn("Unknown location", error)
        self.assertIn("Do not guess", error)


class DateTimeTests(unittest.TestCase):
    def test_returns_the_real_current_date(self):
        result = get_datetime("UTC")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["date"], datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d"))

    def test_reports_offset_and_dst(self):
        result = get_datetime("Asia/Tokyo")
        self.assertEqual(result["utc_offset"], "+09:00")
        self.assertFalse(result["is_dst"], "Japan does not observe DST")

    def test_day_of_week_matches_the_date(self):
        result = get_datetime("UTC")
        expected = datetime.strptime(result["date"], "%Y-%m-%d").strftime("%A")
        self.assertEqual(result["day_of_week"], expected)

    def test_marks_whether_it_is_the_users_own_region(self):
        self.assertTrue(get_datetime("")["is_local_region"])
        self.assertFalse(get_datetime("UTC")["is_local_region"])

    def test_unknown_location_is_an_error_not_a_fallback_to_local(self):
        result = get_datetime("Narnia")
        self.assertIn("error", result)
        self.assertNotIn("date", result)

    def test_custom_format(self):
        self.assertRegex(get_datetime("UTC", format="%Y")["formatted"], r"^\d{4}$")


if __name__ == "__main__":
    unittest.main()
