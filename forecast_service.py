"""Keeps the learned forecast model fresh and turns weather forecasts into hourly predictions."""

import json
import logging
import statistics
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import train_forecast
from forecast_model import MODEL_VERSION, predict_hourly_kwh, predict_pv_kwh, pv_features

FORECAST_MODES = ("off", "shadow", "active")
RETRAIN_INTERVAL = timedelta(days=7)
FAILURE_RETRY = timedelta(hours=6)
CHECK_INTERVAL_SECONDS = 3600
MIN_TEMPERATURE_HOURS = 12


def _weather_by_label(forecast):
    weather = {}
    for item in forecast.get("hourly") or []:
        if not isinstance(item, dict) or "ghi_wm2" not in item or "temperature_c" not in item:
            continue
        label = datetime.fromisoformat(item["time"])
        label = label.replace(tzinfo=timezone.utc) if label.tzinfo is None else label.astimezone(timezone.utc)
        weather[label] = (item["ghi_wm2"], item["temperature_c"])
    return weather


def predict_hours(model, forecast, hours_utc, tz_name):
    """UTC interval start -> {pv_kwh, consumption_kwh}; raises ValueError when the forecast lacks inputs."""
    tz = ZoneInfo(tz_name)
    weather = _weather_by_label(forecast)
    offset = timedelta(hours=model["radiation_label_offset_hours"])
    latitude = model["location"]["latitude"]
    longitude = model["location"]["longitude"]
    temperatures = {}
    for label, (_, temperature) in weather.items():
        temperatures.setdefault((label - offset).astimezone(tz).date(), []).append(temperature)
    daily_profiles = {}
    predictions = {}
    for hour in hours_utc:
        if hour + offset not in weather:
            raise ValueError("The weather forecast has no irradiance for the learned model.")
        local = hour.astimezone(tz)
        day = local.date()
        if day not in daily_profiles:
            values = temperatures.get(day, [])
            if len(values) < MIN_TEMPERATURE_HOURS:
                raise ValueError("The weather forecast has too few temperature hours for the learned model.")
            daily_profiles[day] = predict_hourly_kwh(model["consumption"], day, statistics.fmean(values))
        features = pv_features(hour, weather[hour + offset][0], latitude, longitude)
        predictions[hour] = {
            "pv_kwh": predict_pv_kwh(model["pv"], features),
            "consumption_kwh": daily_profiles[day][local.hour],
        }
    return predictions


class ForecastModelService:
    def __init__(self, path, client, fetch_json, geocode, tz_name, location=None, train_days=365, logger=None, trainer=None):
        self.path = Path(path)
        self.client = client
        self.fetch_json = fetch_json
        self.geocode = geocode
        self.tz_name = tz_name
        self.location = location.strip() if isinstance(location, str) and location.strip() else None
        self.train_days = train_days
        self.logger = logger or logging.getLogger(__name__)
        self._trainer = trainer or self._train_from_sources
        self._lock = threading.Lock()
        self._model = self._load()
        self._trained_this_run = False
        self._training = False
        self._last_attempt = None
        self._last_error = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None

    def _load(self):
        if not self.path.exists():
            return None
        try:
            model = json.loads(self.path.read_text())
        except (OSError, ValueError) as error:
            self.logger.warning("Could not read the forecast model %s: %s", self.path, error)
            return None
        return model if isinstance(model, dict) and model.get("version") == MODEL_VERSION else None

    def model(self):
        with self._lock:
            return self._model

    def set_location(self, location):
        location = location.strip() if isinstance(location, str) else ""
        if not location:
            return
        with self._lock:
            changed = location != self.location
            self.location = location
        if changed:
            self._wake.set()

    def due(self, now):
        with self._lock:
            if not self.location or self._training:
                return False
            if self._last_error and self._last_attempt and now - self._last_attempt < FAILURE_RETRY:
                return False
            if not self._trained_this_run or self._model is None:
                return True
            if self._model.get("location", {}).get("requested") != self.location:
                return True
            trained_at = datetime.fromisoformat(self._model["trained_at"])
            return now - trained_at >= RETRAIN_INTERVAL

    def _train_from_sources(self, location, now):
        latitude, longitude, place = self.geocode(location)
        actuals, weather = train_forecast.fetch_training_data(self.client, self.fetch_json, latitude, longitude, self.train_days, now)
        return train_forecast.train(actuals, weather, latitude, longitude, place.get("name") or location, self.tz_name, now=now)

    def train_now(self, now=None):
        now = now or datetime.now(timezone.utc)
        with self._lock:
            location = self.location
            self._training = True
            self._last_attempt = now
        try:
            model = self._trainer(location, now)
            model["location"]["requested"] = location
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(model))
            temporary.replace(self.path)
        except Exception as error:
            self.logger.warning("Forecast model training failed: %s", error)
            with self._lock:
                self._last_error = str(error)
                self._training = False
            return False
        with self._lock:
            self._model = model
            self._trained_this_run = True
            self._last_error = None
            self._training = False
        self.logger.info("Forecast model trained on %s measured hours.", model["data"]["measured_hours"])
        return True

    def _run(self):
        while not self._stop.is_set():
            if self.due(datetime.now(timezone.utc)):
                self.train_now()
            self._wake.wait(CHECK_INTERVAL_SECONDS)
            self._wake.clear()

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="sofar-forecast-model", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def state(self, mode):
        with self._lock:
            model = self._model
            status = "training" if self._training else "ready" if model else "error" if self._last_error else "missing"
            state = {
                "mode": mode,
                "status": status,
                "location": self.location,
                "last_error": self._last_error,
                "trained_at": None,
                "next_training_after": None,
            }
            if model:
                state.update({
                    "trained_at": model["trained_at"],
                    "next_training_after": (datetime.fromisoformat(model["trained_at"]) + RETRAIN_INTERVAL).isoformat(timespec="seconds"),
                    "data": model.get("data"),
                    "evaluation": model.get("evaluation"),
                })
            return state
