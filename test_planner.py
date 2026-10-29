import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from planner import BatteryModel, optimize_schedule, replay_self_use

TZ = ZoneInfo("Europe/Bratislava")
BATTERY = BatteryModel(
    capacity_kwh=5,
    min_energy_kwh=1.5,
    max_energy_kwh=4.75,
    charge_efficiency=0.92,
    discharge_efficiency=0.95,
)
START = datetime(2026, 10, 26, 0, 0, tzinfo=TZ).astimezone(timezone.utc)


def tariff(hour):
    if hour >= 22 or hour < 6:
        return 0.117
    if 9 <= hour < 13:
        return 0.129
    if 13 <= hour < 19:
        return 0.167
    return 0.222


def make_slots(count=96, price=tariff, load_kwh=0.225, solar=None, start=START):
    slots = []
    for index in range(count):
        moment = start + timedelta(minutes=15 * index)
        local_hour = moment.astimezone(TZ).hour
        slots.append(
            {
                "start": moment,
                "price": price(local_hour),
                "solar_kwh": solar(local_hour) * 0.25 if solar else 0.0,
                "consumption_kwh": load_kwh,
            }
        )
    return slots


def slot_hour(window, key):
    return datetime.fromisoformat(window[key]).astimezone(TZ).hour


class OptimizeScheduleTests(unittest.TestCase):
    def test_cheap_night_charge_serves_expensive_hours(self):
        result = optimize_schedule(
            make_slots(), 1.5, BATTERY, 0.117 / 0.92 * 0.95, display_tz=TZ
        )

        self.assertGreaterEqual(len(result["windows"]), 1)
        self.assertGreater(result["cash_savings_eur"], 0)
        self.assertGreater(result["estimated_savings_eur"], 0)
        starts = [window["charge_start"] for window in result["windows"]]
        self.assertEqual(starts, sorted(starts))
        for window in result["windows"]:
            self.assertLess(window["average_price_eur_per_kwh"], 0.2, window)
            self.assertTrue(100 <= window["charge_power_w"] <= 2000)
            self.assertEqual(window["charge_power_w"] % 100, 0)
            self.assertTrue(31 <= window["recommended_target_soc"] <= 95)
            self.assertGreater(window["charge_end"], window["charge_start"])

    def test_flat_price_never_charges_from_grid(self):
        result = optimize_schedule(
            make_slots(price=lambda hour: 0.1, load_kwh=0.5),
            4.0,
            BATTERY,
            0.1 * 0.95,
            display_tz=TZ,
        )

        self.assertEqual(result["windows"], [])
        self.assertEqual(result["estimated_savings_eur"], 0)

    def test_hand_built_cheap_then_expensive_charges_during_cheap_slots(self):
        slots = make_slots(
            count=32,
            price=lambda hour: 0.05,
            load_kwh=0.5,
        )
        for slot in slots[16:]:
            slot["price"] = 0.30
        result = optimize_schedule(slots, 1.5, BATTERY, 0.05 / 0.92 * 0.95)

        total_grid = sum(window["recommended_grid_charge_kwh"] for window in result["windows"])
        self.assertAlmostEqual(total_grid, 3.53, delta=0.05)
        for window in result["windows"]:
            self.assertLessEqual(
                datetime.fromisoformat(window["charge_end"]),
                (START + timedelta(hours=4, minutes=1)).astimezone(TZ),
            )
        self.assertGreater(result["cash_savings_eur"], 0.5)

    def test_strong_solar_day_does_not_charge_from_grid_at_midday(self):
        def solar(hour):
            return 2.5 if 9 <= hour < 15 else 0.0

        result = optimize_schedule(
            make_slots(count=72, solar=solar, start=START + timedelta(hours=6)),
            1.5,
            BATTERY,
            0.052,
            display_tz=TZ,
        )

        for window in result["windows"]:
            self.assertFalse(9 <= slot_hour(window, "charge_start") < 15, window)

    def test_charge_never_starts_before_the_horizon(self):
        start = datetime(2026, 10, 25, 10, 15, tzinfo=TZ).astimezone(timezone.utc)
        result = optimize_schedule(
            make_slots(count=48, start=start), 1.5, BATTERY, 0.05, display_tz=TZ
        )

        for window in result["windows"]:
            self.assertGreaterEqual(
                datetime.fromisoformat(window["charge_start"]),
                start.astimezone(TZ),
            )

    def test_hourly_plan_matches_plan_cost_and_reports_self_use(self):
        result = optimize_schedule(make_slots(), 1.5, BATTERY, 0.1, display_tz=TZ)

        self.assertEqual(len(result["hourly_plan"]), 24)
        self.assertIn("cost_self_use_eur", result["hourly_plan"][0])
        self.assertAlmostEqual(
            sum(item["cost_eur"] for item in result["hourly_plan"]),
            result["cost_with_plan_eur"],
            places=2,
        )
        self.assertEqual(len(result["slot_plan"]), 96)

    def test_hold_is_never_worse_than_self_use_only(self):
        slots = make_slots(load_kwh=0.3, solar=lambda hour: 0.6 if 10 <= hour < 15 else 0)
        average_price = sum(tariff(hour) for hour in range(24)) / 24
        plain = optimize_schedule(slots, 2.0, BATTERY, average_price * 0.95)
        with_hold = optimize_schedule(
            slots, 2.0, BATTERY, average_price * 0.95, allow_hold=True
        )

        self.assertGreaterEqual(
            with_hold["estimated_savings_eur"],
            plain["estimated_savings_eur"] - 1e-6,
        )

    def test_discharge_to_grid_only_when_allowed_and_export_pays(self):
        slots = make_slots(count=32, price=lambda hour: 0.1, load_kwh=0.0)
        without = optimize_schedule(
            slots, 4.0, BATTERY, 0.1 * 0.95, export_price_eur_kwh=0.3
        )
        with_discharge = optimize_schedule(
            slots,
            4.0,
            BATTERY,
            0.1 * 0.95,
            export_price_eur_kwh=0.3,
            allow_discharge=True,
        )

        self.assertEqual(without["stored_energy_delta_kwh"], 0)
        self.assertGreater(with_discharge["cash_savings_eur"], 0)
        self.assertTrue(
            any(item["export_kwh"] > 0 for item in with_discharge["hourly_plan"])
        )

    def test_high_battery_wear_stops_arbitrage(self):
        slots = make_slots()
        cheap = optimize_schedule(slots, 1.5, BATTERY, 0.2)
        worn = optimize_schedule(
            slots,
            1.5,
            BatteryModel(
                capacity_kwh=5,
                min_energy_kwh=1.5,
                max_energy_kwh=4.75,
                charge_efficiency=0.92,
                discharge_efficiency=0.95,
                wear_eur_per_kwh=2.0,
            ),
            0.2,
        )

        self.assertGreater(len(cheap["windows"]), 0)
        self.assertEqual(worn["windows"], [])

    def test_dst_fallback_keeps_every_real_hour(self):
        start = datetime(2026, 10, 24, 21, 0, tzinfo=timezone.utc)
        result = optimize_schedule(
            make_slots(count=96, start=start),
            2.0,
            BATTERY,
            0.1,
            display_tz=TZ,
        )

        times = [item["time"] for item in result["hourly_plan"]]
        self.assertEqual(len(times), 24)
        self.assertEqual(len(set(times)), 24)
        self.assertIn("2026-10-25T02:00+02:00", times)
        self.assertIn("2026-10-25T02:00+01:00", times)

    def test_rejects_empty_horizon(self):
        with self.assertRaises(ValueError):
            optimize_schedule([], 1.5, BATTERY, 0.1)


class ReplayTests(unittest.TestCase):
    def test_replay_uses_battery_before_grid(self):
        samples = [
            {
                "start": START + timedelta(minutes=5 * index),
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
        samples = [
            {
                "start": START,
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
