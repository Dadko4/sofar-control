import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from planner import BatteryModel, optimize_windows, replay_self_use

TZ = ZoneInfo("Europe/Bratislava")
BATTERY = BatteryModel(
    capacity_kwh=5,
    min_energy_kwh=1.5,
    max_energy_kwh=4.75,
    charge_efficiency=0.92,
    discharge_efficiency=0.95,
)


def tariff(hour):
    if hour >= 22 or hour < 6:
        return 0.052
    if 9 <= hour < 13:
        return 0.061
    if 13 <= hour < 19:
        return 0.092
    return 0.137


def make_hours(start, count, price=tariff, consumption=0.5, solar=0.0):
    hours = []
    for offset in range(count):
        slot = start.astimezone(timezone.utc) + timedelta(hours=offset)
        local = slot.astimezone(TZ)
        hours.append(
            {
                "start": slot,
                "begin": slot,
                "fraction": 1.0,
                "price": price(local.hour),
                "solar_kwh": solar,
                "consumption_kwh": consumption,
            }
        )
    return hours


class OptimizeWindowsTests(unittest.TestCase):
    def test_two_cheap_bands_produce_multiple_windows(self):
        start = datetime(2026, 10, 25, 0, 0, tzinfo=TZ)
        result = optimize_windows(
            make_hours(start, 24), 1.5, BATTERY, terminal_price=0.052, display_tz=TZ
        )

        self.assertGreaterEqual(len(result["windows"]), 2)
        self.assertGreater(result["estimated_savings_eur"], 0)
        starts = [window["charge_start"] for window in result["windows"]]
        self.assertEqual(starts, sorted(starts))
        for window in result["windows"]:
            self.assertLess(window["average_price_eur_per_kwh"], 0.137, window)
            self.assertTrue(31 <= window["recommended_target_soc"] <= 95)
            self.assertGreater(
                window["charge_end"], window["charge_start"]
            )

    def test_flat_price_never_charges_from_grid(self):
        start = datetime(2026, 10, 25, 0, 0, tzinfo=TZ)
        result = optimize_windows(
            make_hours(start, 24, price=lambda hour: 0.1),
            1.5,
            BATTERY,
            terminal_price=0.1,
            display_tz=TZ,
        )

        self.assertEqual(result["windows"], [])
        self.assertEqual(result["estimated_savings_eur"], 0)

    def test_strong_solar_day_skips_midday_charge(self):
        start = datetime(2026, 10, 25, 6, 0, tzinfo=TZ)

        def solar(hour):
            return 2.5 if 9 <= hour < 15 else 0.0

        hours = make_hours(start, 18)
        for slot in hours:
            slot["solar_kwh"] = solar(slot["start"].hour)
        result = optimize_windows(hours, 1.5, BATTERY, 0.052, TZ)

        for window in result["windows"]:
            hour = datetime.fromisoformat(window["charge_start"]).hour
            self.assertFalse(9 <= hour < 15, window)

    def test_partial_first_hour_never_starts_before_now(self):
        now = datetime(2026, 10, 25, 10, 40, tzinfo=TZ)
        hours = make_hours(now.replace(minute=0), 12)
        hours[0]["begin"] = now
        hours[0]["fraction"] = 20 / 60
        hours[0]["consumption_kwh"] *= 20 / 60
        result = optimize_windows(hours, 1.5, BATTERY, 0.052, TZ)

        for window in result["windows"]:
            self.assertGreaterEqual(datetime.fromisoformat(window["charge_start"]), now)

    def test_hourly_plan_reports_both_cost_series(self):
        start = datetime(2026, 10, 25, 0, 0, tzinfo=TZ)
        result = optimize_windows(make_hours(start, 24), 1.5, BATTERY, 0.052, TZ)

        self.assertEqual(len(result["hourly_plan"]), 24)
        self.assertIn("cost_self_use_eur", result["hourly_plan"][0])
        self.assertAlmostEqual(
            sum(item["cost_eur"] for item in result["hourly_plan"]),
            result["cost_with_plan_eur"],
            places=2,
        )


class ReplayTests(unittest.TestCase):
    def test_replay_uses_battery_before_grid(self):
        start = datetime(2026, 10, 25, 0, 0, tzinfo=TZ)
        samples = [
            {
                "start": start + timedelta(minutes=5 * index),
                "hours": 5 / 60,
                "generation_kw": 0,
                "consumption_kw": 1.2,
                "price": 0.1,
            }
            for index in range(12)
        ]
        result = replay_self_use(samples, 4.0, BATTERY)

        self.assertEqual(result["cost_eur"], 0)
        self.assertLess(result["final_energy_kwh"], 4.0)

    def test_replay_buys_grid_when_battery_at_floor(self):
        start = datetime(2026, 10, 25, 0, 0, tzinfo=TZ)
        samples = [
            {
                "start": start,
                "hours": 1.0,
                "generation_kw": 0,
                "consumption_kw": 1.0,
                "price": 0.2,
            }
        ]
        result = replay_self_use(samples, 1.5, BATTERY)

        self.assertAlmostEqual(result["cost_eur"], 0.2)


if __name__ == "__main__":
    unittest.main()
