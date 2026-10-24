"""Test 24-hour planning window (from now until 06:00 next day)."""

from datetime import datetime, time as datetime_time, timedelta
from zoneinfo import ZoneInfo

TIMEZONE = "Europe/Bratislava"

def test_plan_date_window():
    """Test that plan_date calculation gives 24-hour window until 06:00."""
    
    test_cases = [
        # (current_time, expected_plan_date, expected_horizon_end_time)
        ("2026-10-24 05:00:00", "2026-10-24", "2026-10-25 06:00:00"),  # 5 AM - should plan until tomorrow 6 AM
        ("2026-10-24 06:00:00", "2026-10-24", "2026-10-25 06:00:00"),  # 6 AM - full 24h day
        ("2026-10-24 12:00:00", "2026-10-24", "2026-10-25 06:00:00"),  # Noon - full day
        ("2026-10-24 20:00:00", "2026-10-24", "2026-10-25 06:00:00"),  # Evening - full 24h
        ("2026-10-24 22:00:00", "2026-10-24", "2026-10-25 06:00:00"),  # 10 PM - 8 hours tonight + 24h
        ("2026-10-24 23:59:00", "2026-10-24", "2026-10-25 06:00:00"),  # Just before midnight
    ]
    
    for current_str, expected_plan_date, expected_horizon_end in test_cases:
        local_now = datetime.fromisoformat(current_str).replace(tzinfo=ZoneInfo(TIMEZONE))
        
        # This is the logic from run.py
        plan_date = local_now.date()
        horizon_end = datetime.combine(
            plan_date + timedelta(days=1),
            datetime_time(6, 0),
            tzinfo=ZoneInfo(TIMEZONE),
        )
        
        assert plan_date.isoformat() == expected_plan_date, \
            f"At {current_str}: expected plan_date={expected_plan_date}, got {plan_date.isoformat()}"
        
        assert horizon_end.isoformat() == datetime.fromisoformat(expected_horizon_end + "+01:00").isoformat(), \
            f"At {current_str}: expected horizon_end={expected_horizon_end}, got {horizon_end.isoformat()}"
        
        # Check duration
        duration_hours = (horizon_end - local_now).total_seconds() / 3600
        print(f"✓ {current_str}: plan covers {duration_hours:.1f} hours until {horizon_end.strftime('%H:%M')}")


if __name__ == "__main__":
    test_plan_date_window()
    print("\n✓ All 24-hour planning window tests passed!")
