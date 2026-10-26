import logging
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


class ContinuousOptimizationController:
    def __init__(
        self,
        history_store,
        scheduler,
        optimize,
        disable_plan,
        timezone_str,
        logger=None,
    ):
        self.history_store = history_store
        self.scheduler = scheduler
        self.optimize = optimize
        self.disable_plan = disable_plan
        self.timezone = ZoneInfo(timezone_str)
        self.logger = logger or logging.getLogger(__name__)
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread = None

    @staticmethod
    def _timestamp(value):
        return value.isoformat(timespec="seconds") if value else None

    def get_state(self):
        state = self.history_store.get_continuous_optimization_state()
        scheduler_state = self.scheduler.get_state()
        if not state["enabled"]:
            status = "disabled"
        elif scheduler_state.get("status") in {"charging", "transitioning"}:
            status = scheduler_state["status"]
        elif state.get("last_error"):
            status = "error"
        else:
            status = "enabled"
        return {**state, "status": status}

    def set_enabled(self, enabled, settings=None):
        if not isinstance(enabled, bool):
            raise ValueError("Continuous optimization enabled flag must be true or false.")
        with self._lock:
            state = self.history_store.get_continuous_optimization_state()
            was_enabled = state["enabled"]
            if enabled:
                if not isinstance(settings, dict):
                    raise ValueError("Optimization settings are required when enabling automation.")
                state["settings"] = {
                    **settings,
                    "planning_horizon_hours": 24,
                }
                state["enabled"] = True
                state["last_error"] = None
                state["next_run_at"] = self._timestamp(datetime.now(self.timezone))
            else:
                state["enabled"] = False
                state["next_run_at"] = None
                state["last_error"] = None
            self.history_store.save_continuous_optimization_state(state)
            self._wake_event.set()
            if was_enabled and not enabled:
                self.disable_plan()
        return self.get_state()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="sofar-continuous-optimization",
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        self._wake_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _next_hourly_run(self, now):
        utc_now = now.astimezone(timezone.utc)
        next_run = utc_now.replace(minute=55, second=0, microsecond=0)
        if next_run <= utc_now:
            next_run += timedelta(hours=1)
        return next_run.astimezone(self.timezone)

    def _run(self):
        while not self._stop_event.is_set():
            state = self.history_store.get_continuous_optimization_state()
            if not state["enabled"]:
                self._wake_event.wait(60)
                self._wake_event.clear()
                continue

            now = datetime.now(self.timezone)
            scheduled_at = datetime.fromisoformat(state["next_run_at"]) if state.get("next_run_at") else now
            wait_seconds = (scheduled_at - now).total_seconds()
            if wait_seconds > 0:
                self._wake_event.wait(min(wait_seconds, 60))
                self._wake_event.clear()
                continue

            try:
                self._run_cycle(state)
            except Exception as error:
                self.logger.exception("Continuous optimization worker failed: %s", error)

    def _run_cycle(self, state):
        now = datetime.now(self.timezone)
        next_run = self._next_hourly_run(now)
        last_error = state.get("last_error")
        battery_model = None
        attempted_replan = False
        try:
            scheduler_state = self.scheduler.get_state()
            if scheduler_state.get("status") in {"charging", "transitioning"}:
                self.logger.info(
                    "Continuous optimization replan deferred while a charging window is active."
                )
            else:
                attempted_replan = True
                last_error = None
                plan = self.optimize(state["settings"])
                if not isinstance(plan, dict):
                    raise ValueError("Optimization did not return a plan.")
                plan["continuous_optimization"] = True
                battery_model = plan.get("battery_model")
                with self._lock:
                    if not self.history_store.get_continuous_optimization_state()["enabled"]:
                        return
                    self.scheduler.schedule_windows(
                        plan.get("windows"),
                        plan.get("plan_date"),
                        plan,
                    )
                self.logger.info(
                    "Continuous optimization scheduled %s charging windows.",
                    len(plan.get("windows", [])),
                )
        except Exception as error:
            last_error = str(error)
            self.logger.exception("Continuous optimization cycle failed: %s", error)
        self.history_store.update_continuous_optimization_runtime(
            last_run_at=self._timestamp(now)
            if attempted_replan
            else state.get("last_run_at"),
            next_run_at=self._timestamp(next_run),
            last_error=last_error,
            battery_model=battery_model,
        )
