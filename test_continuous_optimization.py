import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from continuous_optimizer import ContinuousOptimizationController
from history import HistoryStore
from multi_window_scheduler import MultiWindowScheduler
from run import calculate_continuous_results, finalize_continuous_optimization_plan


def test_continuous_state_persists_and_records_enabled_sessions():
    with tempfile.TemporaryDirectory() as directory:
        store = HistoryStore(Path(directory) / "history.sqlite3")
        started_at = datetime(2026, 10, 26, 10, tzinfo=timezone.utc)
        state = store.get_continuous_optimization_state()
        state.update(
            enabled=True,
            settings={"location": "Jacovce", "energy_aid": True},
            next_run_at=started_at.isoformat(),
        )
        store.save_continuous_optimization_state(state, changed_at=started_at)

        sessions = store.get_continuous_optimization_sessions(
            datetime(2026, 10, 26, 9, tzinfo=timezone.utc).isoformat(),
            datetime(2026, 10, 26, 11, tzinfo=timezone.utc).isoformat(),
        )
        assert len(sessions) == 1
        assert sessions[0]["settings"]["location"] == "Jacovce"

        state = store.get_continuous_optimization_state()
        state["enabled"] = False
        store.save_continuous_optimization_state(
            state,
            changed_at=datetime(2026, 10, 26, 10, 30, tzinfo=timezone.utc),
        )
        sessions = store.get_continuous_optimization_sessions(
            datetime(2026, 10, 26, 9, tzinfo=timezone.utc).isoformat(),
            datetime(2026, 10, 26, 11, tzinfo=timezone.utc).isoformat(),
        )
        assert sessions[0]["ended_at"] == "2026-10-26T10:30:00+00:00"


def test_continuous_controller_schedules_rolling_plan_and_records_last_run():
    with tempfile.TemporaryDirectory() as directory, patch(
        "continuous_optimizer.datetime"
    ) as mocked_datetime:
        now = datetime(2026, 10, 26, 10, 55, tzinfo=timezone.utc)
        mocked_datetime.now.return_value = now
        mocked_datetime.fromisoformat.side_effect = datetime.fromisoformat
        store = HistoryStore(Path(directory) / "history.sqlite3")
        settings = {
            "location": "Jacovce",
            "energy_aid": True,
            "planning_horizon_hours": 12,
        }
        plan = {
            "plan_date": "2026-10-26",
            "horizon_start": "2026-10-26T11:00:00+01:00",
            "horizon_end": "2026-10-27T11:00:00+01:00",
            "windows": [],
            "battery_model": {"capacity_kwh": 10},
        }
        scheduler = MagicMock()
        scheduler.get_state.return_value = {"status": "idle"}
        optimize = MagicMock(return_value=plan.copy())
        controller = ContinuousOptimizationController(
            store,
            scheduler,
            optimize,
            MagicMock(),
            "Europe/Bratislava",
            MagicMock(),
        )
        controller.set_enabled(True, settings)
        state = store.get_continuous_optimization_state()

        controller._run_cycle(state)

        assert optimize.call_args.args[0]["planning_horizon_hours"] == 24
        scheduled_plan = scheduler.schedule_windows.call_args.args[2]
        assert scheduled_plan["continuous_optimization"] is True
        assert scheduled_plan["windows"] == []
        updated_state = store.get_continuous_optimization_state()
        assert updated_state["last_error"] is None
        assert updated_state["battery_model"] == {"capacity_kwh": 10}


