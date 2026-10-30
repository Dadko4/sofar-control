"""Battery dispatch planner: 15-minute dynamic programming and self-use replay."""

import math
from dataclasses import dataclass
from datetime import timedelta

SLOT_HOURS = 0.25
SOC_STEP_KWH = 0.025
CHARGE_POWER_LEVELS_KW = (0.5, 1.0, 1.5, 2.0)
DISCHARGE_POWER_LEVELS_KW = (1.0, 2.0)
MIN_TARGET_SOC = 31
MAX_CHARGE_POWER_W = 2000
GRID_CHARGE_MARGIN_EUR_PER_KWH = 0.03

SELF_USE = ("self_use", 0.0)
HOLD = ("hold", 0.0)


@dataclass(frozen=True)
class BatteryModel:
    capacity_kwh: float
    min_energy_kwh: float
    max_energy_kwh: float
    charge_efficiency: float
    discharge_efficiency: float
    max_grid_kw: float = 2.0
    max_soc: int = 95
    wear_eur_per_kwh: float = 0.0


def _transition(energy, action, solar_kwh, load_kwh, battery, hours):
    """Advance one slot. Returns (energy_next, grid_charge, grid_import, export, stored, energy_after_charge).

    Solar surplus charges the battery before it is exported. Self-use covers house
    deficits from the battery; hold keeps the battery idle; charge pulls from the grid
    at a fixed power while the inverter serves the load from the grid; discharge sends
    battery energy to the house or the grid.
    """
    kind, power_kw = action
    room = max(0.0, battery.max_energy_kwh - energy)
    grid_charge = (
        min(power_kw * hours, room / battery.charge_efficiency) if kind == "charge" else 0.0
    )
    grid_stored = grid_charge * battery.charge_efficiency
    surplus = max(0.0, solar_kwh - load_kwh)
    deficit = max(0.0, load_kwh - solar_kwh)
    solar_stored = min(surplus * battery.charge_efficiency, room - grid_stored)
    solar_export = surplus - solar_stored / battery.charge_efficiency
    after_charge = energy + grid_stored + solar_stored
    deliverable = max(0.0, energy - battery.min_energy_kwh) * battery.discharge_efficiency
    if kind == "self_use":
        requested = deficit
    elif kind == "discharge":
        requested = power_kw * hours
    else:
        requested = 0.0
    delivered = min(requested, deliverable)
    energy_next = min(
        battery.max_energy_kwh,
        max(battery.min_energy_kwh, after_charge - delivered / battery.discharge_efficiency),
    )
    grid_import = grid_charge + max(0.0, deficit - delivered)
    export = solar_export + max(0.0, delivered - deficit)
    stored = grid_stored + solar_stored
    return energy_next, grid_charge, grid_import, export, stored, after_charge


def _actions(battery, allow_hold, allow_discharge):
    actions = [SELF_USE]
    if allow_hold:
        actions.append(HOLD)
    actions += [
        ("charge", kw)
        for kw in CHARGE_POWER_LEVELS_KW
        if kw <= battery.max_grid_kw + 1e-9
    ]
    if allow_discharge:
        actions += [("discharge", kw) for kw in DISCHARGE_POWER_LEVELS_KW]
    return actions


def _replay(slots, actions, initial_energy, battery, export_price_eur_kwh):
    energy = initial_energy
    rows = []
    for slot, action in zip(slots, actions):
        hours = slot.get("hours", SLOT_HOURS)
        start_energy = energy
        energy, grid_charge, grid_import, export, stored, after_charge = _transition(
            energy,
            action,
            slot["solar_kwh"],
            slot["consumption_kwh"],
            battery,
            hours,
        )
        rows.append(
            {
                "start": slot["start"],
                "hours": hours,
                "price": slot["price"],
                "action": action[0],
                "power_kw": action[1],
                "solar_kwh": slot["solar_kwh"],
                "consumption_kwh": slot["consumption_kwh"],
                "grid_charge_kwh": grid_charge,
                "grid_import_kwh": grid_import,
                "export_kwh": export,
                "soc_start": start_energy / battery.capacity_kwh * 100,
                "soc_after_charge": after_charge / battery.capacity_kwh * 100,
                "soc_end": energy / battery.capacity_kwh * 100,
                "cost_eur": grid_import * slot["price"] - export * export_price_eur_kwh,
                "wear_eur": stored * battery.wear_eur_per_kwh,
            }
        )
    return {
        "rows": rows,
        "final_energy_kwh": energy,
        "cost_eur": sum(row["cost_eur"] for row in rows),
        "wear_eur": sum(row["wear_eur"] for row in rows),
    }


