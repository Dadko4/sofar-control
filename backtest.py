"""Backtest the battery planner on measured days.

Examples:
    python backtest.py --db data/history.sqlite3 --days 30
    python backtest.py --synthetic --days 14

Every local day is simulated with 15-minute slots built from measured PV output,
house load and the Magna 4T tariff. Each strategy plans once at 00:00 (open loop)
and is then evaluated on the measured day, so results show what a day-ahead plan can
achieve. The savings formula matches the planner: cash savings plus the value of
energy left in the battery at the end, minus battery wear.
"""

import argparse
import math
import random
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from planner import SELF_USE, BatteryModel, evaluate_actions, plan_actions
from tariff import import_price

TIMEZONE = "Europe/Bratislava"
SLOT_MINUTES = 15
SLOTS_PER_DAY = 24 * 60 // SLOT_MINUTES
CHARGE_EFFICIENCY = 0.92
DISCHARGE_EFFICIENCY = 0.95
MIN_SOC = 30
MAX_SOC = 95
STRATEGIES = ("persistence", "perfect", "perfect_hold")


@dataclass
class Day:
    date: date
    slots: list
    initial_soc: float


def battery_model(capacity_kwh, wear_eur_per_kwh):
    return BatteryModel(
        capacity_kwh=capacity_kwh,
        min_energy_kwh=capacity_kwh * MIN_SOC / 100,
        max_energy_kwh=capacity_kwh * MAX_SOC / 100,
        charge_efficiency=CHARGE_EFFICIENCY,
        discharge_efficiency=DISCHARGE_EFFICIENCY,
        wear_eur_per_kwh=wear_eur_per_kwh,
    )


def build_days(samples, energy_aid, tz=ZoneInfo(TIMEZONE)):
    """samples: (aware datetime, generation_kw, consumption_kw, soc) tuples in any order.

    Returns complete local days only; a day is skipped if any 15-minute slot has no sample.
    """
    buckets = {}
    for moment, generation_kw, consumption_kw, soc in samples:
        local = moment.astimezone(tz)
        slot_index = (local.hour * 60 + local.minute) // SLOT_MINUTES
        buckets.setdefault((local.date(), slot_index), []).append(
            (generation_kw, consumption_kw, soc)
        )
    days = []
    dates = sorted({key[0] for key in buckets})
    for day_date in dates:
        slots = []
        first_soc = None
        for index in range(SLOTS_PER_DAY):
            readings = buckets.get((day_date, index))
            if not readings:
                break
            start = datetime.combine(day_date, time(0), tzinfo=tz) + timedelta(
                minutes=index * SLOT_MINUTES
            )
            if first_soc is None:
                first_soc = readings[0][2]
            slots.append(
                {
                    "start": start.astimezone(timezone.utc),
                    "price": import_price(start, energy_aid),
                    "solar_kwh": sum(item[0] for item in readings) / len(readings) * SLOT_MINUTES / 60,
                    "consumption_kwh": sum(item[1] for item in readings) / len(readings) * SLOT_MINUTES / 60,
                }
            )
        if len(slots) == SLOTS_PER_DAY and first_soc is not None:
            days.append(Day(day_date, slots, float(first_soc)))
    return days


def terminal_value(day_end_utc, energy_aid, discharge_efficiency=DISCHARGE_EFFICIENCY):
    prices = [
        import_price((day_end_utc + timedelta(hours=hour)).astimezone(ZoneInfo(TIMEZONE)), energy_aid)
        for hour in range(24)
    ]
    return sum(prices) / len(prices) * discharge_efficiency


def persistence_slots(day, previous):
    return [
        dict(slot, solar_kwh=earlier["solar_kwh"], consumption_kwh=earlier["consumption_kwh"])
        for slot, earlier in zip(day.slots, previous.slots)
    ]