def test_continuous_replans_update_one_history_record_and_finalize_session():
    with tempfile.TemporaryDirectory() as directory, patch(
        "multi_window_scheduler.LOGGER"
    ):
        store = HistoryStore(Path(directory) / "history.sqlite3")
        started_at = datetime(2026, 10, 26, 10, tzinfo=timezone.utc)
        ended_at = started_at + timedelta(minutes=30)
        state = store.get_continuous_optimization_state()
        state.update(enabled=True, settings={"energy_aid": True})
        store.save_continuous_optimization_state(state, changed_at=started_at)

        battery_model = {
            "capacity_kwh": 10,
            "min_energy_kwh": 3,
            "max_energy_kwh": 9,
            "charge_efficiency": 0.9,
            "discharge_efficiency": 0.9,
        }
        first_forecast = {
            "plan_date": "2026-10-26",
            "horizon_start": started_at.isoformat(),
            "horizon_end": (started_at + timedelta(hours=24)).isoformat(),
            "generated_at": started_at.isoformat(),
            "windows": [],
            "battery_model": battery_model,
            "continuous_optimization": True,
        }
        scheduler = MultiWindowScheduler(
            MagicMock(),
            Path(directory) / "scheduler.json",
            MagicMock(),
            lambda: False,
            "Europe/Bratislava",
            MagicMock(),
        )
        scheduler.history_store = store
        first_state = scheduler.schedule_windows(
            [],
            first_forecast["plan_date"],
            first_forecast,
        )

        latest_forecast = {
            **first_forecast,
            "horizon_start": (started_at + timedelta(minutes=15)).isoformat(),
            "horizon_end": (started_at + timedelta(hours=24, minutes=15)).isoformat(),
            "generated_at": (started_at + timedelta(minutes=15)).isoformat(),
        }
        latest_state = scheduler.schedule_windows(
            [],
            latest_forecast["plan_date"],
            latest_forecast,
        )
        assert latest_state["plan_id"] == first_state["plan_id"]

        for sample_index in range(7):
            sampled_at = started_at + timedelta(minutes=5 * sample_index)
            store.record_telemetry(
                {
                    "metrics": {
                        "generation_power": 1,
                        "use_power": 2,
                        "battery_soc": 30,
                    },
                    "daily_energy": {
                        "grid_import": {"value": sample_index * 0.05}
                    },
                    "battery_capacity": {"value": 10},
                },
                sampled_at,
                0.1,
                0.12,
            )

        state = store.get_continuous_optimization_state()
        state["enabled"] = False
        store.save_continuous_optimization_state(state, changed_at=ended_at)
        session = store.get_latest_continuous_optimization_session()
        actual = finalize_continuous_optimization_plan(
            store,
            latest_state["plan_id"],
            session["id"],
            battery_model,
        )

        plans = store.get_optimization_plans()
        assert len(plans) == 1
        assert plans[0]["plan_status"] == "completed"
        assert plans[0]["forecast"]["generated_at"] == latest_forecast["generated_at"]
        assert plans[0]["actual"]["continuous_session_start"] == started_at.isoformat(
            timespec="minutes"
        )
        assert plans[0]["actual"]["continuous_session_end"] == ended_at.isoformat(
            timespec="minutes"
        )
        assert actual["telemetry_coverage_percent"] == 100
        assert actual["actual_cost_on_simulation_coverage_eur"] is not None

        scheduler.cancel(record_result=False)
        next_start = ended_at + timedelta(minutes=30)
        state = store.get_continuous_optimization_state()
        state["enabled"] = True
        store.save_continuous_optimization_state(state, changed_at=next_start)
        next_forecast = {
            **first_forecast,
            "horizon_start": next_start.isoformat(),
            "horizon_end": (next_start + timedelta(hours=24)).isoformat(),
            "generated_at": next_start.isoformat(),
        }
        next_state = scheduler.schedule_windows(
            [],
            next_forecast["plan_date"],
            next_forecast,
        )
        assert next_state["plan_id"] != first_state["plan_id"]
        assert len(store.get_optimization_plans()) == 2