def _plan_actions(slots, initial_energy, battery, terminal_value, export_price, actions, grid_margin):
    low = battery.min_energy_kwh
    high = battery.max_energy_kwh
    span = high - low
    count = max(1, round(span / SOC_STEP_KWH))
    levels = [low + span * index / count for index in range(count + 1)]

    def future(values, energy):
        position = min(count, max(0.0, (energy - low) / span * count))
        index = min(count - 1, int(position))
        fraction = position - index
        return values[index] * (1 - fraction) + values[index + 1] * fraction

    def choose(energy, slot, next_values):
        hours = slot.get("hours", SLOT_HOURS)
        best = None
        for action_index, action in enumerate(actions):
            energy_next, grid_charge, grid_import, export, stored, _ = _transition(
                energy, action, slot["solar_kwh"], slot["consumption_kwh"], battery, hours
            )
            total = (
                grid_import * slot["price"]
                - export * export_price
                + battery.wear_eur_per_kwh * stored
                + grid_margin * grid_charge
                + future(next_values, energy_next)
            )
            if best is None or total < best_total:
                best_total, best = total, action
        return best_total, best

    values = [None] * (len(slots) + 1)
    values[-1] = [-terminal_value * energy for energy in levels]
    for index in range(len(slots) - 1, -1, -1):
        values[index] = [
            choose(energy, slots[index], values[index + 1])[0] for energy in levels
        ]

    energy = initial_energy
    chosen = []
    for index, slot in enumerate(slots):
        _, action = choose(energy, slot, values[index + 1])
        chosen.append(action)
        energy = _transition(
            energy,
            action,
            slot["solar_kwh"],
            slot["consumption_kwh"],
            battery,
            slot.get("hours", SLOT_HOURS),
        )[0]
    return chosen


def _round_up_minute(moment):
    if moment.second or moment.microsecond:
        moment = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return moment


def _is_charging(row):
    return row["action"] == "charge" and row["grid_charge_kwh"] > 1e-6


def _charge_power_w(power_kw):
    return min(MAX_CHARGE_POWER_W, max(100, math.ceil(power_kw * 10 - 1e-9) * 100))


