import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from tariff import band_name, import_price, tariff_schedule

TZ = ZoneInfo("Europe/Bratislava")


class TariffTests(unittest.TestCase):
    def test_band_names_follow_published_hours(self):
        monday = datetime(2026, 10, 26, tzinfo=TZ)
        expected = {2: "night", 6: "peak", 9: "late_morning", 13: "afternoon", 18: "afternoon", 19: "peak", 22: "night"}
        for hour, name in expected.items():
            self.assertEqual(band_name(monday.replace(hour=hour)), name, hour)

    def test_schedule_matches_import_price_and_steps_real_hours_over_dst(self):
        start = datetime(2026, 10, 24, 23, 40, tzinfo=TZ)
        schedule = tariff_schedule(start, 6)

        self.assertEqual(len(schedule), 6)
        self.assertEqual(schedule[0]["time"], "2026-10-24T23:00+02:00")
        times = [item["time"] for item in schedule]
        self.assertIn("2026-10-25T02:00+02:00", times)
        self.assertIn("2026-10-25T02:00+01:00", times)
        first = datetime.fromisoformat(schedule[0]["time"])
        self.assertAlmostEqual(schedule[0]["with_aid_eur_per_kwh"], round(import_price(first, True), 4))
        self.assertAlmostEqual(schedule[0]["without_aid_eur_per_kwh"], round(import_price(first, False), 4))

    def test_schedule_requires_aware_start(self):
        with self.assertRaises(ValueError):
            tariff_schedule(datetime(2026, 10, 26, 12), 2)


if __name__ == "__main__":
    unittest.main()
