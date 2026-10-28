import json
import math
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path


class HistoryStore:
    def __init__(self, database_path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS telemetry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sampled_at TEXT NOT NULL,
                    data_time TEXT,
                    generation_kw REAL,
                    consumption_kw REAL,
                    battery_soc REAL,
                    battery_charge_kw REAL,
                    battery_discharge_kw REAL,
                    grid_kw REAL,
                    battery_capacity_kwh REAL,
                    daily_generation_kwh REAL,
                    daily_consumption_kwh REAL,
                    daily_grid_import_kwh REAL,
                    daily_grid_export_kwh REAL,
                    daily_battery_charge_kwh REAL,
                    daily_battery_discharge_kwh REAL,
                    observed_grid_import_delta_kwh REAL,
                    observed_grid_import_cost_with_aid_eur REAL,
                    observed_grid_import_cost_without_aid_eur REAL
                );
                CREATE TABLE IF NOT EXISTS historical_consumption (
                    sampled_at TEXT PRIMARY KEY,
                    consumption_kw REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS history_sync (
                    source TEXT PRIMARY KEY,
                    synced_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_results (
                    plan_date TEXT PRIMARY KEY,
                    windows_count INTEGER,
                    plan_status TEXT,
                    actual_cost_with_aid_eur REAL,
                    actual_cost_without_aid_eur REAL,
                    actual_grid_import_kwh REAL,
                    actual_generation_kwh REAL,
                    actual_consumption_kwh REAL,
                    actual_battery_discharge_kwh REAL,
                    completed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS optimization_plans (
                    plan_id TEXT PRIMARY KEY,
                    plan_date TEXT NOT NULL,
                    horizon_start TEXT NOT NULL,
                    horizon_end TEXT NOT NULL,
                    plan_status TEXT NOT NULL,
                    forecast_json TEXT NOT NULL,
                    actual_json TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS continuous_optimization_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL,
                    settings_json TEXT NOT NULL,
                    battery_model_json TEXT,
                    last_run_at TEXT,
                    next_run_at TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS continuous_optimization_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    settings_json TEXT NOT NULL
                );
                INSERT OR IGNORE INTO continuous_optimization_state
                    (id, enabled, settings_json, updated_at)
                VALUES (1, 0, '{}', CURRENT_TIMESTAMP);
                """
            )

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @staticmethod
    def _number(value):
        if value is None or value == "":
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @classmethod
    def _energy_number(cls, energy):
        if not isinstance(energy, dict):
            return None
        return cls._number(energy.get("value"))

    def record_event(self, event_type, summary, details=None, created_at=None):
        details_json = json.dumps(
            details or {},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        timestamp = created_at or datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO events (created_at, event_type, summary, details_json)
                VALUES (?, ?, ?, ?)
                """,
                (timestamp, event_type, summary, details_json),
            )

    def record_telemetry(
        self,
        status,
        sampled_at,
        tariff_with_aid_eur_per_kwh,
        tariff_without_aid_eur_per_kwh,
    ):
        metrics = status.get("metrics", {})
        daily = status.get("daily_energy", {})
        capacity = status.get("battery_capacity", {})
        if sampled_at.tzinfo is None:
            raise ValueError("Telemetry sample time must include a timezone.")
        sampled_at = sampled_at.replace(microsecond=0)
        timestamp = sampled_at.isoformat(timespec="seconds")
        daily_grid_import = self._energy_number(daily.get("grid_import"))

        with closing(self._connect()) as connection, connection:
            previous = connection.execute(
                """
                SELECT sampled_at, daily_grid_import_kwh
                FROM telemetry
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            import_delta = 0.0
            if previous and daily_grid_import is not None:
                previous_at = datetime.fromisoformat(previous["sampled_at"])
                previous_import = previous["daily_grid_import_kwh"]
                if (
                    previous_at.date() == sampled_at.date()
                    and previous_import is not None
                    and daily_grid_import >= previous_import
                ):
                    import_delta = daily_grid_import - previous_import
            import_cost_with_aid = import_delta * tariff_with_aid_eur_per_kwh
            import_cost_without_aid = import_delta * tariff_without_aid_eur_per_kwh
            connection.execute(
                """
                INSERT INTO telemetry (
                    sampled_at, data_time, generation_kw, consumption_kw,
                    battery_soc, battery_charge_kw, battery_discharge_kw, grid_kw,
                    battery_capacity_kwh, daily_generation_kwh,
                    daily_consumption_kwh, daily_grid_import_kwh,
                    daily_grid_export_kwh, daily_battery_charge_kwh,
                    daily_battery_discharge_kwh, observed_grid_import_delta_kwh,
                    observed_grid_import_cost_with_aid_eur,
                    observed_grid_import_cost_without_aid_eur
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    status.get("data_time"),
                    self._number(metrics.get("generation_power")),
                    self._number(metrics.get("use_power")),
                    self._number(metrics.get("battery_soc")),
                    self._number(metrics.get("charge_power")),
                    self._number(metrics.get("discharge_power")),
                    self._number(metrics.get("grid_power")),
                    self._energy_number(capacity),
                    self._energy_number(daily.get("generation")),
                    self._energy_number(daily.get("consumption")),
                    daily_grid_import,
                    self._energy_number(daily.get("grid_export")),
                    self._energy_number(daily.get("battery_charge")),
                    self._energy_number(daily.get("battery_discharge")),
                    import_delta,
                    import_cost_with_aid,
                    import_cost_without_aid,
                ),
            )

    def daily_consumption_history(self, start_date, end_date):
        if not isinstance(start_date, date) or not isinstance(end_date, date):
            raise ValueError("Consumption history dates must be dates.")
        if end_date < start_date:
            raise ValueError("Consumption history end date precedes its start date.")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT sampled_at, daily_consumption_kwh
                FROM telemetry
                WHERE substr(sampled_at, 1, 10) >= ?
                  AND substr(sampled_at, 1, 10) < ?
                  AND daily_consumption_kwh IS NOT NULL
                ORDER BY id DESC
                """,
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()

        latest_by_day = {}
        for row in rows:
            sampled_at = datetime.fromisoformat(row["sampled_at"])
            sampled_date = sampled_at.date()
            if sampled_date not in latest_by_day:
                latest_by_day[sampled_date] = (sampled_at, row["daily_consumption_kwh"])
        daily_values = {
            sampled_date: value
            for sampled_date, (sampled_at, value) in latest_by_day.items()
            if sampled_at.hour == 23
            and sampled_at.minute >= 30
            and value >= 0
        }

        for sampled_date, profile in self._hourly_profiles(
            start_date,
            end_date,
        ).items():
            if sampled_date not in daily_values and len(profile) == 24:
                daily_values[sampled_date] = sum(profile.values())

        return [
            {"date": sampled_date, "consumption_kwh": value}
            for sampled_date, value in sorted(daily_values.items())
        ]

    def hourly_consumption_history(self, start_date, end_date):
        return [
            {"date": sampled_date, "hourly_consumption_kw": profile}
            for sampled_date, profile in sorted(
                self._hourly_profiles(start_date, end_date).items()
            )
        ]

    def _hourly_profiles(self, start_date, end_date):
        if not isinstance(start_date, date) or not isinstance(end_date, date):
            raise ValueError("Consumption history dates must be dates.")
        if end_date < start_date:
            raise ValueError("Consumption history end date precedes its start date.")
        with closing(self._connect()) as connection:
            telemetry_rows = connection.execute(
                """
                SELECT sampled_at, consumption_kw
                FROM telemetry
                WHERE substr(sampled_at, 1, 10) >= ?
                  AND substr(sampled_at, 1, 10) < ?
                  AND consumption_kw IS NOT NULL
                ORDER BY id
                """,
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
            historical_rows = connection.execute(
                """
                SELECT sampled_at, consumption_kw
                FROM historical_consumption
                WHERE substr(sampled_at, 1, 10) >= ?
                  AND substr(sampled_at, 1, 10) < ?
                """,
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()

        samples_by_day_hour = {}
        samples_by_timestamp = {}
        for row in (*telemetry_rows, *historical_rows):
            sampled_at = datetime.fromisoformat(row["sampled_at"])
            power_kw = row["consumption_kw"]
            if power_kw < 0:
                continue
            samples_by_timestamp[sampled_at.astimezone(timezone.utc)] = (
                sampled_at,
                power_kw,
            )
        for sampled_at, power_kw in samples_by_timestamp.values():
            key = (sampled_at.date(), sampled_at.hour)
            samples_by_day_hour.setdefault(key, []).append(power_kw)

        hourly_profiles = {}
        for (sampled_date, hour), samples in samples_by_day_hour.items():
            if len(samples) >= 6:
                hourly_profiles.setdefault(sampled_date, {})[hour] = (
                    sum(samples) / len(samples)
                )
        return {
            sampled_date: profile
            for sampled_date, profile in hourly_profiles.items()
            if len(profile) >= 18
        }

    def record_consumption_history(self, points, synced_at=None):
        rows = []
        for sampled_at, power_kw in points:
            if not isinstance(sampled_at, datetime) or sampled_at.tzinfo is None:
                raise ValueError("Historical consumption timestamps must include a timezone.")
            number = self._number(power_kw)
            if number is None or number < 0:
                continue
            rows.append(
                (
                    sampled_at.replace(microsecond=0).isoformat(timespec="seconds"),
                    number,
                )
            )
        timestamp = (synced_at or datetime.now(timezone.utc)).isoformat(
            timespec="seconds"
        )
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO historical_consumption
                    (sampled_at, consumption_kw)
                VALUES (?, ?)
                """,
                rows,
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO history_sync (source, synced_at)
                VALUES ('sofarcloud_use_power', ?)
                """,
                (timestamp,),
            )
        return len(rows)

    def consumption_history_synced_at(self):
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT synced_at
                FROM history_sync
                WHERE source = 'sofarcloud_use_power'
                """
            ).fetchone()
        return datetime.fromisoformat(row["synced_at"]) if row else None

    def recent(self, limit=50):
        limit = max(1, min(200, int(limit)))
        with closing(self._connect()) as connection:
            events = connection.execute(
                """
                SELECT id, created_at, event_type, summary, details_json
                FROM events ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            telemetry = connection.execute(
                """
                SELECT id, sampled_at, data_time, generation_kw,
                       consumption_kw, battery_soc, battery_charge_kw,
                       battery_discharge_kw, grid_kw, battery_capacity_kwh,
                       daily_generation_kwh, daily_consumption_kwh,
                       daily_grid_import_kwh, daily_grid_export_kwh,
                       daily_battery_charge_kwh, daily_battery_discharge_kwh,
                       observed_grid_import_delta_kwh,
                       observed_grid_import_cost_with_aid_eur,
                       observed_grid_import_cost_without_aid_eur
                FROM telemetry ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return {
            "events": [
                {
                    "id": row["id"],
                    "created_at": row["created_at"],
                    "event_type": row["event_type"],
                    "summary": row["summary"],
                    "details": json.loads(row["details_json"]),
                }
                for row in events
            ],
            "telemetry": [dict(row) for row in telemetry],
        }

    def record_daily_result(self, plan_date, windows_count, plan_status, grid_import_kwh, cost_with_aid, cost_without_aid, generation_kwh, consumption_kwh, battery_discharge_kwh):
        """Record daily plan results after plan completion."""
        with closing(self._connect()) as connection:
            connection.execute(
                    """
                    INSERT OR REPLACE INTO daily_results
                    (plan_date, windows_count, plan_status, actual_cost_with_aid_eur, 
                     actual_cost_without_aid_eur, actual_grid_import_kwh, actual_generation_kwh,
                     actual_consumption_kwh, actual_battery_discharge_kwh, completed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        plan_date,
                        windows_count,
                        plan_status,
                        self._number(cost_with_aid),
                        self._number(cost_without_aid),
                        self._number(grid_import_kwh),
                        self._number(generation_kwh),
                        self._number(consumption_kwh),
                        self._number(battery_discharge_kwh),
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
            )
            connection.commit()

    def get_daily_results(self, limit=30):
        """Get recent daily plan results."""
        limit = max(1, min(200, int(limit)))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                    """
                    SELECT plan_date, windows_count, plan_status, 
                           actual_cost_with_aid_eur, actual_cost_without_aid_eur,
                           actual_grid_import_kwh, actual_generation_kwh,
                           actual_consumption_kwh, actual_battery_discharge_kwh, completed_at
                    FROM daily_results
                    ORDER BY plan_date DESC
                    LIMIT ?
                    """,
                    (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def record_optimization_plan(self, plan_id, forecast, plan_status="pending"):
        if not isinstance(plan_id, str) or not plan_id:
            raise ValueError("Optimization plan ID must be a non-empty string.")
        if not isinstance(forecast, dict):
            raise ValueError("Optimization forecast must be an object.")
        if not isinstance(plan_status, str) or not plan_status:
            raise ValueError("Optimization plan status must be a non-empty string.")
        plan_date = forecast.get("plan_date")
        horizon_start = forecast.get("horizon_start")
        horizon_end = forecast.get("horizon_end")
        try:
            start = datetime.fromisoformat(horizon_start)
            end = datetime.fromisoformat(horizon_end)
        except (TypeError, ValueError):
            raise ValueError("Optimization forecast must include a valid horizon.") from None
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError("Optimization forecast horizon must be timezone-aware and increasing.")
        if not isinstance(plan_date, str) or not plan_date:
            raise ValueError("Optimization forecast must include its plan date.")
        forecast_json = json.dumps(
            forecast,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO optimization_plans (
                    plan_id, plan_date, horizon_start, horizon_end, plan_status,
                    forecast_json, actual_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    plan_id,
                    plan_date,
                    start.isoformat(timespec="minutes"),
                    end.isoformat(timespec="minutes"),
                    plan_status,
                    forecast_json,
                    created_at,
                ),
            )

    def update_optimization_plan_forecast(self, plan_id, forecast, plan_status):
        if not isinstance(plan_id, str) or not plan_id:
            raise ValueError("Optimization plan ID must be a non-empty string.")
        if not isinstance(forecast, dict):
            raise ValueError("Optimization forecast must be an object.")
        plan_date = forecast.get("plan_date")
        try:
            start = datetime.fromisoformat(forecast.get("horizon_start"))
            end = datetime.fromisoformat(forecast.get("horizon_end"))
        except (TypeError, ValueError):
            raise ValueError("Optimization forecast must include a valid horizon.") from None
        if (
            start.tzinfo is None
            or end.tzinfo is None
            or end <= start
            or not isinstance(plan_date, str)
            or not plan_date
        ):
            raise ValueError("Optimization forecast must include a valid date and horizon.")
        if not isinstance(plan_status, str) or not plan_status:
            raise ValueError("Optimization plan status must be a non-empty string.")
        forecast_json = json.dumps(
            forecast,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE optimization_plans
                SET plan_date = ?, horizon_start = ?, horizon_end = ?,
                    plan_status = ?, forecast_json = ?, actual_json = NULL,
                    completed_at = NULL
                WHERE plan_id = ?
                """,
                (
                    plan_date,
                    start.isoformat(timespec="minutes"),
                    end.isoformat(timespec="minutes"),
                    plan_status,
                    forecast_json,
                    plan_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Optimization plan {plan_id} was not found.")

    def update_optimization_plan_result(self, plan_id, plan_status, actual):
        if not isinstance(plan_id, str) or not plan_id:
            raise ValueError("Optimization plan ID must be a non-empty string.")
        if not isinstance(plan_status, str) or not plan_status:
            raise ValueError("Optimization plan status must be a non-empty string.")
        actual_json = json.dumps(
            actual,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE optimization_plans
                SET plan_status = ?, actual_json = ?, completed_at = ?
                WHERE plan_id = ?
                """,
                (plan_status, actual_json, completed_at, plan_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Optimization plan {plan_id} was not found.")

    def get_optimization_plans(self, limit=30):
        limit = max(1, min(200, int(limit)))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT plan_id, plan_date, horizon_start, horizon_end,
                       plan_status, forecast_json, actual_json, created_at,
                       completed_at
                FROM optimization_plans
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                **{
                    key: row[key]
                    for key in (
                        "plan_id",
                        "plan_date",
                        "horizon_start",
                        "horizon_end",
                        "plan_status",
                        "created_at",
                        "completed_at",
                    )
                },
                "forecast": json.loads(row["forecast_json"]),
                "actual": json.loads(row["actual_json"])
                if row["actual_json"] is not None
                else None,
            }
            for row in rows
        ]

    def telemetry_for_period(self, start, end):
        start_at = datetime.fromisoformat(start)
        end_at = datetime.fromisoformat(end)
        if start_at.tzinfo is None or end_at.tzinfo is None or end_at <= start_at:
            raise ValueError("Telemetry range must be timezone-aware and increasing.")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT sampled_at, generation_kw, consumption_kw, battery_soc,
                       battery_charge_kw, battery_discharge_kw, grid_kw,
                       battery_capacity_kwh,
                       observed_grid_import_delta_kwh,
                       observed_grid_import_cost_with_aid_eur,
                       observed_grid_import_cost_without_aid_eur
                FROM telemetry
                WHERE julianday(sampled_at) >= julianday(?) - (15.0 / 1440.0)
                  AND julianday(sampled_at) <= julianday(?) + (15.0 / 1440.0)
                ORDER BY julianday(sampled_at), id
                """,
                (start_at.isoformat(), end_at.isoformat()),
            ).fetchall()
        return [dict(row) for row in rows]

    def telemetry_chart_period(self, start, end):
        start_at = datetime.fromisoformat(start)
        end_at = datetime.fromisoformat(end)
        if start_at.tzinfo is None or end_at.tzinfo is None or end_at <= start_at:
            raise ValueError("Telemetry chart range must be timezone-aware and increasing.")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT sampled_at, data_time, generation_kw, consumption_kw,
                       battery_soc, battery_charge_kw, battery_discharge_kw, grid_kw
                FROM telemetry
                WHERE julianday(sampled_at) >= julianday(?)
                  AND julianday(sampled_at) <= julianday(?)
                ORDER BY julianday(sampled_at), id
                """,
                (start_at.isoformat(), end_at.isoformat()),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_continuous_optimization_state(self):
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT enabled, settings_json, battery_model_json, last_run_at,
                       next_run_at, last_error
                FROM continuous_optimization_state
                WHERE id = 1
                """
            ).fetchone()
        return {
            "enabled": bool(row["enabled"]),
            "settings": json.loads(row["settings_json"]),
            "battery_model": json.loads(row["battery_model_json"])
            if row["battery_model_json"]
            else None,
            "last_run_at": row["last_run_at"],
            "next_run_at": row["next_run_at"],
            "last_error": row["last_error"],
        }

    def save_continuous_optimization_state(self, state, changed_at=None):
        if not isinstance(state, dict) or not isinstance(state.get("enabled"), bool):
            raise ValueError("Continuous optimization state must include an enabled flag.")
        settings = state.get("settings")
        if not isinstance(settings, dict):
            raise ValueError("Continuous optimization settings must be an object.")
        battery_model = state.get("battery_model")
        if battery_model is not None and not isinstance(battery_model, dict):
            raise ValueError("Continuous optimization battery model must be an object.")
        timestamp = (changed_at or datetime.now(timezone.utc)).isoformat(
            timespec="seconds"
        )
        settings_json = json.dumps(
            settings,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        battery_model_json = (
            json.dumps(
                battery_model,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            if battery_model is not None
            else None
        )
        with closing(self._connect()) as connection, connection:
            previous = connection.execute(
                "SELECT enabled FROM continuous_optimization_state WHERE id = 1"
            ).fetchone()
            was_enabled = bool(previous["enabled"]) if previous else False
            if state["enabled"] and not was_enabled:
                connection.execute(
                    """
                    INSERT INTO continuous_optimization_sessions
                        (started_at, settings_json)
                    VALUES (?, ?)
                    """,
                    (timestamp, settings_json),
                )
            elif not state["enabled"] and was_enabled:
                connection.execute(
                    """
                    UPDATE continuous_optimization_sessions
                    SET ended_at = ?
                    WHERE ended_at IS NULL
                    """,
                    (timestamp,),
                )
            connection.execute(
                """
                INSERT INTO continuous_optimization_state (
                    id, enabled, settings_json, battery_model_json, last_run_at,
                    next_run_at, last_error, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    enabled = excluded.enabled,
                    settings_json = excluded.settings_json,
                    battery_model_json = excluded.battery_model_json,
                    last_run_at = excluded.last_run_at,
                    next_run_at = excluded.next_run_at,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (
                    int(state["enabled"]),
                    settings_json,
                    battery_model_json,
                    state.get("last_run_at"),
                    state.get("next_run_at"),
                    state.get("last_error"),
                    timestamp,
                ),
            )

    def get_continuous_optimization_sessions(self, start, end):
        start_at = datetime.fromisoformat(start)
        end_at = datetime.fromisoformat(end)
        if start_at.tzinfo is None or end_at.tzinfo is None or end_at <= start_at:
            raise ValueError("Continuous optimization range must be timezone-aware and increasing.")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT started_at, ended_at, settings_json
                FROM continuous_optimization_sessions
                WHERE julianday(started_at) < julianday(?)
                  AND (ended_at IS NULL OR julianday(ended_at) > julianday(?))
                ORDER BY julianday(started_at)
                """,
                (end_at.isoformat(), start_at.isoformat()),
            ).fetchall()
        return [
            {
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "settings": json.loads(row["settings_json"]),
            }
            for row in rows
        ]

    def get_continuous_optimization_session(self, session_id):
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT id, started_at, ended_at, settings_json
                FROM continuous_optimization_sessions
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "settings": json.loads(row["settings_json"]),
        }

    def get_latest_continuous_optimization_session(self):
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT id, started_at, ended_at, settings_json
                FROM continuous_optimization_sessions
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "settings": json.loads(row["settings_json"]),
        }

    def update_continuous_optimization_runtime(
        self,
        *,
        last_run_at,
        next_run_at,
        last_error,
        battery_model=None,
    ):
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT battery_model_json FROM continuous_optimization_state WHERE id = 1"
            ).fetchone()
            battery_model_json = row["battery_model_json"] if row else None
            if battery_model is not None:
                battery_model_json = json.dumps(
                    battery_model,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            connection.execute(
                """
                UPDATE continuous_optimization_state
                SET last_run_at = ?, next_run_at = ?, last_error = ?,
                    battery_model_json = ?, updated_at = ?
                WHERE id = 1
                """,
                (
                    last_run_at,
                    next_run_at,
                    last_error,
                    battery_model_json,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
