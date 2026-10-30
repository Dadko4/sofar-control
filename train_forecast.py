"""Train the self-calibrating PV and consumption forecast for this plant and evaluate it.

Examples:
    python train_forecast.py --location Jacovce --output data/forecast_model.json
    python train_forecast.py --location Jacovce --cache /tmp/forecast-cache.json --holdout-days 28

Measured hourly PV and consumption come from SofarCloud station history; weather comes
from the Open-Meteo historical forecast archive (archived forecasts, not observations).
The last --holdout-days are kept out of training and used to compare the learned model
with the current estimate (irradiance x kWp x 0.8, same-weekday median consumption).
"""

import argparse
import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from forecast_model import (
    MODEL_VERSION,
    fit_consumption_model,
    fit_pv_model,
    mean_absolute_error,
    mean_bias,
    predict_hourly_kwh,
    predict_pv_kwh,
    pv_features,
    recent_scale,
    root_mean_squared_error,
)
from station_history import hourly_energy

HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
CHUNK_DAYS = 30
WEATHER_CHUNK_DAYS = 90
MIN_SAMPLES_PER_HOUR = 10
RECENT_DAYS = 14


def fetch_actuals(client, start, end):
    hours = {}
    cursor = start
    while cursor < end:
        chunk_end = min(end, cursor + timedelta(days=CHUNK_DAYS))
        series = client.get_station_series(["generationPower", "usePower"], cursor, chunk_end)
        for hour, bucket in hourly_energy(series).items():
            if bucket["sample_count"] >= MIN_SAMPLES_PER_HOUR and cursor <= hour < chunk_end:
                hours[hour] = {"pv_kwh": bucket["pv_kwh"], "consumption_kwh": bucket["consumption_kwh"]}
        cursor = chunk_end
    return hours


def fetch_weather(fetch_json, latitude, longitude, first_day, last_day):
    weather = {}
    cursor = first_day
    while cursor <= last_day:
        chunk_end = min(last_day, cursor + timedelta(days=WEATHER_CHUNK_DAYS - 1))
        query = urlencode({
            "latitude": latitude,
            "longitude": longitude,
            "start_date": cursor.isoformat(),
            "end_date": chunk_end.isoformat(),
            "hourly": "shortwave_radiation,temperature_2m",
            "timezone": "GMT",
        })
        hourly = fetch_json(f"{HISTORICAL_FORECAST_URL}?{query}").get("hourly") or {}
        for label, ghi, temp in zip(hourly.get("time", []), hourly.get("shortwave_radiation", []), hourly.get("temperature_2m", [])):
            if ghi is None or temp is None:
                continue
            weather[datetime.fromisoformat(label).replace(tzinfo=timezone.utc)] = (float(ghi), float(temp))
        cursor = chunk_end + timedelta(days=1)
    return weather


def fetch_training_data(client, fetch_json, latitude, longitude, days, now=None):
    end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    actuals = fetch_actuals(client, end - timedelta(days=days), end)
    if not actuals:
        raise ValueError("SofarCloud returned no measured hours for this period.")
    weather = fetch_weather(fetch_json, latitude, longitude, min(actuals).date() - timedelta(days=1), (end - timedelta(days=1)).date())
    return actuals, weather


def pv_samples(actuals, weather, latitude, longitude, label_offset_hours):
    samples = []
    for hour, values in actuals.items():
        forecast = weather.get(hour + timedelta(hours=label_offset_hours))
        if forecast is None:
            continue
        features = pv_features(hour, forecast[0], latitude, longitude)
        samples.append({**features, "hour": hour, "actual_kwh": values["pv_kwh"]})
    return samples


def complete_days(actuals, weather, tz):
    by_day = {}
    for hour, values in actuals.items():
        local = hour.astimezone(tz)
        by_day.setdefault(local.date(), {})[local.hour] = (values["consumption_kwh"], hour)
    days = []
    for day, hours in sorted(by_day.items()):
        if len(hours) != 24:
            continue
        temps = [weather[hour + timedelta(hours=1)][1] for _, hour in hours.values() if hour + timedelta(hours=1) in weather]
        if len(temps) < 20:
            continue
        days.append({
            "date": day,
            "mean_temp_c": statistics.fmean(temps),
            "hourly_kwh": [hours[index][0] for index in range(24)],
        })
    return days


