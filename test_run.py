import unittest
import base64
import json
from email.message import Message
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import addinfourl
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta, timezone
from threading import Event, RLock
from zoneinfo import ZoneInfo

from run import (
    TIMEZONE,
    DischargeWatchdog,
    ForecastError,
    HistoryStore,
    ScheduledChargeController,
    SofarCloudClient,
    SofarCloudError,
    TelemetrySampler,
    build_mode_parameters,
    estimate_discharge,
    estimate_daily_consumption,
    estimate_hourly_consumption,
    import_price,
    get_solar_forecast,
    get_scheduled_plan_state,
    load_env_file,
    optimize_day,
    parse_station_status,
    seasonal_daily_consumption,
    sign_sofar_request,
    supports_passive_discharge,
    sync_consumption_history,
    validate_optimization_settings,
)


def sample_tou_rules():
    return [
        {
            "touId": str(index),
            "ruleId": str(index),
            "touEnable": "1" if index < 2 else "0",
            "touStartChargeTimestamp": "22:00" if index == 0 else "16:00",
            "touEndChargeTimestamp": "05:45" if index == 0 else "18:45",
            "touChargeStopSoc": "90" if index == 0 else "60",
            "touChargePower": "900" if index == 0 else "800",
            "touEnableWeek": "1,2,3,4,5,6,7",
            "touStartTimestamp": "01-01",
            "touEndTimestamp": "12-31",
            "touModeControl": "0",
            "touChargeBatteryControl": "0",
        }
        for index in range(4)
    ]


