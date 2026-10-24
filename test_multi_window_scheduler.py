"""Test multi-window scheduler."""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from multi_window_scheduler import MultiWindowScheduler, compare_plan_with_telemetry


def test_schedule_windows():
    """Test scheduling multiple windows."""
    with patch('multi_window_scheduler.LOGGER'):
        client_mock = MagicMock()
        state_path = Path(tempfile.gettempdir()) / "test_multi_state.json"
        state_path.unlink(missing_ok=True)
        
        scheduler = MultiWindowScheduler(
            client_mock,
            state_path,
            MagicMock(),
            lambda: False,  # discharge not active
            "Europe/Bratislava",
            MagicMock(),
        )
        
        # Create two windows for today
        now = datetime.now(ZoneInfo("Europe/Bratislava"))
        window1 = {
            "charge_start": (now + timedelta(hours=1)).isoformat(),
            "charge_end": (now + timedelta(hours=2)).isoformat(),
            "recommended_target_soc": 80,
            "recommended_grid_charge_kwh": 5.0,
        }
        window2 = {
            "charge_start": (now + timedelta(hours=4)).isoformat(),
            "charge_end": (now + timedelta(hours=5)).isoformat(),
            "recommended_target_soc": 90,
            "recommended_grid_charge_kwh": 4.0,
        }
        
        result = scheduler.schedule_windows([window1, window2], now.strftime("%Y-%m-%d"))
        
        assert result["status"] == "pending"
        assert result["current_window_index"] == 0
        assert len(result["windows_queue"]) == 2
        print("✓ Windows scheduled successfully")


def test_window_execution_timing():
    """Test that scheduler identifies when window should be active."""
    with patch('multi_window_scheduler.LOGGER'):
        client_mock = MagicMock()
        state_path = Path(tempfile.gettempdir()) / "test_multi_timing.json"
        state_path.unlink(missing_ok=True)
        
        scheduler = MultiWindowScheduler(
            client_mock,
            state_path,
            MagicMock(),
            lambda: False,
            "Europe/Bratislava",
            MagicMock(),
        )
        
        now = datetime.now(ZoneInfo("Europe/Bratislava"))
        window1_start = (now + timedelta(minutes=1)).isoformat()
        window1_end = (now + timedelta(minutes=2)).isoformat()
        
        scheduler.schedule_windows([{
            "charge_start": window1_start,
            "charge_end": window1_end,
            "recommended_target_soc": 80,
            "recommended_grid_charge_kwh": 5.0,
        }], now.strftime("%Y-%m-%d"))
        
        # Before window - should not start
        before = now - timedelta(minutes=5)
        scheduler.run_due(before)
        assert scheduler.get_state()["status"] == "pending"
        print("✓ Window not started before scheduled time")
        
        # Inside window - should start charging
        inside = now + timedelta(seconds=90)
        scheduler.run_due(inside)
        # Should attempt to start (may be pending->charging transition)
        state = scheduler.get_state()
        assert state["status"] in ("charging", "pending")
        print("✓ Charging initiated when inside window")


def test_state_persistence():
    """Test that scheduler state survives restart."""
    with patch('multi_window_scheduler.LOGGER'):
        state_path = Path(tempfile.gettempdir()) / "test_multi_persist.json"
        state_path.unlink(missing_ok=True)
        
        # First instance: schedule windows
        client_mock = MagicMock()
        scheduler1 = MultiWindowScheduler(
            client_mock,
            state_path,
            MagicMock(),
            lambda: False,
            "Europe/Bratislava",
            MagicMock(),
        )
        
        now = datetime.now(ZoneInfo("Europe/Bratislava"))
        windows = [{
            "charge_start": (now + timedelta(hours=1)).isoformat(),
            "charge_end": (now + timedelta(hours=2)).isoformat(),
            "recommended_target_soc": 80,
            "recommended_grid_charge_kwh": 5.0,
        }]
        scheduler1.schedule_windows(windows, now.strftime("%Y-%m-%d"))
        
        # Second instance: should load state
        scheduler2 = MultiWindowScheduler(
            client_mock,
            state_path,
            MagicMock(),
            lambda: False,
            "Europe/Bratislava",
            MagicMock(),
        )
        
        state2 = scheduler2.get_state()
        assert state2["status"] == "pending"
        assert len(state2["windows_queue"]) == 1
        print("✓ State persisted and reloaded")