def baseline_consumption(day, history):
    """Current planner logic: median of the last four same weekdays within 28 days."""
    window = [item for item in history if day.toordinal() - 28 <= item["date"].toordinal() < day.toordinal()]
    same = [item for item in window if item["date"].weekday() == day.weekday()][-4:]
    samples = same if len(same) >= 2 else window[-3:]
    if not samples:
        return None
    daily = statistics.median(sum(item["hourly_kwh"]) for item in samples)
    profile = [statistics.median(item["hourly_kwh"][hour] for item in samples) for hour in range(24)]
    total = sum(profile) or 1.0
    return [daily * value / total for value in profile]


def pv_metrics(pairs_by_hour, tz):
    daily = {}
    for hour, predicted, actual in pairs_by_hour:
        entry = daily.setdefault(hour.astimezone(tz).date(), [0.0, 0.0])
        entry[0] += predicted
        entry[1] += actual
    pairs = [(predicted, actual) for _, predicted, actual in pairs_by_hour]
    if not pairs:
        return None
    return {
        "hourly_mae_kwh": round(mean_absolute_error(pairs), 3),
        "hourly_rmse_kwh": round(root_mean_squared_error(pairs), 3),
        "daily_mae_kwh": round(mean_absolute_error(daily.values()), 2),
        "daily_bias_kwh": round(mean_bias(daily.values()), 2),
        "days": len(daily),
    }


def consumption_metrics(rows):
    if not rows:
        return None
    hourly = [(p, a) for predicted, actual in rows for p, a in zip(predicted, actual)]
    daily = [(sum(predicted), sum(actual)) for predicted, actual in rows]
    return {
        "hourly_mae_kwh": round(mean_absolute_error(hourly), 3),
        "hourly_rmse_kwh": round(root_mean_squared_error(hourly), 3),
        "daily_mae_kwh": round(mean_absolute_error(daily), 2),
        "daily_bias_kwh": round(mean_bias(daily), 2),
        "days": len(rows),
    }


def improvement(model, baseline, key="daily_mae_kwh"):
    if not model or not baseline or not baseline[key]:
        return None
    return round(100 * (1 - model[key] / baseline[key]), 1)


def _pv_baseline(holdout, weather, offset, kwp, pv_efficiency):
    rows = []
    for sample in holdout:
        forecast = weather.get(sample["hour"] + timedelta(hours=offset))
        if forecast is not None:
            rows.append((sample["hour"], forecast[0] / 1000 * kwp * pv_efficiency, sample["actual_kwh"]))
    return rows


