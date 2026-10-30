"""Self-calibrating PV and consumption forecasts learned from one plant's own history.

PV: the ratio between measured output and forecast irradiance is learned per sun
position (elevation x azimuth bin), separately for clear and cloudy hours. Orientation,
shading and system size are therefore learned rather than configured. Sparse bins shrink
towards the plant-wide ratio of the same sky class.

Consumption: daily energy is a linear model of day type and heating/cooling degree days
from the forecast temperature; it is split into hours with a learned daily profile.
"""

import math
import statistics
from datetime import timedelta, timezone

MODEL_VERSION = 1
ELEVATION_STEP_DEG = 5
AZIMUTH_STEP_DEG = 15
CLEAR_SKY_THRESHOLD = 0.5
PV_PRIOR_WEIGHT_KWH_M2 = 2.0
MIN_IRRADIANCE_WM2 = 5.0
HEATING_BASES_C = tuple(float(value) for value in range(10, 19))
COOLING_BASES_C = (20.0, 22.0, 24.0, 26.0)
PROFILE_WINDOW_DAYS = 42
DAY_TYPES = ("workday", "saturday", "sunday")
MIN_PROFILE_DAYS = 4


def solar_position(moment, latitude, longitude):
    """Sun (elevation, azimuth) in degrees; azimuth 0 = south, -90 = east, +90 = west."""
    moment = moment.astimezone(timezone.utc)
    hours = moment.hour + moment.minute / 60 + moment.second / 3600
    gamma = 2 * math.pi / 365 * (moment.timetuple().tm_yday - 1 + (hours - 12) / 24)
    equation_of_time = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    hour_angle = math.radians((hours * 60 + equation_of_time + 4 * longitude) / 4 - 180)
    lat = math.radians(latitude)
    cos_zenith = math.sin(lat) * math.sin(declination) + math.cos(lat) * math.cos(declination) * math.cos(hour_angle)
    elevation = 90 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zenith))))
    azimuth = math.degrees(
        math.atan2(
            math.sin(hour_angle),
            math.cos(hour_angle) * math.sin(lat) - math.tan(declination) * math.cos(lat),
        )
    )
    return elevation, azimuth


def clear_sky_ghi(elevation):
    """Haurwitz clear-sky global horizontal irradiance in W/m2."""
    cos_zenith = math.sin(math.radians(elevation))
    if cos_zenith <= 0:
        return 0.0
    return 1098 * cos_zenith * math.exp(-0.057 / cos_zenith)


def pv_features(interval_start, irradiance_wm2, latitude, longitude, hours=1.0):
    elevation, azimuth = solar_position(interval_start + timedelta(hours=hours / 2), latitude, longitude)
    clear = clear_sky_ghi(elevation)
    return {
        "irradiance_wm2": max(0.0, irradiance_wm2),
        "elevation": elevation,
        "azimuth": azimuth,
        "clear_sky": clear > 0 and irradiance_wm2 / clear >= CLEAR_SKY_THRESHOLD,
    }