def test_continuous_replans_update_one_history_record_and_finalize_session():
    with tempfile.TemporaryDirectory() as directory, patch(
        "multi_window_scheduler.LOGGER"
    ):
        store = HistoryStore(Path(directory) / "history.sqlite3")
        started_at = datetime(2026, 10, 26, 10, tzinfo=timezone.utc)
        ended_at = started_at + timedelta(minutes=30)
        state = store.get_continuous_optimization_state()
        state.update(enabled=True, settings={"energy_aid": True})
        store.save_continuous_optimization_state(state, changed_at=started_at)

        battery_model = {
            "capacity_kwh": 10,
            "min_energy_kwh": 3,
            "max_energy_kwh": 9,
            "charge_efficiency": 0.9,
            "discharge_efficiency": 0.9,
        }
        first_forecast = {
            "plan_date": "2026-10-26",
            "horizon_start": started_at.isoformat(),
            "horizon_end": (started_at + timedelta(hours=24)).isoformat(),
            "generated_at": started_at.isoformat(),
            "windows": [],
            "battery_model": battery_model,
            "continuous_optimization": True,
        }
        scheduler = MultiWindowScheduler(
            MagicMock(),
            Path(directory) / "scheduler.json",
            MagicMock(),
            lambda: False,
            "Europe/Bratislava",
            MagicMock(),
        )
        scheduler.history_store = store
        first_state = scheduler.schedule_windows(
            [],
            first_forecast["plan_date"],
            first_forecast,
        )

        latest_forecast = {
            **first_forecast,
            "horizon_start": (started_at + timedelta(minutes=15)).isoformat(),
            "horizon_end": (started_at + timedelta(hours=24, minutes=15)).isoformat(),
            "generated_at": (started_at + timedelta(minutes=15)).isoformat(),
        }
        latest_state = scheduler.schedule_windows(
            [],
            latest_forecast["plan_date"],
            latest_forecast,
        )
        assert latest_state["plan_id"] == first_state["plan_id"]

        for sample_index in range(7):
            sampled_at = started_at + timedelta(minutes=5 * sample_index)
            store.record_telemetry(
                {
                    "metrics": {
                        "generation_power": 1,
                        "use_power": 2,
                        "battery_soc": 30,
                    },
                    "daily_energy": {
                        "grid_import": {"value": sample_index * 0.05}
                    },
                    "battery_capacity": {"value": 10},
                },
                sampled_at,
                0.1,
                0.12,
            )

        state = store.get_continuous_optimization_state()
        state["enabled"] = False
        store.save_continuous_optimization_state(state, changed_at=ended_at)
        session = store.get_latest_continuous_optimization_session()
        actual = finalize_continuous_optimization_plan(
            store,
            latest_state["plan_id"],
            session["id"],
            battery_model,
        )

        plans = store.get_optimization_plans()
        assert len(plans) == 1
        assert plans[0]["plan_status"] == "completed"
        assert plans[0]["forecast"]["generated_at"] == latest_forecast["generated_at"]
        assert plans[0]["actual"]["continuous_session_start"] == started_at.isoformat(
            timespec="minutes"
        )
        assert plans[0]["actual"]["continuous_session_end"] == ended_at.isoformat(
            timespec="minutes"
        )
        assert actual["telemetry_coverage_percent"] == 100
        assert actual["actual_cost_on_simulation_coverage_eur"] is not None