def train(actuals, weather, latitude, longitude, location_name, tz_name, holdout_days=28, kwp=7.0, pv_efficiency=0.8, now=None):
    """Fit PV and consumption models on measured hours, evaluate on the last holdout days."""
    tz = ZoneInfo(tz_name)
    last_day = max(actuals).astimezone(tz).date()
    holdout_start = last_day - timedelta(days=holdout_days - 1)

    def is_train(hour):
        return hour.astimezone(tz).date() < holdout_start

    offsets = {}
    for offset in (0, 1):
        samples = [sample for sample in pv_samples(actuals, weather, latitude, longitude, offset) if is_train(sample["hour"])]
        model = fit_pv_model(samples)
        offsets[offset] = mean_absolute_error((predict_pv_kwh(model, sample), sample["actual_kwh"]) for sample in samples)
    label_offset = min(offsets, key=offsets.get)

    samples = pv_samples(actuals, weather, latitude, longitude, label_offset)
    pv_model = fit_pv_model([sample for sample in samples if is_train(sample["hour"])])
    holdout = [sample for sample in samples if not is_train(sample["hour"])]
    pv_eval = {
        "learned": pv_metrics([(sample["hour"], predict_pv_kwh(pv_model, sample), sample["actual_kwh"]) for sample in holdout], tz),
        "current": pv_metrics(_pv_baseline(holdout, weather, 0, kwp, pv_efficiency), tz),
        "current_with_fixed_alignment": pv_metrics(_pv_baseline(holdout, weather, label_offset, kwp, pv_efficiency), tz),
    }
    pv_eval["daily_mae_improvement_percent"] = improvement(pv_eval["learned"], pv_eval["current"])

    days = complete_days(actuals, weather, tz)
    learned_rows, current_rows = [], []
    for day in (day for day in days if day["date"] >= holdout_start):
        previous = [item for item in days if item["date"] < day["date"]]
        daily_model = fit_consumption_model(previous)
        daily_model["recent_scale"] = recent_scale(daily_model, previous[-RECENT_DAYS:])
        learned_rows.append((predict_hourly_kwh(daily_model, day["date"], day["mean_temp_c"]), day["hourly_kwh"]))
        baseline = baseline_consumption(day["date"], days)
        if baseline is not None:
            current_rows.append((baseline, day["hourly_kwh"]))
    consumption_eval = {"learned": consumption_metrics(learned_rows), "current": consumption_metrics(current_rows)}
    consumption_eval["daily_mae_improvement_percent"] = improvement(consumption_eval["learned"], consumption_eval["current"])

    final_pv = fit_pv_model(samples)
    final_consumption = fit_consumption_model(days)
    final_consumption["recent_scale"] = recent_scale(final_consumption, days[-RECENT_DAYS:])
    return {
        "version": MODEL_VERSION,
        "trained_at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds"),
        "location": {"name": location_name, "latitude": latitude, "longitude": longitude},
        "data": {
            "first_hour": min(actuals).isoformat(),
            "last_hour": max(actuals).isoformat(),
            "measured_hours": len(actuals),
            "complete_days": len(days),
            "holdout_start": holdout_start.isoformat(),
        },
        "radiation_label_offset_hours": label_offset,
        "radiation_offset_train_mae_kwh": {str(key): round(value, 4) for key, value in offsets.items()},
        "pv": final_pv,
        "consumption": final_consumption,
        "evaluation": {"pv": pv_eval, "consumption": consumption_eval},
    }


def _load_cache(path):
    raw = json.loads(path.read_text())
    actuals = {datetime.fromisoformat(key): value for key, value in raw["actuals"].items()}
    weather = {datetime.fromisoformat(key): tuple(value) for key, value in raw["weather"].items()}
    return actuals, weather


def _save_cache(path, actuals, weather):
    path.write_text(json.dumps({
        "actuals": {key.isoformat(): value for key, value in actuals.items()},
        "weather": {key.isoformat(): list(value) for key, value in weather.items()},
    }))


def main():
    import run

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--location", required=True)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--holdout-days", type=int, default=28)
    parser.add_argument("--kwp", type=float, default=7.0, help="only used for the current-method baseline")
    parser.add_argument("--output")
    parser.add_argument("--cache")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    latitude, longitude, place = run.geocode_location(args.location)
    cache = Path(args.cache) if args.cache else None
    if cache and cache.exists() and not args.refresh:
        actuals, weather = _load_cache(cache)
    else:
        actuals, weather = fetch_training_data(run.CLIENT, run._open_meteo_json, latitude, longitude, args.days)
        if cache:
            _save_cache(cache, actuals, weather)
    result = train(
        actuals, weather, latitude, longitude, place.get("name") or args.location, run.TIMEZONE,
        holdout_days=args.holdout_days, kwp=args.kwp, pv_efficiency=run.PV_SYSTEM_EFFICIENCY,
    )
    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2))
    consumption = result["consumption"]
    summary = {key: result[key] for key in ("data", "radiation_label_offset_hours", "radiation_offset_train_mae_kwh", "evaluation")}
    summary["consumption_model"] = {key: consumption[key] for key in ("intercepts_kwh", "heating_kwh_per_degree_day", "heating_base_c", "cooling_kwh_per_degree_day", "cooling_base_c", "recent_scale", "daily_residual_std_kwh")}
    summary["pv_sky_ratio"] = result["pv"]["sky_ratio_kwh_per_kwh_m2"]
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
