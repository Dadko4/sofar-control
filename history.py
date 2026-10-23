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
