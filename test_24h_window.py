"""Check the default rolling 24-hour optimization horizon."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from run import TIMEZONE, optimize_day


def test_rolling_24_hour_horizon():
    local_timezone = ZoneInfo(TIMEZONE)
    forecast_start = datetime(2026, 10, 24, tzinfo=timezone.utc)
    forecast = {
        "location": "Jacovce",
        "hourly": [
            {
                "time": (forecast_start + timedelta(hours=hour)).isoformat(),
                "shortwave_radiation": 0,
            }
            for hour in range(72)
        ],
    }
    status = {
        "metrics": {"battery_soc": 50},
        "battery_capacity": {"value": 5},
    }
    test_times = (
        "2026-10-24 05:00:00",
        "2026-10-24 06:00:00",
        "2026-10-24 12:00:00",
        "2026-10-24 20:00:00",
        "2026-10-24 22:00:00",
        "2026-10-24 23:59:00",
    )

    for current_time in test_times:
        now = datetime.fromisoformat(current_time).replace(tzinfo=local_timezone)
        plan = optimize_day(
            status,
            forecast,
            7,
            6,
            25,
            True,
            now=now,
        )
        horizon_start = datetime.fromisoformat(plan["horizon_start"])
        horizon_end = datetime.fromisoformat(plan["horizon_end"])
        expected_start = now.astimezone(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        if now.minute or now.second or now.microsecond:
            expected_start += timedelta(hours=1)

        assert horizon_start == expected_start.astimezone(local_timezone)
        assert horizon_end.astimezone(timezone.utc) - horizon_start.astimezone(
            timezone.utc
        ) == timedelta(hours=24)
        assert plan["plan_date"] == horizon_start.date().isoformat()
        assert len(plan["hourly_plan"]) == 24
        print(f"✓ {current_time}: rolling horizon {horizon_start} – {horizon_end}")


if __name__ == "__main__":
    test_rolling_24_hour_horizon()
    print("\n✓ All rolling 24-hour horizon checks passed!")
