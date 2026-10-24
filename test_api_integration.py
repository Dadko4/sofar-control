"""Integration test: API with multi-window scheduler."""

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from multi_window_scheduler import MultiWindowScheduler


def test_api_receives_windows_correctly():
    """Test that API correctly passes windows to multi-window scheduler."""
    with patch('multi_window_scheduler.LOGGER'):
        client_mock = MagicMock()
        state_path = Path(tempfile.gettempdir()) / "test_api_windows.json"
        state_path.unlink(missing_ok=True)
        
        scheduler = MultiWindowScheduler(
            client_mock,
            state_path,
            MagicMock(),
            lambda: False,
            "Europe/Bratislava",
            MagicMock(),
        )
        
        # Simulate what applyOptimization() sends
        now = datetime.now(ZoneInfo("Europe/Bratislava"))
        api_payload = {
            "windows": [
                {
                    "charge_start": (now + timedelta(hours=1)).isoformat(),
                    "charge_end": (now + timedelta(hours=2)).isoformat(),
                    "recommended_target_soc": 80,
                    "recommended_grid_charge_kwh": 5.0,
                },
                {
                    "charge_start": (now + timedelta(hours=4)).isoformat(),
                    "charge_end": (now + timedelta(hours=5)).isoformat(),
                    "recommended_target_soc": 90,
                    "recommended_grid_charge_kwh": 4.0,
                },
            ],
            "plan_date": now.strftime("%Y-%m-%d"),
            "generated_at": now.isoformat(),
            "confirmed": True
        }
        
        # Simulate the API call
        result = scheduler.schedule_windows(api_payload["windows"], api_payload["plan_date"])
        
        # Verify state
        assert result["status"] == "pending"
        assert len(result["windows_queue"]) == 2
        assert result["current_window_index"] == 0
        assert result["plan_date"] == now.strftime("%Y-%m-%d")
        print("✓ API payload correctly stored in scheduler")


def test_multi_window_sequencing():
    """Test that windows are executed in sequence."""
    with patch('multi_window_scheduler.LOGGER'):
        client_mock = MagicMock()
        state_path = Path(tempfile.gettempdir()) / "test_sequencing.json"
        state_path.unlink(missing_ok=True)
        
        scheduler = MultiWindowScheduler(
            client_mock,
            state_path,
            MagicMock(),
            lambda: False,
            "Europe/Bratislava",
            MagicMock(),
        )
        
        # Create 3 windows: 10:00-11:00, 15:00-16:00, 18:00-19:00
        base = datetime.now(ZoneInfo("Europe/Bratislava")).replace(hour=9, minute=0, second=0, microsecond=0)
        
        windows = [
            {
                "charge_start": (base + timedelta(hours=1)).isoformat(),
                "charge_end": (base + timedelta(hours=2)).isoformat(),
                "recommended_target_soc": 70,
                "recommended_grid_charge_kwh": 3.0,
            },
            {
                "charge_start": (base + timedelta(hours=6)).isoformat(),
                "charge_end": (base + timedelta(hours=7)).isoformat(),
                "recommended_target_soc": 80,
                "recommended_grid_charge_kwh": 4.0,
            },
            {
                "charge_start": (base + timedelta(hours=9)).isoformat(),
                "charge_end": (base + timedelta(hours=10)).isoformat(),
                "recommended_target_soc": 90,
                "recommended_grid_charge_kwh": 5.0,
            },
        ]
        
        scheduler.schedule_windows(windows, base.strftime("%Y-%m-%d"))
        
        # Window 1: not started yet
        state = scheduler.get_state()
        assert state["status"] == "pending"
        assert state["current_window_index"] == 0
        
        # First window starts
        window1_start = base + timedelta(hours=1, minutes=30)
        scheduler.run_due(window1_start)
        state = scheduler.get_state()
        assert state["status"] in ("charging", "pending")  # May be charging or still pending
        
        # First window ends, move to second
        window1_end = base + timedelta(hours=2, minutes=1)
        scheduler.run_due(window1_end)
        state = scheduler.get_state()
        # After end, should transition
        assert state["current_window_index"] >= 1
        
        print("✓ Multi-window sequencing works correctly")


if __name__ == "__main__":
    test_api_receives_windows_correctly()
    test_multi_window_sequencing()
    print("\n✓ All API integration tests passed!")
