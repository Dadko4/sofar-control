"""Pure multi-window charge planner and self-use counterfactual replay."""

import math
from dataclasses import dataclass
from datetime import timedelta

CHARGE_STEP_KWH = 0.25
MIN_WINDOW_GRID_KWH = 0.5
MIN_IMPROVEMENT_EUR = 0.0005
MIN_TARGET_SOC = 31


@dataclass(frozen=True)
class BatteryModel:
    capacity_kwh: float
    min_energy_kwh: float
    max_energy_kwh: float
    charge_efficiency: float
    discharge_efficiency: float
    max_grid_kw: float = 2.0
    max_soc: int = 95


def _step(energy, grid_charge_kwh, solar_kwh, consumption_kwh, battery):
    """Advance one hour in self-use; returns (energy, soc_after_charge, grid_charge, grid_load)."""
    room = max(0.0, battery.max_energy_kwh - energy) / battery.charge_efficiency
    grid_charge = min(max(0.0, grid_charge_kwh), room)
    energy += grid_charge * battery.charge_efficiency
    energy_after_charge = energy
    # The inverter serves the load from the grid only while it is charging.
    charging_share = min(1.0, grid_charge / battery.max_grid_kw)
    grid_load = 0.0
    for share, from_grid in (
        (charging_share, True),
        (1.0 - charging_share, False),
    ):
        net = (solar_kwh - consumption_kwh) * share
        if net >= 0:
            energy = min(
                battery.max_energy_kwh,
                energy + net * battery.charge_efficiency,
            )
        elif from_grid:
            grid_load += -net
        else:
            deliverable = (
                max(0.0, energy - battery.min_energy_kwh)
                * battery.discharge_efficiency
            )
            delivered = min(-net, deliverable)
            energy -= delivered / battery.discharge_efficiency
            grid_load += -net - delivered
    return energy, energy_after_charge, grid_charge, grid_load


def simulate_hours(hours, grid_charge, initial_energy, battery):
    energy = initial_energy
    rows = []
    cost = 0.0
    for hour, requested in zip(hours, grid_charge):
        energy_start = energy
        energy, after_charge, charged, grid_load = _step(
            energy,
            requested,
            hour["solar_kwh"],
            hour["consumption_kwh"],
            battery,
        )
        hour_cost = (charged + grid_load) * hour["price"]
        cost += hour_cost
        rows.append(
            {
                "start": hour["start"],
                "price": hour["price"],
                "solar_kwh": hour["solar_kwh"],
                "consumption_kwh": hour["consumption_kwh"],
                "grid_charge_kwh": charged,
                "grid_import_kwh": charged + grid_load,
                "soc_start": energy_start / battery.capacity_kwh * 100,
                "soc_after_charge": after_charge / battery.capacity_kwh * 100,
                "soc_end": energy / battery.capacity_kwh * 100,
                "cost_eur": hour_cost,
            }
        )
    return {"cost_eur": cost, "final_energy_kwh": energy, "rows": rows}


def _objective(result, battery, terminal_price):
    stored = max(0.0, result["final_energy_kwh"] - battery.min_energy_kwh)
    return result["cost_eur"] - terminal_price * battery.discharge_efficiency * stored


def _greedy_charge(hours, initial_energy, battery, terminal_price):
    grid_charge = [0.0] * len(hours)
    caps = [battery.max_grid_kw * hour["fraction"] for hour in hours]
    current = _objective(
        simulate_hours(hours, grid_charge, initial_energy, battery),
        battery,
        terminal_price,
    )
    while True:
        best = None
        best_rate = 0.0
        for index, cap in enumerate(caps):
            if grid_charge[index] + 1e-9 >= cap:
                continue
            targets = {
                min(cap, grid_charge[index] + CHARGE_STEP_KWH),
                cap,
            }
            for target in targets:
                trial = grid_charge[:]
                trial[index] = target
                value = _objective(
                    simulate_hours(hours, trial, initial_energy, battery),
                    battery,
                    terminal_price,
                )
                gain = current - value
                rate = gain / (target - grid_charge[index])
                if gain > MIN_IMPROVEMENT_EUR and rate > best_rate:
                    best_rate = rate
                    best = (index, target, value)
        if best is None:
            return grid_charge
        grid_charge[best[0]] = best[1]
        current = best[2]


def _round_up_minute(moment):
    if moment.second or moment.microsecond:
        moment = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return moment


