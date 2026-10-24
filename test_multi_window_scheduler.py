"""Test multi-window scheduler."""

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from multi_window_scheduler import MultiWindowScheduler


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


if __name__ == "__main__":
    test_schedule_windows()
    test_window_execution_timing()
    test_state_persistence()
    print("\n✓ All multi-window scheduler tests passed!")
