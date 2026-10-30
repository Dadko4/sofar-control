import unittest
from datetime import date, datetime, timedelta, timezone

from forecast_model import (
    clear_sky_ghi,
    day_type,
    fit_consumption_model,
    fit_pv_model,
    predict_daily_kwh,
    predict_hourly_kwh,
    predict_pv_kwh,
    pv_features,
    recent_scale,
    solar_position,
)

LAT, LON = 48.6, 18.15
SHAPE = [0.02] * 6 + [0.05] * 3 + [0.04] * 8 + [0.06] * 5 + [0.03] * 2
SHAPE = [value / sum(SHAPE) for value in SHAPE]


def shaded_plant(features):
    output = features["irradiance_wm2"] / 1000 * 5.6
    if features["azimuth"] > 45 and features["elevation"] < 25:
        output *= 0.3
    return output


def pv_samples(days=90, start=date(2026, 4, 1), cloud=0.85):
    samples = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        for hour in range(24):
            interval = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
            elevation, _ = solar_position(interval + timedelta(minutes=30), LAT, LON)
            features = pv_features(interval, clear_sky_ghi(elevation) * cloud, LAT, LON)
            samples.append({**features, "actual_kwh": shaded_plant(features)})
    return samples


def consumption_days(count=60, start=date(2026, 1, 5), heating_slope=0.9):
    days = []
    for offset in range(count):
        day = start + timedelta(days=offset)
        temp = -2 + (offset % 20)
        base = {"workday": 8.0, "saturday": 10.0, "sunday": 11.0}[day_type(day)]
        total = base + heating_slope * max(0.0, 15 - temp)
        days.append({"date": day, "mean_temp_c": temp, "hourly_kwh": [total * value for value in SHAPE]})
    return days


class SolarPositionTests(unittest.TestCase):
    def test_equinox_noon_is_south_at_complement_of_latitude(self):
        elevation, azimuth = solar_position(datetime(2026, 3, 20, 10, 54, tzinfo=timezone.utc), LAT, LON)

        self.assertAlmostEqual(elevation, 90 - LAT, delta=1.5)
        self.assertLess(abs(azimuth), 5)

    def test_morning_sun_is_east_and_afternoon_sun_is_west(self):
        _, morning = solar_position(datetime(2026, 6, 21, 5, 0, tzinfo=timezone.utc), LAT, LON)
        _, afternoon = solar_position(datetime(2026, 6, 21, 15, 0, tzinfo=timezone.utc), LAT, LON)

        self.assertLess(morning, -60)
        self.assertGreater(afternoon, 60)

    def test_night_has_no_clear_sky_irradiance(self):
        elevation, _ = solar_position(datetime(2026, 6, 21, 23, 0, tzinfo=timezone.utc), LAT, LON)

        self.assertLess(elevation, 0)
        self.assertEqual(clear_sky_ghi(elevation), 0.0)


class PvModelTests(unittest.TestCase):
    def test_learns_west_shading_without_being_told_about_it(self):
        model = fit_pv_model(pv_samples())
        probe_day = datetime(2026, 5, 15, tzinfo=timezone.utc)
        shaded = unshaded = None
        for hour in range(24):
            interval = probe_day + timedelta(hours=hour)
            elevation, _ = solar_position(interval + timedelta(minutes=30), LAT, LON)
            features = pv_features(interval, clear_sky_ghi(elevation) * 0.85, LAT, LON)
            if features["elevation"] <= 5:
                continue
            if features["azimuth"] > 50 and features["elevation"] < 20:
                shaded = (predict_pv_kwh(model, features), shaded_plant(features))
            if abs(features["azimuth"]) < 10:
                unshaded = (predict_pv_kwh(model, features), shaded_plant(features))

        self.assertIsNotNone(shaded)
        self.assertIsNotNone(unshaded)
        self.assertAlmostEqual(shaded[0], shaded[1], delta=0.1 * unshaded[1])
        self.assertAlmostEqual(unshaded[0], unshaded[1], delta=0.05 * unshaded[1])

    def test_no_output_at_night_or_without_irradiance(self):
        model = fit_pv_model(pv_samples(days=20))
        night = pv_features(datetime(2026, 5, 15, 23, tzinfo=timezone.utc), 0.0, LAT, LON)

        self.assertEqual(predict_pv_kwh(model, night), 0.0)

    def test_prediction_is_capped_at_observed_peak(self):
        model = fit_pv_model(pv_samples(days=20))
        noon = pv_features(datetime(2026, 5, 15, 10, tzinfo=timezone.utc), 5000.0, LAT, LON)

        self.assertLessEqual(predict_pv_kwh(model, noon), model["max_hourly_kwh"])

    def test_needs_daylight_data(self):
        with self.assertRaises(ValueError):
            fit_pv_model([])


class ConsumptionModelTests(unittest.TestCase):
    def test_recovers_heating_slope_and_day_types(self):
        model = fit_consumption_model(consumption_days())

        self.assertEqual(model["heating_base_c"], 15.0)
        self.assertAlmostEqual(model["heating_kwh_per_degree_day"], 0.9, places=2)
        self.assertAlmostEqual(model["intercepts_kwh"]["workday"], 8.0, places=1)
        self.assertAlmostEqual(model["intercepts_kwh"]["sunday"], 11.0, places=1)
        self.assertEqual(model["cooling_kwh_per_degree_day"], 0.0)

    def test_hourly_forecast_follows_profile_and_daily_total(self):
        model = fit_consumption_model(consumption_days())
        hourly = predict_hourly_kwh(model, date(2026, 3, 2), 5.0)

        self.assertEqual(len(hourly), 24)
        self.assertAlmostEqual(sum(hourly), predict_daily_kwh(model, date(2026, 3, 2), 5.0), places=6)
        self.assertGreater(hourly[18], hourly[2])

    def test_negative_temperature_effect_is_dropped(self):
        days = consumption_days(heating_slope=-0.3)
        model = fit_consumption_model(days)

        self.assertEqual(model["heating_kwh_per_degree_day"], 0.0)

    def test_recent_scale_tracks_level_change_and_is_clamped(self):
        model = fit_consumption_model(consumption_days())
        higher = [{**day, "hourly_kwh": [value * 1.2 for value in day["hourly_kwh"]]} for day in consumption_days(count=14, start=date(2026, 3, 9))]
        extreme = [{**day, "hourly_kwh": [value * 3 for value in day["hourly_kwh"]]} for day in consumption_days(count=14, start=date(2026, 3, 9))]

        self.assertAlmostEqual(recent_scale(model, higher), 1.2, places=2)
        self.assertEqual(recent_scale(model, extreme), 1.33)
        self.assertEqual(recent_scale(model, []), 1.0)

    def test_needs_two_weeks_of_days(self):
        with self.assertRaises(ValueError):
            fit_consumption_model(consumption_days(count=10))


if __name__ == "__main__":
    unittest.main()
