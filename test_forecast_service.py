import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from forecast_model import MODEL_VERSION
from forecast_service import ForecastModelService, predict_hours
from run import TIMEZONE, optimize_day

NOW = datetime(2026, 10, 30, 8, 0, tzinfo=timezone.utc)


def flat_model(trained_at=NOW, location="Jacovce"):
    profile = [1 / 24] * 24
    return {
        "version": MODEL_VERSION,
        "trained_at": trained_at.isoformat(timespec="seconds"),
        "location": {"name": location, "requested": location, "latitude": 48.6, "longitude": 18.15},
        "data": {"measured_hours": 100},
        "radiation_label_offset_hours": 1,
        "pv": {
            "sky_ratio_kwh_per_kwh_m2": {"0": 5.0, "1": 5.0},
            "bins": {},
            "max_hourly_kwh": 10.0,
            "prior_weight_kwh_m2": 2.0,
        },
        "consumption": {
            "intercepts_kwh": {"workday": 12.0, "saturday": 12.0, "sunday": 12.0},
            "heating_kwh_per_degree_day": 0.0,
            "cooling_kwh_per_degree_day": 0.0,
            "heating_base_c": 15.0,
            "cooling_base_c": 24.0,
            "profiles": {"workday": profile, "saturday": profile, "sunday": profile},
            "recent_scale": 1.0,
        },
    }


def weather_forecast(start=datetime(2026, 10, 30, tzinfo=timezone.utc), hours=72, ghi=lambda label: 0.0):
    return {
        "location": "Jacovce",
        "hourly": [
            {
                "time": (start + timedelta(hours=hour)).isoformat(),
                "shortwave_radiation": ghi(start + timedelta(hours=hour)),
                "ghi_wm2": ghi(start + timedelta(hours=hour)),
                "temperature_c": 10.0,
            }
            for hour in range(hours)
        ],
    }


class FakeTrainer:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def __call__(self, location, now):
        self.calls += 1
        if self.fail:
            raise RuntimeError("archive unavailable")
        return flat_model(now, location)


def service(directory, trainer, location="Jacovce"):
    return ForecastModelService(Path(directory) / "forecast_model.json", None, None, None, TIMEZONE, location=location, trainer=trainer)


class RetrainScheduleTests(unittest.TestCase):
    def test_trains_at_startup_then_only_after_seven_days(self):
        with TemporaryDirectory() as directory:
            trainer = FakeTrainer()
            forecast = service(directory, trainer)

            self.assertTrue(forecast.due(NOW))
            self.assertTrue(forecast.train_now(NOW))
            self.assertFalse(forecast.due(NOW + timedelta(days=6, hours=23)))
            self.assertTrue(forecast.due(NOW + timedelta(days=7)))

    def test_retrains_at_startup_even_with_a_recent_saved_model(self):
        with TemporaryDirectory() as directory:
            Path(directory, "forecast_model.json").write_text(json.dumps(flat_model(NOW - timedelta(hours=1))))
            forecast = service(directory, FakeTrainer())

            self.assertIsNotNone(forecast.model())
            self.assertTrue(forecast.due(NOW))

    def test_failed_training_keeps_saved_model_and_backs_off(self):
        with TemporaryDirectory() as directory:
            Path(directory, "forecast_model.json").write_text(json.dumps(flat_model(NOW - timedelta(days=3))))
            forecast = service(directory, FakeTrainer(fail=True))

            self.assertFalse(forecast.train_now(NOW))
            self.assertEqual(forecast.model()["trained_at"], (NOW - timedelta(days=3)).isoformat(timespec="seconds"))
            self.assertFalse(forecast.due(NOW + timedelta(hours=5)))
            self.assertTrue(forecast.due(NOW + timedelta(hours=6)))
            self.assertEqual(forecast.state("shadow")["last_error"], "archive unavailable")

    def test_location_change_triggers_retraining(self):
        with TemporaryDirectory() as directory:
            forecast = service(directory, FakeTrainer())
            forecast.train_now(NOW)
            forecast.set_location("Nitra")

            self.assertTrue(forecast.due(NOW + timedelta(minutes=1)))

    def test_without_location_nothing_is_trained(self):
        with TemporaryDirectory() as directory:
            forecast = service(directory, FakeTrainer(), location=None)

            self.assertFalse(forecast.due(NOW))

    def test_saved_model_survives_restart_and_wrong_version_is_ignored(self):
        with TemporaryDirectory() as directory:
            service(directory, FakeTrainer()).train_now(NOW)
            self.assertEqual(service(directory, FakeTrainer()).model()["location"]["requested"], "Jacovce")

            Path(directory, "forecast_model.json").write_text(json.dumps({**flat_model(), "version": 999}))
            self.assertIsNone(service(directory, FakeTrainer()).model())