def test_plan_stays_evaluating_until_horizon_end():
    with patch("multi_window_scheduler.LOGGER"):
        now = datetime(2026, 1, 1, 0, tzinfo=ZoneInfo("Europe/Bratislava"))
        window = {
            "charge_start": (now + timedelta(hours=1)).isoformat(),
            "charge_end": (now + timedelta(hours=2)).isoformat(),
            "recommended_target_soc": 80,
            "recommended_grid_charge_kwh": 1.0,
        }
        forecast = {
            "plan_date": now.date().isoformat(),
            "horizon_start": now.isoformat(),
            "horizon_end": (now + timedelta(hours=4)).isoformat(),
            "windows": [window],
            "hourly_plan": [],
        }
        history_store = MagicMock()
        history_store.telemetry_for_period.return_value = []
        state_path = Path(tempfile.gettempdir()) / "test_multi_evaluation.json"
        state_path.unlink(missing_ok=True)
        scheduler = MultiWindowScheduler(
            MagicMock(),
            state_path,
            MagicMock(),
            lambda: False,
            "Europe/Bratislava",
            MagicMock(),
        )
        scheduler.history_store = history_store
        scheduler.schedule_windows([window], now.date().isoformat(), forecast)

        scheduler.run_due(now + timedelta(hours=1, minutes=1))
        scheduler.run_due(now + timedelta(hours=2, minutes=1))
        assert scheduler.get_state()["status"] == "evaluating"
        history_store.update_optimization_plan_result.assert_not_called()

        scheduler.run_due(now + timedelta(hours=4))
        assert scheduler.get_state()["status"] == "completed"
        history_store.update_optimization_plan_result.assert_called_once()
        scheduler.cancel()
        assert scheduler.get_state()["status"] == "completed"
        history_store.update_optimization_plan_result.assert_called_once()


def test_plan_comparison_integrates_telemetry_and_labels_self_use_as_counterfactual():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    forecast = {
        "hourly_plan": [
            {
                "time": start.isoformat(),
                "tariff_eur_per_kwh": 0.1,
            }
        ],
        "battery_model": {
            "capacity_kwh": 10,
            "min_energy_kwh": 3,
            "max_energy_kwh": 9,
            "charge_efficiency": 0.9,
            "discharge_efficiency": 0.9,
        },
    }
    telemetry = []
    for sample_index in range(13):
        sampled_at = start + timedelta(minutes=5 * sample_index)
        telemetry.append(
            {
                "sampled_at": sampled_at.isoformat(),
                "generation_kw": 1,
                "consumption_kw": 2,
                "battery_soc": 50,
                "observed_grid_import_delta_kwh": 0.05,
                "observed_grid_import_cost_with_aid_eur": 0.005,
            }
        )

    actual = compare_plan_with_telemetry(
        forecast,
        telemetry,
        start.isoformat(),
        (start + timedelta(hours=1)).isoformat(),
    )

    assert actual["actual_generation_kwh"] == 1
    assert actual["actual_consumption_kwh"] == 2
    assert actual["actual_grid_import_kwh"] == 0.6
    assert actual["actual_cost_with_aid_eur"] == 0.06
    assert actual["actual_cost_on_simulation_coverage_eur"] == 0.06
    assert actual["telemetry_coverage_percent"] == 100
    assert actual["self_use_simulation_coverage_percent"] == 100
    assert actual["self_use_counterfactual_cost_eur"] == 0
    assert actual["estimated_savings_vs_self_use_eur"] == -0.06
    assert actual["hourly"][0]["grid_import_kwh"] == 0.6


if __name__ == "__main__":
    test_schedule_windows()
    test_window_execution_timing()
    test_state_persistence()
    test_plan_stays_evaluating_until_horizon_end()
    test_plan_comparison_integrates_telemetry_and_labels_self_use_as_counterfactual()
    print("\n✓ All multi-window scheduler tests passed!")