def test_continuous_results_sum_only_enabled_session_estimates():
    now = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
    history_store = MagicMock()
    history_store.get_continuous_optimization_state.return_value = {
        "battery_model": {
            "capacity_kwh": 10,
            "min_energy_kwh": 3,
            "max_energy_kwh": 9.5,
            "charge_efficiency": 0.92,
            "discharge_efficiency": 0.95,
        }
    }
    history_store.get_continuous_optimization_sessions.return_value = [
        {
            "started_at": "2026-10-27T10:00:00+00:00",
            "ended_at": None,
            "settings": {"energy_aid": True},
        }
    ]
    history_store.telemetry_for_period.return_value = [{"sampled_at": "telemetry"}]
    actual = {
        "estimated_savings_vs_self_use_eur": 0.5,
        "actual_cost_on_simulation_coverage_eur": 2.0,
        "self_use_counterfactual_cost_eur": 2.8,
        "self_use_cost_on_simulation_coverage_eur": 2.5,
        "telemetry_coverage_percent": 80,
        "hourly": [
            {
                "time": "2026-10-27T10:00:00+00:00",
                "coverage_percent": 80,
                "simulation_cost_eur": 1.0,
                "self_use_cost_eur": 1.2,
                "estimated_savings_eur": 0.2,
            },
            {
                "time": "2026-10-27T11:00:00+00:00",
                "coverage_percent": 80,
                "simulation_cost_eur": 1.0,
                "self_use_cost_eur": 1.3,
                "estimated_savings_eur": 0.3,
            },
        ],
    }
    with patch("run.compare_plan_with_telemetry", return_value=actual) as compare:
        results = calculate_continuous_results(history_store, 7, now=now)

    assert results["savings_eur"] == 0.5
    assert results["actual_cost_eur"] == 2.0
    assert results["self_use_cost_eur"] == 2.5
    assert results["coverage_percent"] == 80
    assert results["active_hours"] == 2
    assert results["daily"][-1]["savings_eur"] == 0.5
    assert results["daily"][-1]["cumulative_savings_eur"] == 0.5
    assert results["daily"][-1]["actual_cost_eur"] == 2
    compare.assert_called_once()
    forecast = compare.call_args.args[0]
    assert len(forecast["hourly_plan"]) == 2


def test_continuous_plan_without_charge_windows_keeps_evaluation_horizon():
    with tempfile.TemporaryDirectory() as directory:
        now = datetime(2026, 10, 26, 10, tzinfo=timezone.utc)
        state_path = Path(directory) / "scheduler.json"
        scheduler = MultiWindowScheduler(
            MagicMock(),
            state_path,
            MagicMock(),
            lambda: False,
            "UTC",
            MagicMock(),
        )
        plan = {
            "plan_date": "2026-10-26",
            "horizon_start": now.isoformat(),
            "horizon_end": "2026-10-27T10:00:00+00:00",
            "windows": [],
            "continuous_optimization": True,
        }
        state = scheduler.schedule_windows([], plan["plan_date"], plan)
        scheduler.run_due(now)

        assert state["continuous_optimization"] is True
        assert scheduler.get_state()["status"] == "evaluating"


def test_continuous_replan_waits_until_current_charge_finishes():
    with tempfile.TemporaryDirectory() as directory, patch(
        "continuous_optimizer.datetime"
    ) as mocked_datetime:
        now = datetime(2026, 10, 26, 10, 55, tzinfo=timezone.utc)
        mocked_datetime.now.return_value = now
        store = HistoryStore(Path(directory) / "history.sqlite3")
        last_run = "2026-10-26T09:55:00+00:00"
        state = store.get_continuous_optimization_state()
        state.update(
            enabled=True,
            settings={"location": "Jacovce"},
            last_run_at=last_run,
        )
        store.save_continuous_optimization_state(state, changed_at=now)
        scheduler = MagicMock()
        scheduler.get_state.return_value = {"status": "charging"}
        optimize = MagicMock()
        controller = ContinuousOptimizationController(
            store,
            scheduler,
            optimize,
            MagicMock(),
            "UTC",
            MagicMock(),
        )

        controller._run_cycle(state)

        optimize.assert_not_called()
        assert store.get_continuous_optimization_state()["last_run_at"] == last_run


if __name__ == "__main__":
    test_continuous_state_persists_and_records_enabled_sessions()
    test_continuous_controller_schedules_rolling_plan_and_records_last_run()
    test_continuous_replans_update_one_history_record_and_finalize_session()
    test_continuous_replans_update_one_history_record_and_finalize_session()
    test_continuous_results_sum_only_enabled_session_estimates()
    test_continuous_plan_without_charge_windows_keeps_evaluation_horizon()
    test_continuous_replan_waits_until_current_charge_finishes()