def _windows(rows, battery, display_tz):
    windows = []
    index = 0
    while index < len(rows):
        if not _is_charging(rows[index]):
            index += 1
            continue
        last = index
        while last + 1 < len(rows) and _is_charging(rows[last + 1]):
            last += 1
        chunk = rows[index : last + 1]
        total = sum(row["grid_charge_kwh"] for row in chunk)
        duration = sum(row["hours"] for row in chunk)
        power_w = _charge_power_w(total / duration)
        start = _round_up_minute(chunk[0]["start"])
        end = _round_up_minute(
            chunk[0]["start"] + timedelta(hours=total / (power_w / 1000))
        )
        if end <= start:
            end = start + timedelta(minutes=1)
        if display_tz is not None:
            start = start.astimezone(display_tz)
            end = end.astimezone(display_tz)
        target_soc = min(
            battery.max_soc,
            max(MIN_TARGET_SOC, math.ceil(chunk[-1]["soc_after_charge"] - 1e-9)),
        )
        weighted_price = sum(row["grid_charge_kwh"] * row["price"] for row in chunk) / total
        windows.append(
            {
                "charge_start": start.isoformat(timespec="minutes"),
                "charge_end": end.isoformat(timespec="minutes"),
                "charge_power_w": power_w,
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


def _display(moment, display_tz):
    return moment if display_tz is None else moment.astimezone(display_tz)


def _hourly_plan(rows, base_rows, display_tz):
    groups = {}
    for row, base in zip(rows, base_rows):
        key = row["start"].replace(minute=0, second=0, microsecond=0)
        group = groups.setdefault(
            key,
            {"prices": [], "solar": 0.0, "consumption": 0.0, "charge": 0.0,
             "grid_import": 0.0, "export": 0.0, "cost": 0.0, "base_cost": 0.0,
             "soc_start": row["soc_start"], "soc_end": row["soc_end"],
             "base_soc_end": base["soc_end"]},
        )
        group["prices"].append(row["price"])
        group["solar"] += row["solar_kwh"]
        group["consumption"] += row["consumption_kwh"]
        group["charge"] += row["grid_charge_kwh"]
        group["grid_import"] += row["grid_import_kwh"]
        group["export"] += row["export_kwh"]
        group["cost"] += row["cost_eur"]
        group["base_cost"] += base["cost_eur"]
        group["soc_end"] = row["soc_end"]
        group["base_soc_end"] = base["soc_end"]
    return [
        {
            "time": _display(key, display_tz).isoformat(timespec="minutes"),
            "tariff_eur_per_kwh": round(sum(group["prices"]) / len(group["prices"]), 4),
            "solar_kwh": round(group["solar"], 3),
            "consumption_kwh": round(group["consumption"], 3),
            "grid_charge_kwh": round(group["charge"], 3),
            "grid_import_kwh": round(group["grid_import"], 3),
            "export_kwh": round(group["export"], 3),
            "soc_start": round(group["soc_start"], 1),
            "soc_end": round(group["soc_end"], 1),
            "cost_eur": round(group["cost"], 4),
            "cost_self_use_eur": round(group["base_cost"], 4),
            "soc_self_use_end": round(group["base_soc_end"], 1),
        }
        for key, group in groups.items()
    ]


def _slot_plan(rows, display_tz):
    plan = []
    for row in rows:
        moment = row["start"]
        if display_tz is not None:
            moment = moment.astimezone(display_tz)
        charging = _is_charging(row)
        plan.append(
            {
                "time": moment.isoformat(timespec="minutes"),
                "action": "charge" if charging else "self_use",
                "charge_power_w": _charge_power_w(row["grid_charge_kwh"] / row["hours"]) if charging else None,
                "tariff_eur_per_kwh": round(row["price"], 4),
                "soc_end": round(row["soc_end"], 1),
            }
        )
    return plan


def optimize_schedule(
    slots,
    initial_energy,
    battery,
    terminal_value_eur_per_kwh,
    *,
    export_price_eur_kwh=0.0,
    allow_hold=False,
    allow_discharge=False,
    display_tz=None,
    grid_charge_margin_eur_kwh=GRID_CHARGE_MARGIN_EUR_PER_KWH,
):
    """Choose battery actions per slot minimising cost over the horizon.

    slots need start (aware UTC datetime), price, solar_kwh and consumption_kwh, and
    may set hours (default 0.25). terminal_value_eur_per_kwh values the energy left in
    the battery at the end of the horizon (stored DC kWh). Battery wear is charged per
    kWh stored and reported separately from the electricity cost.
    """
    if not slots:
        raise ValueError("At least one forecast slot is required.")
    actions = _actions(battery, allow_hold, allow_discharge)
    chosen = _plan_actions(
        slots,
        initial_energy,
        battery,
        terminal_value_eur_per_kwh,
        export_price_eur_kwh,
        actions,
        grid_charge_margin_eur_kwh,
    )
    plan = _replay(slots, chosen, initial_energy, battery, export_price_eur_kwh)
    windows = _windows(plan["rows"], battery, display_tz)
    for number, window in enumerate(windows, start=1):
        window["id"] = f"window_{number}"
        for key in ("_first", "_last", "_total"):
            window.pop(key)
    baseline = _replay(
        slots,
        [SELF_USE] * len(slots),
        initial_energy,
        battery,
        export_price_eur_kwh,
    )
    stored_delta = plan["final_energy_kwh"] - baseline["final_energy_kwh"]
    cash_savings = baseline["cost_eur"] - plan["cost_eur"]
    wear_delta = plan["wear_eur"] - baseline["wear_eur"]
    return {
        "windows": windows,
        "cost_with_plan_eur": round(plan["cost_eur"], 4),
        "cost_self_use_eur": round(baseline["cost_eur"], 4),
        "cash_savings_eur": round(cash_savings, 4),
        "stored_energy_delta_kwh": round(stored_delta, 3),
        "battery_wear_cost_eur": round(wear_delta, 4),
        "estimated_savings_eur": round(
            cash_savings
            + terminal_value_eur_per_kwh * stored_delta
            - wear_delta,
            4,
        ),
        "hourly_plan": _hourly_plan(plan["rows"], baseline["rows"], display_tz),
        "slot_plan": _slot_plan(plan["rows"], display_tz),
    }


def plan_actions(
    slots,
    initial_energy,
    battery,
    terminal_value_eur_per_kwh,
    *,
    export_price_eur_kwh=0.0,
    allow_hold=False,
    allow_discharge=False,
    grid_charge_margin_eur_kwh=GRID_CHARGE_MARGIN_EUR_PER_KWH,
):
    return _plan_actions(
        slots,
        initial_energy,
        battery,
        terminal_value_eur_per_kwh,
        export_price_eur_kwh,
        _actions(battery, allow_hold, allow_discharge),
        grid_charge_margin_eur_kwh,
    )


def evaluate_actions(slots, actions, initial_energy, battery, export_price_eur_kwh=0.0):
    return _replay(slots, actions, initial_energy, battery, export_price_eur_kwh)


def replay_self_use(samples, initial_energy, battery):
    """Replay measured load and solar as if the inverter had stayed in self-use.

    samples need start, hours, generation_kw, consumption_kw and price.
    Returns cumulative cost points and the final battery energy.
    """
    energy = initial_energy
    cumulative = 0.0
    points = []
    for sample in samples:
        hours = sample["hours"]
        energy, _, grid_import, _, _, _ = _transition(
            energy,
            SELF_USE,
            max(0.0, sample["generation_kw"]) * hours,
            max(0.0, sample["consumption_kw"]) * hours,
            battery,
            hours,
        )
        cumulative += grid_import * sample["price"]
        points.append(
            {
                "start": sample["start"],
                "cumulative_cost_eur": cumulative,
                "soc": energy / battery.capacity_kwh * 100,
            }
        )
    return {"points": points, "final_energy_kwh": energy, "cost_eur": cumulative}