def _build_windows(hours, rows, battery, display_tz=None):
    windows = []
    count = len(rows)
    index = 0
    while index < count:
        if rows[index]["grid_charge_kwh"] <= 1e-6:
            index += 1
            continue
        last = index
        while (
            last + 1 < count
            and rows[last + 1]["grid_charge_kwh"] > 1e-6
        ):
            last += 1
        first_row, last_row = rows[index], rows[last]
        total = sum(row["grid_charge_kwh"] for row in rows[index : last + 1])
        first_begin = hours[index]["begin"]
        if index == last:
            start = first_begin
            end = start + timedelta(
                hours=first_row["grid_charge_kwh"] / battery.max_grid_kw
            )
        else:
            slot_end = hours[index]["start"] + timedelta(hours=1)
            start = max(
                first_begin,
                slot_end
                - timedelta(
                    hours=first_row["grid_charge_kwh"] / battery.max_grid_kw
                ),
            )
            end = hours[last]["start"] + timedelta(
                hours=last_row["grid_charge_kwh"] / battery.max_grid_kw
            )
        start = _round_up_minute(start)
        end = _round_up_minute(end)
        if end <= start:
            end = start + timedelta(minutes=1)
        if display_tz is not None:
            start = start.astimezone(display_tz)
            end = end.astimezone(display_tz)
        target_soc = min(
            battery.max_soc,
            max(MIN_TARGET_SOC, math.ceil(last_row["soc_after_charge"] - 1e-9)),
        )
        weighted_price = (
            sum(
                row["grid_charge_kwh"] * row["price"]
                for row in rows[index : last + 1]
            )
            / total
        )
        windows.append(
            {
                "charge_start": start.isoformat(timespec="minutes"),
                "charge_end": end.isoformat(timespec="minutes"),
                "recommended_target_soc": target_soc,
                "recommended_grid_charge_kwh": round(total, 2),
                "average_price_eur_per_kwh": round(weighted_price, 4),
                "_first": index,
                "_last": last,
                "_total": total,
            }
        )
        index = last + 1
    return windows


def _public_row(row, display_tz=None):
    moment = row["start"]
    if display_tz is not None:
        moment = moment.astimezone(display_tz)
    return {
        "time": moment.isoformat(timespec="minutes"),
        "tariff_eur_per_kwh": round(row["price"], 4),
        "solar_kwh": round(row["solar_kwh"], 3),
        "consumption_kwh": round(row["consumption_kwh"], 3),
        "grid_charge_kwh": round(row["grid_charge_kwh"], 3),
        "grid_import_kwh": round(row["grid_import_kwh"], 3),
        "soc_start": round(row["soc_start"], 1),
        "soc_end": round(row["soc_end"], 1),
        "cost_eur": round(row["cost_eur"], 4),
    }


def optimize_windows(hours, initial_energy, battery, terminal_price, display_tz=None):
    """Choose grid-charge hours minimising cost; hours need start, begin, fraction,
    price, solar_kwh and consumption_kwh. Pass hours in UTC so DST changes keep
    hourly arithmetic correct; display_tz only affects the reported timestamps."""
    if not hours:
        raise ValueError("At least one forecast hour is required.")
    grid_charge = _greedy_charge(hours, initial_energy, battery, terminal_price)
    rows = simulate_hours(hours, grid_charge, initial_energy, battery)["rows"]
    windows = _build_windows(hours, rows, battery, display_tz)
    kept = []
    final_charge = [0.0] * len(hours)
    for window in windows:
        if window["_total"] < MIN_WINDOW_GRID_KWH:
            continue
        kept.append(window)
        for index in range(window["_first"], window["_last"] + 1):
            final_charge[index] = rows[index]["grid_charge_kwh"]
    if len(kept) != len(windows):
        rows = simulate_hours(hours, final_charge, initial_energy, battery)["rows"]
        windows = _build_windows(hours, rows, battery, display_tz)
        kept = [w for w in windows if w["_total"] >= MIN_WINDOW_GRID_KWH]
    plan = simulate_hours(hours, final_charge, initial_energy, battery)
    baseline = simulate_hours(hours, [0.0] * len(hours), initial_energy, battery)
    for number, window in enumerate(kept, start=1):
        window["id"] = f"window_{number}"
        for key in ("_first", "_last", "_total"):
            window.pop(key)
    stored_delta = plan["final_energy_kwh"] - baseline["final_energy_kwh"]
    terminal_value = terminal_price * battery.discharge_efficiency * stored_delta
    return {
        "windows": kept,
        "cost_with_plan_eur": round(plan["cost_eur"], 4),
        "cost_self_use_eur": round(baseline["cost_eur"], 4),
        "stored_energy_delta_kwh": round(stored_delta, 3),
        "estimated_savings_eur": round(
            baseline["cost_eur"] - plan["cost_eur"] + terminal_value, 4
        ),
        "cash_savings_eur": round(baseline["cost_eur"] - plan["cost_eur"], 4),
        "hourly_plan": [
            dict(
                _public_row(plan_row, display_tz),
                cost_self_use_eur=round(base_row["cost_eur"], 4),
                soc_self_use_end=round(base_row["soc_end"], 1),
            )
            for plan_row, base_row in zip(plan["rows"], baseline["rows"])
        ],
    }


def replay_self_use(samples, initial_energy, battery):
    """Replay measured load and solar as if the inverter had stayed in self-use.

    samples need start, hours, generation_kw, consumption_kw and price.
    Returns cumulative cost points and the final battery energy."""
    energy = initial_energy
    cumulative = 0.0
    points = []
    for sample in samples:
        hours = sample["hours"]
        energy, _, _, grid_load = _step(
            energy,
            0.0,
            max(0.0, sample["generation_kw"]) * hours,
            max(0.0, sample["consumption_kw"]) * hours,
            battery,
        )
        cumulative += grid_load * sample["price"]
        points.append(
            {
                "start": sample["start"],
                "cumulative_cost_eur": cumulative,
                "soc": energy / battery.capacity_kwh * 100,
            }
        )
    return {"points": points, "final_energy_kwh": energy, "cost_eur": cumulative}
