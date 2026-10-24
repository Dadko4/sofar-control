"""Test daily results recording and retrieval."""

import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from history import HistoryStore


def test_record_and_retrieve_daily_result():
    """Test recording and retrieving daily results."""
    db_path = Path(tempfile.gettempdir()) / "test_daily_results.db"
    db_path.unlink(missing_ok=True)
    
    store = HistoryStore(db_path)
    
    # Record a daily result
    store.record_daily_result(
        plan_date="2026-10-23",
        windows_count=2,
        plan_status="completed",
        grid_import_kwh=5.5,
        cost_with_aid=0.68,
        cost_without_aid=0.85,
        generation_kwh=12.3,
        consumption_kwh=8.4,
        battery_discharge_kwh=2.1,
    )
    
    # Retrieve results
    results = store.get_daily_results(limit=10)
    
    assert len(results) > 0
    result = results[0]
    assert result["plan_date"] == "2026-10-23"
    assert result["windows_count"] == 2
    assert result["plan_status"] == "completed"
    assert result["actual_grid_import_kwh"] == 5.5
    assert result["actual_cost_with_aid_eur"] == 0.68
    assert result["actual_cost_without_aid_eur"] == 0.85
    print("✓ Daily results recorded and retrieved correctly")


def test_multiple_daily_results():
    """Test storing and sorting multiple daily results."""
    db_path = Path(tempfile.gettempdir()) / "test_multi_results.db"
    db_path.unlink(missing_ok=True)
    
    store = HistoryStore(db_path)
    
    # Record multiple results
    for i, date in enumerate(["2026-10-21", "2026-10-22", "2026-10-23"]):
        store.record_daily_result(
            plan_date=date,
            windows_count=i + 1,
            plan_status="completed",
            grid_import_kwh=5.0 + i,
            cost_with_aid=0.60 + i * 0.05,
            cost_without_aid=0.80 + i * 0.05,
            generation_kwh=10.0 + i,
            consumption_kwh=8.0,
            battery_discharge_kwh=1.0 + i,
        )
    
    # Retrieve and verify order (should be DESC by date)
    results = store.get_daily_results(limit=10)
    
    assert len(results) == 3
    assert results[0]["plan_date"] == "2026-10-23"  # Latest first
    assert results[1]["plan_date"] == "2026-10-22"
    assert results[2]["plan_date"] == "2026-10-21"
    print("✓ Multiple daily results stored and sorted correctly")


def test_optimization_plan_forecast_and_actual_are_persisted():
    db_path = Path(tempfile.gettempdir()) / "test_optimization_plan_results.db"
    db_path.unlink(missing_ok=True)
    store = HistoryStore(db_path)
    forecast = {
        "plan_date": "2026-10-23",
        "horizon_start": "2026-10-23T10:00+02:00",
        "horizon_end": "2026-10-24T10:00+02:00",
        "hourly_plan": [],
    }

    store.record_optimization_plan("plan-1", forecast)
    store.update_optimization_plan_result(
        "plan-1",
        "completed",
        {"actual_grid_import_kwh": 1.25},
    )

    plans = store.get_optimization_plans()
    assert len(plans) == 1
    assert plans[0]["plan_id"] == "plan-1"
    assert plans[0]["plan_status"] == "completed"
    assert plans[0]["forecast"] == forecast
    assert plans[0]["actual"]["actual_grid_import_kwh"] == 1.25


def test_telemetry_query_includes_boundary_samples():
    db_path = Path(tempfile.gettempdir()) / "test_plan_telemetry_range.db"
    db_path.unlink(missing_ok=True)
    store = HistoryStore(db_path)
    start = datetime(2026, 10, 24, 10, tzinfo=ZoneInfo("Europe/Bratislava"))
    end = start + timedelta(hours=1)
    for sampled_at in (
        start - timedelta(minutes=10),
        start + timedelta(minutes=5),
        end + timedelta(minutes=10),
    ):
        store.record_telemetry(
            {
                "metrics": {
                    "generation_power": 1,
                    "use_power": 2,
                    "battery_soc": 50,
                },
                "daily_energy": {"grid_import": {"value": 1}},
                "battery_capacity": {"value": 5},
            },
            sampled_at,
            0.1,
            0.2,
        )

    rows = store.telemetry_for_period(start.isoformat(), end.isoformat())
    assert len(rows) == 3


if __name__ == "__main__":
    test_record_and_retrieve_daily_result()
    test_multiple_daily_results()
    test_optimization_plan_forecast_and_actual_are_persisted()
    test_telemetry_query_includes_boundary_samples()
    print("\n✓ All daily results tests passed!")