def _pv_bin(features):
    elevation_bin = int(max(0.0, features["elevation"]) // ELEVATION_STEP_DEG)
    azimuth_bin = int((features["azimuth"] + 180) // AZIMUTH_STEP_DEG)
    return f"{elevation_bin}:{azimuth_bin}:{int(features['clear_sky'])}"


def fit_pv_model(samples):
    """samples: pv_features dicts plus actual_kwh for one hour."""
    usable = [
        sample for sample in samples
        if sample["elevation"] > 0 and sample["irradiance_wm2"] >= MIN_IRRADIANCE_WM2
    ]
    if not usable:
        raise ValueError("No daylight hours with irradiance to learn PV output from.")
    bins = {}
    totals = {"0": [0.0, 0.0], "1": [0.0, 0.0]}
    for sample in usable:
        irradiance_kwh_m2 = sample["irradiance_wm2"] / 1000
        entry = bins.setdefault(_pv_bin(sample), [0.0, 0.0])
        entry[0] += sample["actual_kwh"]
        entry[1] += irradiance_kwh_m2
        total = totals[str(int(sample["clear_sky"]))]
        total[0] += sample["actual_kwh"]
        total[1] += irradiance_kwh_m2
    overall = sum(total[0] for total in totals.values()) / sum(total[1] for total in totals.values())
    ratios = {sky: (total[0] / total[1] if total[1] > 0 else overall) for sky, total in totals.items()}
    peak = sorted(sample["actual_kwh"] for sample in usable)
    return {
        "sky_ratio_kwh_per_kwh_m2": ratios,
        "bins": {key: [round(value[0], 4), round(value[1], 4)] for key, value in bins.items()},
        "max_hourly_kwh": round(peak[min(len(peak) - 1, int(len(peak) * 0.999))], 3),
        "prior_weight_kwh_m2": PV_PRIOR_WEIGHT_KWH_M2,
        "training_hours": len(usable),
    }


def predict_pv_kwh(model, features, hours=1.0):
    if features["elevation"] <= 0 or features["irradiance_wm2"] <= 0:
        return 0.0
    prior = model["sky_ratio_kwh_per_kwh_m2"][str(int(features["clear_sky"]))]
    actual, irradiance = model["bins"].get(_pv_bin(features), (0.0, 0.0))
    weight = model["prior_weight_kwh_m2"]
    ratio = (actual + weight * prior) / (irradiance + weight)
    return min(model["max_hourly_kwh"] * hours, ratio * features["irradiance_wm2"] / 1000 * hours)


def day_type(local_date):
    return {5: "saturday", 6: "sunday"}.get(local_date.weekday(), "workday")


def degree_days(mean_temp_c, heating_base_c, cooling_base_c):
    return max(0.0, heating_base_c - mean_temp_c), max(0.0, mean_temp_c - cooling_base_c)


def _solve(matrix, vector):
    size = len(vector)
    rows = [list(matrix[index]) + [vector[index]] for index in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(rows[row][column]))
        if abs(rows[pivot][column]) < 1e-12:
            raise ValueError("Consumption regression is singular.")
        rows[column], rows[pivot] = rows[pivot], rows[column]
        for row in range(size):
            if row != column:
                factor = rows[row][column] / rows[column][column]
                rows[row] = [value - factor * base for value, base in zip(rows[row], rows[column])]
    return [rows[index][size] / rows[index][index] for index in range(size)]


def _least_squares(features, targets):
    size = len(features[0])
    normal = [[sum(row[i] * row[j] for row in features) for j in range(size)] for i in range(size)]
    right = [sum(row[i] * target for row, target in zip(features, targets)) for i in range(size)]
    return _solve(normal, right)


def _fit_daily(days, targets, heating_base, cooling_base):
    degrees = [degree_days(day["mean_temp_c"], heating_base, cooling_base) for day in days]
    use_heating = sum(1 for heating, _ in degrees if heating > 0) >= MIN_PROFILE_DAYS
    use_cooling = sum(1 for _, cooling in degrees if cooling > 0) >= MIN_PROFILE_DAYS
    while True:
        design = []
        for day, (heating, cooling) in zip(days, degrees):
            row = [1.0 if day_type(day["date"]) == name else 0.0 for name in DAY_TYPES]
            row += [heating] if use_heating else []
            row += [cooling] if use_cooling else []
            design.append(row)
        coefficients = _least_squares(design, targets)
        slopes = coefficients[len(DAY_TYPES):]
        if use_heating and slopes[0] < 0:
            use_heating = False
            continue
        if use_cooling and slopes[-1] < 0:
            use_cooling = False
            continue
        break
    slopes = iter(coefficients[len(DAY_TYPES):])
    fit = {
        "intercepts_kwh": dict(zip(DAY_TYPES, coefficients[: len(DAY_TYPES)])),
        "heating_kwh_per_degree_day": next(slopes) if use_heating else 0.0,
        "cooling_kwh_per_degree_day": next(slopes) if use_cooling else 0.0,
        "heating_base_c": heating_base,
        "cooling_base_c": cooling_base,
        "recent_scale": 1.0,
    }
    error = sum((target - predict_daily_kwh(fit, day["date"], day["mean_temp_c"])) ** 2 for day, target in zip(days, targets))
    return error, fit


def fit_consumption_model(days):
    """days: dicts with date (local), mean_temp_c and hourly_kwh (24 values by local hour)."""
    days = sorted(days, key=lambda day: day["date"])
    present_types = {day_type(day["date"]) for day in days}
    if len(days) < 14 or present_types != set(DAY_TYPES):
        raise ValueError("At least two weeks of complete days are needed to learn consumption.")
    targets = [sum(day["hourly_kwh"]) for day in days]
    _, fit = min(
        (_fit_daily(days, targets, heating, cooling) for heating in HEATING_BASES_C for cooling in COOLING_BASES_C),
        key=lambda candidate: candidate[0],
    )

    def profile(group):
        if len(group) < MIN_PROFILE_DAYS:
            return None
        fractions = [
            [value / sum(day["hourly_kwh"]) for value in day["hourly_kwh"]]
            for day in group
            if sum(day["hourly_kwh"]) > 0
        ]
        medians = [statistics.median(day[hour] for day in fractions) for hour in range(24)]
        total = sum(medians)
        return [round(value / total, 5) for value in medians]

    recent = days[-PROFILE_WINDOW_DAYS:]
    fallback = profile(recent) or profile(days)
    model = {
        "intercepts_kwh": {name: round(value, 4) for name, value in fit["intercepts_kwh"].items()},
        "heating_kwh_per_degree_day": round(fit["heating_kwh_per_degree_day"], 4),
        "cooling_kwh_per_degree_day": round(fit["cooling_kwh_per_degree_day"], 4),
        "heating_base_c": fit["heating_base_c"],
        "cooling_base_c": fit["cooling_base_c"],
        "profiles": {
            kind: profile([day for day in recent if day_type(day["date"]) == kind]) or fallback
            for kind in DAY_TYPES
        },
        "recent_scale": 1.0,
        "training_days": len(days),
    }
    residuals = [target - predict_daily_kwh(model, day["date"], day["mean_temp_c"]) for day, target in zip(days, targets)]
    model["daily_residual_std_kwh"] = round(statistics.pstdev(residuals), 3)
    return model


def predict_daily_kwh(model, local_date, mean_temp_c):
    heating, cooling = degree_days(mean_temp_c, model["heating_base_c"], model["cooling_base_c"])
    value = (
        model["intercepts_kwh"][day_type(local_date)]
        + model["heating_kwh_per_degree_day"] * heating
        + model["cooling_kwh_per_degree_day"] * cooling
    )
    return max(0.0, value * model["recent_scale"])


def predict_hourly_kwh(model, local_date, mean_temp_c):
    shape = model["profiles"][day_type(local_date)]
    daily = predict_daily_kwh(model, local_date, mean_temp_c)
    total = sum(shape)
    return [daily * fraction / total for fraction in shape]


def recent_scale(model, recent_days, low=0.75, high=1.33):
    if not recent_days:
        return 1.0
    predicted = sum(predict_daily_kwh({**model, "recent_scale": 1.0}, day["date"], day["mean_temp_c"]) for day in recent_days)
    actual = sum(sum(day["hourly_kwh"]) for day in recent_days)
    if predicted <= 0:
        return 1.0
    return round(min(high, max(low, actual / predicted)), 4)


def mean_absolute_error(pairs):
    pairs = list(pairs)
    return sum(abs(predicted - actual) for predicted, actual in pairs) / len(pairs) if pairs else None


def root_mean_squared_error(pairs):
    pairs = list(pairs)
    return math.sqrt(sum((predicted - actual) ** 2 for predicted, actual in pairs) / len(pairs)) if pairs else None


def mean_bias(pairs):
    pairs = list(pairs)
    return sum(predicted - actual for predicted, actual in pairs) / len(pairs) if pairs else None
