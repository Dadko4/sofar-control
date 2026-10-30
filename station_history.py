from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from tariff import import_price

DEFAULT_INTERVAL = timedelta(minutes=5)
MAX_INTERVAL = timedelta(minutes=15)
ENERGY_FIELDS = (
    "pv_kwh",
    "consumption_kwh",
    "grid_import_kwh",
    "grid_export_kwh",
    "battery_charge_kwh",
    "battery_discharge_kwh",
    "grid_charge_kwh",
    "cost_with_aid_eur",
    "cost_without_aid_eur",
)


def _timeline(series):
    rows = {}
    for name, points in series.items():
        for moment, value in points:
            rows.setdefault(moment.astimezone(timezone.utc), {})[name] = value
    return sorted(rows.items())


def _hour_key(moment):
    return moment.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _hour_energy(timeline):
    hours = {}
    for index, (moment, values) in enumerate(timeline):
        interval = DEFAULT_INTERVAL
        if index + 1 < len(timeline):
            interval = min(timeline[index + 1][0] - moment, MAX_INTERVAL)
        fraction = interval.total_seconds() / 3600
        grid = values.get("gridPower", 0.0)
        charge = values.get("chargePower", 0.0)
        bucket = hours.setdefault(_hour_key(moment), {"sample_count": 0, **{field: 0.0 for field in ENERGY_FIELDS}})
        bucket["sample_count"] += 1
        bucket["pv_kwh"] += max(values.get("generationPower", 0.0), 0.0) * fraction
        bucket["consumption_kwh"] += max(values.get("usePower", 0.0), 0.0) * fraction
        bucket["grid_import_kwh"] += max(-grid, 0.0) * fraction
        bucket["grid_export_kwh"] += max(grid, 0.0) * fraction
        bucket["battery_charge_kwh"] += max(charge, 0.0) * fraction
        bucket["battery_discharge_kwh"] += max(-charge, 0.0) * fraction
        bucket["grid_charge_kwh"] += min(max(charge, 0.0), max(-grid, 0.0)) * fraction
    return hours


def build_daily_history(series, tz_name, now, soc_samples=(), days=7):
    """Local-day hourly energy summaries from SofarCloud 5-minute power series.

    series: {"generationPower", "usePower", "gridPower", "chargePower"} -> [(datetime, kW)].
    gridPower > 0 is export, chargePower > 0 is battery charging.
    soc_samples: [(datetime, soc_percent)] from local telemetry (SofarCloud history has no SOC).
    """
    tz = ZoneInfo(tz_name)
    now_utc = now.astimezone(timezone.utc)
    first_date = now.astimezone(tz).date() - timedelta(days=days - 1)
    hours = _hour_energy(_timeline(series))
    soc_by_hour = {}
    for moment, soc in sorted(soc_samples, key=lambda sample: sample[0]):
        soc_by_hour[_hour_key(moment)] = soc

    day_summaries = []
    for offset in range(days):
        day = first_date + timedelta(days=offset)
        midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
        day_start = midnight.astimezone(timezone.utc)
        day_end = (midnight + timedelta(days=1)).astimezone(timezone.utc)
        totals = {field: 0.0 for field in ENERGY_FIELDS}
        entries = []
        hour = day_start
        while hour < day_end and hour <= now_utc:
            local = hour.astimezone(tz)
            bucket = hours.get(hour)
            entry = {
                "time": local.isoformat(timespec="minutes"),
                "tariff_with_aid_eur_per_kwh": round(import_price(local, True), 4),
                "tariff_without_aid_eur_per_kwh": round(import_price(local, False), 4),
                "soc_end": soc_by_hour.get(hour),
            }
            if bucket is None:
                entry.update({field: None for field in ENERGY_FIELDS})
            else:
                bucket["cost_with_aid_eur"] = bucket["grid_import_kwh"] * import_price(local, True)
                bucket["cost_without_aid_eur"] = bucket["grid_import_kwh"] * import_price(local, False)
                entry.update({field: round(bucket[field], 4) for field in ENERGY_FIELDS})
                for field in ENERGY_FIELDS:
                    totals[field] += bucket[field]
            entries.append(entry)
            hour += timedelta(hours=1)
        day_summaries.append({
            "date": day.isoformat(),
            "hours": entries,
            "totals": {field: round(value, 3) for field, value in totals.items()},
        })
    return day_summaries