class OptimizationTests(unittest.TestCase):
    @staticmethod
    def sample_forecast(daytime_radiation):
        start = datetime(2026, 10, 23)
        return {
            "location": "Jacovce",
            "hourly": [
                {
                    "time": (start + timedelta(hours=hour)).isoformat(),
                    "shortwave_radiation": (
                        daytime_radiation
                        if 7 <= (start + timedelta(hours=hour)).hour <= 16
                        else 0
                    ),
                }
                for hour in range(72)
            ],
        }

    @staticmethod
    def sample_status(soc=30):
        return {
            "metrics": {"battery_soc": soc},
            "battery_capacity": {"value": 5},
        }

    def test_four_t_tariff_bands_include_transit_and_weekend_discount(self):
        monday = datetime(2026, 10, 26)
        saturday = datetime(2026, 10, 24)

        self.assertEqual(import_price(monday.replace(hour=2), True), 0.117)
        self.assertEqual(import_price(monday.replace(hour=10), True), 0.129)
        self.assertEqual(import_price(monday.replace(hour=15), True), 0.167)
        self.assertEqual(import_price(monday.replace(hour=18), True), 0.167)
        self.assertEqual(import_price(monday.replace(hour=7), True), 0.222)
        self.assertEqual(import_price(monday.replace(hour=20), True), 0.222)
        self.assertEqual(import_price(monday.replace(hour=2), False), 0.156)
        self.assertEqual(import_price(monday.replace(hour=15), False), 0.206)
        self.assertAlmostEqual(import_price(saturday.replace(hour=7), True), 0.222 - 0.12 * (0.222 - 0.052))
        self.assertAlmostEqual(import_price(saturday.replace(hour=2), True), 0.117 - 0.12 * (0.117 - 0.052))

    def test_seasonal_consumption_matches_configured_summer_and_winter(self):
        self.assertAlmostEqual(
            seasonal_daily_consumption(datetime(2026, 1, 15).date(), 6, 25),
            25,
        )
        self.assertAlmostEqual(
            seasonal_daily_consumption(datetime(2026, 7, 16).date(), 6, 25),
            6,
            places=2,
        )

    def test_optimizer_uses_historical_same_weekday_consumption(self):
        target_date = datetime(2026, 10, 24).date()
        with TemporaryDirectory() as directory:
            history = HistoryStore(Path(directory) / "history.sqlite3")
            for days_ago, consumption in ((7, 10), (14, 12), (21, 14)):
                historical_day = target_date - timedelta(days=days_ago)
                for hour in range(24):
                    power_kw = 2.4 if hour in (7, 8, 18, 19) else 0.4
                    for sample in range(7):
                        sampled_at = datetime.combine(
                            historical_day,
                            datetime.min.time(),
                            tzinfo=ZoneInfo(TIMEZONE),
                        ) + timedelta(hours=hour, minutes=sample * 5)
                        history.record_telemetry(
                            {
                                "metrics": {"use_power": power_kw},
                                "daily_energy": {
                                    "consumption": {"value": consumption}
                                },
                            },
                            sampled_at,
                            0.117,
                            0.156,
                        )

            plan = optimize_day(
                self.sample_status(40),
                self.sample_forecast(40),
                7,
                6,
                25,
                True,
                now=datetime(2026, 10, 24, 0, 0, tzinfo=ZoneInfo(TIMEZONE)),
                history_store=history,
            )

        self.assertEqual(plan["consumption_estimate_source"], "same_weekday_history")
        self.assertEqual(plan["consumption_history_days"], 3)
        self.assertEqual(plan["hourly_consumption_profile_source"], "same_weekday_history")
        self.assertEqual(plan["hourly_consumption_profile_days"], 3)
        self.assertAlmostEqual(
            sum(item["consumption_kwh"] for item in plan["hourly_plan"]),
            12,
            places=2,
        )
        hourly_by_time = {
            item["time"][11:13]: item["consumption_kwh"]
            for item in plan["hourly_plan"]
        }
        self.assertGreater(hourly_by_time["07"], hourly_by_time["12"])

    def test_cloudy_forecast_needs_more_grid_charge_than_sunny_forecast(self):
        now = datetime(2026, 10, 23, 10, tzinfo=ZoneInfo(TIMEZONE))
        cloudy = optimize_day(
            self.sample_status(),
            self.sample_forecast(40),
            7,
            6,
            25,
            True,
            now=now,
        )
        sunny = optimize_day(
            self.sample_status(),
            self.sample_forecast(700),
            7,
            6,
            25,
            True,
            now=now,
        )

        def grid_kwh(plan):
            return sum(window["recommended_grid_charge_kwh"] for window in plan["windows"])

        self.assertGreater(grid_kwh(cloudy), grid_kwh(sunny))
        self.assertEqual(len(cloudy["hourly_plan"]), 24)
        self.assertGreaterEqual(cloudy["estimated_savings_eur"], 0)
        self.assertAlmostEqual(
            sum(hour["grid_charge_kwh"] for hour in cloudy["hourly_plan"]),
            grid_kwh(cloudy),
            delta=0.05,
        )

    def test_multi_window_optimizer_includes_next_morning_and_daily_consumption(self):
        start = datetime(2026, 10, 24, tzinfo=timezone.utc)
        forecast = {
            "location": "Jacovce",
            "hourly": [
                {
                    "time": (start + timedelta(hours=hour)).isoformat(),
                    "shortwave_radiation": 0,
                }
                for hour in range(72)
            ],
        }
        plan = optimize_day(
            self.sample_status(soc=50),
            forecast,
            7,
            24,
            24,
            True,
            now=datetime(2026, 10, 24, 20, 47, tzinfo=ZoneInfo(TIMEZONE)),
        )

        times = [datetime.fromisoformat(hour["time"]) for hour in plan["hourly_plan"]]
        self.assertEqual(len(times), 24)
        self.assertEqual(
            sum(moment.date().isoformat() == "2026-10-24" for moment in times),
            3,
        )
        self.assertEqual(
            sum(moment.date().isoformat() == "2026-10-25" for moment in times),
            21,
        )
        self.assertAlmostEqual(
            sum(hour["consumption_kwh"] for hour in plan["hourly_plan"]),
            23.16,
            places=2,
        )
        self.assertIn("cash_savings_eur", plan)
        self.assertEqual(times[-1].strftime("%Y-%m-%d %H:%M"), "2026-10-25 19:00")

    def test_optimizer_supports_rolling_12_and_36_hour_horizons(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        forecast = {
            "location": "Jacovce",
            "hourly": [
                {
                    "time": (start + timedelta(hours=hour)).isoformat(),
                    "shortwave_radiation": 0,
                }
                for hour in range(72)
            ],
        }
        now = datetime(2026, 1, 1, 12, 47, tzinfo=ZoneInfo(TIMEZONE))
        for horizon_hours in (12, 36):
            with self.subTest(horizon_hours=horizon_hours):
                plan = optimize_day(
                    self.sample_status(soc=50),
                    forecast,
                    7,
                    24,
                    24,
                    True,
                    now=now,
                    planning_horizon_hours=horizon_hours,
                )
                self.assertEqual(len(plan["hourly_plan"]), horizon_hours)
                self.assertEqual(plan["planning_horizon_hours"], horizon_hours)
                start_at = datetime.fromisoformat(plan["horizon_start"])
                end_at = datetime.fromisoformat(plan["horizon_end"])
                self.assertEqual(
                    (end_at - start_at).total_seconds(),
                    horizon_hours * 3600,
                )

    def test_optimizer_rolls_start_date_forward_when_next_hour_is_tomorrow(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        forecast = {
            "location": "Jacovce",
            "hourly": [
                {
                    "time": (start + timedelta(hours=hour)).isoformat(),
                    "shortwave_radiation": 0,
                }
                for hour in range(72)
            ],
        }
        now = datetime(2026, 1, 1, 23, 47, tzinfo=ZoneInfo(TIMEZONE))

        plan = optimize_day(
            self.sample_status(soc=50),
            forecast,
            7,
            24,
            24,
            True,
            now=now,
            planning_horizon_hours=12,
        )

        self.assertEqual(plan["plan_date"], "2026-01-02")
        self.assertTrue(plan["horizon_start"].startswith("2026-01-02T00:00"))
        self.assertEqual(len(plan["hourly_plan"]), 12)

    def test_horizon_across_dst_fallback_keeps_every_real_hour(self):
        start = datetime(2026, 10, 24, tzinfo=timezone.utc)
        forecast = {
            "location": "Jacovce",
            "hourly": [
                {
                    "time": (start + timedelta(hours=hour)).isoformat(),
                    "shortwave_radiation": 0,
                }
                for hour in range(72)
            ],
        }
        plan = optimize_day(
            self.sample_status(soc=50),
            forecast,
            7,
            24,
            24,
            True,
            now=datetime(2026, 10, 24, 23, 0, tzinfo=ZoneInfo(TIMEZONE)),
        )

        times = [item["time"] for item in plan["hourly_plan"]]
        self.assertEqual(len(times), 24)
        self.assertEqual(len(set(times)), 24)
        self.assertIn("2026-10-25T02:00+02:00", times)
        self.assertIn("2026-10-25T02:00+01:00", times)

    def test_full_battery_does_not_buy_energy_at_peak_prices(self):
        status = {
            "metrics": {"battery_soc": 100},
            "battery_capacity": {"value": 100},
        }
        forecast_start = datetime(2026, 10, 23, tzinfo=timezone.utc)
        forecast = {
            "location": "Jacovce",
            "hourly": [
                {
                    "time": (forecast_start + timedelta(hours=hour)).isoformat(),
                    "shortwave_radiation": 0,
                }
                for hour in range(72)
            ],
        }
        plan = optimize_day(
            status,
            forecast,
            7,
            1,
            1,
            True,
            now=datetime(2026, 10, 23, 22, tzinfo=ZoneInfo(TIMEZONE)),
        )

        for window in plan["windows"]:
            self.assertLess(window["average_price_eur_per_kwh"], 0.2, window)
        self.assertLessEqual(
            sum(window["recommended_grid_charge_kwh"] for window in plan["windows"]),
            1,
        )

    def test_optimizer_rejects_incomplete_forecast_and_invalid_settings(self):
        now = datetime(2026, 10, 23, 10, tzinfo=ZoneInfo(TIMEZONE))
        with self.assertRaises(ForecastError):
            optimize_day(
                self.sample_status(),
                {"hourly": []},
                7,
                6,
                25,
                True,
                now=now,
            )
        with self.assertRaisesRegex(ValueError, "between 0.1 and 100"):
            validate_optimization_settings(
                {
                    "location": "Jacovce",
                    "pv_capacity_kwp": 0,
                    "summer_consumption_kwh": 6,
                    "winter_consumption_kwh": 25,
                    "energy_aid": True,
                }
            )
        settings = validate_optimization_settings(
            {
                "location": "Jacovce",
                "pv_capacity_kwp": 7,
                "summer_consumption_kwh": 6,
                "winter_consumption_kwh": 25,
                "energy_aid": True,
                "planning_horizon_hours": 36,
            }
        )
        self.assertEqual(settings["planning_horizon_hours"], 36)
        with self.assertRaisesRegex(ValueError, "12, 24 or 36"):
            validate_optimization_settings(
                {
                    "location": "Jacovce",
                    "pv_capacity_kwp": 7,
                    "summer_consumption_kwh": 6,
                    "winter_consumption_kwh": 25,
                    "energy_aid": True,
                    "planning_horizon_hours": 48,
                }
            )

    def test_weather_forecast_resolves_slovak_location_and_hourly_radiation(self):
        with patch(
            "run._open_meteo_json",
            side_effect=[
                {
                    "results": [
                        {
                            "name": "Jacovce",
                            "country": "Slovakia",
                            "country_code": "SK",
                            "latitude": 48.6,
                            "longitude": 18.2,
                        }
                    ]
                },
                {
                    "hourly": {
                        "time": ["2026-10-23T11:00"],
                        "shortwave_radiation": [125.5],
                    }
                },
            ],
        ) as request:
            forecast = get_solar_forecast("Jacovce")

        self.assertEqual(forecast["location"], "Jacovce")
        self.assertEqual(forecast["hourly"][0]["shortwave_radiation"], 125.5)
        self.assertEqual(request.call_count, 2)
        self.assertNotIn("global_tilted_irradiance", request.call_args_list[1].args[0])

    def test_tilted_forecast_requests_panel_plane_irradiance(self):
        with patch(
            "run._open_meteo_json",
            side_effect=[
                {"results": [{"name": "Jacovce", "country_code": "SK", "latitude": 48.6, "longitude": 18.2}]},
                {
                    "hourly": {
                        "time": ["2026-10-23T11:00"],
                        "global_tilted_irradiance": [310.0],
                    }
                },
            ],
        ) as request:
            forecast = get_solar_forecast("Jacovce", tilt_deg=35, azimuth_deg=-90)

        url = request.call_args_list[1].args[0]
        self.assertIn("hourly=global_tilted_irradiance", url)
        self.assertIn("tilt=35", url)
        self.assertIn("azimuth=-90", url)
        self.assertEqual(forecast["hourly"][0]["shortwave_radiation"], 310.0)


class ScheduledPlanStateTests(unittest.TestCase):
    def test_active_multi_window_plan_is_displayed_instead_of_legacy_state(self):
        multi_state = {
            "status": "pending",
            "windows_queue": [{"charge_start": "2026-10-24T22:00+02:00"}],
        }
        legacy_state = {"status": "pending", "plan": {"charge_start": "2026-10-25T05:50+01:00"}}
        with patch("run.MULTI_WINDOW_SCHEDULER") as multi_scheduler, patch(
            "run.SCHEDULED_CHARGE"
        ) as legacy_scheduler:
            multi_scheduler.get_state.return_value = multi_state
            legacy_scheduler.get_state.return_value = legacy_state
            legacy_scheduler.ACTIVE_STATUSES = {"pending", "charging"}

            self.assertEqual(get_scheduled_plan_state(), multi_state)

    def test_legacy_plan_remains_visible_when_multi_window_scheduler_is_idle(self):
        multi_state = {"status": "idle", "windows_queue": []}
        legacy_state = {"status": "pending", "plan": {"charge_start": "2026-10-25T05:50+01:00"}}
        with patch("run.MULTI_WINDOW_SCHEDULER") as multi_scheduler, patch(
            "run.SCHEDULED_CHARGE"
        ) as legacy_scheduler:
            multi_scheduler.get_state.return_value = multi_state
            legacy_scheduler.get_state.return_value = legacy_state
            legacy_scheduler.ACTIVE_STATUSES = {"pending", "charging"}

            self.assertEqual(get_scheduled_plan_state(), legacy_state)


class ScheduledChargeTests(unittest.TestCase):
    @staticmethod
    def make_plan(now):
        return {
            "generated_at": now.isoformat(),
            "charge_start": (now + timedelta(hours=17)).isoformat(),
            "charge_end": (now + timedelta(hours=18)).isoformat(),
            "recommended": True,
            "recommended_target_soc": 80,
            "recommended_grid_charge_kwh": 1.5,
            "estimated_savings_eur": 0.2,
            "plan_date": (now + timedelta(days=1)).date().isoformat(),
        }

    def test_plan_persists_and_switches_to_charge_then_self_use(self):
        now = datetime(2026, 10, 23, 12, tzinfo=ZoneInfo(TIMEZONE))
        client = MagicMock()
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "scheduled-plan.json"
            controller = ScheduledChargeController(
                client, state_path, RLock(), lambda: False
            )
            plan = self.make_plan(now)

            saved = controller.schedule(plan, now=now)
            self.assertEqual(saved["status"], "pending")
            restored = ScheduledChargeController(
                client, state_path, RLock(), lambda: False
            )
            self.assertEqual(restored.get_state()["plan"], plan)

            restored.run_due(now=now + timedelta(hours=17) - timedelta(seconds=1))
            client.set_energy_storage_mode.assert_not_called()
            restored.run_due(now=now + timedelta(hours=17))
            self.assertEqual(restored.get_state()["status"], "charging")
            client.set_energy_storage_mode.assert_called_once_with(
                "grid_charge",
                2000,
                confirmed=True,
                target_soc=80,
            )

            restored.run_due(now=now + timedelta(hours=18))
            self.assertEqual(restored.get_state()["status"], "completed")
            self.assertEqual(
                client.set_energy_storage_mode.call_args_list[-1].args,
                ("self_use",),
            )
            self.assertEqual(
                client.set_energy_storage_mode.call_args_list[-1].kwargs,
                {"confirmed": True},
            )

    def test_active_discharge_blocks_scheduled_charge(self):
        now = datetime(2026, 10, 23, 12, tzinfo=ZoneInfo(TIMEZONE))
        client = MagicMock()
        with TemporaryDirectory() as directory:
            controller = ScheduledChargeController(
                client,
                Path(directory) / "scheduled-plan.json",
                RLock(),
                lambda: True,
            )
            controller.schedule(self.make_plan(now), now=now)
            controller.run_due(now=now + timedelta(hours=17))

        self.assertEqual(controller.get_state()["status"], "failed")
        client.set_energy_storage_mode.assert_not_called()

    def test_rejects_second_plan_while_first_is_active(self):
        now = datetime(2026, 10, 23, 12, tzinfo=ZoneInfo(TIMEZONE))
        first_plan = self.make_plan(now)
        second_plan = self.make_plan(now)
        second_plan["recommended_grid_charge_kwh"] = 2.5
        client = MagicMock()
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "scheduled-plan.json"
            controller = ScheduledChargeController(
                client,
                state_path,
                RLock(),
                lambda: False,
            )
            controller.schedule(first_plan, now=now)

            with self.assertRaisesRegex(RuntimeError, "already active"):
                controller.schedule(second_plan, now=now)

            self.assertEqual(
                controller.get_state()["plan"]["recommended_grid_charge_kwh"],
                first_plan["recommended_grid_charge_kwh"],
            )
            persisted_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted_state["status"], "pending")
            self.assertEqual(
                persisted_state["plan"]["recommended_grid_charge_kwh"],
                first_plan["recommended_grid_charge_kwh"],
            )

        client.set_energy_storage_mode.assert_not_called()

    def test_schedule_rejects_stale_plan(self):
        now = datetime(2026, 10, 23, 12, tzinfo=ZoneInfo(TIMEZONE))
        client = MagicMock()
        with TemporaryDirectory() as directory:
            controller = ScheduledChargeController(
                client,
                Path(directory) / "scheduled-plan.json",
                RLock(),
                lambda: False,
            )
            stale_plan = self.make_plan(now - timedelta(minutes=11))
            with self.assertRaisesRegex(ValueError, "expired"):
                controller.schedule(stale_plan, now=now)

    def test_manual_cancel_prevents_later_scheduled_mode_change(self):
        now = datetime(2026, 10, 23, 12, tzinfo=ZoneInfo(TIMEZONE))
        client = MagicMock()
        with TemporaryDirectory() as directory:
            controller = ScheduledChargeController(
                client,
                Path(directory) / "scheduled-plan.json",
                RLock(),
                lambda: False,
            )
            controller.schedule(self.make_plan(now), now=now)
            controller.cancel()
            controller.run_due(now=now + timedelta(hours=17))

        self.assertEqual(controller.get_state()["status"], "cancelled")
        client.set_energy_storage_mode.assert_not_called()

    def test_interrupted_start_after_window_restores_self_use(self):
        now = datetime(2026, 10, 23, 12, tzinfo=ZoneInfo(TIMEZONE))
        client = MagicMock()
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "scheduled-plan.json"
            controller = ScheduledChargeController(
                client, state_path, RLock(), lambda: False
            )
            controller.schedule(self.make_plan(now), now=now)
            controller._state["status"] = "starting"
            controller._save_state()
            restored = ScheduledChargeController(
                client, state_path, RLock(), lambda: False
            )
            restored.run_due(now=now + timedelta(hours=18))

        self.assertEqual(restored.get_state()["status"], "failed")
        client.set_energy_storage_mode.assert_called_once_with(
            "self_use",
            confirmed=True,
        )


class HistoryStoreTests(unittest.TestCase):
    @staticmethod
    def record_daily_consumption(history, day, hour, consumption):
        history.record_telemetry(
            {"metrics": {}, "daily_energy": {"consumption": {"value": consumption}}},
            datetime.combine(
                day,
                datetime.min.time(),
                tzinfo=ZoneInfo(TIMEZONE),
            ).replace(hour=hour, minute=55 if hour == 23 else 0),
            0.052,
            0.083,
        )

    def test_estimates_from_median_of_same_weekday_days(self):
        target_date = datetime(2026, 10, 24).date()
        with TemporaryDirectory() as directory:
            history = HistoryStore(Path(directory) / "history.sqlite3")
            for days_ago, consumption in ((7, 10), (14, 12), (21, 40), (28, 14)):
                self.record_daily_consumption(
                    history,
                    target_date - timedelta(days=days_ago),
                    23,
                    consumption,
                )

            estimate = estimate_daily_consumption(target_date, 6, 25, history)

        self.assertEqual(estimate["source"], "same_weekday_history")
        self.assertEqual(estimate["sample_days"], 4)
        self.assertEqual(estimate["value_kwh"], 13)

    def test_historical_cloud_points_are_persisted_and_used_for_profile(self):
        target_date = datetime(2026, 10, 24).date()
        with TemporaryDirectory() as directory:
            history = HistoryStore(Path(directory) / "history.sqlite3")
            imported = []
            for days_ago in (7, 14):
                historical_day = target_date - timedelta(days=days_ago)
                for hour in range(24):
                    for sample in range(12):
                        sampled_at = datetime.combine(
                            historical_day,
                            datetime.min.time(),
                            tzinfo=ZoneInfo(TIMEZONE),
                        ) + timedelta(hours=hour, minutes=sample * 5)
                        imported.append(
                            (sampled_at, 2.4 if hour in (7, 8, 18, 19) else 0.4)
                        )
            inserted = history.record_consumption_history(imported)
            daily = history.daily_consumption_history(
                target_date - timedelta(days=15),
                target_date,
            )
            profile = estimate_hourly_consumption(target_date, 24, history)

        self.assertEqual(inserted, 576)
        self.assertEqual(len(daily), 2)
        self.assertEqual(profile["source"], "same_weekday_history")
        self.assertEqual(profile["sample_days"], 2)
        self.assertAlmostEqual(sum(profile["hourly_kwh"]), 24)
        self.assertGreater(profile["hourly_kwh"][7], profile["hourly_kwh"][12])

    def test_cloud_history_sync_is_cached_and_refreshes_incrementally(self):
        now = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
        client = MagicMock()
        client.get_consumption_history.return_value = []
        with TemporaryDirectory() as directory:
            history = HistoryStore(Path(directory) / "history.sqlite3")

            self.assertEqual(sync_consumption_history(client, history, now), 0)
            initial_start, initial_end = client.get_consumption_history.call_args.args
            self.assertEqual(initial_end, now)
            self.assertEqual(initial_start, now - timedelta(days=365))

            self.assertEqual(
                sync_consumption_history(
                    client,
                    history,
                    now + timedelta(hours=12),
                ),
                0,
            )
            self.assertEqual(client.get_consumption_history.call_count, 1)

            self.assertEqual(
                sync_consumption_history(
                    client,
                    history,
                    now + timedelta(days=1),
                ),
                0,
            )
            refresh_start, refresh_end = client.get_consumption_history.call_args.args

        self.assertEqual(refresh_end, now + timedelta(days=1))
        self.assertEqual(refresh_start, now - timedelta(days=2))

    def test_hourly_profile_falls_back_to_uniform_until_two_days_available(self):
        target_date = datetime(2026, 10, 24).date()
        with TemporaryDirectory() as directory:
            history = HistoryStore(Path(directory) / "history.sqlite3")
            historical_day = target_date - timedelta(days=7)
            for hour in range(24):
                for sample in range(6):
                    sampled_at = datetime.combine(
                        historical_day,
                        datetime.min.time(),
                        tzinfo=ZoneInfo(TIMEZONE),
                    ) + timedelta(hours=hour, minutes=sample * 5)
                    history.record_telemetry(
                        {"metrics": {"use_power": 2 if hour == 7 else 0.5}},
                        sampled_at,
                        0.052,
                        0.083,
                    )

            estimate = estimate_hourly_consumption(target_date, 24, history)

        self.assertEqual(estimate["source"], "uniform_estimate")
        self.assertEqual(estimate["sample_days"], 0)
        self.assertEqual(estimate["hourly_kwh"], [1] * 24)

    def test_falls_back_to_recent_complete_days_then_seasonal_estimate(self):
        target_date = datetime(2026, 10, 23).date()
        with TemporaryDirectory() as directory:
            history = HistoryStore(Path(directory) / "history.sqlite3")
            for days_ago, consumption in ((1, 10), (2, 12), (3, 14)):
                self.record_daily_consumption(
                    history,
                    target_date - timedelta(days=days_ago),
                    23,
                    consumption,
                )
            self.record_daily_consumption(
                history,
                target_date - timedelta(days=4),
                21,
                100,
            )

            recent_estimate = estimate_daily_consumption(
                target_date,
                6,
                25,
                history,
            )
            seasonal_estimate = estimate_daily_consumption(target_date, 6, 25)

        self.assertEqual(recent_estimate["source"], "recent_days_history")
        self.assertEqual(recent_estimate["sample_days"], 3)
        self.assertEqual(recent_estimate["value_kwh"], 12)
        self.assertEqual(seasonal_estimate["source"], "seasonal_estimate")
        self.assertEqual(seasonal_estimate["sample_days"], 0)

    def test_mode_change_is_logged_by_client(self):
        with TemporaryDirectory() as directory:
            history = HistoryStore(Path(directory) / "history.sqlite3")
            client = SofarCloudClient("user", "password", "plant")
            client.history_store = history

            client._record_mode_event(
                "grid_charge",
                "1",
                {
                    "changed": True,
                    "power_w": 1200,
                    "target_soc": 95,
                    "start_time": "22:00",
                    "end_time": "23:00",
                },
            )
            event = history.recent()["events"][0]

        self.assertEqual(event["event_type"], "mode_change")
        self.assertEqual(event["details"]["mode"], "grid_charge")
        self.assertEqual(event["details"]["power_w"], 1200)

    def test_telemetry_sampler_uses_both_energy_aid_tariffs(self):
        client = MagicMock()
        client.get_status.return_value = {"metrics": {}, "daily_energy": {}}
        history = MagicMock()
        sampler = TelemetrySampler(client, history)
        sampled_at = datetime(2026, 10, 23, 10, tzinfo=ZoneInfo(TIMEZONE))

        sampler.sample_once(sampled_at)

        history.record_telemetry.assert_called_once_with(
            client.get_status.return_value,
            sampled_at,
            0.129,
            0.167,
        )

    def test_records_mode_and_estimated_savings_events(self):
        with TemporaryDirectory() as directory:
            history = HistoryStore(Path(directory) / "history.sqlite3")
            history.record_event(
                "mode_change",
                "Inverter mode changed to Self-use.",
                {"mode": "self_use", "changed": True},
                created_at="2026-10-23T12:00:00+02:00",
            )
            history.record_event(
                "optimization_calculated",
                "Estimated savings €0.18.",
                {"estimated_savings_eur": 0.18, "savings_are_estimated": True},
                created_at="2026-10-23T12:01:00+02:00",
            )

            result = history.recent()

        self.assertEqual(len(result["events"]), 2)
        self.assertEqual(result["events"][0]["event_type"], "optimization_calculated")
        self.assertTrue(result["events"][0]["details"]["savings_are_estimated"])
        self.assertEqual(result["events"][1]["details"]["mode"], "self_use")

    def test_telemetry_records_import_delta_and_tariff_cost(self):
        with TemporaryDirectory() as directory:
            history = HistoryStore(Path(directory) / "history.sqlite3")
            sampled_at = datetime(2026, 10, 23, 12, tzinfo=ZoneInfo(TIMEZONE))
            status = {
                "data_time": "1792756800",
                "metrics": {
                    "generation_power": 1.2,
                    "use_power": 0.8,
                    "battery_soc": 72,
                    "charge_power": 0.1,
                    "discharge_power": 0,
                    "grid_power": 0.05,
                },
                "battery_capacity": {"value": 5},
                "daily_energy": {
                    "generation": {"value": 2.0},
                    "consumption": {"value": 1.5},
                    "grid_import": {"value": 4.0},
                    "grid_export": {"value": 0.3},
                    "battery_charge": {"value": 0.5},
                    "battery_discharge": {"value": 0.1},
                },
            }
            history.record_telemetry(status, sampled_at, 0.061, 0.092)
            status["daily_energy"]["grid_import"]["value"] = 4.25
            history.record_telemetry(
                status,
                sampled_at + timedelta(minutes=5),
                0.061,
                0.092,
            )

            latest = history.recent()["telemetry"][0]

        self.assertEqual(latest["battery_soc"], 72)
        self.assertEqual(latest["daily_grid_import_kwh"], 4.25)
        self.assertAlmostEqual(latest["observed_grid_import_delta_kwh"], 0.25)
        self.assertAlmostEqual(
            latest["observed_grid_import_cost_with_aid_eur"],
            0.01525,
        )
        self.assertAlmostEqual(
            latest["observed_grid_import_cost_without_aid_eur"],
            0.023,
        )


class SofarCloudClientTests(unittest.TestCase):
    def test_fetches_historical_consumption_with_payload_signature(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        client._access_token = "test-token"
        client._token_expires_at = 9999999999
        start_time = datetime(2026, 10, 22, tzinfo=timezone.utc)
        end_time = datetime(2026, 10, 23, tzinfo=timezone.utc)
        point_time = int(datetime(2026, 10, 22, 8, tzinfo=timezone.utc).timestamp())
        response = {
            "code": "0",
            "data": {
                "curves": [
                    {
                        "storageName": "usePower",
                        "unit": "kW",
                        "detailList": [
                            {"collectionTime": str(point_time), "value": 1.25},
                            {"collectionTime": "invalid", "value": 2},
                        ],
                    }
                ]
            },
        }

        with patch.object(client, "_request_json", return_value=response) as request:
            points = client.get_consumption_history(start_time, end_time)

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0][0].astimezone(timezone.utc).timestamp(), point_time)
        self.assertEqual(points[0][1], 1.25)
        args = request.call_args
        self.assertEqual(args.args[:3], ("POST", "/api/device/stationInfo/selectStationHistoryData", "station history"))
        self.assertEqual(args.kwargs["payload"]["paramList"], ["usePower"])
        self.assertEqual(
            args.kwargs["headers"]["sign"],
            sign_sofar_request(args.kwargs["payload"]),
        )

    def test_status_maps_station_realtime_values(self):
        result = parse_station_status(
            {
                "stationName": "Jacovce",
                "stationState": 2,
                "stationBatteryCapacity": 5,
                "stationBatteryCapacityUnit": "kWh",
                "stationRealTimeVo": {
                    "generationPower": 1.25,
                    "usePower": 0.8,
                    "batterySoc": 72,
                    "batteryPower": -0.4,
                    "chargePower": 0,
                    "disChargePower": -0.4,
                    "gridPower": 0.05,
                    "dataTime": "1792669677",
                    "generationValue": 2.7,
                    "generationValueUnit": "kWh",
                    "useValue": 4.3,
                    "useValueUnit": "kWh",
                    "gridValue": 0.2,
                    "gridValueUnit": "kWh",
                    "buyValue": 4.4,
                    "buyValueUnit": "kWh",
                    "chargeValue": 3.6,
                    "chargeValueUnit": "kWh",
                    "disChargeValue": 1.4,
                    "disChargeValueUnit": "kWh",
                },
            },
            "HYD 5KTL-3PH",
        )

        self.assertEqual(result["station_name"], "Jacovce")
        self.assertEqual(result["model"], "HYD 5KTL-3PH")
        self.assertEqual(result["battery_capacity"], {"value": 5, "unit": "kWh"})
        self.assertEqual(
            result["metrics"],
            {
                "generation_power": 1.25,
                "use_power": 0.8,
                "battery_soc": 72,
                "battery_power": -0.4,
                "charge_power": 0,
                "discharge_power": -0.4,
                "grid_power": 0.05,
            },
        )
        self.assertEqual(result["data_time"], "1792669677")
        self.assertEqual(
            result["daily_energy"],
            {
                "generation": {"value": 2.7, "unit": "kWh"},
                "consumption": {"value": 4.3, "unit": "kWh"},
                "grid_export": {"value": 0.2, "unit": "kWh"},
                "grid_import": {"value": 4.4, "unit": "kWh"},
                "battery_charge": {"value": 3.6, "unit": "kWh"},
                "battery_discharge": {"value": 1.4, "unit": "kWh"},
            },
        )

    def test_status_logs_in_and_reads_station_detail(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        responses = [
            {"code": "0", "data": {"accessToken": "test-token"}},
            {
                "code": "0",
                "data": {
                    "stationName": "Test plant",
                    "stationRealTimeVo": {"batterySoc": 55},
                },
            },
        ]

        with patch.object(client, "_request_json", side_effect=responses) as request:
            status = client.get_status()

        self.assertEqual(status["station_name"], "Test plant")
        self.assertEqual(status["metrics"]["battery_soc"], 55)
        self.assertEqual(
            request.call_args_list[0].args[:3],
            ("POST", "/api/user/auth/he/login", "login"),
        )
        self.assertEqual(
            request.call_args_list[1].args[:3],
            (
                "POST",
                "/api/device/stationInfo/selectStationDetail?stationId=plant-123",
                "station detail",
            ),
        )
        self.assertEqual(request.call_args_list[1].args[2], "station detail")
        self.assertEqual(request.call_args_list[1].kwargs["headers"]["authorization"], "test-token")
        self.assertEqual(request.call_args_list[1].kwargs["headers"]["request-from"], "app")
        self.assertEqual(request.call_args_list[1].kwargs["payload"], {})

    def test_missing_credentials_fail_without_network_access(self):
        client = SofarCloudClient("", "", "plant-123")

        with patch.object(client, "_request_json") as request:
            with self.assertRaisesRegex(SofarCloudError, "SOFAR_USERNAME"):
                client.get_status()

        request.assert_not_called()

    def test_missing_plant_id_fails_before_login(self):
        client = SofarCloudClient("test@example.com", "test-password", "")

        with patch.object(client, "_request_json") as request:
            with self.assertRaisesRegex(SofarCloudError, "SOFAR_PLANT_ID") as raised:
                client.get_status()

        self.assertEqual(raised.exception.stage, "configuration")
        request.assert_not_called()

    def test_non_json_detail_response_includes_safe_diagnostics(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        client._access_token = "test-token"
        client._token_expires_at = 9999999999
        headers = Message()
        headers["Content-Type"] = "text/html; charset=utf-8"
        response = addinfourl(
            BytesIO(b"<html><body>login required</body></html>"),
            headers,
            "https://example.invalid",
            code=200,
        )

        with patch("run.urlopen", return_value=response):
            with self.assertRaises(SofarCloudError) as raised:
                client.get_status()

        self.assertEqual(raised.exception.stage, "station detail")
        self.assertEqual(raised.exception.http_status, 200)
        self.assertEqual(raised.exception.content_type, "text/html; charset=utf-8")
        self.assertEqual(raised.exception.response_format, "html")
        self.assertNotIn("login required", str(raised.exception))

    def test_env_file_loads_values_without_overriding_environment(self):
        with TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "SOFAR_TEST_FROM_FILE='file value'\n"
                "SOFAR_TEST_FROM_ENV=file value\n"
                "# ignored comment\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"SOFAR_TEST_FROM_ENV": "existing value"}):
                load_env_file(env_path)
                import os

                self.assertEqual(os.environ["SOFAR_TEST_FROM_FILE"], "file value")
                self.assertEqual(os.environ["SOFAR_TEST_FROM_ENV"], "existing value")
                os.environ.pop("SOFAR_TEST_FROM_FILE", None)

    def test_login_rejects_unsuccessful_api_response(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")

        with patch.object(client, "_request_json", return_value={"code": "666666"}):
            with self.assertRaisesRegex(SofarCloudError, "login failed") as raised:
                client.get_status()

        self.assertEqual(raised.exception.stage, "login")
        self.assertEqual(raised.exception.api_code, "666666")


    def test_api_error_response_does_not_expose_credentials(self):
        client = SofarCloudClient("private-user", "private-password", "plant-123")

        with patch.object(
            client,
            "_request_json",
            return_value={"code": "101021", "message": "private-user is invalid"},
        ):
            with self.assertRaises(SofarCloudError) as raised:
                client.get_status()

        self.assertEqual(
            raised.exception.as_dict(),
            {
                "error": "SofarCloud login failed.",
                "stage": "login",
                "api_code": "101021",
            },
        )

    def test_charge_mode_parameters_are_today_only_and_soc_capped(self):
        from datetime import date

        params = build_mode_parameters(
            "grid_charge",
            2000,
            date(2026, 10, 22),
            "11:16",
            sample_tou_rules(),
        )

        self.assertEqual(params["mode"], "1")
        rules = params["energyStoreRuleList"]
        self.assertEqual(len(rules), 4)
        self.assertEqual(rules[0]["touEnable"], "1")
        self.assertEqual(rules[0]["touChargeStopSoc"], "95")
        self.assertEqual(rules[0]["touChargePower"], "2000")
        self.assertEqual(rules[0]["touStartChargeTimestamp"], "11:16")
        self.assertEqual(rules[0]["touEndChargeTimestamp"], "23:59")
        self.assertEqual(rules[0]["touStartTimestamp"], "10-22")
        self.assertEqual(rules[0]["touEndTimestamp"], "10-22")
        self.assertEqual(rules[0]["touEnableWeek"], "1,2,3,4,5,6,7")
        self.assertEqual([rule["touEnable"] for rule in rules[1:]], ["0", "0", "0"])
        self.assertEqual(sample_tou_rules()[0]["touEnable"], "1")

    def test_charge_plan_supports_custom_target_and_overnight_window(self):
        from datetime import date

        params = build_mode_parameters(
            "grid_charge",
            2000,
            date(2026, 10, 23),
            "22:00",
            sample_tou_rules(),
            target_soc=76,
            end_time="06:00",
            end_date=date(2026, 10, 24),
        )
        rule = params["energyStoreRuleList"][0]

        self.assertEqual(rule["touChargeStopSoc"], "76")
        self.assertEqual(rule["touStartChargeTimestamp"], "22:00")
        self.assertEqual(rule["touEndChargeTimestamp"], "06:00")
        self.assertEqual(rule["touStartTimestamp"], "10-23")
        self.assertEqual(rule["touEndTimestamp"], "10-24")

    def test_charge_plan_rejects_invalid_target_and_overnight_dates(self):
        from datetime import date

        for target_soc, end_date in (
            (30, date(2026, 10, 24)),
            (96, date(2026, 10, 24)),
            (75, date(2026, 10, 23)),
            (75, date(2026, 10, 25)),
        ):
            with self.subTest(target_soc=target_soc, end_date=end_date):
                with self.assertRaises(ValueError):
                    build_mode_parameters(
                        "grid_charge",
                        2000,
                        date(2026, 10, 23),
                        "22:00",
                        sample_tou_rules(),
                        target_soc=target_soc,
                        end_time="06:00",
                        end_date=end_date,
                    )

    def test_charge_power_is_limited_to_two_kw(self):
        for power in (0, -1, 2001, True, 1000.5):
            with self.subTest(power=power):
                with self.assertRaisesRegex(ValueError, "between 1 and 2000 W"):
                    build_mode_parameters("grid_charge", power)

    def test_charge_plan_rejects_invalid_start_time_and_missing_rules(self):
        for start_time, rules in (("25:99", sample_tou_rules()), ("11:30", None)):
            with self.subTest(start_time=start_time, rules=rules):
                with self.assertRaises(ValueError):
                    build_mode_parameters(
                        "grid_charge",
                        2000,
                        start_time=start_time,
                        current_rules=rules,
                    )

    def test_discharge_estimate_uses_configured_capacity_and_soc_floor(self):
        estimate = estimate_discharge(73, 2500, 5)

        self.assertEqual(estimate["energy_kwh"], 2.15)
        self.assertEqual(estimate["duration_seconds"], 3096)
        self.assertEqual(estimate["duration_minutes"], 51.6)

    def test_discharge_estimate_rejects_soc_at_floor_and_invalid_power(self):
        for soc, power in ((30, 2500), (float("nan"), 2500), (73, 2501), (73, 99)):
            with self.subTest(soc=soc, power=power):
                with self.assertRaises(ValueError):
                    estimate_discharge(soc, power, 5)

    def test_passive_discharge_is_supported_only_for_known_model_and_fields(self):
        fields = ["passiveManualBup", "passiveManualBlo"]

        self.assertTrue(supports_passive_discharge("HYD 5-20KTL-3PH", fields))
        self.assertFalse(supports_passive_discharge("HYD 5KTL-3PH", fields))
        self.assertFalse(supports_passive_discharge("HYD 5-20KTL-3PH", fields[:1]))

    def test_discharge_mode_parameters_use_negative_passive_power(self):
        params = build_mode_parameters("grid_discharge", 2500)

        self.assertEqual(
            params,
            {
                "mode": "3",
                "passiveManualBup": -2500,
                "passiveManualBlo": -2500,
            },
        )

    def test_passive_discharge_power_is_limited(self):
        for power in (99, 2501, True, 1000.5):
            with self.subTest(power=power):
                with self.assertRaisesRegex(ValueError, "between 100 and 2500 W"):
                    build_mode_parameters("grid_discharge", power)

    def test_request_sign_uses_only_scalar_fields(self):
        self.assertEqual(
            sign_sofar_request({"a": "value", "nested": {"secret": "excluded"}}),
            sign_sofar_request({"a": "value"}),
        )
        self.assertEqual(
            sign_sofar_request({"b": 2, "a": "value"}),
            sign_sofar_request({"a": "value", "b": 2}),
        )

    def test_mode_change_requires_confirmation_before_network_access(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        with patch.object(client, "get_energy_storage_schema") as schema:
            with self.assertRaisesRegex(SofarCloudError, "Confirm the mode change"):
                client.set_energy_storage_mode("grid_charge", 2000)
        schema.assert_not_called()

    def test_self_use_request_is_idempotent_when_already_active(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        schema = {
            "modes": [{"value": "0"}, {"value": "1"}],
            "fields": [],
        }
        with patch.object(client, "get_energy_storage_schema", return_value=schema), \
                patch.object(client, "read_energy_storage_mode", return_value={"mode": "0"}), \
                patch.object(client, "_execute_command") as command:
            result = client.set_energy_storage_mode("self_use", confirmed=True)

        self.assertEqual(result, {"mode": "0", "verified": True, "changed": False})
        command.assert_not_called()

    def test_self_use_transition_is_sent_and_verified(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        client._access_token = "test-token"
        client._token_expires_at = 9999999999
        schema = {
            "modes": [{"value": "0"}, {"value": "1"}],
            "fields": [],
        }
        socket_client = MagicMock()
        socket_client.connected = True
        command = MagicMock(return_value={"result": 1})

        with patch.object(client, "get_energy_storage_schema", return_value=schema), \
                patch.object(
                    client,
                    "read_energy_storage_mode",
                    side_effect=[{"mode": "1"}, {"mode": "0"}],
                ), \
                patch("run.socketio.Client", return_value=socket_client), \
                patch.object(client, "_execute_command", command):
            result = client.set_energy_storage_mode("self_use", confirmed=True)

        payload = command.call_args.args[0]
        self.assertEqual(payload["params"], {"mode": "0"})
        self.assertEqual(payload["originValue"], {"mode": "1"})
        self.assertEqual(payload["updateValue"], {"mode": "0"})
        self.assertEqual(result["mode"], "0")
        self.assertTrue(result["verified"])
        self.assertTrue(result["changed"])
        self.assertIsNone(result["power_w"])
        self.assertIsNone(result["target_soc"])
        self.assertIsNone(result["start_time"])
        socket_client.connect.assert_called_once()
        socket_client.disconnect.assert_called_once()

    def test_charge_is_blocked_at_target_soc_before_any_write(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        schema = {
            "modes": [{"value": "0"}, {"value": "1"}],
            "fields": [
                "touEnable",
                "touChargeStopSoc",
                "touChargePower",
                "touStartChargeTimestamp",
                "touEndChargeTimestamp",
                "touStartTimestamp",
                "touEndTimestamp",
                "touEnableWeek",
            ],
        }
        with patch.object(client, "get_energy_storage_schema", return_value=schema), \
                patch.object(
                    client,
                    "get_status",
                    return_value={"metrics": {"battery_soc": 95}},
                ), \
                patch.object(client, "read_energy_storage_mode") as read_mode, \
                patch.object(client, "_execute_command") as command:
            with self.assertRaisesRegex(SofarCloudError, "at or above the 95%"):
                client.set_energy_storage_mode("grid_charge", 2000, confirmed=True)

        read_mode.assert_not_called()
        command.assert_not_called()

    def test_charge_is_blocked_when_soc_is_not_finite(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        schema = {
            "modes": [{"value": "0"}, {"value": "1"}],
            "fields": [
                "touEnable",
                "touChargeStopSoc",
                "touChargePower",
                "touStartChargeTimestamp",
                "touEndChargeTimestamp",
                "touStartTimestamp",
                "touEndTimestamp",
                "touEnableWeek",
            ],
        }
        with patch.object(client, "get_energy_storage_schema", return_value=schema), \
                patch.object(
                    client,
                    "get_status",
                    return_value={"metrics": {"battery_soc": "nan"}},
                ), \
                patch.object(client, "read_energy_storage_mode") as read_mode:
            with self.assertRaisesRegex(SofarCloudError, "SOC is invalid"):
                client.set_energy_storage_mode("grid_charge", 2000, confirmed=True)

        read_mode.assert_not_called()

    def test_charge_sends_bounded_today_only_schedule_after_confirmation(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        client._access_token = "test-token"
        client._token_expires_at = 9999999999
        schema = {
            "modes": [{"value": "0"}, {"value": "1"}],
            "fields": [
                "touEnable",
                "touChargeStopSoc",
                "touChargePower",
                "touStartChargeTimestamp",
                "touEndChargeTimestamp",
                "touStartTimestamp",
                "touEndTimestamp",
                "touEnableWeek",
            ],
        }
        socket_client = MagicMock()
        socket_client.connected = True
        command = MagicMock(return_value={"result": 1})
        mode_reads = []

        def read_mode():
            if not mode_reads:
                mode_reads.append(True)
                return {"mode": "0"}
            updated_rules = command.call_args.args[0]["updateValue"]["energyStoreRuleList"]
            return {"mode": "1", "energyStoreRuleList": updated_rules}

        with patch.object(client, "get_energy_storage_schema", return_value=schema), \
                patch.object(
                    client,
                    "get_status",
                    return_value={"metrics": {"battery_soc": 24}},
                ), \
                patch.object(
                    client,
                    "read_energy_storage_mode",
                    side_effect=read_mode,
                ), \
                patch("run.socketio.Client", return_value=socket_client), \
                patch.object(client, "_execute_command", command):
            result = client.set_energy_storage_mode(
                "grid_charge",
                2000,
                confirmed=True,
            )

        params = command.call_args.args[0]["params"]
        charge_rule = params["energyStoreRuleList"][0]
        self.assertEqual(params["mode"], "1")
        self.assertEqual(charge_rule["touChargeStopSoc"], "95")
        self.assertEqual(charge_rule["touChargePower"], "2000")
        self.assertEqual(charge_rule["touStartTimestamp"], charge_rule["touEndTimestamp"])
        self.assertRegex(charge_rule["touStartChargeTimestamp"], r"^\d{2}:\d{2}$")
        self.assertEqual(charge_rule["touEndChargeTimestamp"], "23:59")
        self.assertEqual([rule["touEnable"] for rule in params["energyStoreRuleList"][1:]], ["0", "0", "0"])
        self.assertEqual(command.call_args.args[0]["originValue"], {"mode": "0"})
        self.assertEqual(
            [rule["ruleId"] for rule in params["energyStoreRuleList"]],
            ["0", "1", "2", "3"],
        )
        self.assertEqual(
            [rule["touId"] for rule in params["energyStoreRuleList"]],
            ["0", "0", "0", "0"],
        )
        self.assertEqual(
            [rule["touEnableWeek"] for rule in params["energyStoreRuleList"]],
            ["1,2,3,4,5,6,7"] * 4,
        )
        self.assertEqual(
            [rule["touModeControl"] for rule in params["energyStoreRuleList"]],
            ["0"] * 4,
        )
        self.assertEqual(len(mode_reads), 1)
        self.assertEqual(result["mode"], "1")
        self.assertTrue(result["verified"])
        socket_client.connect.assert_called_once()
        socket_client.disconnect.assert_called_once()

    def test_charge_does_not_replace_unknown_rules_outside_self_use(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        schema = {
            "modes": [{"value": "0"}, {"value": "1"}],
            "fields": [
                "touEnable",
                "touChargeStopSoc",
                "touChargePower",
                "touStartChargeTimestamp",
                "touEndChargeTimestamp",
                "touStartTimestamp",
                "touEndTimestamp",
                "touEnableWeek",
            ],
        }

        with patch.object(client, "get_energy_storage_schema", return_value=schema), \
                patch.object(
                    client,
                    "get_status",
                    return_value={"metrics": {"battery_soc": 24}},
                ), \
                patch.object(
                    client,
                    "read_energy_storage_mode",
                    return_value={"mode": "1"},
                ), \
                patch.object(client, "_execute_command") as command:
            with self.assertRaisesRegex(
                SofarCloudError,
                "did not return its current Time-of-Use rules",
            ):
                client.set_energy_storage_mode(
                    "grid_charge",
                    2000,
                    confirmed=True,
                )

        command.assert_not_called()

    def test_future_overnight_charge_plan_accepts_lower_target_than_current_soc(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        client._access_token = "test-token"
        client._token_expires_at = 9999999999
        tomorrow = datetime.now(ZoneInfo(TIMEZONE)).date() + timedelta(days=1)
        original_rules = sample_tou_rules()
        schema = {
            "modes": [{"value": "0"}, {"value": "1"}],
            "fields": [
                "touEnable",
                "touChargeStopSoc",
                "touChargePower",
                "touStartChargeTimestamp",
                "touEndChargeTimestamp",
                "touStartTimestamp",
                "touEndTimestamp",
                "touEnableWeek",
            ],
        }
        socket_client = MagicMock()
        socket_client.connected = True
        command = MagicMock(return_value={"result": 1})

        def read_mode():
            if command.call_count == 0:
                return {"mode": "0", "energyStoreRuleList": original_rules}
            rules = command.call_args.args[0]["updateValue"]["energyStoreRuleList"]
            return {"mode": "1", "energyStoreRuleList": rules}

        with patch.object(client, "get_energy_storage_schema", return_value=schema), \
                patch.object(
                    client,
                    "get_status",
                    return_value={
                        "metrics": {"battery_soc": 80},
                    },
                ), \
                patch.object(client, "read_energy_storage_mode", side_effect=read_mode), \
                patch("run.socketio.Client", return_value=socket_client), \
                patch.object(client, "_execute_command", command):
            result = client.set_energy_storage_mode(
                "grid_charge",
                2000,
                confirmed=True,
                target_soc=90,
                charge_start_time="04:30",
                charge_end_time="06:00",
                charge_start_date=tomorrow.isoformat(),
                charge_end_date=tomorrow.isoformat(),
            )

        rule = command.call_args.args[0]["params"]["energyStoreRuleList"][0]
        self.assertEqual(rule["touChargeStopSoc"], "90")
        self.assertEqual(rule["touStartChargeTimestamp"], "04:30")
        self.assertEqual(rule["touEndChargeTimestamp"], "06:00")
        self.assertEqual(result["target_soc"], 90)
        self.assertTrue(result["verified"])

    def test_expired_optimization_schedule_is_rejected_before_mode_read_or_write(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        schema = {
            "modes": [{"value": "0"}, {"value": "1"}],
            "fields": [
                "touEnable",
                "touChargeStopSoc",
                "touChargePower",
                "touStartChargeTimestamp",
                "touEndChargeTimestamp",
                "touStartTimestamp",
                "touEndTimestamp",
                "touEnableWeek",
            ],
        }
        yesterday = datetime.now(ZoneInfo(TIMEZONE)).date() - timedelta(days=1)
        with patch.object(client, "get_energy_storage_schema", return_value=schema), \
                patch.object(
                    client,
                    "get_status",
                    return_value={"metrics": {"battery_soc": 80}},
                ), \
                patch.object(client, "read_energy_storage_mode") as read_mode, \
                patch.object(client, "_execute_command") as command:
            with self.assertRaisesRegex(SofarCloudError, "schedule has expired"):
                client.set_energy_storage_mode(
                    "grid_charge",
                    2000,
                    confirmed=True,
                    charge_start_time="04:00",
                    charge_end_time="06:00",
                    charge_start_date=yesterday.isoformat(),
                    charge_end_date=yesterday.isoformat(),
                )

        read_mode.assert_not_called()
        command.assert_not_called()

    def test_charge_is_not_reported_successful_when_schedule_was_ignored(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        client._access_token = "test-token"
        client._token_expires_at = 9999999999
        original_rules = sample_tou_rules()
        schema = {
            "modes": [{"value": "0"}, {"value": "1"}],
            "fields": [
                "touEnable",
                "touChargeStopSoc",
                "touChargePower",
                "touStartChargeTimestamp",
                "touEndChargeTimestamp",
                "touStartTimestamp",
                "touEndTimestamp",
                "touEnableWeek",
            ],
        }
        with patch.object(client, "get_energy_storage_schema", return_value=schema), \
                patch.object(client, "get_status", return_value={"metrics": {"battery_soc": 24}}), \
                patch.object(
                    client,
                    "read_energy_storage_mode",
                    side_effect=[
                        {"mode": "1", "energyStoreRuleList": original_rules},
                        {"mode": "1", "energyStoreRuleList": original_rules},
                    ],
                ), \
                patch("run.socketio.Client"), \
                patch.object(client, "_execute_command", return_value={"result": 1}):
            with self.assertRaisesRegex(
                SofarCloudError,
                "requested charging schedule was not applied",
            ) as raised:
                client.set_energy_storage_mode("grid_charge", 2000, confirmed=True)

        self.assertEqual(raised.exception.stage, "mode verification")

    def test_discharge_is_blocked_at_or_below_the_soc_floor_before_any_write(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        schema = {
            "model": "HYD 5-20KTL-3PH",
            "modes": [{"value": "0"}, {"value": "1"}, {"value": "2"}],
            "fields": ["passiveManualBup", "passiveManualBlo"],
        }
        with patch("run.ENABLE_PASSIVE_DISCHARGE", True), \
                patch.object(client, "get_energy_storage_schema", return_value=schema), \
                patch.object(
                    client,
                    "get_status",
                    return_value={"metrics": {"battery_soc": 30}},
                ), \
                patch.object(client, "read_energy_storage_mode") as read_mode, \
                patch.object(client, "_execute_command") as command:
            with self.assertRaisesRegex(SofarCloudError, "above 30%"):
                client.set_energy_storage_mode(
                    "grid_discharge",
                    2500,
                    confirmed=True,
                )

        read_mode.assert_not_called()
        command.assert_not_called()

    def test_discharge_sends_and_verifies_passive_power_even_when_mode_is_not_listed(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        client._access_token = "test-token"
        client._token_expires_at = 9999999999
        schema = {
            "model": "HYD 5-20KTL-3PH",
            "modes": [{"value": "0"}, {"value": "1"}, {"value": "2"}],
            "fields": ["passiveManualBup", "passiveManualBlo"],
        }
        socket_client = MagicMock()
        socket_client.connected = True
        command = MagicMock(return_value={"result": 1})
        mode_reads = []

        def read_mode():
            if not mode_reads:
                mode_reads.append(True)
                return {"mode": "0"}
            update = command.call_args.args[0]["updateValue"]
            return {
                "mode": "3",
                "passiveManualBup": update["passiveManualBup"],
                "passiveManualBlo": update["passiveManualBlo"],
            }

        with patch("run.ENABLE_PASSIVE_DISCHARGE", True), \
                patch.object(client, "get_energy_storage_schema", return_value=schema), \
                patch.object(
                    client,
                    "get_status",
                    return_value={"metrics": {"battery_soc": 73}},
                ), \
                patch.object(client, "read_energy_storage_mode", side_effect=read_mode), \
                patch("run.socketio.Client", return_value=socket_client), \
                patch.object(client, "_execute_command", command):
            result = client.set_energy_storage_mode(
                "grid_discharge",
                2500,
                confirmed=True,
            )

        params = command.call_args.args[0]["params"]
        self.assertEqual(params["mode"], "3")
        self.assertEqual(params["passiveManualBup"], -2500)
        self.assertEqual(params["passiveManualBlo"], -2500)
        self.assertEqual(command.call_args.args[0]["updateValue"]["mode"], "3")
        self.assertEqual(result["control_mode"], "passive")
        self.assertEqual(result["mode"], "3")
        self.assertEqual(result["target_soc"], 30)
        self.assertEqual(result["start_soc"], 73)
        self.assertEqual(result["estimated_energy_kwh"], 2.15)
        self.assertEqual(result["estimated_duration_minutes"], 51.6)
        self.assertTrue(result["verified"])
        socket_client.connect.assert_called_once()
        socket_client.disconnect.assert_called_once()

    def test_passive_discharge_is_blocked_for_unrecognized_model_before_write(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        schema = {
            "model": "HYD 5KTL-3PH",
            "modes": [{"value": "0"}, {"value": "1"}, {"value": "2"}],
            "fields": ["passiveManualBup", "passiveManualBlo"],
        }
        with patch("run.ENABLE_PASSIVE_DISCHARGE", True), \
                patch.object(client, "get_energy_storage_schema", return_value=schema), \
                patch.object(client, "get_status") as get_status, \
                patch.object(client, "read_energy_storage_mode") as read_mode, \
                patch.object(client, "_execute_command") as command:
            with self.assertRaisesRegex(SofarCloudError, "unavailable"):
                client.set_energy_storage_mode(
                    "grid_discharge",
                    2500,
                    confirmed=True,
                )

        get_status.assert_not_called()
        read_mode.assert_not_called()
        command.assert_not_called()

    def test_passive_discharge_is_disabled_by_default_before_write(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        schema = {
            "model": "HYD 5-20KTL-3PH",
            "modes": [{"value": "0"}, {"value": "1"}, {"value": "2"}],
            "fields": ["passiveManualBup", "passiveManualBlo"],
        }
        with patch("run.ENABLE_PASSIVE_DISCHARGE", False), \
                patch.object(client, "get_energy_storage_schema", return_value=schema), \
                patch.object(client, "get_status") as get_status, \
                patch.object(client, "read_energy_storage_mode") as read_mode, \
                patch.object(client, "_execute_command") as command:
            with self.assertRaisesRegex(SofarCloudError, "disabled"):
                client.set_energy_storage_mode(
                    "grid_discharge",
                    2500,
                    confirmed=True,
                )

        get_status.assert_not_called()
        read_mode.assert_not_called()
        command.assert_not_called()

    def test_passive_discharge_readback_must_match_signed_power(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        self.assertTrue(
            client._passive_discharge_matches(
                {
                    "passiveManualBup": "-2500",
                    "passiveManualBlo": -2500.0,
                },
                {
                    "passiveManualBup": -2500,
                    "passiveManualBlo": -2500,
                },
            )
        )
        self.assertFalse(
            client._passive_discharge_matches(
                {"passiveManualBup": 2500, "passiveManualBlo": -2500},
                {"passiveManualBup": -2500, "passiveManualBlo": -2500},
            )
        )

    def test_discharge_watchdog_returns_to_self_use_at_soc_floor(self):
        watchdog = DischargeWatchdog()
        client = MagicMock()
        client.get_status.return_value = {"metrics": {"battery_soc": 30}}
        deadline = (
            datetime.now(ZoneInfo(TIMEZONE)) + timedelta(minutes=5)
        ).isoformat()

        watchdog._monitor(client, deadline, Event())

        client.set_energy_storage_mode.assert_called_once_with(
            "self_use",
            confirmed=True,
        )
        state = watchdog.get_state()
        self.assertFalse(state["active"])
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["stop_reason"], "target_soc_reached")

    def test_discharge_watchdog_fails_closed_when_soc_cannot_be_read(self):
        watchdog = DischargeWatchdog()
        client = MagicMock()
        client.get_status.side_effect = SofarCloudError(
            "Telemetry unavailable.",
            "station detail",
        )
        deadline = (
            datetime.now(ZoneInfo(TIMEZONE)) + timedelta(minutes=5)
        ).isoformat()

        watchdog._monitor(client, deadline, Event())

        client.set_energy_storage_mode.assert_called_once_with(
            "self_use",
            confirmed=True,
        )
        self.assertEqual(
            watchdog.get_state()["stop_reason"],
            "telemetry_unavailable",
        )


class DashboardAuthenticationTests(unittest.TestCase):
    def test_basic_auth_accepts_configured_credentials(self):
        from run import DashboardHandler

        credentials = base64.b64encode(b"operator:private-password").decode("ascii")
        handler = unittest.mock.Mock()
        handler.headers = {"Authorization": f"Basic {credentials}"}
        with patch("run.WEB_USERNAME", "operator"), patch(
            "run.WEB_PASSWORD", "private-password"
        ):
            self.assertTrue(DashboardHandler._is_authorized(handler))
        handler.send_response.assert_not_called()

    def test_basic_auth_rejects_missing_credentials_with_challenge(self):
        from run import DashboardHandler

        handler = unittest.mock.Mock()
        handler.headers = {}
        with patch("run.WEB_USERNAME", "operator"), patch(
            "run.WEB_PASSWORD", "private-password"
        ):
            self.assertFalse(DashboardHandler._is_authorized(handler))
        handler.send_response.assert_called_once_with(401)
        handler.send_header.assert_any_call("WWW-Authenticate", 'Basic realm="Sofar Opti"')

    def test_non_loopback_bind_requires_authentication(self):
        from run import main

        with patch("run.WEB_HOST", "0.0.0.0"), patch(
            "run.WEB_USERNAME", ""
        ), patch("run.WEB_PASSWORD", ""), patch("run.ThreadingHTTPServer") as server:
            with self.assertRaisesRegex(SystemExit, "required when SOFAR_WEB_HOST"):
                main()
        server.assert_not_called()


if __name__ == "__main__":
    unittest.main()
