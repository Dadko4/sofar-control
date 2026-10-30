import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from station_history import build_daily_history
from tariff import import_price

TZ = ZoneInfo("Europe/Bratislava")


def _day_series(day_start, generation, use, grid, charge):
    series = {"generationPower": [], "usePower": [], "gridPower": [], "chargePower": []}
    for step in range(288):
        moment = day_start + timedelta(minutes=5 * step)
        values = {
            "generationPower": generation(moment),
            "usePower": use(moment),
            "gridPower": grid(moment),
            "chargePower": charge(moment),
        }
        for name, value in values.items():
            series[name].append((moment, value))
    return series


class BuildDailyHistoryTests(unittest.TestCase):
    def setUp(self):
        self.day_start = datetime(2026, 10, 29, tzinfo=TZ)
        self.now = datetime(2026, 10, 29, 23, 59, tzinfo=TZ)

    def _thursday(self, **overrides):
        generation = overrides.get("generation", lambda m: 2.0 if 8 <= m.hour < 20 else 0.0)
        use = overrides.get("use", lambda m: 0.5)
        grid = overrides.get("grid", lambda m: generation(m) - use(m))
        charge = overrides.get("charge", lambda m: 0.0)
        series = _day_series(self.day_start, generation, use, grid, charge)
        return build_daily_history(series, "Europe/Bratislava", self.now, overrides.get("soc", []), days=1)[0]

    def test_daily_totals_integrate_power_and_split_grid_flows(self):
        day = self._thursday()

        self.assertEqual(day["date"], "2026-10-29")
        self.assertEqual(len(day["hours"]), 24)
        totals = day["totals"]
        self.assertAlmostEqual(totals["pv_kwh"], 24.0, places=2)
        self.assertAlmostEqual(totals["consumption_kwh"], 12.0, places=2)
        self.assertAlmostEqual(totals["grid_export_kwh"], 18.0, places=2)
        self.assertAlmostEqual(totals["grid_import_kwh"], 6.0, places=2)

    def test_battery_charge_and_discharge_follow_signed_charge_power(self):
        day = self._thursday(charge=lambda m: 1.0 if m.hour == 13 else (-0.5 if m.hour == 3 else 0.0))

        totals = day["totals"]
        self.assertAlmostEqual(totals["battery_charge_kwh"], 1.0, places=2)
        self.assertAlmostEqual(totals["battery_discharge_kwh"], 0.5, places=2)

    def test_grid_charging_counts_only_charge_covered_by_import(self):
        day = self._thursday(charge=lambda m: 1.0 if m.hour in (3, 13) else 0.0)

        by_hour = {hour["time"][11:13]: hour for hour in day["hours"]}
        self.assertAlmostEqual(by_hour["03"]["grid_charge_kwh"], 0.5, places=3)
        self.assertAlmostEqual(by_hour["13"]["grid_charge_kwh"], 0.0, places=3)
        self.assertAlmostEqual(day["totals"]["grid_charge_kwh"], 0.5, places=2)

    def test_hourly_import_is_priced_with_and_without_energy_aid(self):
        day = self._thursday()

        night = next(hour for hour in day["hours"] if hour["time"].startswith("2026-10-29T23:00"))
        self.assertAlmostEqual(night["grid_import_kwh"], 0.5, places=3)
        self.assertAlmostEqual(night["cost_with_aid_eur"], 0.5 * import_price(datetime(2026, 10, 29, 23, tzinfo=TZ), True), places=4)
        self.assertAlmostEqual(night["cost_without_aid_eur"], 0.5 * import_price(datetime(2026, 10, 29, 23, tzinfo=TZ), False), places=4)

    def test_last_soc_sample_in_an_hour_becomes_soc_end(self):
        soc = [
            (datetime(2026, 10, 29, 10, 20, tzinfo=TZ), 80),
            (datetime(2026, 10, 29, 10, 50, tzinfo=TZ), 85),
        ]
        day = self._thursday(soc=soc)

        ten = next(hour for hour in day["hours"] if hour["time"].startswith("2026-10-29T10:00"))
        self.assertEqual(ten["soc_end"], 85)
        self.assertIsNone(next(hour for hour in day["hours"] if hour["time"].startswith("2026-10-29T11:00"))["soc_end"])

    def test_hours_without_samples_stay_empty_but_keep_tariff(self):
        day = build_daily_history({"usePower": []}, "Europe/Bratislava", self.now, days=1)[0]

        self.assertEqual(len(day["hours"]), 24)
        self.assertIsNone(day["hours"][0]["pv_kwh"])
        self.assertAlmostEqual(day["hours"][0]["tariff_with_aid_eur_per_kwh"], import_price(self.day_start, True), places=4)
        self.assertEqual(day["totals"]["pv_kwh"], 0.0)

    def test_fall_back_day_has_25_local_hours(self):
        now = datetime(2026, 10, 30, 12, 30, tzinfo=TZ)
        days = build_daily_history({"usePower": []}, "Europe/Bratislava", now, days=7)

        self.assertEqual([day["date"] for day in days][0], "2026-10-24")
        self.assertEqual([day["date"] for day in days][-1], "2026-10-30")
        fall_back = next(day for day in days if day["date"] == "2026-10-25")
        self.assertEqual(len(fall_back["hours"]), 25)
        today = days[-1]
        self.assertEqual(len(today["hours"]), 13)

    def test_repeated_fall_back_hour_is_not_merged(self):
        first_0200 = datetime(2026, 10, 25, 0, 0, tzinfo=ZoneInfo("UTC"))
        second_0200 = first_0200 + timedelta(hours=1)
        series = {"usePower": [(first_0200.astimezone(TZ), 1.0), (second_0200.astimezone(TZ), 2.0)]}
        now = datetime(2026, 10, 25, 23, 0, tzinfo=TZ)
        day = build_daily_history(series, "Europe/Bratislava", now, days=1)[0]

        with_data = [hour for hour in day["hours"] if hour["consumption_kwh"] is not None]
        self.assertEqual(len(with_data), 2)
        self.assertEqual([hour["time"][-6:] for hour in with_data], ["+02:00", "+01:00"])
        self.assertTrue(all(hour["time"].startswith("2026-10-25T02:00") for hour in with_data))


if __name__ == "__main__":
    unittest.main()