class PredictHoursTests(unittest.TestCase):
    def test_uses_irradiance_labelled_at_hour_end_and_daily_temperature(self):
        forecast = weather_forecast(ghi=lambda label: 500.0 if label.hour == 11 else 0.0)
        hour = datetime(2026, 10, 30, 10, tzinfo=timezone.utc)

        predictions = predict_hours(flat_model(), forecast, [hour, hour + timedelta(hours=1)], TIMEZONE)

        self.assertAlmostEqual(predictions[hour]["pv_kwh"], 2.5)
        self.assertEqual(predictions[hour + timedelta(hours=1)]["pv_kwh"], 0.0)
        self.assertAlmostEqual(predictions[hour]["consumption_kwh"], 0.5)

    def test_missing_model_inputs_raise(self):
        forecast = {"hourly": [{"time": "2026-10-30T11:00+00:00", "shortwave_radiation": 1}]}

        with self.assertRaises(ValueError):
            predict_hours(flat_model(), forecast, [datetime(2026, 10, 30, 10, tzinfo=timezone.utc)], TIMEZONE)


class OptimizeDayModeTests(unittest.TestCase):
    STATUS = {"metrics": {"battery_soc": 40}, "battery_capacity": {"value": 5}}
    LOCAL_NOW = datetime(2026, 10, 30, 6, 0, tzinfo=ZoneInfo(TIMEZONE))

    def plan(self, mode, model):
        forecast = weather_forecast(ghi=lambda label: 400.0 if 8 <= label.hour <= 15 else 0.0)
        return optimize_day(self.STATUS, forecast, 7, 6, 25, True, now=self.LOCAL_NOW, forecast_model=model, forecast_mode=mode)

    def test_shadow_mode_logs_model_but_keeps_formula(self):
        off = self.plan("off", flat_model())
        shadow = self.plan("shadow", flat_model())

        self.assertEqual([hour["solar_kwh"] for hour in off["hourly_plan"]], [hour["solar_kwh"] for hour in shadow["hourly_plan"]])
        self.assertFalse(shadow["forecast_model"]["used"])
        self.assertEqual(len(shadow["forecast_model"]["hourly"]), 24)
        self.assertNotIn("hourly", off["forecast_model"])

    def test_active_mode_plans_with_model_values(self):
        active = self.plan("active", flat_model())

        self.assertTrue(active["forecast_model"]["used"])
        self.assertAlmostEqual(sum(hour["consumption_kwh"] for hour in active["hourly_plan"]), 12.0, places=2)
        sunny = [hour["solar_kwh"] for hour in active["hourly_plan"] if hour["solar_kwh"] > 0]
        self.assertTrue(sunny)
        self.assertTrue(all(abs(value - 2.0) < 1e-6 for value in sunny))

    def test_active_mode_without_model_falls_back_to_formula(self):
        active = self.plan("active", None)
        off = self.plan("off", None)

        self.assertFalse(active["forecast_model"]["used"])
        self.assertIn("error", active["forecast_model"])
        self.assertEqual([hour["solar_kwh"] for hour in off["hourly_plan"]], [hour["solar_kwh"] for hour in active["hourly_plan"]])


if __name__ == "__main__":
    unittest.main()
