"""Test live metrics calculation."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from live_metrics import calculate_live_metrics


def test_no_active_plan():
    """Test when no plan is active."""
    history_mock = MagicMock()
    scheduler_mock = MagicMock()
    scheduler_mock.get_state.return_value = {
        "status": "idle",
        "windows_queue": [],
        "current_window_index": 0,
    }
    
    result = calculate_live_metrics(history_mock, scheduler_mock, "Europe/Bratislava")
    
    assert result["is_active"] is False
    assert "reason" in result
    print("✓ Detects inactive plan")


def test_plan_not_started():
    """Test when plan is scheduled but not started yet."""
    history_mock = MagicMock()
    scheduler_mock = MagicMock()
    
    now = datetime.now(ZoneInfo("Europe/Bratislava"))
    future_start = (now + timedelta(hours=1)).isoformat()
    future_end = (now + timedelta(hours=2)).isoformat()
    
    scheduler_mock.get_state.return_value = {
        "status": "pending",
        "windows_queue": [{
            "charge_start": future_start,
            "charge_end": future_end,
            "recommended_target_soc": 80,
        }],
        "current_window_index": 0,
        "plan_date": now.strftime("%Y-%m-%d"),
    }
    
    result = calculate_live_metrics(history_mock, scheduler_mock, "Europe/Bratislava", now)
    
    assert result["is_active"] is False
    assert "not started" in result["reason"]
    print("✓ Detects plan not started yet")


def test_active_plan_with_metrics():
    """Test metrics calculation during active plan."""
    history_mock = MagicMock()
    
    # Mock the telemetry query
    def mock_connect():
        conn_mock = MagicMock()
        cursor_mock = MagicMock()
        cursor_mock.fetchone.return_value = (
            5.5,   # grid_import_kwh
            0.68,  # cost_with_aid
            0.85,  # cost_without_aid
        )
        conn_mock.execute.return_value = cursor_mock
        conn_mock.__enter__ = lambda: conn_mock
        conn_mock.__exit__ = lambda *args: None
        return conn_mock
    
    history_mock._connect = mock_connect
    
    scheduler_mock = MagicMock()
    
    now = datetime.now(ZoneInfo("Europe/Bratislava"))
    plan_start = (now - timedelta(minutes=30)).isoformat()
    window_start = plan_start
    window_end = (now + timedelta(minutes=30)).isoformat()
    
    scheduler_mock.get_state.return_value = {
        "status": "charging",
        "windows_queue": [{
            "charge_start": window_start,
            "charge_end": window_end,
            "recommended_target_soc": 80,
        }],
        "current_window_index": 0,
        "plan_date": now.strftime("%Y-%m-%d"),
    }
    
    result = calculate_live_metrics(history_mock, scheduler_mock, "Europe/Bratislava", now)
    
    assert result["is_active"] is True
    assert result["grid_import_kwh"] == 5.5
    assert result["plan_cost_eur"] == 0.68
    assert result["self_use_cost_eur"] == 0.85
    assert abs(result["savings_eur"] - 0.17) < 0.001  # Allow floating point error
    assert result["time_in_window_percent"] == 50.0  # 30 min into 60 min window
    print("✓ Calculates live metrics correctly")


if __name__ == "__main__":
    test_no_active_plan()
    test_plan_not_started()
    test_active_plan_with_metrics()
    print("\n✓ All live metrics tests passed!")
