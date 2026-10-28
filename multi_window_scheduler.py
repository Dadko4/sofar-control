"""Multi-window charging scheduler.

Manages multiple charging windows per day with automatic sequencing:
- Stores queue of windows to charge
- Starts first window when time comes
- Transitions to self-use after each window
- Retry logic (5x with 2min intervals) on mode change failures
- Records daily results to history store
"""

import json
import math
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from planner import BatteryModel, replay_self_use

LOGGER = None  # Will be set by run.py


def _finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def compare_plan_with_telemetry(
    forecast,
    telemetry,
    horizon_start,
    horizon_end,
    coverage_horizon_end=None,
):
    start = datetime.fromisoformat(horizon_start).astimezone(timezone.utc)
    end = datetime.fromisoformat(horizon_end).astimezone(timezone.utc)
    coverage_end = datetime.fromisoformat(
        coverage_horizon_end or horizon_end
    ).astimezone(timezone.utc)
    if end <= start or coverage_end <= start:
        raise ValueError("Plan evaluation horizon must be increasing.")

    forecast_hours = forecast.get("hourly_plan", [])
    actual_by_hour = {}
    price_by_hour = {}
    for row in forecast_hours:
        hour = datetime.fromisoformat(row["time"]).astimezone(timezone.utc)
        hour = hour.replace(minute=0, second=0, microsecond=0)
        actual_by_hour[hour] = {
            "time": row["time"],
            "solar_kwh": 0.0,
            "consumption_kwh": 0.0,
            "grid_import_kwh": 0.0,
            "cost_eur": 0.0,
            "simulation_cost_eur": 0.0,
            "self_use_cost_eur": 0.0,
            "covered_seconds": 0.0,
            "sample_count": 0,
            "soc_end": None,
            "has_grid_data": False,
            "has_simulation_cost": False,
            "has_self_use_cost": False,
        }
        price_by_hour[hour] = _finite_number(row.get("tariff_eur_per_kwh"))

    samples = []
    for row in telemetry:
        sampled_at = datetime.fromisoformat(row["sampled_at"]).astimezone(timezone.utc)
        if start <= sampled_at < end:
            hour = sampled_at.replace(minute=0, second=0, microsecond=0)
            actual = actual_by_hour.get(hour)
            if actual is not None:
                actual["sample_count"] += 1
                actual["soc_end"] = _finite_number(row.get("battery_soc"))

    ordered = sorted(
        (
            (
                datetime.fromisoformat(row["sampled_at"]).astimezone(timezone.utc),
                row,
            )
            for row in telemetry
        ),
        key=lambda sample: sample[0],
    )
    actual_start_soc = None
    for sampled_at, row in ordered:
        if sampled_at >= start:
            actual_start_soc = _finite_number(row.get("battery_soc"))
            if actual_start_soc is not None:
                break

    actual_grid_import = 0.0
    actual_cost = 0.0
    actual_cost_on_simulation_coverage = 0.0
    grid_data_intervals = 0
    matched_cost_intervals = 0
    covered_seconds = 0.0
    replay_samples = []

    for (previous_at, previous), (sampled_at, row) in zip(ordered, ordered[1:]):
        interval_seconds = (sampled_at - previous_at).total_seconds()
        if interval_seconds <= 0:
            continue
        overlap_start = max(previous_at, start)
        overlap_end = min(sampled_at, end)
        if overlap_end <= overlap_start:
            continue

        grid_delta = _finite_number(row.get("observed_grid_import_delta_kwh"))
        grid_cost = _finite_number(
            row.get("observed_grid_import_cost_with_aid_eur")
        )
        if grid_delta is not None or grid_cost is not None:
            overlap_fraction = (
                (overlap_end - overlap_start).total_seconds() / interval_seconds
            )
            actual_grid_import += (grid_delta or 0.0) * overlap_fraction
            actual_cost += (grid_cost or 0.0) * overlap_fraction
            grid_data_intervals += 1

        if interval_seconds > 900:
            continue
        generation_start = _finite_number(previous.get("generation_kw"))
        generation_end = _finite_number(row.get("generation_kw"))
        consumption_start = _finite_number(previous.get("consumption_kw"))
        consumption_end = _finite_number(row.get("consumption_kw"))
        if any(
            value is None
            for value in (
                generation_start,
                generation_end,
                consumption_start,
                consumption_end,
            )
        ):
            continue
        if grid_cost is not None:
            actual_cost_on_simulation_coverage += grid_cost * (
                (overlap_end - overlap_start).total_seconds() / interval_seconds
            )
            matched_cost_intervals += 1

        cursor = overlap_start
        while cursor < overlap_end:
            hour = cursor.replace(minute=0, second=0, microsecond=0)
            segment_end = min(overlap_end, hour + timedelta(hours=1))
            duration_seconds = (segment_end - cursor).total_seconds()
            start_fraction = (cursor - previous_at).total_seconds() / interval_seconds
            end_fraction = (segment_end - previous_at).total_seconds() / interval_seconds

            generation_at_start = generation_start + (
                generation_end - generation_start
            ) * start_fraction
            generation_at_end = generation_start + (
                generation_end - generation_start
            ) * end_fraction
            consumption_at_start = consumption_start + (
                consumption_end - consumption_start
            ) * start_fraction
            consumption_at_end = consumption_start + (
                consumption_end - consumption_start
            ) * end_fraction
            generation_kw = max(
                0.0, (generation_at_start + generation_at_end) / 2
            )
            consumption_kw = max(
                0.0, (consumption_at_start + consumption_at_end) / 2
            )
            actual = actual_by_hour.get(hour)
            if actual is not None:
                duration_hours = duration_seconds / 3600
                actual["solar_kwh"] += generation_kw * duration_hours
                actual["consumption_kwh"] += consumption_kw * duration_hours
                actual["covered_seconds"] += duration_seconds
                covered_seconds += duration_seconds
                if grid_cost is not None:
                    actual["simulation_cost_eur"] += grid_cost * (
                        duration_seconds / interval_seconds
                    )
                    actual["has_simulation_cost"] = True
                price = price_by_hour.get(hour)
                if price is not None:
                    replay_samples.append(
                        {
                            "start": cursor,
                            "hours": duration_hours,
                            "generation_kw": generation_kw,
                            "consumption_kw": consumption_kw,
                            "price": price,
                            "has_observed_cost": grid_cost is not None,
                        }
                    )
            cursor = segment_end

    # Counter readings are interval deltas, so distribute them across the
    # forecast hours covered by each telemetry interval.
    for (previous_at, _), (sampled_at, row) in zip(ordered, ordered[1:]):
        interval_seconds = (sampled_at - previous_at).total_seconds()
        if interval_seconds <= 0:
            continue
        overlap_start = max(previous_at, start)
        overlap_end = min(sampled_at, end)
        if overlap_end <= overlap_start:
            continue
        grid_delta = _finite_number(row.get("observed_grid_import_delta_kwh"))
        grid_cost = _finite_number(
            row.get("observed_grid_import_cost_with_aid_eur")
        )
        if grid_delta is None and grid_cost is None:
            continue
        cursor = overlap_start
        while cursor < overlap_end:
            hour = cursor.replace(minute=0, second=0, microsecond=0)
            segment_end = min(overlap_end, hour + timedelta(hours=1))
            fraction = (segment_end - cursor).total_seconds() / interval_seconds
            actual = actual_by_hour.get(hour)
            if actual is not None:
                actual["has_grid_data"] = True
                actual["grid_import_kwh"] += (grid_delta or 0.0) * fraction
                actual["cost_eur"] += (grid_cost or 0.0) * fraction
            cursor = segment_end

    self_use_cost = None
    matched_self_use_cost = 0.0
    matched_self_use_coverage_seconds = 0.0
    battery_data = forecast.get("battery_model")
    if (
        replay_samples
        and actual_start_soc is not None
        and isinstance(battery_data, dict)
    ):
        battery = BatteryModel(
            capacity_kwh=float(battery_data["capacity_kwh"]),
            min_energy_kwh=float(battery_data["min_energy_kwh"]),
            max_energy_kwh=float(battery_data["max_energy_kwh"]),
            charge_efficiency=float(battery_data["charge_efficiency"]),
            discharge_efficiency=float(battery_data["discharge_efficiency"]),
            max_grid_kw=float(battery_data.get("max_grid_kw", 2.0)),
            max_soc=int(battery_data.get("max_soc", 95)),
        )
        self_use = replay_self_use(
            replay_samples,
            battery.capacity_kwh * actual_start_soc / 100,
            battery,
        )
        self_use_cost = round(self_use["cost_eur"], 4)
        previous_cumulative = 0.0
        for sample, point in zip(replay_samples, self_use["points"]):
            hourly_cost = point["cumulative_cost_eur"] - previous_cumulative
            previous_cumulative = point["cumulative_cost_eur"]
            if not sample["has_observed_cost"]:
                continue
            matched_self_use_cost += hourly_cost
            matched_self_use_coverage_seconds += sample["hours"] * 3600
            hour = point["start"].astimezone(timezone.utc)
            hour = hour.replace(minute=0, second=0, microsecond=0)
            actual = actual_by_hour.get(hour)
            if actual is not None:
                actual["self_use_cost_eur"] += hourly_cost
                actual["has_self_use_cost"] = True

    hourly = []
    for hour, actual in sorted(actual_by_hour.items()):
        hourly.append(
            {
                "time": actual["time"],
                "solar_kwh": round(actual["solar_kwh"], 3)
                if actual["covered_seconds"]
                else None,
                "consumption_kwh": round(actual["consumption_kwh"], 3)
                if actual["covered_seconds"]
                else None,
                "grid_import_kwh": round(actual["grid_import_kwh"], 3)
                if actual["has_grid_data"]
                else None,
                "cost_eur": round(actual["cost_eur"], 4)
                if actual["has_grid_data"]
                else None,
                "simulation_cost_eur": round(actual["simulation_cost_eur"], 4)
                if actual["has_simulation_cost"]
                else None,
                "self_use_cost_eur": round(actual["self_use_cost_eur"], 4)
                if actual["has_self_use_cost"]
                else None,
                "estimated_savings_eur": round(
                    actual["self_use_cost_eur"] - actual["simulation_cost_eur"], 4
                )
                if actual["has_simulation_cost"] and actual["has_self_use_cost"]
                else None,
                "soc_end": round(actual["soc_end"], 1)
                if actual["soc_end"] is not None
                else None,
                "coverage_percent": round(
                    min(100.0, actual["covered_seconds"] / 3600 * 100), 1
                ),
                "sample_count": actual["sample_count"],
            }
        )

    horizon_seconds = (coverage_end - start).total_seconds()
    replayed_seconds = sum(sample["hours"] * 3600 for sample in replay_samples)
    total_generation = sum(
        sample["solar_kwh"] or 0 for sample in hourly
    )
    total_consumption = sum(
        sample["consumption_kwh"] or 0 for sample in hourly
    )
    return {
        "actual_grid_import_kwh": round(actual_grid_import, 3)
        if grid_data_intervals
        else None,
        "actual_cost_with_aid_eur": round(actual_cost, 4)
        if grid_data_intervals
        else None,
        "actual_cost_on_simulation_coverage_eur": round(
            actual_cost_on_simulation_coverage, 4
        )
        if matched_cost_intervals
        else None,
        "self_use_counterfactual_cost_eur": self_use_cost,
        "self_use_cost_on_simulation_coverage_eur": round(
            matched_self_use_cost, 4
        )
        if matched_self_use_coverage_seconds
        else None,
        "cost_comparison_coverage_percent": round(
            min(100.0, matched_self_use_coverage_seconds / horizon_seconds * 100),
            1,
        )
        if horizon_seconds > 0
        else 0,
        "estimated_savings_vs_self_use_eur": round(
            matched_self_use_cost - actual_cost_on_simulation_coverage, 4
        )
        if matched_self_use_coverage_seconds and matched_cost_intervals
        else None,
        "actual_generation_kwh": round(total_generation, 3)
        if covered_seconds
        else None,
        "actual_consumption_kwh": round(total_consumption, 3)
        if covered_seconds
        else None,
        "actual_start_soc": round(actual_start_soc, 1)
        if actual_start_soc is not None
        else None,
        "actual_end_soc": next(
            (sample["soc_end"] for sample in reversed(hourly) if sample["soc_end"] is not None),
            None,
        ),
        "telemetry_coverage_percent": round(
            min(100.0, covered_seconds / horizon_seconds * 100), 1
        ),
        "self_use_simulation_coverage_percent": round(
            min(100.0, replayed_seconds / horizon_seconds * 100), 1
        ),
        "telemetry_sample_count": sum(
            sample["sample_count"] for sample in hourly
        ),
        "hourly": hourly,
    }


