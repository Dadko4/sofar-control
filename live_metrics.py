"""Live monitoring metrics for active charging plans."""

import math
import sqlite3
from datetime import datetime, timedelta
from contextlib import closing
from zoneinfo import ZoneInfo


def calculate_live_metrics(history_store, multi_window_scheduler, timezone_str, now=None):
    """Calculate real-time cost vs self-use baseline for active plan.
    
    Returns dict with:
    - is_active: bool - whether plan is currently running
    - current_window_index: int - which window (0-indexed)
    - current_window: dict - {start, end, target_soc, ...}
    - time_in_window_percent: float - 0-100
    - grid_import_kwh: float - actual grid import so far in plan
    - plan_cost_eur: float - actual cost with plan (using real tariffs)
    - self_use_cost_eur: float - what cost would be without plan
    - savings_eur: float - difference
    - plan_start: str - ISO timestamp
    - now: str - ISO timestamp
    """
    if now is None:
        now = datetime.now(ZoneInfo(timezone_str))
    
    if history_store is None:
        return {
            "is_active": False,
            "error": "History store not initialized"
        }
    
    state = multi_window_scheduler.get_state()
    windows = state.get("windows_queue", [])
    
    if state["status"] not in ("charging", "transitioning", "pending"):
        return {
            "is_active": False,
            "reason": state["status"]
        }
    
    if not windows:
        return {
            "is_active": False,
            "reason": "No windows in queue"
        }
    
    # Plan started at first window's start time
    plan_start = datetime.fromisoformat(windows[0]["charge_start"])
    if plan_start.tzinfo is None:
        plan_start = plan_start.replace(tzinfo=ZoneInfo(timezone_str))
    
    if now < plan_start:
        return {
            "is_active": False,
            "reason": "Plan not started yet"
        }
    
    # Get telemetry since plan start
    try:
        with closing(history_store._connect()) as conn:
            cursor = conn.execute(
                """
                SELECT 
                    SUM(observed_grid_import_delta_kwh) as total_grid_import_kwh,
                    SUM(observed_grid_import_cost_with_aid_eur) as cost_with_aid,
                    SUM(observed_grid_import_cost_without_aid_eur) as cost_without_aid
                FROM telemetry
                WHERE sampled_at >= ? AND sampled_at <= ?
                """,
                (plan_start.isoformat(), now.isoformat())
            )
            row = cursor.fetchone()
            grid_import_kwh = row[0] or 0.0
            cost_with_aid = row[1] or 0.0
            cost_without_aid = row[2] or 0.0
    except (sqlite3.Error, TypeError, IndexError):
        # If history not available, return partial
        grid_import_kwh = 0.0
        cost_with_aid = 0.0
        cost_without_aid = 0.0
    
    # Current window info
    current_idx = state.get("current_window_index", 0)
    if current_idx >= len(windows):
        current_window = None
        time_in_window_pct = 100.0
    else:
        current_window = windows[current_idx]
        window_start = datetime.fromisoformat(current_window["charge_start"])
        window_end = datetime.fromisoformat(current_window["charge_end"])
        if window_start.tzinfo is None:
            window_start = window_start.replace(tzinfo=ZoneInfo(timezone_str))
        if window_end.tzinfo is None:
            window_end = window_end.replace(tzinfo=ZoneInfo(timezone_str))
        
        if now < window_start:
            time_in_window_pct = 0.0
        elif now >= window_end:
            time_in_window_pct = 100.0
        else:
            total_duration = (window_end - window_start).total_seconds()
            elapsed = (now - window_start).total_seconds()
            time_in_window_pct = min(100.0, max(0.0, (elapsed / total_duration) * 100.0))
    
    return {
        "is_active": True,
        "plan_date": state.get("plan_date"),
        "current_window_index": current_idx,
        "total_windows": len(windows),
        "current_window": current_window,
        "time_in_window_percent": time_in_window_pct,
        "grid_import_kwh": grid_import_kwh,
        "plan_cost_eur": cost_with_aid,
        "self_use_cost_eur": cost_without_aid,
        "savings_eur": cost_without_aid - cost_with_aid,
        "plan_start": plan_start.isoformat(timespec="seconds"),
        "now": now.isoformat(timespec="seconds"),
    }