def evaluate_day(day, previous, battery, energy_aid, export_price=0.0):
    initial = min(battery.max_energy_kwh, max(battery.min_energy_kwh, battery.capacity_kwh * day.initial_soc / 100))
    value = terminal_value(day.slots[-1]["start"] + timedelta(minutes=SLOT_MINUTES), energy_aid)
    baseline = evaluate_actions(day.slots, [SELF_USE] * len(day.slots), initial, battery, export_price)
    outcomes = {"self_use": None}
    forecasts = {
        "persistence": persistence_slots(day, previous) if previous else None,
        "perfect": day.slots,
        "perfect_hold": day.slots,
    }
    for name in STRATEGIES:
        forecast = forecasts[name]
        if forecast is None:
            continue
        actions = plan_actions(
            forecast,
            initial,
            battery,
            value,
            export_price_eur_kwh=export_price,
            allow_hold=name == "perfect_hold",
        )
        result = evaluate_actions(day.slots, actions, initial, battery, export_price)
        cash = baseline["cost_eur"] - result["cost_eur"]
        wear = result["wear_eur"] - baseline["wear_eur"]
        stored = result["final_energy_kwh"] - baseline["final_energy_kwh"]
        outcomes[name] = {
            "cash_savings_eur": cash,
            "estimated_savings_eur": cash + value * stored - wear,
        }
    return outcomes


def run_backtest(days, battery, energy_aid, export_price=0.0):
    totals = {name: [] for name in STRATEGIES}
    for index, day in enumerate(days):
        previous = days[index - 1] if index else None
        outcome = evaluate_day(day, previous, battery, energy_aid, export_price)
        for name in STRATEGIES:
            if outcome.get(name) is not None:
                totals[name].append(outcome[name]["estimated_savings_eur"])
    return {
        name: {
            "days": len(values),
            "mean_savings_eur_per_day": sum(values) / len(values) if values else None,
            "total_savings_eur": sum(values),
        }
        for name, values in totals.items()
    }


def load_samples(db_path):
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT sampled_at, generation_kw, consumption_kw, battery_soc
            FROM telemetry
            WHERE generation_kw IS NOT NULL AND consumption_kw IS NOT NULL AND battery_soc IS NOT NULL
            ORDER BY sampled_at
            """
        ).fetchall()
    samples = []
    for sampled_at, generation_kw, consumption_kw, soc in rows:
        moment = datetime.fromisoformat(sampled_at)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=ZoneInfo(TIMEZONE))
        samples.append((moment, generation_kw, max(0.0, consumption_kw), soc))
    return samples


def synthetic_samples(days, seed=7, first_day=date(2026, 10, 1), soc=50.0):
    rng = random.Random(seed)
    tz = ZoneInfo(TIMEZONE)
    samples = []
    for day_index in range(days):
        cloud = rng.uniform(0.2, 1.0)
        day_start = datetime.combine(first_day + timedelta(days=day_index), time(0), tzinfo=tz)
        for minute in range(0, 24 * 60, 5):
            moment = day_start + timedelta(minutes=minute)
            hour = minute / 60
            sun = math.sin(math.pi * (hour - 7) / 10) if 7 <= hour <= 17 else 0.0
            generation = 2.0 * max(0.0, sun) * cloud
            peak = 1.5 if (7 <= hour < 8 or 18 <= hour < 22) else 0.0
            consumption = 0.35 + peak + rng.uniform(0.0, 0.1)
            samples.append((moment.astimezone(timezone.utc), generation, consumption, soc))
    return samples


def format_report(result, days_used):
    lines = [f"Days simulated: {days_used}", "", f"{'strategy':<14}{'days':>6}{'mean EUR/day':>14}{'total EUR':>12}"]
    for name in STRATEGIES:
        item = result[name]
        mean_text = "n/a" if item["mean_savings_eur_per_day"] is None else f"{item['mean_savings_eur_per_day']:.3f}"
        lines.append(f"{name:<14}{item['days']:>6}{mean_text:>14}{item['total_savings_eur']:>12.2f}")
    lines.append("")
    lines.append("Savings are versus Self-use on the same measured day.")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--db", type=Path, help="history.sqlite3 with telemetry rows")
    source.add_argument("--synthetic", action="store_true", help="generate deterministic sample data")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--capacity-kwh", type=float, default=5.0)
    parser.add_argument("--wear-eur-kwh", type=float, default=0.01)
    parser.add_argument("--export-eur-kwh", type=float, default=0.0)
    parser.add_argument("--no-energy-aid", action="store_true")
    args = parser.parse_args(argv)

    samples = synthetic_samples(args.days) if args.synthetic else load_samples(args.db)
    days = build_days(samples, not args.no_energy_aid)[-args.days:]
    if not days:
        raise SystemExit("No complete days with 15-minute coverage were found.")
    battery = battery_model(args.capacity_kwh, args.wear_eur_kwh)
    result = run_backtest(days, battery, not args.no_energy_aid, args.export_eur_kwh)
    print(format_report(result, len(days)))


if __name__ == "__main__":
    main()