class MultiWindowScheduler:
    """Queue-based multi-window charger for 4T tariff optimization."""
    
    def __init__(self, client, state_path, mode_lock, discharge_active_fn, timezone_str, logger):
        self.client = client
        self.state_path = Path(state_path)
        self.mode_lock = mode_lock
        self.discharge_active_fn = discharge_active_fn
        self.timezone_str = timezone_str
        global LOGGER
        LOGGER = logger
        self.history_store = None
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread = None
        self._state = {
            "status": "idle",
            "windows_queue": [],  # List of {charge_start, charge_end, target_soc, ...}
            "current_window_index": 0,
            "plan_date": None,
            "error": None,
            "plan_id": None,
            "plan_snapshot": None,
            "horizon_start": None,
            "horizon_end": None,
        }
        self._load_state()
    
    def _load_state(self):
        """Load saved state from disk."""
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(state, dict) and "windows_queue" in state:
                self._state = state
        except FileNotFoundError:
            pass
        except Exception as e:
            LOGGER.warning(f"Could not load multi-window state: {e}")
    
    def _save_state(self):
        """Save state to disk atomically."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            temp_path = self.state_path.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(self._state, f)
            temp_path.replace(self.state_path)
        except (OSError, TypeError, ValueError):
            LOGGER.exception("Could not save multi-window state.")
            raise
    
    def schedule_windows(self, windows, plan_date, plan_snapshot=None):
        """Schedule multiple charging windows for a day.
        
        Args:
            windows: List of window dicts with charge_start, charge_end, target_soc, grid_charge_kwh
            plan_date: Date string (YYYY-MM-DD)
        """
        if not isinstance(windows, list) or (
            not windows
            and not (
                isinstance(plan_snapshot, dict)
                and plan_snapshot.get("continuous_optimization") is True
            )
        ):
            raise ValueError("At least one charging window is required.")
        if not isinstance(plan_date, str) or not plan_date:
            raise ValueError("Plan date must be a non-empty string.")
        if plan_snapshot is not None:
            if not isinstance(plan_snapshot, dict):
                raise ValueError("Plan snapshot must be an object.")
            if (
                plan_snapshot.get("plan_date") != plan_date
                or plan_snapshot.get("windows") != windows
            ):
                raise ValueError("Plan snapshot does not match its scheduled windows.")

        with self._lock:
            previous_state = dict(self._state)
        is_continuous = (
            isinstance(plan_snapshot, dict)
            and plan_snapshot.get("continuous_optimization") is True
        )
        session = (
            self.history_store.get_latest_continuous_optimization_session()
            if is_continuous and self.history_store is not None
            else None
        )
        continuous_session_id = (
            session["id"]
            if isinstance(session, dict) and session.get("ended_at") is None
            else None
        )
        continuous_plan_id = (
            f"continuous-session-{continuous_session_id}"
            if continuous_session_id is not None
            else None
        )
        reuse_continuous_plan = (
            continuous_session_id is not None
            and previous_state.get("continuous_session_id") == continuous_session_id
            and previous_state.get("plan_id") == continuous_plan_id
        )
        if (
            previous_state.get("plan_id")
            and not reuse_continuous_plan
            and previous_state.get("status") in {
                "pending",
                "charging",
                "transitioning",
                "evaluating",
            }
        ):
            self._record_plan_result("replaced", datetime.now(ZoneInfo(self.timezone_str)))

        if continuous_plan_id is not None:
            plan_id = continuous_plan_id
        elif plan_snapshot is not None:
            plan_id = uuid.uuid4().hex
        else:
            plan_id = None
        if plan_id and self.history_store is not None:
            if reuse_continuous_plan:
                self.history_store.update_optimization_plan_forecast(
                    plan_id,
                    plan_snapshot,
                    "continuous",
                )
            else:
                self.history_store.record_optimization_plan(
                    plan_id,
                    plan_snapshot,
                    "continuous" if is_continuous else "pending",
                )
        with self._lock:
            self._state = {
                "status": "pending",
                "windows_queue": windows,
                "current_window_index": 0,
                "plan_date": plan_date,
                "error": None,
                "plan_id": plan_id,
                "plan_snapshot": plan_snapshot,
                "horizon_start": (
                    plan_snapshot.get("horizon_start")
                    if isinstance(plan_snapshot, dict)
                    else None
                ),
                "horizon_end": (
                    plan_snapshot.get("horizon_end")
                    if isinstance(plan_snapshot, dict)
                    else None
                ),
                "continuous_optimization": (
                    is_continuous
                    if isinstance(plan_snapshot, dict)
                    else False
                ),
                "continuous_session_id": continuous_session_id,
            }
            self._save_state()
            LOGGER.info(f"Scheduled {len(windows)} charging windows for {plan_date}")
            state = dict(self._state)
        state.pop("plan_snapshot", None)
        if plan_id:
            state["plan_id"] = plan_id
        return state
    
    def get_state(self):
        """Get current scheduler state."""
        with self._lock:
            state = dict(self._state)
        state.pop("plan_snapshot", None)
        return state
    
    def run_due(self, now=None):
        """Check if a window should be active now and execute it."""
        if now is None:
            now = datetime.now(ZoneInfo(self.timezone_str))
        if now.tzinfo is None:
            now = now.replace(tzinfo=ZoneInfo(self.timezone_str))

        action = None
        with self._lock:
            if self._state["status"] == "evaluating":
                if now < self._evaluation_end(self._state):
                    return
                action = "finish"
            else:
                if self._state["status"] not in (
                    "pending",
                    "charging",
                    "transitioning",
                ):
                    return
                windows = self._state.get("windows_queue", [])
                idx = self._state.get("current_window_index", 0)
                if idx >= len(windows):
                    self._state["status"] = (
                        "evaluating" if self._state.get("plan_id") else "completed"
                    )
                    self._save_state()
                    if (
                        self._state.get("plan_id")
                        and now >= self._evaluation_end(self._state)
                    ):
                        action = "finish"
                    else:
                        return
                else:
                    window = windows[idx]
                    start_dt = datetime.fromisoformat(window["charge_start"])
                    end_dt = datetime.fromisoformat(window["charge_end"])
                    if start_dt.tzinfo is None:
                        start_dt = start_dt.replace(
                            tzinfo=ZoneInfo(self.timezone_str)
                        )
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=ZoneInfo(self.timezone_str))

                    if now < start_dt:
                        return
                    if now >= end_dt:
                        self._state["current_window_index"] += 1
                        self._state["status"] = "transitioning"
                        self._save_state()
                        action = "transition"
                    elif self._state["status"] == "pending":
                        self._state["status"] = "charging"
                        self._save_state()
                        action = "start"

        if action == "finish":
            self._finish_evaluation(now)
        elif action == "transition":
            self._return_to_self_use_then_next(now)
        elif action == "start":
            self._start_window_charging(window)
    
    def _start_window_charging(self, window):
        """Start charging for a window with retry logic."""
        target_soc = window.get("recommended_target_soc", 80)
        max_retries = 5
        retry_interval = 120  # 2 min
        
        for attempt in range(1, max_retries + 1):
            try:
                if self.discharge_active_fn():
                    LOGGER.warning("Cannot start charging: discharge active")
                    return
                
                with self.mode_lock:
                    self.client.set_energy_storage_mode(
                        "grid_charge",
                        2000,
                        confirmed=True,
                        target_soc=target_soc,
                    )
                LOGGER.info(f"Window charging started: target SOC {target_soc}%")
                return
            except Exception as e:
                if attempt < max_retries:
                    LOGGER.warning(f"Charging start failed (attempt {attempt}/{max_retries}): {e}")
                    time.sleep(retry_interval)
                else:
                    LOGGER.error(f"Charging start failed after {max_retries} attempts")
                    with self._lock:
                        self._state["status"] = "failed"
                        self._state["error"] = str(e)
                        self._save_state()
                    self._record_plan_result(
                        "failed", datetime.now(ZoneInfo(self.timezone_str))
                    )
    
    def _return_to_self_use_then_next(self, now=None):
        """Return to self-use after window ends, then start next window if any."""
        now = now or datetime.now(ZoneInfo(self.timezone_str))
        max_retries = 5
        retry_interval = 120

        for attempt in range(1, max_retries + 1):
            try:
                with self.mode_lock:
                    self.client.set_energy_storage_mode("self_use", confirmed=True)
            except Exception as e:
                if attempt < max_retries:
                    LOGGER.warning(f"Self-use return failed (attempt {attempt}/{max_retries}): {e}")
                    time.sleep(retry_interval)
                else:
                    LOGGER.error(f"Self-use return failed after {max_retries} attempts")
                    with self._lock:
                        self._state["status"] = "failed"
                        self._state["error"] = str(e)
                        self._save_state()
                    self._record_plan_result(
                        "failed", datetime.now(ZoneInfo(self.timezone_str))
                    )
                return

            LOGGER.info("Returned to self-use after window")
            with self._lock:
                windows = self._state.get("windows_queue", [])
                idx = self._state.get("current_window_index", 0)
                plan_id = self._state.get("plan_id")
                if idx < len(windows):
                    self._state["status"] = "pending"
                    should_finish = False
                elif plan_id:
                    self._state["status"] = "evaluating"
                    should_finish = now >= self._evaluation_end(self._state)
                else:
                    self._state["status"] = "completed"
                    should_finish = False
                self._save_state()
            if idx >= len(windows) and not plan_id:
                self._record_daily_result()
            if should_finish:
                self._finish_evaluation(now)
            return
    
    def cancel(self, record_result=True):
        """Cancel scheduled windows."""
        with self._lock:
            if self._state["status"] not in {
                "pending",
                "charging",
                "transitioning",
                "evaluating",
            }:
                return self.get_state()
            state = dict(self._state)
        if state.get("plan_id") and record_result:
            self._record_plan_result(
                "cancelled", datetime.now(ZoneInfo(self.timezone_str))
            )
        with self._lock:
            if (
                self._state.get("plan_id") != state.get("plan_id")
                or self._state["status"]
                not in {"pending", "charging", "transitioning", "evaluating"}
            ):
                return self.get_state()
            self._state["status"] = "cancelled"
            self._save_state()
            return self.get_state()

    def _evaluation_end(self, state):
        horizon_end = state.get("horizon_end")
        if horizon_end:
            moment = datetime.fromisoformat(horizon_end)
        else:
            windows = state.get("windows_queue", [])
            moment = datetime.fromisoformat(windows[-1]["charge_end"])
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=ZoneInfo(self.timezone_str))
        return moment

    def _record_plan_result(self, plan_status, now):
        with self._lock:
            state = dict(self._state)
        plan_id = state.get("plan_id")
        forecast = state.get("plan_snapshot")
        if not plan_id or not isinstance(forecast, dict) or self.history_store is None:
            return
        horizon_start = state.get("horizon_start") or forecast.get("horizon_start")
        horizon_end = state.get("horizon_end") or forecast.get("horizon_end")
        if not horizon_start or not horizon_end:
            raise ValueError("Saved optimization plan is missing its evaluation horizon.")
        start = datetime.fromisoformat(horizon_start)
        end = datetime.fromisoformat(horizon_end)
        evaluation_end = min(now.astimezone(end.tzinfo), end)
        if evaluation_end <= start:
            actual = {
                "actual_grid_import_kwh": None,
                "actual_cost_with_aid_eur": None,
                "self_use_counterfactual_cost_eur": None,
                "estimated_savings_vs_self_use_eur": None,
                "actual_generation_kwh": None,
                "actual_consumption_kwh": None,
                "actual_start_soc": None,
                "actual_end_soc": None,
                "telemetry_coverage_percent": 0,
                "telemetry_sample_count": 0,
                "hourly": [],
            }
        else:
            telemetry = self.history_store.telemetry_for_period(
                horizon_start,
                evaluation_end.isoformat(timespec="minutes"),
            )
            actual = compare_plan_with_telemetry(
                forecast,
                telemetry,
                horizon_start,
                evaluation_end.isoformat(timespec="minutes"),
                coverage_horizon_end=horizon_end,
            )
        actual["evaluation_status"] = plan_status
        self.history_store.update_optimization_plan_result(
            plan_id,
            plan_status,
            actual,
        )

    def _finish_evaluation(self, now):
        with self._lock:
            is_continuous = self._state.get("continuous_optimization") is True
        if not is_continuous:
            self._record_plan_result("completed", now)
        with self._lock:
            if self._state.get("status") == "evaluating":
                self._state["status"] = "completed"
                self._save_state()
    
    def start(self):
        """Start background monitoring thread."""
        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
    
    def stop(self):
        """Stop background monitoring thread."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
    
    def _run(self):
        """Background loop: check due windows every second."""
        while not self._stop_event.is_set():
            try:
                self.run_due()
            except Exception as e:
                LOGGER.exception(f"Multi-window scheduler error: {e}")
            self._stop_event.wait(1)
    
    def _record_daily_result(self):
        """Record daily plan results to history store."""
        if self.history_store is None:
            LOGGER.warning("Cannot record daily result: history store not initialized")
            return
        
        try:
            with self._lock:
                state = dict(self._state)
            
            plan_date = state.get("plan_date")
            windows = state.get("windows_queue", [])
            if not plan_date or not windows:
                return
            
            # Get telemetry since first window start
            plan_start = datetime.fromisoformat(windows[0]["charge_start"])
            if plan_start.tzinfo is None:
                plan_start = plan_start.replace(tzinfo=ZoneInfo(self.timezone_str))
            
            plan_end = datetime.fromisoformat(windows[-1]["charge_end"])
            if plan_end.tzinfo is None:
                plan_end = plan_end.replace(tzinfo=ZoneInfo(self.timezone_str))
            
            # Query telemetry
            import sqlite3
            from contextlib import closing
            
            with closing(self.history_store._connect()) as conn:
                # Get total costs and grid import
                row = conn.execute(
                    """
                    SELECT 
                        SUM(observed_grid_import_delta_kwh) as total_grid_kwh,
                        SUM(observed_grid_import_cost_with_aid_eur) as cost_with_aid,
                        SUM(observed_grid_import_cost_without_aid_eur) as cost_without_aid
                    FROM telemetry
                    WHERE sampled_at >= ? AND sampled_at <= ?
                    """,
                    (plan_start.isoformat(), plan_end.isoformat())
                ).fetchone()
                
                grid_import_kwh = row[0] or 0.0
                cost_with_aid = row[1] or 0.0
                cost_without_aid = row[2] or 0.0
                
                # Get daily totals at end of day
                row = conn.execute(
                    """
                    SELECT 
                        daily_generation_kwh, daily_consumption_kwh, daily_battery_discharge_kwh
                    FROM telemetry
                    WHERE substr(sampled_at, 1, 10) = ?
                    ORDER BY sampled_at DESC
                    LIMIT 1
                    """,
                    (plan_date,)
                ).fetchone()
                
                generation_kwh = (row[0] if row else None) or 0.0
                consumption_kwh = (row[1] if row else None) or 0.0
                battery_discharge_kwh = (row[2] if row else None) or 0.0
            
            # Record result
            self.history_store.record_daily_result(
                plan_date,
                len(windows),
                state.get("status", "unknown"),
                grid_import_kwh,
                cost_with_aid,
                cost_without_aid,
                generation_kwh,
                consumption_kwh,
                battery_discharge_kwh,
            )
            LOGGER.info(f"Recorded daily result for {plan_date}: {len(windows)} windows, €{cost_with_aid:.2f} cost")
        except Exception as e:
            LOGGER.exception(f"Could not record daily result: {e}")
