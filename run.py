import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import sqlite3
import statistics
import tempfile
import threading
import time
import uuid
from contextlib import closing
from datetime import date as date_type, datetime, time as datetime_time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import socketio

from history import HistoryStore
from continuous_optimizer import ContinuousOptimizationController
from live_metrics import calculate_live_metrics
from multi_window_scheduler import MultiWindowScheduler, compare_plan_with_telemetry
from planner import SLOT_HOURS, BatteryModel, optimize_schedule
from tariff import import_price


def load_env_file(path):
    """Load simple KEY=VALUE entries without overriding existing environment values."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


load_env_file(Path(__file__).with_name(".env"))

BASE_URL = os.environ.get("SOFAR_BASE_URL", "https://eu.sofarcloud.com").rstrip("/")
USERNAME = os.environ.get("SOFAR_USERNAME", "")
PASSWORD = os.environ.get("SOFAR_PASSWORD", "")
PLANT_ID = os.environ.get("SOFAR_PLANT_ID", "780239285650591744")
INVERTER_MODEL = os.environ.get("SOFAR_INVERTER_MODEL", "HYD 5KTL-3PH")
COMMAND_MODEL = os.environ.get("SOFAR_COMMAND_MODEL", "HYD 5-20KTL-3PH")
INVERTER_SN = os.environ.get("SOFAR_INVERTER_SN", "SP2ES105N7E171")
WEB_PORT = int(os.environ.get("SOFAR_WEB_PORT", "8765"))
WEB_HOST = os.environ.get("SOFAR_WEB_HOST", "127.0.0.1")
WEB_USERNAME = os.environ.get("SOFAR_WEB_USERNAME", "")
WEB_PASSWORD = os.environ.get("SOFAR_WEB_PASSWORD", "")
TIMEZONE = os.environ.get("SOFAR_TIMEZONE", "Europe/Bratislava")
BATTERY_CAPACITY_KWH = float(os.environ.get("SOFAR_BATTERY_CAPACITY_KWH", "5"))
MAX_DISCHARGE_POWER_W = int(os.environ.get("SOFAR_MAX_DISCHARGE_POWER_W", "2500"))
DISCHARGE_TARGET_SOC = 30
DISCHARGE_MONITOR_INTERVAL = 10
ENABLE_PASSIVE_DISCHARGE = os.environ.get(
    "SOFAR_ENABLE_PASSIVE_DISCHARGE", "false"
).lower() in {"1", "true", "yes"}
PASSIVE_MODE_COMMAND_MODEL = "HYD 5-20KTL-3PH"
PASSIVE_DISCHARGE_FIELDS = frozenset({"passiveManualBup", "passiveManualBlo"})
MODE_CONTROL_LOCK = threading.RLock()
ALLOWED_ORIGINS = {
    origin.strip()
    for origin in os.environ.get(
        "SOFAR_ALLOWED_ORIGINS",
        f"http://127.0.0.1:{WEB_PORT},http://localhost:{WEB_PORT}",
    ).split(",")
    if origin.strip()
}
WEB_ROOT = Path(__file__).parent / "web"
DATA_DIR = Path(os.environ.get("SOFAR_DATA_DIR", str(Path(__file__).parent / "data")))
SCHEDULED_PLAN_FILE = DATA_DIR / "scheduled-plan.json"
MULTI_WINDOW_STATE_FILE = DATA_DIR / "multi_window_state.json"
REQUEST_TIMEOUT = 15
COMMAND_TIMEOUT = 60
SOCKET_URL = "https://eu-socket.sofarcloud.com"
OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
BATTERY_CHARGE_EFFICIENCY = 0.92
BATTERY_DISCHARGE_EFFICIENCY = 0.95
PV_SYSTEM_EFFICIENCY = 0.8
MINIMUM_BATTERY_SOC = 30
MAXIMUM_BATTERY_SOC = 95
BATTERY_WEAR_EUR_KWH = float(os.environ.get("SOFAR_BATTERY_WEAR_EUR_KWH", "0.01"))
EXPORT_PRICE_EUR_KWH = float(os.environ.get("SOFAR_EXPORT_PRICE_EUR_KWH", "0"))
PV_TILT_SETTING = os.environ.get("SOFAR_PV_TILT_DEG", "").strip()
PV_TILT_DEG = float(PV_TILT_SETTING) if PV_TILT_SETTING else None
PV_AZIMUTH_DEG = float(os.environ.get("SOFAR_PV_AZIMUTH_DEG", "0"))

if not math.isfinite(BATTERY_CAPACITY_KWH) or BATTERY_CAPACITY_KWH <= 0:
    raise ValueError("SOFAR_BATTERY_CAPACITY_KWH must be a positive number.")
if not 100 <= MAX_DISCHARGE_POWER_W <= 2500:
    raise ValueError("SOFAR_MAX_DISCHARGE_POWER_W must be between 100 and 2500.")
if not math.isfinite(BATTERY_WEAR_EUR_KWH) or BATTERY_WEAR_EUR_KWH < 0:
    raise ValueError("SOFAR_BATTERY_WEAR_EUR_KWH must be zero or positive.")
if not math.isfinite(EXPORT_PRICE_EUR_KWH) or EXPORT_PRICE_EUR_KWH < 0:
    raise ValueError("SOFAR_EXPORT_PRICE_EUR_KWH must be zero or positive.")
if PV_TILT_DEG is not None and not 0 <= PV_TILT_DEG <= 90:
    raise ValueError("SOFAR_PV_TILT_DEG must be between 0 and 90.")
if not -180 <= PV_AZIMUTH_DEG <= 180:
    raise ValueError("SOFAR_PV_AZIMUTH_DEG must be between -180 and 180.")

LOGIN_PATH = "/api/user/auth/he/login"
STATION_DETAIL_PATH = "/api/device/stationInfo/selectStationDetail"
STATION_HISTORY_PATH = "/api/device/stationInfo/selectStationHistoryData"
LOGIN_SIGN = "H_3uwQ6lJ4g"
DETAIL_SIGN = "fgHFeEeOOMk"
COMMAND_SCHEMA_PATH = "/api/device/selectCmdParamByModel"
DEVICE_COMMAND_PATH = "/api/device/command"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger(__name__)
HISTORY_STORE = None
CONTINUOUS_OPTIMIZER = None


def record_history_event(history_store, event_type, summary, details=None):
    if history_store is None:
        return None
    try:
        history_store.record_event(
            event_type,
            summary,
            details,
            created_at=datetime.now(ZoneInfo(TIMEZONE)).isoformat(timespec="seconds"),
        )
    except (sqlite3.Error, TypeError, ValueError) as error:
        LOGGER.exception("Could not save %s to the history database.", event_type)
        return f"History entry could not be saved: {error}"
    return None


class SofarCloudError(Exception):
    """Raised when SofarCloud returns an unusable response."""

    def __init__(
        self,
        message,
        stage,
        api_code=None,
        http_status=None,
        content_type=None,
        response_format=None,
    ):
        super().__init__(message)
        self.stage = stage
        self.api_code = api_code
        self.http_status = http_status
        self.content_type = content_type
        self.response_format = response_format

    def as_dict(self):
        details = {"error": str(self), "stage": self.stage}
        if self.api_code is not None:
            details["api_code"] = self.api_code
        if self.http_status is not None:
            details["http_status"] = self.http_status
        if self.content_type:
            details["content_type"] = self.content_type
        if self.response_format:
            details["response_format"] = self.response_format
        return details


def parse_station_status(detail, model):
    realtime = detail.get("stationRealTimeVo") or {}
    return {
        "model": model,
        "station_name": detail.get("stationName") or "FVE",
        "station_state": detail.get("stationState"),
        "battery_capacity": {
            "value": detail.get("stationBatteryCapacity"),
            "unit": detail.get("stationBatteryCapacityUnit"),
        },
        "metrics": {
            "generation_power": realtime.get("generationPower"),
            "use_power": realtime.get("usePower"),
            "battery_soc": realtime.get("batterySoc"),
            "battery_power": realtime.get("batteryPower"),
            "charge_power": realtime.get("chargePower"),
            "discharge_power": realtime.get("disChargePower"),
            "grid_power": realtime.get("gridPower"),
        },
        "daily_energy": {
            "generation": {
                "value": realtime.get("generationValue"),
                "unit": realtime.get("generationValueUnit"),
            },
            "consumption": {
                "value": realtime.get("useValue"),
                "unit": realtime.get("useValueUnit"),
            },
            "grid_export": {
                "value": realtime.get("gridValue"),
                "unit": realtime.get("gridValueUnit"),
            },
            "grid_import": {
                "value": realtime.get("buyValue"),
                "unit": realtime.get("buyValueUnit"),
            },
            "battery_charge": {
                "value": realtime.get("chargeValue"),
                "unit": realtime.get("chargeValueUnit"),
            },
            "battery_discharge": {
                "value": realtime.get("disChargeValue"),
                "unit": realtime.get("disChargeValueUnit"),
            },
        },
        "data_time": realtime.get("dataTime"),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def sign_sofar_request(values):
    """Match the request signature used by SofarCloud's web client."""
    message = "&".join(
        f"{key}={value}"
        for key, value in sorted(values.items())
        if isinstance(value, (str, int, float, bool))
    )
    key = hashlib.md5(message.encode("utf-8")).hexdigest().encode("ascii")
    digest = hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:8]).decode("ascii").rstrip("=")


def estimate_discharge(soc, power_w, capacity_kwh=BATTERY_CAPACITY_KWH):
    if isinstance(soc, bool):
        raise ValueError("Current battery SOC is unavailable.")
    try:
        soc = float(soc)
    except (TypeError, ValueError):
        raise ValueError("Current battery SOC is unavailable.") from None
    if not math.isfinite(soc) or not DISCHARGE_TARGET_SOC < soc <= 100:
        raise ValueError("Discharging requires a valid SOC above 30%.")
    if isinstance(power_w, bool) or not isinstance(power_w, int) or not 100 <= power_w <= MAX_DISCHARGE_POWER_W:
        raise ValueError(
            f"Discharging power must be between 100 and {MAX_DISCHARGE_POWER_W} W."
        )
    try:
        capacity_kwh = float(capacity_kwh)
    except (TypeError, ValueError):
        raise ValueError("Battery capacity must be a positive number.") from None
    if not math.isfinite(capacity_kwh) or capacity_kwh <= 0:
        raise ValueError("Battery capacity must be a positive number.")

    energy_kwh = capacity_kwh * (soc - DISCHARGE_TARGET_SOC) / 100
    duration_seconds = math.ceil(energy_kwh * 3_600_000 / power_w)
    return {
        "energy_kwh": energy_kwh,
        "duration_seconds": duration_seconds,
        "duration_minutes": duration_seconds / 60,
    }


class ForecastError(Exception):
    """Raised when the weather provider cannot supply a usable forecast."""


def _open_meteo_json(url):
    request = Request(
        url,
        headers={"accept": "application/json", "user-agent": "SofarOpti/0.1"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise ForecastError(f"Open-Meteo returned HTTP {error.code}.") from None
    except (URLError, TimeoutError):
        raise ForecastError("Could not connect to Open-Meteo.") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ForecastError("Open-Meteo returned an invalid response.") from None
    if not isinstance(result, dict):
        raise ForecastError("Open-Meteo returned an unexpected response.")
    if result.get("error"):
        raise ForecastError("Open-Meteo could not provide this forecast.")
    return result


def get_solar_forecast(location, tilt_deg=None, azimuth_deg=0):
    if not isinstance(location, str) or not location.strip() or len(location) > 100:
        raise ValueError("Enter a location name up to 100 characters.")
    geocoding_query = urlencode(
        {
            "name": location.strip(),
            "count": 10,
            "language": "sk",
            "format": "json",
            "countryCode": "SK",
        }
    )
    places = _open_meteo_json(f"{OPEN_METEO_GEOCODING_URL}?{geocoding_query}")
    results = places.get("results")
    if not isinstance(results, list):
        raise ForecastError("Open-Meteo could not find this location in Slovakia.")
    match = next(
        (
            place
            for place in results
            if isinstance(place, dict) and place.get("country_code") == "SK"
        ),
        None,
    )
    if match is None:
        raise ForecastError("Open-Meteo could not find this location in Slovakia.")
    try:
        latitude = float(match["latitude"])
        longitude = float(match["longitude"])
    except (KeyError, TypeError, ValueError):
        raise ForecastError("Open-Meteo returned invalid location coordinates.") from None
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ForecastError("Open-Meteo returned invalid location coordinates.")

    variable = "shortwave_radiation" if tilt_deg is None else "global_tilted_irradiance"
    forecast_params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": variable,
        "forecast_days": 3,
        "timezone": "GMT",
    }
    if tilt_deg is not None:
        forecast_params.update({"tilt": tilt_deg, "azimuth": azimuth_deg})
    response = _open_meteo_json(f"{OPEN_METEO_FORECAST_URL}?{urlencode(forecast_params)}")
    hourly = response.get("hourly")
    if not isinstance(hourly, dict):
        raise ForecastError("Open-Meteo did not return hourly solar data.")
    times = hourly.get("time")
    radiation = hourly.get(variable)
    if not isinstance(times, list) or not isinstance(radiation, list) or len(times) != len(radiation):
        raise ForecastError("Open-Meteo returned incomplete hourly solar data.")
    forecast = []
    for hour, value in zip(times, radiation):
        try:
            irradiance = float(value)
            hour_time = datetime.fromisoformat(hour)
        except (TypeError, ValueError):
            raise ForecastError("Open-Meteo returned invalid hourly solar data.") from None
        if not math.isfinite(irradiance):
            raise ForecastError("Open-Meteo returned invalid hourly solar data.")
        forecast.append(
            {
                "time": hour_time.replace(tzinfo=timezone.utc).isoformat(timespec="minutes"),
                "shortwave_radiation": max(0, irradiance),
            }
        )
    if not forecast:
        raise ForecastError("Open-Meteo returned an empty solar forecast.")
    return {
        "location": match.get("name") or location.strip(),
        "country": match.get("country") or "Slovensko",
        "hourly": forecast,
    }


def seasonal_daily_consumption(target_date, summer_kwh, winter_kwh):
    day_of_year = target_date.timetuple().tm_yday
    winter_peak_day = 15
    season = math.cos(2 * math.pi * (day_of_year - winter_peak_day) / 365.2425)
    return (summer_kwh + winter_kwh) / 2 + (winter_kwh - summer_kwh) / 2 * season


def estimate_daily_consumption(
    target_date,
    summer_kwh,
    winter_kwh,
    history_store=None,
):
    seasonal_estimate = seasonal_daily_consumption(
        target_date,
        summer_kwh,
        winter_kwh,
    )
    if history_store is None:
        return {
            "value_kwh": seasonal_estimate,
            "source": "seasonal_estimate",
            "sample_days": 0,
        }

    recent_days = history_store.daily_consumption_history(
        target_date - timedelta(days=28),
        target_date,
    )
    matching_weekdays = [
        item
        for item in recent_days
        if item["date"].weekday() == target_date.weekday()
    ][-4:]
    if len(matching_weekdays) >= 2:
        samples = matching_weekdays
        source = "same_weekday_history"
    elif recent_days:
        samples = recent_days[-3:]
        source = "recent_days_history"
    else:
        return {
            "value_kwh": seasonal_estimate,
            "source": "seasonal_estimate",
            "sample_days": 0,
        }

    return {
        "value_kwh": statistics.median(
            item["consumption_kwh"] for item in samples
        ),
        "source": source,
        "sample_days": len(samples),
    }


def estimate_hourly_consumption(
    target_date,
    daily_consumption_kwh,
    history_store=None,
):
    if history_store is None:
        return {
            "hourly_kwh": [daily_consumption_kwh / 24] * 24,
            "source": "uniform_estimate",
            "sample_days": 0,
        }

    recent_profiles = history_store.hourly_consumption_history(
        target_date - timedelta(days=28),
        target_date,
    )
    matching_weekdays = [
        profile
        for profile in recent_profiles
        if profile["date"].weekday() == target_date.weekday()
    ][-4:]
    if len(matching_weekdays) >= 2:
        samples = matching_weekdays
        source = "same_weekday_history"
    elif len(recent_profiles) >= 2:
        samples = recent_profiles[-3:]
        source = "recent_days_history"
    else:
        return {
            "hourly_kwh": [daily_consumption_kwh / 24] * 24,
            "source": "uniform_estimate",
            "sample_days": 0,
        }

    hourly_profile = []
    for hour in range(24):
        samples_for_hour = [
            profile["hourly_consumption_kw"][hour]
            for profile in samples
            if hour in profile["hourly_consumption_kw"]
        ]
        hourly_profile.append(
            statistics.median(samples_for_hour)
            if samples_for_hour
            else daily_consumption_kwh / 24
        )

    profile_total = sum(hourly_profile)
    if profile_total <= 0:
        return {
            "hourly_kwh": [daily_consumption_kwh / 24] * 24,
            "source": "uniform_estimate",
            "sample_days": 0,
        }
    scale = daily_consumption_kwh / profile_total
    return {
        "hourly_kwh": [value * scale for value in hourly_profile],
        "source": source,
        "sample_days": len(samples),
    }


def sync_consumption_history(client, history_store, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("History synchronization time must include a timezone.")
    now = now.astimezone(timezone.utc)
    last_sync = history_store.consumption_history_synced_at()
    if last_sync is not None:
        last_sync = last_sync.astimezone(timezone.utc)
        if now - last_sync < timedelta(hours=24):
            return 0
        start = max(
            last_sync - timedelta(days=2),
            now - timedelta(days=365),
        )
    else:
        start = now - timedelta(days=365)
    points = client.get_consumption_history(start, now)
    return history_store.record_consumption_history(points, synced_at=now)


def validate_optimization_settings(settings):
    if not isinstance(settings, dict):
        raise ValueError("Optimization settings must be a JSON object.")
    location = settings.get("location")
    if not isinstance(location, str) or not location.strip() or len(location) > 100:
        raise ValueError("Enter a location name up to 100 characters.")
    try:
        pv_capacity_kwp = float(settings["pv_capacity_kwp"])
        summer_consumption_kwh = float(settings["summer_consumption_kwh"])
        winter_consumption_kwh = float(settings["winter_consumption_kwh"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Enter valid PV and seasonal consumption values.") from None
    if isinstance(settings.get("pv_capacity_kwp"), bool) or not math.isfinite(
        pv_capacity_kwp
    ) or not 0.1 <= pv_capacity_kwp <= 100:
        raise ValueError("PV capacity must be between 0.1 and 100 kWp.")
    if any(
        isinstance(settings.get(field), bool)
        or not math.isfinite(value)
        or not 1 <= value <= 100
        for field, value in (
            ("summer_consumption_kwh", summer_consumption_kwh),
            ("winter_consumption_kwh", winter_consumption_kwh),
        )
    ):
        raise ValueError("Seasonal consumption must be between 1 and 100 kWh per day.")
    energy_aid = settings.get("energy_aid")
    if not isinstance(energy_aid, bool):
        raise ValueError("Energy-aid setting must be true or false.")
    planning_horizon_hours = settings.get("planning_horizon_hours", 24)
    if (
        isinstance(planning_horizon_hours, bool)
        or not isinstance(planning_horizon_hours, int)
        or planning_horizon_hours not in {12, 24, 36}
    ):
        raise ValueError("Planning horizon must be 12, 24 or 36 hours.")
    return {
        "location": location.strip(),
        "pv_capacity_kwp": pv_capacity_kwp,
        "summer_consumption_kwh": summer_consumption_kwh,
        "winter_consumption_kwh": winter_consumption_kwh,
        "energy_aid": energy_aid,
        "planning_horizon_hours": planning_horizon_hours,
    }


def calculate_optimization(settings, client, history_store):
    settings = validate_optimization_settings(settings)
    forecast = get_solar_forecast(settings["location"], PV_TILT_DEG, PV_AZIMUTH_DEG)
    status = client.get_status()
    history_sync_warning = None
    if history_store is not None:
        try:
            history_points_synced = sync_consumption_history(client, history_store)
            if history_points_synced:
                LOGGER.info(
                    "Imported %s historical SofarCloud consumption points.",
                    history_points_synced,
                )
        except SofarCloudError as error:
            LOGGER.warning(
                "Historical consumption sync failed at %s (API code=%s).",
                error.stage,
                error.api_code,
            )
            history_sync_warning = (
                "SofarCloud historical consumption could not be downloaded; "
                "the plan uses locally saved history or the seasonal fallback."
            )
    result = optimize_day(
        status,
        forecast,
        settings["pv_capacity_kwp"],
        settings["summer_consumption_kwh"],
        settings["winter_consumption_kwh"],
        settings["energy_aid"],
        history_store=history_store,
        planning_horizon_hours=settings["planning_horizon_hours"],
    )
    if history_sync_warning:
        result["consumption_history_sync_warning"] = history_sync_warning
    warning = record_history_event(
        history_store,
        "optimization_calculated",
        f"Optimization calculated for {result['plan_date']}; {len(result.get('windows', []))} charging windows; estimated savings €{result.get('estimated_savings_eur', 0):.2f}.",
        {
            "location": result["location"],
            "plan_date": result["plan_date"],
            "windows_count": len(result.get("windows", [])),
            "cost_with_plan_eur": result.get("cost_with_plan_eur"),
            "cost_self_use_eur": result.get("cost_self_use_eur"),
            "estimated_savings_eur": result.get("estimated_savings_eur"),
            "savings_are_estimated": True,
            "energy_aid": settings["energy_aid"],
            "pv_capacity_kwp": settings["pv_capacity_kwp"],
            "summer_consumption_kwh": settings["summer_consumption_kwh"],
            "winter_consumption_kwh": settings["winter_consumption_kwh"],
            "consumption_estimate_source": result.get(
                "consumption_estimate_source"
            ),
            "consumption_history_days": result.get("consumption_history_days"),
            "hourly_consumption_profile_source": result.get(
                "hourly_consumption_profile_source"
            ),
            "hourly_consumption_profile_days": result.get(
                "hourly_consumption_profile_days"
            ),
        },
    )
    if warning:
        result["history_warning"] = warning
    return result


def _local_day_profile_total(target_date, hourly_profile, timezone_info):
    day_start = datetime.combine(
        target_date,
        datetime_time.min,
        tzinfo=timezone_info,
    ).astimezone(timezone.utc)
    day_end = datetime.combine(
        target_date + timedelta(days=1),
        datetime_time.min,
        tzinfo=timezone_info,
    ).astimezone(timezone.utc)
    profile_total = 0.0
    current_hour = day_start
    while current_hour < day_end:
        local_hour = current_hour.astimezone(timezone_info).hour
        profile_total += hourly_profile[local_hour]
        current_hour += timedelta(hours=1)
    if profile_total <= 0:
        raise ValueError("Hourly consumption estimate must be positive.")
    return profile_total


def _ceil_quarter_hour(moment):
    moment = moment.astimezone(timezone.utc)
    floored = moment.replace(minute=moment.minute - moment.minute % 15, second=0, microsecond=0)
    return floored if floored == moment else floored + timedelta(minutes=15)


def _terminal_value_eur_per_kwh(horizon_end_utc, energy_aid):
    display_timezone = ZoneInfo(TIMEZONE)
    prices = [
        import_price((horizon_end_utc + timedelta(hours=hour)).astimezone(display_timezone), energy_aid)
        for hour in range(24)
    ]
    return sum(prices) / len(prices) * BATTERY_DISCHARGE_EFFICIENCY


def optimize_day(
    status,
    forecast,
    pv_capacity_kwp,
    summer_consumption_kwh,
    winter_consumption_kwh,
    energy_aid,
    now=None,
    history_store=None,
    planning_horizon_hours=24,
):
    """Compute charging windows across a rolling horizon in 15-minute slots."""
    
    try:
        pv_capacity_kwp = float(pv_capacity_kwp)
        summer_consumption_kwh = float(summer_consumption_kwh)
        winter_consumption_kwh = float(winter_consumption_kwh)
        soc = float(status["metrics"]["battery_soc"])
        capacity = float(status["battery_capacity"]["value"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Valid battery SOC, capacity, PV and consumption values are required.") from None
    
    if not math.isfinite(pv_capacity_kwp) or not 0.1 <= pv_capacity_kwp <= 100:
        raise ValueError("PV capacity must be between 0.1 and 100 kWp.")
    if any(
        not math.isfinite(value) or not 1 <= value <= 100
        for value in (summer_consumption_kwh, winter_consumption_kwh)
    ):
        raise ValueError("Seasonal consumption must be between 1 and 100 kWh per day.")
    if not isinstance(energy_aid, bool):
        raise ValueError("Energy-aid setting must be true or false.")
    if not math.isfinite(soc) or not 0 <= soc <= 100:
        raise ValueError("Current battery SOC is invalid.")
    if not math.isfinite(capacity) or capacity <= 0:
        raise ValueError("Battery capacity must be positive.")
    if not isinstance(forecast, dict) or not isinstance(forecast.get("hourly"), list):
        raise ForecastError("Hourly solar forecast is missing.")
    if (
        isinstance(planning_horizon_hours, bool)
        or not isinstance(planning_horizon_hours, int)
        or planning_horizon_hours not in {12, 24, 36}
    ):
        raise ValueError("Planning horizon must be 12, 24 or 36 hours.")
    
    try:
        local_now = now or datetime.now(ZoneInfo(TIMEZONE))
        if local_now.tzinfo is None:
            local_now = local_now.replace(tzinfo=ZoneInfo(TIMEZONE))
        local_now = local_now.astimezone(ZoneInfo(TIMEZONE))
    except ZoneInfoNotFoundError:
        raise ValueError(f"Unknown SOFAR_TIMEZONE: {TIMEZONE}") from None
    
    start_utc = _ceil_quarter_hour(local_now)
    hour_start_utc = start_utc.replace(minute=0, second=0, microsecond=0)
    display_timezone = ZoneInfo(TIMEZONE)
    plan_date = start_utc.astimezone(display_timezone).date()
    horizon_end_utc = start_utc + timedelta(hours=planning_horizon_hours)
    horizon_end = horizon_end_utc.astimezone(display_timezone)
    
    forecast_hours = {}
    for item in forecast["hourly"]:
        if not isinstance(item, dict):
            raise ForecastError("Hourly solar forecast contains invalid data.")
        try:
            hour = datetime.fromisoformat(item["time"])
            irradiance = float(item["shortwave_radiation"])
        except (KeyError, TypeError, ValueError):
            raise ForecastError("Hourly solar forecast contains invalid data.") from None
        if hour.tzinfo is None:
            hour = hour.replace(tzinfo=timezone.utc)
        else:
            hour = hour.astimezone(timezone.utc)
        hour = hour.replace(minute=0, second=0, microsecond=0)
        if not math.isfinite(irradiance) or irradiance < 0:
            raise ForecastError("Hourly solar forecast contains invalid radiation values.")
        if hour_start_utc <= hour < horizon_end_utc:
            forecast_hours[hour] = irradiance
    
    expected_hours = math.ceil((horizon_end_utc - hour_start_utc).total_seconds() / 3600)
    if len(forecast_hours) < expected_hours:
        raise ForecastError("Solar forecast does not cover the next charging cycle.")
    
    daily_profiles = {}
    profile_dates = sorted(
        {
            hour.astimezone(display_timezone).date()
            for hour in forecast_hours
        }
    )
    for profile_date in profile_dates:
        daily_estimate = estimate_daily_consumption(
            profile_date,
            summer_consumption_kwh,
            winter_consumption_kwh,
            history_store,
        )
        hourly_estimate = estimate_hourly_consumption(
            profile_date,
            daily_estimate["value_kwh"],
            history_store,
        )
        full_day_profile_total = _local_day_profile_total(
            profile_date,
            hourly_estimate["hourly_kwh"],
            display_timezone,
        )
        daily_profiles[profile_date] = {
            "daily_estimate": daily_estimate,
            "hourly_estimate": hourly_estimate,
            "scale": daily_estimate["value_kwh"] / full_day_profile_total,
        }
    consumption_estimate = daily_profiles[plan_date]["daily_estimate"]
    hourly_consumption_estimate = daily_profiles[plan_date]["hourly_estimate"]
    
    initial_energy_kwh = capacity * soc / 100
    battery = BatteryModel(
        capacity_kwh=capacity,
        min_energy_kwh=capacity * MINIMUM_BATTERY_SOC / 100,
        max_energy_kwh=capacity * MAXIMUM_BATTERY_SOC / 100,
        charge_efficiency=BATTERY_CHARGE_EFFICIENCY,
        discharge_efficiency=BATTERY_DISCHARGE_EFFICIENCY,
        wear_eur_per_kwh=BATTERY_WEAR_EUR_KWH,
    )
    
    slots = []
    slot_start = start_utc
    while slot_start < horizon_end_utc:
        hour_utc = slot_start.replace(minute=0, second=0, microsecond=0)
        hour_local = slot_start.astimezone(display_timezone)
        daily_profile = daily_profiles[hour_local.date()]
        irradiance = forecast_hours[hour_utc]
        solar_kwh = irradiance / 1000 * pv_capacity_kwp * PV_SYSTEM_EFFICIENCY * SLOT_HOURS
        consumption_kwh = (
            daily_profile["hourly_estimate"]["hourly_kwh"][hour_local.hour]
            * daily_profile["scale"]
            * SLOT_HOURS
        )
        slots.append(
            {
                "start": slot_start,
                "hours": SLOT_HOURS,
                "price": import_price(hour_local, energy_aid),
                "solar_kwh": solar_kwh,
                "consumption_kwh": consumption_kwh,
            }
        )
        slot_start += timedelta(minutes=15)
    
    if not slots:
        raise ForecastError("No forecast hours found for the planning horizon.")
    
    terminal_value = _terminal_value_eur_per_kwh(horizon_end_utc, energy_aid)
    result = optimize_schedule(
        slots,
        initial_energy_kwh,
        battery,
        terminal_value,
        export_price_eur_kwh=EXPORT_PRICE_EUR_KWH,
        display_tz=display_timezone,
    )
    
    return {
        "location": forecast.get("location") or "",
        "generated_at": local_now.isoformat(timespec="seconds"),
        "plan_date": plan_date.isoformat(),
        "horizon_start": start_utc.astimezone(display_timezone).isoformat(timespec="minutes"),
        "horizon_end": horizon_end.isoformat(timespec="minutes"),
        "planning_horizon_hours": planning_horizon_hours,
        "initial_soc": soc,
        "battery_model": {
            "capacity_kwh": capacity,
            "min_energy_kwh": battery.min_energy_kwh,
            "max_energy_kwh": battery.max_energy_kwh,
            "charge_efficiency": battery.charge_efficiency,
            "discharge_efficiency": battery.discharge_efficiency,
            "max_grid_kw": battery.max_grid_kw,
            "max_soc": battery.max_soc,
            "wear_eur_per_kwh": battery.wear_eur_per_kwh,
        },
        "energy_aid": energy_aid,
        "terminal_value_eur_per_kwh": round(terminal_value, 4),
        "windows": result["windows"],
        "cost_with_plan_eur": result["cost_with_plan_eur"],
        "cost_self_use_eur": result["cost_self_use_eur"],
        "cash_savings_eur": result["cash_savings_eur"],
        "estimated_savings_eur": result["estimated_savings_eur"],
        "battery_wear_cost_eur": result["battery_wear_cost_eur"],
        "hourly_plan": result["hourly_plan"],
        "slot_plan": result["slot_plan"],
        "consumption_estimate_source": consumption_estimate["source"],
        "consumption_history_days": consumption_estimate["sample_days"],
        "hourly_consumption_profile_source": hourly_consumption_estimate["source"],
        "hourly_consumption_profile_days": hourly_consumption_estimate["sample_days"],
    }


def supports_passive_discharge(model, fields):
    return (
        model == PASSIVE_MODE_COMMAND_MODEL
        and isinstance(fields, (list, tuple, set, frozenset))
        and PASSIVE_DISCHARGE_FIELDS.issubset(fields)
    )


def default_tou_rules():
    return [
        {
            "ruleId": str(index),
            "touChargeBatteryControl": "0",
            "touChargePower": "2000",
            "touChargeStopSoc": "100",
            "touEnable": "0",
            "touEnableWeek": "1,2,3,4,5,6,7",
            "touEndChargeTimestamp": "22:59",
            "touEndTimestamp": "12-31",
            "touId": "0",
            "touModeControl": "0",
            "touStartChargeTimestamp": "01:01",
            "touStartTimestamp": "01-01",
        }
        for index in range(4)
    ]


def build_mode_parameters(
    mode,
    power_w=None,
    today=None,
    start_time=None,
    current_rules=None,
    target_soc=95,
    end_time="23:59",
    end_date=None,
):
    if mode == "self_use":
        return {"mode": "0"}
    if mode == "grid_discharge":
        if isinstance(power_w, bool) or not isinstance(power_w, int) or not 100 <= power_w <= MAX_DISCHARGE_POWER_W:
            raise ValueError(
                f"Discharging power must be between 100 and {MAX_DISCHARGE_POWER_W} W."
            )
        return {
            "mode": "3",
            "passiveManualBup": -power_w,
            "passiveManualBlo": -power_w,
        }
    if mode != "grid_charge":
        raise ValueError("Unsupported inverter mode.")
    if isinstance(power_w, bool) or not isinstance(power_w, int) or not 1 <= power_w <= 2000:
        raise ValueError("Charging power must be between 1 and 2000 W.")
    if isinstance(target_soc, bool) or not isinstance(target_soc, int) or not 31 <= target_soc <= MAXIMUM_BATTERY_SOC:
        raise ValueError(f"Charging target SOC must be between 31 and {MAXIMUM_BATTERY_SOC}%.")
    try:
        current_time = datetime.now(ZoneInfo(TIMEZONE))
    except ZoneInfoNotFoundError:
        raise ValueError(f"Unknown SOFAR_TIMEZONE: {TIMEZONE}") from None
    date = today or current_time.date()
    start_time = start_time or current_time.strftime("%H:%M")
    if not isinstance(current_rules, list) or not current_rules or not all(
        isinstance(rule, dict) for rule in current_rules
    ):
        raise ValueError("Current Time-of-Use rules are required to build a charge plan.")
    try:
        datetime.strptime(start_time, "%H:%M")
        datetime.strptime(end_time, "%H:%M")
    except (TypeError, ValueError):
        raise ValueError("Charging times must use HH:MM format.") from None
    end_date = end_date or date
    if not isinstance(date, date_type) or not isinstance(end_date, date_type):
        raise ValueError("Charging schedule dates are invalid.")
    if end_date < date or end_date - date > timedelta(days=1):
        raise ValueError("Charging schedule can span no more than two consecutive dates.")
    if end_date == date and end_time <= start_time:
        raise ValueError("Charging end time must be after the start time.")
    start_date_text = date.strftime("%m-%d")
    end_date_text = end_date.strftime("%m-%d")
    rules = [dict(rule) for rule in current_rules]
    for rule in rules:
        rule["touEnable"] = "0"
    charge_rule = rules[0]
    charge_rule.update(
        {
            "touEnable": "1",
            "touChargeStopSoc": str(target_soc),
            "touChargePower": str(power_w),
            "touStartChargeTimestamp": start_time,
            "touEndChargeTimestamp": end_time,
            "touStartTimestamp": start_date_text,
            "touEndTimestamp": end_date_text,
            "touEnableWeek": "1,2,3,4,5,6,7",
        }
    )
    return {
        "mode": "1",
        "energyStoreRuleList": rules,
    }


class SofarCloudClient:
    def __init__(self, username, password, plant_id, base_url=BASE_URL, model=INVERTER_MODEL):
        self.username = username
        self.password = password
        self.plant_id = plant_id
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._access_token = None
        self._token_expires_at = 0
        self._command_lock = threading.Lock()
        self.history_store = None

    def _record_mode_event(self, mode, mode_code, result):
        mode_names = {
            "self_use": "Self-use",
            "grid_charge": "Time-of-Use charging",
            "grid_discharge": "Passive discharge",
        }
        changed = result.get("changed", True)
        summary = (
            f"Inverter mode changed to {mode_names.get(mode, mode)}."
            if changed
            else f"Inverter is already in {mode_names.get(mode, mode)}."
        )
        return record_history_event(
            self.history_store,
            "mode_change" if changed else "mode_confirmed",
            summary,
            {
                "mode": mode,
                "mode_code": mode_code,
                "changed": changed,
                "power_w": result.get("power_w"),
                "target_soc": result.get("target_soc"),
                "start_time": result.get("start_time"),
                "end_time": result.get("end_time"),
                "start_date": result.get("start_date"),
                "end_date": result.get("end_date"),
            },
        )

    def _request_json(self, method, path, stage, headers=None, payload=None):
        request_headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "request-from": "web",
            "scene": "eu",
            "timezone": "Europe/Bratislava",
            "user-agent": "SofarOpti local dashboard",
        }
        if headers:
            request_headers.update(headers)

        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                content_type = response.headers.get("Content-Type", "")
                body = response.read()
                try:
                    result = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    response_format = self._classify_response(body, content_type)
                    raise SofarCloudError(
                        "SofarCloud returned a non-JSON response.",
                        stage,
                        http_status=response.status,
                        content_type=content_type,
                        response_format=response_format,
                    ) from None
                if not isinstance(result, dict):
                    raise SofarCloudError(
                        "SofarCloud returned an unexpected JSON structure.",
                        stage,
                        http_status=response.status,
                        content_type=content_type,
                        response_format="json",
                    )
                return result
        except HTTPError as error:
            raise SofarCloudError(
                "SofarCloud rejected the request.",
                stage,
                http_status=error.code,
                content_type=error.headers.get("Content-Type", ""),
            ) from None
        except (URLError, TimeoutError) as error:
            raise SofarCloudError("Could not connect to SofarCloud.", stage) from error

    @staticmethod
    def _classify_response(body, content_type):
        content_type = content_type.lower()
        if "html" in content_type or body.lstrip().lower().startswith(
            (b"<!doctype html", b"<html")
        ):
            return "html"
        if "xml" in content_type or body.lstrip().startswith(b"<?xml"):
            return "xml"
        return "text or binary"

    def _login(self):
        if not self.username or not self.password:
            raise SofarCloudError(
                "Set SOFAR_USERNAME and SOFAR_PASSWORD before requesting live values.",
                "configuration",
            )

        response = self._request_json(
            "POST",
            LOGIN_PATH,
            "login",
            headers={
                "authorization": "Bearer",
                "custom-origin": self.base_url,
                "origin": self.base_url,
                "referer": f"{self.base_url}/login",
                "sign": LOGIN_SIGN,
            },
            payload={
                "accountName": self.username,
                "expireTime": 18000,
                "password": self.password,
            },
        )
        data = response.get("data") or {}
        token = data.get("accessToken")
        if str(response.get("code")) != "0" or not token:
            raise SofarCloudError(
                "SofarCloud login failed.",
                "login",
                api_code=response.get("code"),
            )

        expires_at = data.get("expiresTime")
        try:
            expires_at = int(expires_at) / 1000
        except (TypeError, ValueError):
            expires_at = time.time() + 900
        self._access_token = token
        self._token_expires_at = max(time.time(), expires_at - 60)
        return token

    def get_status(self):
        if not self.plant_id:
            raise SofarCloudError(
                "Set SOFAR_PLANT_ID before requesting live values.",
                "configuration",
            )
        token = self._access_token
        if not token or time.time() >= self._token_expires_at:
            token = self._login()

        query = urlencode({"stationId": self.plant_id})
        response = self._request_json(
            "POST",
            f"{STATION_DETAIL_PATH}?{query}",
            "station detail",
            headers={
                "authorization": token,
                "app-version": "2.3.6",
                "custom-origin": "sofar",
                "custom-device-type": "1",
                "request-from": "app",
                "bundlefrom": "2",
                "appfrom": "6",
                "accept-language": "en",
                "user-agent": "okhttp/4.9.9",
            },
            payload={},
        )
        if str(response.get("code")) != "0" or not response.get("data"):
            raise SofarCloudError(
                "SofarCloud could not return details for this plant.",
                "station detail",
                api_code=response.get("code"),
            )
        return parse_station_status(response["data"], self.model)

    def get_consumption_history(self, start_time, end_time):
        if not self.plant_id:
            raise SofarCloudError(
                "Set SOFAR_PLANT_ID before requesting historical values.",
                "configuration",
            )
        if (
            not isinstance(start_time, datetime)
            or not isinstance(end_time, datetime)
            or start_time.tzinfo is None
            or end_time.tzinfo is None
            or end_time <= start_time
            or end_time - start_time > timedelta(days=365)
        ):
            raise ValueError("Historical data range must be within 365 days.")
        payload = {
            "queryType": 1,
            "stationId": self.plant_id,
            "paramList": ["usePower"],
            "startTime": int(start_time.timestamp()),
            "endTime": int(end_time.timestamp()),
        }
        response = self._request_json(
            "POST",
            STATION_HISTORY_PATH,
            "station history",
            headers={
                "authorization": self._access_token
                if self._access_token
                and time.time() < self._token_expires_at
                else self._login(),
                "custom-origin": self.base_url,
                "origin": self.base_url,
                "referer": f"{self.base_url}/plants/plantDetail/{self.plant_id}",
                "request-from": "web",
                "accept-language": "en",
                "sign": sign_sofar_request(payload),
            },
            payload=payload,
        )
        if str(response.get("code")) != "0":
            raise SofarCloudError(
                "SofarCloud could not return historical consumption data.",
                "station history",
                api_code=response.get("code"),
            )
        data = response.get("data")
        curves = data.get("curves") if isinstance(data, dict) else None
        if not isinstance(curves, list):
            raise SofarCloudError(
                "SofarCloud returned an unexpected historical-data structure.",
                "station history",
                api_code=response.get("code"),
            )
        curve = next(
            (
                item
                for item in curves
                if isinstance(item, dict) and item.get("storageName") == "usePower"
            ),
            None,
        )
        if curve is None:
            return []
        points = curve.get("detailList")
        if not isinstance(points, list):
            raise SofarCloudError(
                "SofarCloud returned an invalid historical consumption series.",
                "station history",
                api_code=response.get("code"),
            )
        unit = str(curve.get("unit") or "").strip().lower()
        if unit not in {"kw", "w"}:
            raise SofarCloudError(
                "SofarCloud returned an unsupported historical power unit.",
                "station history",
                api_code=response.get("code"),
            )
        parsed_points = []
        for point in points:
            if not isinstance(point, dict):
                continue
            try:
                timestamp = int(point["collectionTime"])
                power_kw = float(point["value"])
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(power_kw) or power_kw < 0:
                continue
            if unit == "w":
                power_kw /= 1000
            parsed_points.append(
                (
                    datetime.fromtimestamp(timestamp, timezone.utc).astimezone(
                        ZoneInfo(TIMEZONE)
                    ),
                    power_kw,
                )
            )
        return parsed_points

    def _web_headers(self, payload):
        token = self._access_token
        if not token or time.time() >= self._token_expires_at:
            token = self._login()
        return {
            "authorization": f"Bearer {token}",
            "custom-origin": self.base_url,
            "origin": self.base_url,
            "referer": f"{self.base_url}/device/deviceDetail/{INVERTER_SN}",
            "request-from": "web",
            "accept-language": "en-US,en;q=0.9",
            "user-agent": "Mozilla/5.0",
            "sign": sign_sofar_request(payload),
        }

    def get_energy_storage_schema(self):
        payload = {"deviceModel": COMMAND_MODEL, "sn": INVERTER_SN}
        response = self._request_json(
            "POST",
            COMMAND_SCHEMA_PATH,
            "command schema",
            headers=self._web_headers(payload),
            payload=payload,
        )
        if str(response.get("code")) != "0":
            raise SofarCloudError(
                "SofarCloud could not return the inverter command schema.",
                "command schema",
                api_code=response.get("code"),
            )
        categories = response.get("data")
        if not isinstance(categories, list):
            raise SofarCloudError(
                "SofarCloud returned an unexpected inverter command schema.",
                "command schema",
                response_format="json",
            )
        for category in categories:
            if not isinstance(category, dict):
                continue
            commands = category.get("cmdList")
            if not isinstance(commands, list):
                continue
            for command in commands:
                if not isinstance(command, dict):
                    continue
                if command.get("groupName") == "he_energyStorageModeSetting":
                    fields = command.get("itemList", [])
                    if not isinstance(fields, list) or not all(
                        isinstance(field, dict) for field in fields
                    ):
                        raise SofarCloudError(
                            "SofarCloud returned malformed inverter mode fields.",
                            "command schema",
                            response_format="json",
                        )
                    mode_field = next(
                        (field for field in fields if field.get("fieldName") == "mode"),
                        {},
                    )
                    return {
                        "model": COMMAND_MODEL,
                        "serial_number": INVERTER_SN,
                        "battery_capacity_kwh": BATTERY_CAPACITY_KWH,
                        "max_discharge_power_w": MAX_DISCHARGE_POWER_W,
                        "discharge_target_soc": DISCHARGE_TARGET_SOC,
                        "modes": [
                            {
                                "value": option.get("value"),
                                "label": option.get("key"),
                            }
                            for option in mode_field.get("optionItems", [])
                        ],
                        "fields": [field.get("fieldName") for field in fields],
                        "passive_discharge_supported": supports_passive_discharge(
                            COMMAND_MODEL,
                            [field.get("fieldName") for field in fields],
                        ),
                        "passive_discharge_enabled": ENABLE_PASSIVE_DISCHARGE
                        and supports_passive_discharge(
                            COMMAND_MODEL,
                            [field.get("fieldName") for field in fields],
                        ),
                    }
        raise SofarCloudError(
            "SofarCloud did not advertise an energy-storage mode command.",
            "command schema",
            response_format="json",
        )

    def read_energy_storage_mode(self):
        request_id = uuid.uuid4().hex
        response_event = threading.Event()
        responses = []
        socket_client = socketio.Client(reconnection=False, logger=False, engineio_logger=False)

        def on_command_response(message):
            if isinstance(message, str):
                try:
                    message = json.loads(message)
                except json.JSONDecodeError:
                    return
            if isinstance(message, dict) and message.get("requestId") == request_id:
                responses.append(message)
                response_event.set()

        socket_client.on("commandResponse", on_command_response)
        with self._command_lock:
            try:
                token = self._access_token
                if not token or time.time() >= self._token_expires_at:
                    token = self._login()
                socket_client.connect(
                    f"{SOCKET_URL}?token={quote(token, safe='')}",
                    transports=["websocket"],
                    wait_timeout=REQUEST_TIMEOUT,
                )
                payload = {
                    "requestId": request_id,
                    "nodeId": INVERTER_SN,
                    "functionName": "he_energyStorageModeRead",
                    "groupName": "he_energyStorageModeRead",
                    "params": {"mode": ""},
                }
                command_response = self._execute_command(payload, response_event, responses)
                params = command_response.get("params")
                if not isinstance(params, dict):
                    raise SofarCloudError(
                        "The inverter returned an unexpected mode response.",
                        "mode read",
                        response_format="json",
                    )
                return params
            except socketio.exceptions.ConnectionError:
                raise SofarCloudError(
                    "Could not connect to SofarCloud's command response service.",
                    "mode read",
                ) from None
            finally:
                if socket_client.connected:
                    socket_client.disconnect()

    def _execute_command(self, payload, response_event, responses):
        result = self._request_json(
            "POST",
            DEVICE_COMMAND_PATH,
            "mode command",
            headers=self._web_headers(payload),
            payload=payload,
        )
        if result.get("result") != 1:
            raise SofarCloudError(
                "SofarCloud rejected the inverter mode command.",
                "mode command",
                api_code=result.get("code") or result.get("result"),
            )
        if not response_event.wait(COMMAND_TIMEOUT):
            raise SofarCloudError(
                "SofarCloud did not confirm the inverter mode command.",
                "mode command",
                api_code="timeout",
            )
        command_response = responses[0]
        if str(command_response.get("result")) != "1":
            raise SofarCloudError(
                "The inverter rejected the mode command.",
                "mode command",
                api_code=command_response.get("code") or command_response.get("result"),
            )
        return command_response

    def set_energy_storage_mode(
        self,
        mode,
        power_w=None,
        confirmed=False,
        target_soc=MAXIMUM_BATTERY_SOC,
        charge_start_time=None,
        charge_end_time="23:59",
        charge_start_date=None,
        charge_end_date=None,
    ):
        if not confirmed:
            raise SofarCloudError(
                "Confirm the mode change before sending it to the inverter.",
                "mode control",
            )
        schema = self.get_energy_storage_schema()
        supported_modes = {str(option["value"]) for option in schema["modes"]}
        mode_code = {
            "self_use": "0",
            "grid_charge": "1",
            "grid_discharge": "3",
        }.get(mode, "")
        passive_discharge_supported = supports_passive_discharge(
            schema.get("model", COMMAND_MODEL),
            schema.get("fields", []),
        )
        if mode == "grid_discharge":
            if not ENABLE_PASSIVE_DISCHARGE:
                raise SofarCloudError(
                    "Passive discharge is disabled. Enable SOFAR_ENABLE_PASSIVE_DISCHARGE only after reviewing the export and SOC-limit risks.",
                    "mode control",
                )
            if not passive_discharge_supported:
                raise SofarCloudError(
                    "Passive discharge is unavailable for this inverter model or command schema.",
                    "mode control",
                )
        elif mode_code not in supported_modes:
            raise SofarCloudError(
                "The live inverter schema does not support the requested mode.",
                "mode control",
            )
        discharge_plan = None
        if mode == "grid_charge":
            if (
                isinstance(target_soc, bool)
                or not isinstance(target_soc, int)
                or not 31 <= target_soc <= MAXIMUM_BATTERY_SOC
            ):
                raise SofarCloudError(
                    f"Charging target SOC must be between 31 and {MAXIMUM_BATTERY_SOC}%.",
                    "mode control",
                )
            required_fields = {
                "touEnable",
                "touChargeStopSoc",
                "touChargePower",
                "touStartChargeTimestamp",
                "touEndChargeTimestamp",
                "touStartTimestamp",
                "touEndTimestamp",
                "touEnableWeek",
            }
            if not required_fields.issubset(schema["fields"]):
                raise SofarCloudError(
                    "The live inverter schema is missing required charge schedule fields.",
                    "mode control",
                )
            status = self.get_status()
            state_of_charge = status["metrics"]["battery_soc"]
            try:
                state_of_charge = float(state_of_charge)
            except (TypeError, ValueError):
                raise SofarCloudError(
                    "Current battery SOC is unavailable; the charge request was not sent.",
                    "mode control",
                ) from None
            if not math.isfinite(state_of_charge) or not 0 <= state_of_charge <= 100:
                raise SofarCloudError(
                    "Current battery SOC is invalid; the charge request was not sent.",
                    "mode control",
                )
            try:
                local_now = datetime.now(ZoneInfo(TIMEZONE))
                start_date = (
                    date_type.fromisoformat(charge_start_date)
                    if isinstance(charge_start_date, str)
                    else charge_start_date or local_now.date()
                )
                end_date = (
                    date_type.fromisoformat(charge_end_date)
                    if isinstance(charge_end_date, str)
                    else charge_end_date or start_date
                )
            except (ValueError, ZoneInfoNotFoundError):
                raise SofarCloudError(
                    "Charging schedule date or timezone is invalid; the request was not sent.",
                    "mode control",
                ) from None
            start_time = charge_start_time or local_now.strftime("%H:%M")
            try:
                scheduled_at = datetime.combine(
                    start_date,
                    datetime.strptime(start_time, "%H:%M").time(),
                    tzinfo=ZoneInfo(TIMEZONE),
                )
            except (TypeError, ValueError, ZoneInfoNotFoundError):
                raise SofarCloudError(
                    "Charging schedule start time is invalid; the request was not sent.",
                    "mode control",
                ) from None
            future_schedule = scheduled_at > local_now
            if (
                charge_start_time is not None or charge_start_date is not None
            ) and not future_schedule:
                raise SofarCloudError(
                    "The optimization schedule has expired; calculate a fresh plan before applying it.",
                    "mode control",
                )
            if state_of_charge >= target_soc and not future_schedule:
                raise SofarCloudError(
                    f"Battery SOC is already at or above the {target_soc}% charge target.",
                    "mode control",
                )
        elif mode == "grid_discharge":
            status = self.get_status()
            state_of_charge = status["metrics"]["battery_soc"]
            try:
                discharge_plan = estimate_discharge(state_of_charge, power_w)
            except ValueError as error:
                raise SofarCloudError(str(error), "mode control") from None
            try:
                start_at = datetime.now(ZoneInfo(TIMEZONE)).replace(microsecond=0)
            except ZoneInfoNotFoundError:
                raise SofarCloudError(
                    f"Unknown SOFAR_TIMEZONE: {TIMEZONE}",
                    "configuration",
                ) from None
            deadline = start_at + timedelta(seconds=discharge_plan["duration_seconds"])
            discharge_plan.update(
                {
                    "start_soc": float(state_of_charge),
                    "start_at": start_at,
                    "deadline": deadline,
                }
            )

        current_mode_state = self.read_energy_storage_mode()
        current_mode = str(current_mode_state.get("mode", ""))
        if not current_mode:
            raise SofarCloudError(
                "Current inverter mode could not be verified; the mode request was not sent.",
                "mode control",
            )
        if mode == "grid_charge":
            current_rules = current_mode_state.get("energyStoreRuleList")
            if not isinstance(current_rules, list) or not current_rules or not all(
                isinstance(rule, dict) for rule in current_rules
            ):
                if current_mode == "0":
                    current_rules = default_tou_rules()
                else:
                    raise SofarCloudError(
                        "The inverter did not return its current Time-of-Use rules; the charge request was not sent.",
                        "mode control",
                    )
            try:
                params = build_mode_parameters(
                    mode,
                    power_w,
                    today=start_date,
                    start_time=start_time,
                    current_rules=current_rules,
                    target_soc=target_soc,
                    end_time=charge_end_time,
                    end_date=end_date,
                )
            except ValueError as error:
                raise SofarCloudError(str(error), "mode control") from None
        else:
            try:
                params = build_mode_parameters(mode, power_w)
            except ValueError as error:
                raise SofarCloudError(str(error), "mode control") from None
        if mode == "self_use" and current_mode == mode_code:
            result = {"mode": mode_code, "verified": True, "changed": False}
            history_warning = self._record_mode_event(mode, mode_code, result)
            if history_warning:
                result["history_warning"] = history_warning
            return result

        request_id = uuid.uuid4().hex
        response_event = threading.Event()
        responses = []
        socket_client = socketio.Client(reconnection=False, logger=False, engineio_logger=False)

        def on_command_response(message):
            if isinstance(message, str):
                try:
                    message = json.loads(message)
                except json.JSONDecodeError:
                    return
            if isinstance(message, dict) and message.get("requestId") == request_id:
                responses.append(message)
                response_event.set()

        socket_client.on("commandResponse", on_command_response)
        payload = {
            "requestId": request_id,
            "nodeId": INVERTER_SN,
            "functionName": "he_energyStorageModeSetting",
            "groupName": "he_energyStorageModeSetting",
            "params": params,
            "originValue": current_mode_state,
            "updateValue": {"mode": mode_code, **params},
        }
        with self._command_lock:
            try:
                token = self._access_token
                if not token or time.time() >= self._token_expires_at:
                    token = self._login()
                socket_client.connect(
                    f"{SOCKET_URL}?token={quote(token, safe='')}",
                    transports=["websocket"],
                    wait_timeout=REQUEST_TIMEOUT,
                )
                self._execute_command(payload, response_event, responses)
            except socketio.exceptions.ConnectionError:
                raise SofarCloudError(
                    "Could not connect to SofarCloud's command response service.",
                    "mode control",
                ) from None
            finally:
                if socket_client.connected:
                    socket_client.disconnect()
        verified_state = self.read_energy_storage_mode()
        verified_mode = str(verified_state.get("mode", ""))
        if verified_mode != mode_code:
            raise SofarCloudError(
                "SofarCloud acknowledged the command, but the live mode did not match.",
                "mode verification",
                api_code=verified_mode,
            )
        if mode == "grid_charge" and not self._charge_schedule_matches(
            verified_state.get("energyStoreRuleList"),
            params["energyStoreRuleList"],
        ):
            raise SofarCloudError(
                "SofarCloud acknowledged Time-of-Use mode, but the requested charging schedule was not applied.",
                "mode verification",
                api_code="schedule_mismatch",
            )
        if mode == "grid_discharge" and not self._passive_discharge_matches(
            verified_state,
            params,
        ):
            raise SofarCloudError(
                "SofarCloud acknowledged Passive mode, but the requested discharge power was not applied.",
                "mode verification",
                api_code="power_mismatch",
            )
        result = {
            "mode": mode_code,
            "power_w": power_w if mode == "grid_charge" else None,
            "target_soc": target_soc if mode == "grid_charge" else None,
            "start_time": params["energyStoreRuleList"][0]["touStartChargeTimestamp"]
            if mode == "grid_charge"
            else None,
            "end_time": params["energyStoreRuleList"][0]["touEndChargeTimestamp"]
            if mode == "grid_charge"
            else None,
            "start_date": params["energyStoreRuleList"][0]["touStartTimestamp"]
            if mode == "grid_charge"
            else None,
            "end_date": params["energyStoreRuleList"][0]["touEndTimestamp"]
            if mode == "grid_charge"
            else None,
            "verified": True,
            "changed": True,
        }
        if mode == "grid_discharge":
            result.update(
                {
                    "power_w": power_w,
                    "target_soc": DISCHARGE_TARGET_SOC,
                    "start_soc": discharge_plan["start_soc"],
                    "estimated_energy_kwh": round(discharge_plan["energy_kwh"], 3),
                    "estimated_duration_minutes": round(
                        discharge_plan["duration_minutes"], 1
                    ),
                    "deadline": discharge_plan["deadline"].isoformat(),
                    "control_mode": "passive",
                }
            )
        history_warning = self._record_mode_event(mode, mode_code, result)
        if history_warning:
            result["history_warning"] = history_warning
        return result

    @staticmethod
    def _charge_schedule_matches(actual_rules, expected_rules):
        if not isinstance(actual_rules, list) or len(actual_rules) != len(expected_rules):
            return False
        fields = (
            "touEnable",
            "touChargeStopSoc",
            "touChargePower",
            "touStartChargeTimestamp",
            "touEndChargeTimestamp",
            "touStartTimestamp",
            "touEndTimestamp",
            "touEnableWeek",
        )
        for index, (actual, expected) in enumerate(zip(actual_rules, expected_rules)):
            if not isinstance(actual, dict):
                return False
            fields_to_check = fields if index == 0 else ("touEnable",)
            if any(
                str(actual.get(field, "")) != str(expected.get(field, ""))
                for field in fields_to_check
            ):
                return False
        return True

    @staticmethod
    def _passive_discharge_matches(actual, expected):
        if not isinstance(actual, dict):
            return False
        for field in PASSIVE_DISCHARGE_FIELDS:
            try:
                actual_power = float(actual[field])
                expected_power = float(expected[field])
            except (KeyError, TypeError, ValueError):
                return False
            if not math.isfinite(actual_power) or not math.isclose(
                actual_power,
                expected_power,
            ):
                return False
        return True


def retry_set_energy_storage_mode(
    client,
    mode,
    power_w=None,
    confirmed=False,
    target_soc=MAXIMUM_BATTERY_SOC,
    charge_start_time=None,
    charge_end_time="23:59",
    charge_start_date=None,
    charge_end_date=None,
    max_retries=5,
    retry_interval_sec=120,
):
    """Set energy storage mode with retry logic (5 attempts with 2-min intervals)."""
    last_error = None
    
    for attempt in range(1, max_retries + 1):
        try:
            result = client.set_energy_storage_mode(
                mode,
                power_w,
                confirmed=confirmed,
                target_soc=target_soc,
                charge_start_time=charge_start_time,
                charge_end_time=charge_end_time,
                charge_start_date=charge_start_date,
                charge_end_date=charge_end_date,
            )
            return result
        except (SofarCloudError, ValueError) as error:
            last_error = error
            if attempt < max_retries:
                LOGGER.warning(
                    "Mode change to '%s' failed (attempt %d/%d): %s. Retrying in %d seconds...",
                    mode,
                    attempt,
                    max_retries,
                    error,
                    retry_interval_sec,
                )
                time.sleep(retry_interval_sec)
            else:
                LOGGER.error(
                    "Mode change to '%s' failed after %d attempts: %s",
                    mode,
                    max_retries,
                    error,
                )
                raise error


class DischargeWatchdog:
    def __init__(self):
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._state = {"active": False, "status": "idle"}

    def get_state(self):
        with self._lock:
            return dict(self._state)

    def start(self, client, plan):
        with self._lock:
            if self._state["active"]:
                raise RuntimeError("A discharge monitor is already active.")
            self._stop_event = threading.Event()
            self._state = {
                "active": True,
                "status": "monitoring",
                "target_soc": DISCHARGE_TARGET_SOC,
                "power_w": plan["power_w"],
                "start_soc": plan["start_soc"],
                "last_soc": plan["start_soc"],
                "estimated_energy_kwh": plan["estimated_energy_kwh"],
                "estimated_duration_minutes": plan["estimated_duration_minutes"],
                "deadline": plan["deadline"],
                "message": "Server-side SOC monitoring is active.",
            }
            thread = threading.Thread(
                target=self._monitor,
                args=(client, plan["deadline"], self._stop_event),
                name="sofar-discharge-watchdog",
                daemon=True,
            )
            try:
                thread.start()
            except RuntimeError:
                self._state = {
                    "active": False,
                    "status": "error",
                    "message": "The discharge monitor thread could not be started.",
                }
                raise

    def stop(self, reason="operator_changed_mode"):
        self._stop_event.set()
        with self._lock:
            self._state.update(
                {
                    "active": False,
                    "status": "stopped",
                    "stop_reason": reason,
                    "message": "Discharge monitoring stopped after an inverter mode change.",
                }
            )

    def _monitor(self, client, deadline, stop_event):
        while not stop_event.is_set():
            try:
                remaining = (
                    datetime.fromisoformat(deadline)
                    - datetime.now(ZoneInfo(TIMEZONE))
                ).total_seconds()
            except (ValueError, ZoneInfoNotFoundError) as error:
                LOGGER.error("Discharge monitor could not evaluate its deadline: %s", error)
                if self._request_self_use(client, "invalid_deadline", stop_event):
                    return
                stop_event.wait(DISCHARGE_MONITOR_INTERVAL)
                continue

            reason = None
            soc = None
            if remaining <= 0:
                reason = "estimated_time_limit"
            else:
                try:
                    status = client.get_status()
                    soc = float(status["metrics"]["battery_soc"])
                    if not math.isfinite(soc) or not 0 <= soc <= 100:
                        raise ValueError("SOC is outside the valid range.")
                except (SofarCloudError, KeyError, TypeError, ValueError) as error:
                    LOGGER.error("Discharge SOC read failed; requesting Self-use: %s", error)
                    reason = "telemetry_unavailable"
                if soc is not None:
                    with self._lock:
                        self._state.update(
                            {
                                "last_soc": soc,
                                "status": "monitoring",
                                "message": "Server-side SOC monitoring is active.",
                            }
                        )
                    if soc <= DISCHARGE_TARGET_SOC:
                        reason = "target_soc_reached"

            if reason and self._request_self_use(client, reason, stop_event):
                return
            stop_event.wait(
                min(DISCHARGE_MONITOR_INTERVAL, max(1, remaining))
                if remaining > 0
                else DISCHARGE_MONITOR_INTERVAL
            )

    def _request_self_use(self, client, reason, stop_event):
        with self._lock:
            self._state.update(
                {
                    "status": "returning_to_self_use",
                    "message": f"Returning to Self-use ({reason}).",
                }
            )
        
        max_retries = 5
        retry_interval_sec = 120
        
        for attempt in range(1, max_retries + 1):
            try:
                with MODE_CONTROL_LOCK:
                    if stop_event.is_set():
                        return True
                    client.set_energy_storage_mode("self_use", confirmed=True)
                # Success
                self.stop(reason)
                LOGGER.info("Discharge stopped; inverter returned to Self-use (%s).", reason)
                return True
            except SofarCloudError as error:
                if attempt < max_retries:
                    LOGGER.warning(
                        "Discharge return to Self-use failed (attempt %d/%d): %s. Retrying in %d seconds...",
                        attempt,
                        max_retries,
                        error,
                        retry_interval_sec,
                    )
                    time.sleep(retry_interval_sec)
                else:
                    LOGGER.error(
                        "Could not return the inverter to Self-use after %d attempts for %s (stage=%s, API code=%s)",
                        max_retries,
                        reason,
                        error.stage,
                        error.api_code,
                    )
                    return False
            except ValueError as error:
                LOGGER.error("Discharge return to Self-use validation error: %s", error)
                return False


class ScheduledChargeController:
    ACTIVE_STATUSES = {"pending", "starting", "charging", "returning_to_self_use"}

    def __init__(self, client, state_path, mode_lock, discharge_active):
        self.client = client
        self.state_path = Path(state_path)
        self.mode_lock = mode_lock
        self.discharge_active = discharge_active
        self.history_store = None
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread = None
        self._state = {
            "status": "idle",
            "plan": None,
            "error": None,
        }
        self._load_state()

    def _load_state(self):
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("The saved scheduled charging plan cannot be read.") from error
        if (
            not isinstance(state, dict)
            or state.get("status") not in self.ACTIVE_STATUSES | {
                "idle", "completed", "failed", "expired", "cancelled"
            }
            or (state.get("status") in self.ACTIVE_STATUSES and not isinstance(state.get("plan"), dict))
        ):
            raise RuntimeError("The saved scheduled charging plan is invalid.")
        self._state = state

    def _save_state(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.state_path.parent,
                prefix=f"{self.state_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as state_file:
                temporary_path = Path(state_file.name)
                json.dump(self._state, state_file, ensure_ascii=False)
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary_path, self.state_path)
        except OSError:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

    def get_state(self):
        with self._lock:
            return json.loads(json.dumps(self._state))

    def schedule(self, plan, now=None):
        if not isinstance(plan, dict) or plan.get("recommended") is not True:
            raise ValueError("Only a recommended charging plan can be scheduled.")
        try:
            local_now = now or datetime.now(ZoneInfo(TIMEZONE))
            if local_now.tzinfo is None:
                local_now = local_now.replace(tzinfo=ZoneInfo(TIMEZONE))
            start = datetime.fromisoformat(plan["charge_start"])
            end = datetime.fromisoformat(plan["charge_end"])
            generated = datetime.fromisoformat(plan["generated_at"])
            target_soc = plan["recommended_target_soc"]
            charge_kwh = float(plan["recommended_grid_charge_kwh"])
        except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
            raise ValueError("The charging plan contains invalid dates or values.") from None
        if any(value.tzinfo is None for value in (start, end, generated)):
            raise ValueError("The charging plan must include timezone-aware dates.")
        if (
            target_soc is None
            or isinstance(target_soc, bool)
            or not isinstance(target_soc, int)
            or not 31 <= target_soc <= MAXIMUM_BATTERY_SOC
            or not math.isfinite(charge_kwh)
            or not 0 < charge_kwh <= 18
            or end <= start
            or (end - start).total_seconds() > 9 * 60 * 60
            or end.astimezone(ZoneInfo(TIMEZONE)).strftime("%H:%M") != "06:00"
            or start <= local_now
            or (local_now - generated).total_seconds() > 10 * 60
            or generated - local_now > timedelta(minutes=1)
        ):
            raise ValueError("The charging plan is expired or has invalid charging limits.")
        with self._lock:
            if self._state["status"] in self.ACTIVE_STATUSES:
                raise RuntimeError("A scheduled charging plan is already active.")
            previous_state = self._state
            self._state = {
                "status": "pending",
                "plan": {
                    key: plan[key]
                    for key in (
                        "generated_at",
                        "charge_start",
                        "charge_end",
                        "recommended_target_soc",
                        "recommended_grid_charge_kwh",
                        "estimated_savings_eur",
                        "plan_date",
                        "recommended",
                    )
                },
                "error": None,
                "created_at": local_now.isoformat(timespec="seconds"),
            }
            try:
                self._save_state()
            except OSError:
                self._state = previous_state
                raise
            result = self.get_state()
            plan_summary = self._plan_event_details(result["plan"])
            warning = record_history_event(
                self.history_store,
                "plan_scheduled",
                f"Charging plan scheduled for {result['plan']['plan_date']}.",
                plan_summary,
            )
            if warning:
                result["history_warning"] = warning
            return result

    @staticmethod
    def _plan_event_details(plan):
        return {
            "plan_date": plan.get("plan_date"),
            "charge_start": plan.get("charge_start"),
            "charge_end": plan.get("charge_end"),
            "recommended_grid_charge_kwh": plan.get(
                "recommended_grid_charge_kwh"
            ),
            "recommended_target_soc": plan.get("recommended_target_soc"),
            "estimated_savings_eur": plan.get("estimated_savings_eur"),
            "savings_are_estimated": True,
        }

    def _record_plan_event(self, event_type, summary):
        plan = self._state.get("plan") or {}
        warning = record_history_event(
            self.history_store,
            event_type,
            summary,
            self._plan_event_details(plan),
        )
        if warning:
            self._state["history_warning"] = warning

    def _record_plan_results(self, plan_summary):
        """Record actual results of a completed plan for comparison with estimates."""
        if self.history_store is None or not plan_summary.get("plan_date"):
            return
        
        plan_date_str = plan_summary.get("plan_date")
        try:
            plan_date = date_type.fromisoformat(plan_date_str)
        except (ValueError, TypeError):
            return
        
        next_date = plan_date + timedelta(days=1)
        
        start_ts = datetime.fromisoformat(plan_summary.get("charge_start", "")).timestamp() if plan_summary.get("charge_start") else None
        end_ts = datetime.fromisoformat(plan_summary.get("charge_end", "")).timestamp() if plan_summary.get("charge_end") else None
        
        if start_ts is None or end_ts is None:
            return
        
        import sqlite3
        try:
            with closing(self.history_store._connect()) as conn:
                grid_import_rows = conn.execute(
                    """
                    SELECT SUM(observed_grid_import_delta_kwh) as total_import,
                           SUM(observed_grid_import_cost_with_aid_eur) as total_cost_with_aid,
                           SUM(observed_grid_import_cost_without_aid_eur) as total_cost_without_aid
                    FROM telemetry
                    WHERE datetime(sampled_at) >= datetime(?, 'unixepoch')
                      AND datetime(sampled_at) < datetime(?, 'unixepoch')
                    """,
                    (start_ts, end_ts)
                ).fetchone()
        except Exception:
            grid_import_rows = None
        
        if grid_import_rows:
            actual_grid_import = grid_import_rows[0] or 0
            actual_cost_with_aid = grid_import_rows[1] or 0
            actual_cost_without_aid = grid_import_rows[2] or 0
            
            results_details = {
                "plan_date": plan_summary.get("plan_date"),
                "charge_start": plan_summary.get("charge_start"),
                "charge_end": plan_summary.get("charge_end"),
                "estimated_savings_eur": plan_summary.get("estimated_savings_eur"),
                "actual_grid_import_kwh": round(actual_grid_import, 2),
                "actual_cost_with_aid_eur": round(actual_cost_with_aid, 2),
                "actual_cost_without_aid_eur": round(actual_cost_without_aid, 2),
            }
            
            record_history_event(
                self.history_store,
                "plan_results",
                f"Plan results for {plan_date_str}: actual grid import {round(actual_grid_import, 2)} kWh, cost €{round(actual_cost_with_aid, 2)} (with aid) / €{round(actual_cost_without_aid, 2)}.",
                results_details,
            )

    def run_due(self, now=None):
        current = now or datetime.now(ZoneInfo(TIMEZONE))
        if current.tzinfo is None:
            current = current.replace(tzinfo=ZoneInfo(TIMEZONE))
        current = current.astimezone(ZoneInfo(TIMEZONE))
        start_plan = None
        should_return = False
        with self._lock:
            status = self._state["status"]
            if status in {"pending", "starting"}:
                plan = self._state["plan"]
                start = datetime.fromisoformat(plan["charge_start"]).astimezone(
                    ZoneInfo(TIMEZONE)
                )
                end = datetime.fromisoformat(plan["charge_end"]).astimezone(
                    ZoneInfo(TIMEZONE)
                )
                if current >= end:
                    if status == "starting":
                        self._state.update(
                            {
                                "status": "returning_to_self_use",
                                "error": "The charging window passed while recovering an interrupted start.",
                                "return_reason": "start_failed",
                            }
                        )
                        should_return = True
                    else:
                        self._state.update(
                            {
                                "status": "expired",
                                "error": "The scheduled charge window passed before it started.",
                            }
                        )
                    self._save_state()
                    if status == "starting":
                        self._record_plan_event(
                            "plan_recovery",
                            "Interrupted charging start; returning the inverter to Self-use.",
                        )
                    else:
                        self._record_plan_event(
                            "plan_expired",
                            "Charging plan expired before it started.",
                        )
                elif current < start:
                    return
                else:
                    start_plan = plan
                if start_plan is not None and status == "pending":
                    self._state.update({"status": "starting"})
                    try:
                        self._save_state()
                    except OSError:
                        self._state.update({"status": "pending"})
                        raise
            if status == "charging":
                plan_end = datetime.fromisoformat(
                    self._state["plan"]["charge_end"]
                ).astimezone(ZoneInfo(TIMEZONE))
                if current >= plan_end:
                    self._state.update(
                        {
                            "status": "returning_to_self_use",
                            "return_reason": "scheduled_end",
                        }
                    )
                    self._save_state()
                    should_return = True
            if status == "returning_to_self_use":
                retry_after = self._state.get("retry_after")
                should_return = (
                    not retry_after or current >= datetime.fromisoformat(retry_after)
                )

        if start_plan is not None:
            if self.discharge_active():
                with self._lock:
                    if self._state["status"] == "starting":
                        if status == "starting":
                            self._state.update(
                                {
                                    "status": "returning_to_self_use",
                                    "error": "The inverter was restored to Self-use because discharge monitoring is active during recovery.",
                                    "return_reason": "start_failed",
                                }
                            )
                            should_return = True
                        else:
                            self._state.update(
                                {
                                    "status": "failed",
                                    "error": "Charging was not started because discharge monitoring is active.",
                                }
                            )
                        self._save_state()
                        if status == "starting":
                            self._record_plan_event(
                                "plan_failed",
                                "Scheduled charging was not resumed because discharge monitoring is active.",
                            )
                        else:
                            self._record_plan_event(
                                "plan_failed",
                                "Scheduled charging was blocked by active discharge monitoring.",
                            )
                if should_return:
                    self._try_return_to_self_use(current)
                return
            try:
                max_retries = 5
                retry_interval_sec = 120  # 2 minutes
                last_error = None
                
                for attempt in range(1, max_retries + 1):
                    try:
                        with self.mode_lock:
                            with self._lock:
                                if self._state["status"] != "starting":
                                    return
                            self.client.set_energy_storage_mode(
                                "grid_charge",
                                2000,
                                confirmed=True,
                                target_soc=start_plan["recommended_target_soc"],
                            )
                        # Success - break out of retry loop
                        break
                    except (SofarCloudError, ValueError) as error:
                        last_error = error
                        if attempt < max_retries:
                            LOGGER.warning(
                                "Scheduled charging failed to start (attempt %d/%d): %s. Retrying in %d seconds...",
                                attempt,
                                max_retries,
                                error,
                                retry_interval_sec,
                            )
                            time.sleep(retry_interval_sec)
                        else:
                            raise error
            except (SofarCloudError, ValueError) as error:
                LOGGER.error("Scheduled charging could not start after %d attempts: %s", max_retries, error)
                with self._lock:
                    if self._state["status"] != "starting":
                        return
                    self._state.update(
                        {
                            "status": "returning_to_self_use",
                            "error": str(error),
                            "return_reason": "start_failed",
                        }
                    )
                    self._save_state()
                    self._record_plan_event(
                        "plan_failed",
                        "Scheduled charging command failed after multiple retry attempts; Self-use recovery was requested.",
                    )
                self._try_return_to_self_use(current)
                return
            with self._lock:
                if self._state["status"] == "starting":
                    self._state.update(
                        {
                            "status": "charging",
                            "started_at": current.isoformat(timespec="seconds"),
                            "error": None,
                        }
                    )
                    self._save_state()
                    self._record_plan_event(
                        "plan_started",
                        f"Scheduled charging started for {start_plan['charge_end']}.",
                    )
            LOGGER.info(
                "Scheduled charging started for plan ending at %s.",
                start_plan["charge_end"],
            )
            return
        if should_return:
            self._try_return_to_self_use(current)

    def _try_return_to_self_use(self, current):
        max_retries = 5
        retry_interval_sec = 120  # 2 minutes
        last_error = None
        
        for attempt in range(1, max_retries + 1):
            try:
                with self.mode_lock:
                    with self._lock:
                        if self._state["status"] != "returning_to_self_use":
                            return
                    self.client.set_energy_storage_mode("self_use", confirmed=True)
                
                # Success - break out of retry loop
                break
            except SofarCloudError as error:
                last_error = error
                if attempt < max_retries:
                    LOGGER.warning(
                        "Scheduled plan return to Self-use failed (attempt %d/%d): %s. Retrying in %d seconds...",
                        attempt,
                        max_retries,
                        error,
                        retry_interval_sec,
                    )
                    time.sleep(retry_interval_sec)
                else:
                    LOGGER.error(
                        "Scheduled plan could not return the inverter to Self-use after %d attempts: %s",
                        max_retries,
                        error,
                    )
                    with self._lock:
                        if self._state["status"] == "returning_to_self_use":
                            self._state.update(
                                {
                                    "error": str(error),
                                    "retry_after": (current + timedelta(seconds=60)).isoformat(),
                                }
                            )
                            self._save_state()
                            self._record_plan_event(
                                "self_use_return_failed",
                                f"Could not return to Self-use after {max_retries} attempts; the app will retry.",
                            )
                    return
            except ValueError as error:
                last_error = error
                LOGGER.error("Scheduled plan return to Self-use failed with validation error: %s", error)
                with self._lock:
                    if self._state["status"] == "returning_to_self_use":
                        self._state.update(
                            {
                                "error": str(error),
                                "retry_after": None,
                            }
                        )
                        self._save_state()
                        self._record_plan_event(
                            "self_use_return_failed",
                            f"Could not return to Self-use: {error}",
                        )
                return
        
        # Success - complete the plan
        with self._lock:
            if self._state["status"] != "returning_to_self_use":
                return
            final_status = (
                "completed"
                if self._state.get("return_reason") == "scheduled_end"
                else "failed"
            )
            self._state.update(
                {
                    "status": final_status,
                    "finished_at": current.isoformat(timespec="seconds"),
                    "retry_after": None,
                }
            )
            self._save_state()
            plan_summary = self._plan_event_details(self._state.get("plan") or {})
            self._record_plan_event(
                "plan_completed",
                "Scheduled charging ended and the inverter returned to Self-use."
                if final_status == "completed"
                else "Scheduled charging failed and the inverter was returned to Self-use.",
            )
            if final_status == "completed" and self.history_store is not None:
                try:
                    self._record_plan_results(plan_summary)
                except (sqlite3.Error, KeyError, TypeError, ValueError) as error:
                    LOGGER.warning("Could not record plan results: %s", error)
            LOGGER.info("Scheduled charging ended; inverter returned to Self-use.")

    def cancel(self):
        with self._lock:
            if self._state["status"] not in self.ACTIVE_STATUSES:
                return self.get_state()
            self._state.update(
                {
                    "status": "cancelled",
                    "finished_at": datetime.now(ZoneInfo(TIMEZONE)).isoformat(
                        timespec="seconds"
                    ),
                }
            )
            self._save_state()
            self._record_plan_event(
                "plan_cancelled",
                "Charging plan cancelled after a manual Self-use command.",
            )
            return self.get_state()

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="sofar-scheduled-charge",
                daemon=True,
            )
            self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self.run_due()
            except OSError:
                LOGGER.exception("Could not persist scheduled charging state.")
            self._stop_event.wait(1)


class TelemetrySampler:
    def __init__(self, client, history_store, interval_seconds=300):
        self.client = client
        self.history_store = history_store
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread = None

    def sample_once(self, sampled_at=None):
        sampled_at = sampled_at or datetime.now(ZoneInfo(TIMEZONE))
        status = self.client.get_status()
        self.history_store.record_telemetry(
            status,
            sampled_at,
            import_price(sampled_at, True),
            import_price(sampled_at, False),
        )

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="sofar-telemetry-history",
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self.sample_once()
            except SofarCloudError as error:
                LOGGER.warning(
                    "Telemetry history sample failed at %s (API code=%s).",
                    error.stage,
                    error.api_code,
                )
            except (sqlite3.Error, KeyError, TypeError, ValueError) as error:
                LOGGER.exception("Telemetry history sample could not be saved: %s", error)
            self._stop_event.wait(self.interval_seconds)


DISCHARGE_WATCHDOG = DischargeWatchdog()
CLIENT = SofarCloudClient(USERNAME, PASSWORD, PLANT_ID, model=INVERTER_MODEL)
SCHEDULED_CHARGE = ScheduledChargeController(
    CLIENT,
    SCHEDULED_PLAN_FILE,
    MODE_CONTROL_LOCK,
    lambda: DISCHARGE_WATCHDOG.get_state()["active"],
)
MULTI_WINDOW_SCHEDULER = MultiWindowScheduler(
    CLIENT,
    MULTI_WINDOW_STATE_FILE,
    MODE_CONTROL_LOCK,
    lambda: DISCHARGE_WATCHDOG.get_state()["active"],
    TIMEZONE,
    LOGGER,
)
TELEMETRY_SAMPLER = None


def get_scheduled_plan_state():
    multi_window_state = MULTI_WINDOW_SCHEDULER.get_state()
    legacy_state = SCHEDULED_CHARGE.get_state()
    if multi_window_state.get("status") in {
        "pending",
        "charging",
        "transitioning",
        "evaluating",
    }:
        return multi_window_state
    if legacy_state.get("status") in SCHEDULED_CHARGE.ACTIVE_STATUSES:
        return legacy_state
    if multi_window_state.get("windows_queue"):
        return multi_window_state
    return legacy_state


def calculate_continuous_session_result(history_store, session, battery_model):
    if not isinstance(session, dict) or not isinstance(session.get("settings"), dict):
        raise ValueError("Continuous optimization session is invalid.")
    try:
        session_start = datetime.fromisoformat(session["started_at"])
        session_end = datetime.fromisoformat(session["ended_at"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Continuous optimization session must have start and end times.") from None
    if (
        session_start.tzinfo is None
        or session_end.tzinfo is None
        or session_end <= session_start
    ):
        raise ValueError("Continuous optimization session times must be increasing and timezone-aware.")

    start_utc = session_start.astimezone(timezone.utc)
    end_utc = session_end.astimezone(timezone.utc)
    hourly_plan = []
    hour = start_utc.replace(minute=0, second=0, microsecond=0)
    while hour < end_utc:
        local_hour = hour.astimezone(ZoneInfo(TIMEZONE))
        hourly_plan.append(
            {
                "time": hour.isoformat(timespec="minutes"),
                "tariff_eur_per_kwh": import_price(
                    local_hour, session["settings"].get("energy_aid", True)
                ),
            }
        )
        hour += timedelta(hours=1)

    actual = compare_plan_with_telemetry(
        {"hourly_plan": hourly_plan, "battery_model": battery_model},
        history_store.telemetry_for_period(
            start_utc.isoformat(timespec="seconds"),
            end_utc.isoformat(timespec="seconds"),
        ),
        start_utc.isoformat(timespec="seconds"),
        end_utc.isoformat(timespec="seconds"),
    )
    actual["continuous_session_start"] = session_start.isoformat(timespec="minutes")
    actual["continuous_session_end"] = session_end.isoformat(timespec="minutes")
    return actual


def finalize_continuous_optimization_plan(
    history_store, plan_id, session_id, battery_model
):
    session = (
        history_store.get_continuous_optimization_session(session_id)
        if session_id is not None
        else None
    )
    if session is None:
        session = history_store.get_latest_continuous_optimization_session()
    if session is None or not session.get("ended_at"):
        raise ValueError("A closed continuous optimization session was not found.")
    actual = calculate_continuous_session_result(
        history_store,
        session,
        battery_model,
    )
    actual["continuous_optimization"] = True
    actual["evaluation_status"] = "completed"
    history_store.update_optimization_plan_result(
        plan_id,
        "completed",
        actual,
    )
    return actual


def calculate_continuous_results(history_store, days, now=None):
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 30:
        raise ValueError("Results period must be between 1 and 30 days.")
    local_now = now or datetime.now(ZoneInfo(TIMEZONE))
    if local_now.tzinfo is None:
        raise ValueError("Continuous results time must include a timezone.")
    end = local_now.astimezone(timezone.utc)
    local_end = end.astimezone(ZoneInfo(TIMEZONE))
    first_date = local_end.date() - timedelta(days=days - 1)
    start = datetime.combine(
        first_date,
        datetime_time.min,
        tzinfo=ZoneInfo(TIMEZONE),
    ).astimezone(timezone.utc)
    daily_totals = {}
    for day_offset in range(days):
        day = first_date + timedelta(days=day_offset)
        daily_totals[day.isoformat()] = {
            "actual_cost": 0.0,
            "self_use_cost": 0.0,
            "savings": 0.0,
            "has_savings": False,
            "active_seconds": 0.0,
            "covered_seconds": 0.0,
        }
    sessions = history_store.get_continuous_optimization_sessions(
        start.isoformat(timespec="seconds"),
        end.isoformat(timespec="seconds"),
    )
    battery_model = history_store.get_continuous_optimization_state().get(
        "battery_model"
    )
    if not battery_model:
        return {
            "days": days,
            "savings_eur": None,
            "actual_cost_eur": None,
            "self_use_cost_eur": None,
            "coverage_percent": 0,
            "active_hours": 0,
            "sessions": 0,
            "savings_are_estimated": True,
            "daily": [
                {
                    "date": day,
                    "savings_eur": None,
                    "cumulative_savings_eur": None,
                    "actual_cost_eur": None,
                    "self_use_cost_eur": None,
                    "coverage_percent": 0,
                    "active_hours": 0,
                }
                for day in daily_totals
            ],
        }

    savings_total = 0.0
    actual_cost_total = 0.0
    self_use_cost_total = 0.0
    covered_seconds_total = 0.0
    active_seconds_total = 0.0
    evaluated_sessions = 0
    for session in sessions:
        session_start = max(
            start, datetime.fromisoformat(session["started_at"]).astimezone(timezone.utc)
        )
        session_end = min(
            end,
            datetime.fromisoformat(session["ended_at"]).astimezone(timezone.utc)
            if session.get("ended_at")
            else end,
        )
        if session_end <= session_start:
            continue
        settings = session["settings"]
        hourly_plan = []
        hour = session_start.replace(minute=0, second=0, microsecond=0)
        while hour < session_end:
            local_hour = hour.astimezone(ZoneInfo(TIMEZONE))
            hourly_plan.append(
                {
                    "time": hour.isoformat(timespec="minutes"),
                    "tariff_eur_per_kwh": import_price(
                        local_hour, settings.get("energy_aid", True)
                    ),
                }
            )
            hour += timedelta(hours=1)
        actual = compare_plan_with_telemetry(
            {"hourly_plan": hourly_plan, "battery_model": battery_model},
            history_store.telemetry_for_period(
                session_start.isoformat(timespec="seconds"),
                session_end.isoformat(timespec="seconds"),
            ),
            session_start.isoformat(timespec="seconds"),
            session_end.isoformat(timespec="seconds"),
        )
        savings = actual.get("estimated_savings_vs_self_use_eur")
        actual_cost = actual.get("actual_cost_on_simulation_coverage_eur")
        self_use_cost = actual.get("self_use_cost_on_simulation_coverage_eur")
        if self_use_cost is None:
            self_use_cost = actual.get("self_use_counterfactual_cost_eur")
        session_seconds = (session_end - session_start).total_seconds()
        coverage = actual.get("telemetry_coverage_percent", 0) / 100
        active_seconds_total += session_seconds
        covered_seconds_total += session_seconds * coverage
        for hour_result in actual.get("hourly", []):
            hour_start = datetime.fromisoformat(hour_result["time"]).astimezone(
                timezone.utc
            )
            hour_end = hour_start + timedelta(hours=1)
            active_start = max(session_start, hour_start)
            active_end = min(session_end, hour_end)
            if active_end <= active_start:
                continue
            day_key = hour_start.astimezone(ZoneInfo(TIMEZONE)).date().isoformat()
            daily = daily_totals.get(day_key)
            if daily is None:
                continue
            active_seconds = (active_end - active_start).total_seconds()
            covered_seconds = min(
                active_seconds,
                (hour_result.get("coverage_percent") or 0) * 36,
            )
            daily["active_seconds"] += active_seconds
            daily["covered_seconds"] += covered_seconds
            daily_actual_cost = hour_result.get("simulation_cost_eur")
            daily_self_use_cost = hour_result.get("self_use_cost_eur")
            daily_savings = hour_result.get("estimated_savings_eur")
            if (
                daily_actual_cost is not None
                and daily_self_use_cost is not None
                and daily_savings is not None
            ):
                daily["actual_cost"] += daily_actual_cost
                daily["self_use_cost"] += daily_self_use_cost
                daily["savings"] += daily_savings
                daily["has_savings"] = True
        if savings is not None and actual_cost is not None and self_use_cost is not None:
            savings_total += savings
            actual_cost_total += actual_cost
            self_use_cost_total += self_use_cost
            evaluated_sessions += 1

    has_results = evaluated_sessions > 0
    daily_results = []
    cumulative_savings = 0.0
    has_cumulative_savings = False
    for day, totals in daily_totals.items():
        has_daily_savings = totals["has_savings"]
        if has_daily_savings:
            cumulative_savings += totals["savings"]
            has_cumulative_savings = True
        active_seconds = totals["active_seconds"]
        daily_results.append(
            {
                "date": day,
                "savings_eur": round(totals["savings"], 4)
                if has_daily_savings
                else None,
                "cumulative_savings_eur": round(cumulative_savings, 4)
                if has_cumulative_savings
                else None,
                "actual_cost_eur": round(totals["actual_cost"], 4)
                if has_daily_savings
                else None,
                "self_use_cost_eur": round(totals["self_use_cost"], 4)
                if has_daily_savings
                else None,
                "coverage_percent": round(
                    totals["covered_seconds"] / active_seconds * 100, 1
                )
                if active_seconds
                else 0,
                "active_hours": round(active_seconds / 3600, 1),
            }
        )
    return {
        "days": days,
        "savings_eur": round(savings_total, 4) if has_results else None,
        "actual_cost_eur": round(actual_cost_total, 4) if has_results else None,
        "self_use_cost_eur": round(self_use_cost_total, 4) if has_results else None,
        "coverage_percent": round(
            100 * covered_seconds_total / active_seconds_total, 1
        )
        if active_seconds_total
        else 0,
        "active_hours": round(active_seconds_total / 3600, 1),
        "sessions": evaluated_sessions,
        "savings_are_estimated": True,
        "daily": daily_results,
    }


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "SofarOpti/0.1"

    def do_GET(self):
        if not self._is_authorized():
            return
        if self.path == "/":
            self._send_file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
        elif self.path == "/api/status":
            self._send_status()
        elif self.path == "/api/capabilities":
            self._send_capabilities()
        elif self.path == "/api/mode":
            self._send_mode()
        elif self.path == "/api/discharge-status":
            self._send_json(200, DISCHARGE_WATCHDOG.get_state())
        elif self.path == "/api/optimization-plan":
            self._send_json(200, get_scheduled_plan_state())
        elif self.path == "/api/continuous-optimization":
            if CONTINUOUS_OPTIMIZER is None:
                self._send_json(503, {"error": "Continuous optimization is not initialized."})
            else:
                self._send_json(200, CONTINUOUS_OPTIMIZER.get_state())
        elif urlsplit(self.path).path == "/api/telemetry":
            if HISTORY_STORE is None:
                self._send_json(503, {"error": "History logging is not initialized."})
                return
            query = parse_qs(urlsplit(self.path).query)
            try:
                raw_hours = query.get("hours", ["24"])[0]
                if not raw_hours.isdigit() or not 1 <= int(raw_hours) <= 48:
                    raise ValueError("Telemetry chart period must be between 1 and 48 hours.")
                end = datetime.now(timezone.utc)
                start = end - timedelta(hours=int(raw_hours))
                samples = HISTORY_STORE.telemetry_chart_period(
                    start.isoformat(timespec="seconds"),
                    end.isoformat(timespec="seconds"),
                )
                self._send_json(
                    200,
                    {
                        "start": start.isoformat(timespec="seconds"),
                        "end": end.isoformat(timespec="seconds"),
                        "samples": samples,
                    },
                )
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
            except sqlite3.Error as error:
                LOGGER.exception("Could not load telemetry chart data: %s", error)
                self._send_json(503, {"error": "Could not load telemetry chart data."})
        elif urlsplit(self.path).path == "/api/continuous-results":
            if HISTORY_STORE is None:
                self._send_json(503, {"error": "History logging is not initialized."})
                return
            query = parse_qs(urlsplit(self.path).query)
            try:
                raw_days = query.get("days", ["7"])[0]
                if not raw_days.isdigit():
                    raise ValueError("Results period must be an integer from 1 to 30 days.")
                results = calculate_continuous_results(
                    HISTORY_STORE,
                    int(raw_days),
                )
                self._send_json(200, results)
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
            except (sqlite3.Error, TypeError, KeyError) as error:
                LOGGER.exception("Could not calculate continuous results: %s", error)
                self._send_json(503, {"error": "Could not calculate continuous results."})
        elif self.path == "/api/live-metrics":
            try:
                metrics = calculate_live_metrics(HISTORY_STORE, MULTI_WINDOW_SCHEDULER, TIMEZONE)
                self._send_json(200, metrics)
            except Exception as error:
                LOGGER.exception("Could not calculate live metrics: %s", error)
                self._send_json(500, {"error": "Could not calculate live metrics."})
        elif self.path == "/api/daily-results":
            if HISTORY_STORE is None:
                self._send_json(503, {"error": "History logging is not initialized."})
            else:
                try:
                    results = HISTORY_STORE.get_daily_results()
                    self._send_json(200, {"daily_results": results})
                except sqlite3.Error as error:
                    LOGGER.exception("Could not read daily results: %s", error)
                    self._send_json(503, {"error": "Could not read daily results."})
        elif self.path == "/api/optimization-results":
            if HISTORY_STORE is None:
                self._send_json(503, {"error": "History logging is not initialized."})
            else:
                try:
                    plans = HISTORY_STORE.get_optimization_plans()
                    self._send_json(200, {"plans": plans})
                except (sqlite3.Error, ValueError, TypeError) as error:
                    LOGGER.exception("Could not read optimization results: %s", error)
                    self._send_json(503, {"error": "Could not read optimization results."})
        elif self.path == "/api/history":
            if HISTORY_STORE is None:
                self._send_json(503, {"error": "History logging is not initialized."})
            else:
                try:
                    self._send_json(200, HISTORY_STORE.recent())
                except sqlite3.Error as error:
                    LOGGER.exception("Could not read operation history: %s", error)
                    self._send_json(503, {"error": "Could not read operation history."})
        elif self.path == "/api/optimization":
            self.send_error(405, "Use POST to calculate a charging optimization.")
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._is_authorized():
            return
        if self.path not in {
            "/api/mode",
            "/api/optimization",
            "/api/optimization-plan",
            "/api/continuous-optimization",
        }:
            self.send_error(404)
            return
        origin = self.headers.get("Origin")
        if origin and origin not in ALLOWED_ORIGINS and "*" not in ALLOWED_ORIGINS:
            self._send_json(403, {"error": "Requests from this origin are not allowed."})
            return
        if self.headers.get_content_type() != "application/json":
            self._send_json(415, {"error": "Expected an application/json request."})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "Invalid Content-Length."})
            return
        if not 0 < content_length <= 65536:
            self._send_json(400, {"error": "Request body size is invalid."})
            return
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "Invalid JSON request body."})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "Expected a JSON object."})
            return
        if self.path == "/api/continuous-optimization":
            self._set_continuous_optimization(payload)
            return
        if self.path == "/api/optimization":
            self._send_optimization(payload)
            return
        if self.path == "/api/optimization-plan":
            if CONTINUOUS_OPTIMIZER and CONTINUOUS_OPTIMIZER.get_state()["enabled"]:
                self._send_json(
                    409,
                    {"error": "Disable continuous optimization before scheduling a manual plan."},
                )
                return
            if payload.get("confirmed") is not True:
                self._send_json(400, {"error": "Confirm the scheduled charging plan before saving it."})
                return
            if DISCHARGE_WATCHDOG.get_state()["active"]:
                self._send_json(409, {"error": "A discharge plan is active; scheduled charging cannot be started."})
                return
            try:
                # Support both single plan and multi-window scheduling
                if "windows" in payload and isinstance(payload["windows"], list):
                    # Multi-window scheduling
                    legacy_status = SCHEDULED_CHARGE.get_state()["status"]
                    multi_status = MULTI_WINDOW_SCHEDULER.get_state()["status"]
                    if legacy_status in SCHEDULED_CHARGE.ACTIVE_STATUSES - {"pending"}:
                        raise RuntimeError(
                            "A single-window charging plan is already running."
                        )
                    if multi_status in {"charging", "transitioning"}:
                        raise RuntimeError(
                            "A multi-window charging plan is already running."
                        )
                    if legacy_status == "pending":
                        SCHEDULED_CHARGE.cancel()
                    windows = payload["windows"]
                    plan_date = payload.get("plan_date", "")
                    result = MULTI_WINDOW_SCHEDULER.schedule_windows(
                        windows,
                        plan_date,
                        payload.get("plan_snapshot"),
                    )
                    self._send_json(202, result)
                else:
                    # Legacy single-window scheduling
                    multi_status = MULTI_WINDOW_SCHEDULER.get_state()["status"]
                    legacy_status = SCHEDULED_CHARGE.get_state()["status"]
                    if multi_status in {"charging", "transitioning"}:
                        raise RuntimeError(
                            "A multi-window charging plan is already running."
                        )
                    if legacy_status in SCHEDULED_CHARGE.ACTIVE_STATUSES:
                        raise RuntimeError(
                            "A single-window charging plan is already running."
                        )
                    if multi_status in {"pending", "evaluating"}:
                        MULTI_WINDOW_SCHEDULER.cancel()
                    result = SCHEDULED_CHARGE.schedule(payload.get("plan"))
                    self._send_json(202, result)
            except ValueError as error:
                self._send_json(400, {"error": str(error), "stage": "validation"})
            except RuntimeError as error:
                self._send_json(409, {"error": str(error), "stage": "scheduled plan"})
            except (OSError, sqlite3.Error) as error:
                LOGGER.error("Could not persist scheduled charging plan: %s", error)
                self._send_json(500, {"error": "Could not save the scheduled charging plan."})
            return
        if payload.get("confirmed") is not True:
            self._send_json(400, {"error": "Confirm the mode change before sending it."})
            return
        if CONTINUOUS_OPTIMIZER and CONTINUOUS_OPTIMIZER.get_state()["enabled"]:
            if payload.get("mode") != "self_use":
                self._send_json(
                    409,
                    {"error": "Disable continuous optimization before choosing another inverter mode."},
                )
                return
            try:
                CONTINUOUS_OPTIMIZER.set_enabled(False)
            except Exception as error:
                LOGGER.exception("Could not stop continuous optimization: %s", error)
                self._send_json(
                    503,
                    {"error": "Continuous optimization was disabled, but the inverter could not be returned to Self-use."},
                )
                return
        with MODE_CONTROL_LOCK:
            scheduled_status = SCHEDULED_CHARGE.get_state()["status"]
            multi_window_status = MULTI_WINDOW_SCHEDULER.get_state()["status"]
            scheduled_plan_active = (
                scheduled_status in SCHEDULED_CHARGE.ACTIVE_STATUSES
                or multi_window_status in {"pending", "charging", "transitioning"}
                or multi_window_status == "evaluating"
            )
            if scheduled_plan_active and payload.get("mode") != "self_use":
                self._send_json(
                    409,
                    {
                        "error": "A scheduled charging plan is active. Return to Self-use to cancel it before choosing another mode."
                    },
                )
                return
            if payload.get("mode") == "grid_discharge" and DISCHARGE_WATCHDOG.get_state()["active"]:
                self._send_json(409, {"error": "A discharge plan is already being monitored."})
                return
            try:
                result = retry_set_energy_storage_mode(
                    CLIENT,
                    payload.get("mode"),
                    payload.get("power_w"),
                    confirmed=payload.get("confirmed") is True,
                    target_soc=payload.get("target_soc", MAXIMUM_BATTERY_SOC),
                    charge_start_time=payload.get("charge_start_time"),
                    charge_end_time=payload.get("charge_end_time", "23:59"),
                    charge_start_date=payload.get("charge_start_date"),
                    charge_end_date=payload.get("charge_end_date"),
                )
                if payload.get("mode") == "grid_discharge":
                    try:
                        DISCHARGE_WATCHDOG.start(CLIENT, result)
                    except RuntimeError as error:
                        LOGGER.error("Discharge monitor could not start: %s", error)
                        try:
                            CLIENT.set_energy_storage_mode("self_use", confirmed=True)
                        except SofarCloudError as fallback_error:
                            LOGGER.critical(
                                "Discharge monitor failed and the inverter could not be returned to Self-use "
                                "(stage=%s, API code=%s)",
                                fallback_error.stage,
                                fallback_error.api_code,
                            )
                            self._send_json(
                                502,
                                {
                                    "error": "The discharge monitor failed to start and the inverter may still be discharging.",
                                    "stage": "discharge monitor",
                                },
                            )
                        else:
                            self._send_json(
                                503,
                                {
                                    "error": "The discharge monitor failed to start; the inverter was returned to Self-use.",
                                    "stage": "discharge monitor",
                                },
                            )
                        return
                else:
                    DISCHARGE_WATCHDOG.stop()
                    if payload.get("mode") == "self_use":
                        SCHEDULED_CHARGE.cancel()
                        MULTI_WINDOW_SCHEDULER.cancel()
                self._send_json(200, result)
            except RuntimeError as error:
                self._send_json(409, {"error": str(error), "stage": "discharge monitor"})
            except ValueError as error:
                self._send_json(400, {"error": str(error), "stage": "validation"})
            except SofarCloudError as error:
                LOGGER.warning("Mode control failed at %s (API code=%s)", error.stage, error.api_code)
                response = error.as_dict()
                warning = record_history_event(
                    CLIENT.history_store,
                    "mode_change_failed",
                    f"Mode change to {payload.get('mode')} failed at {error.stage}.",
                    {
                        "mode": payload.get("mode"),
                        "stage": error.stage,
                        "api_code": error.api_code,
                        "error": str(error),
                    },
                )
                if warning:
                    response["history_warning"] = warning
                self._send_json(502, response)

    def _set_continuous_optimization(self, payload):
        if CONTINUOUS_OPTIMIZER is None:
            self._send_json(503, {"error": "Continuous optimization is not initialized."})
            return
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            self._send_json(400, {"error": "Enabled must be true or false."})
            return
        if enabled and payload.get("confirmed") is not True:
            self._send_json(400, {"error": "Confirm continuous optimization before enabling it."})
            return
        try:
            settings = None
            if enabled:
                settings = validate_optimization_settings(payload.get("settings"))
                if DISCHARGE_WATCHDOG.get_state()["active"]:
                    raise RuntimeError("Stop the discharge plan before enabling continuous optimization.")
                if SCHEDULED_CHARGE.get_state()["status"] in SCHEDULED_CHARGE.ACTIVE_STATUSES:
                    raise RuntimeError("Cancel the active single-window plan before enabling continuous optimization.")
                multi_state = MULTI_WINDOW_SCHEDULER.get_state()
                if (
                    multi_state.get("status")
                    in {"pending", "charging", "transitioning", "evaluating"}
                    and not multi_state.get("continuous_optimization")
                ):
                    raise RuntimeError("Cancel the active manual plan before enabling continuous optimization.")
            state = CONTINUOUS_OPTIMIZER.set_enabled(enabled, settings)
            self._send_json(202, state)
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
        except RuntimeError as error:
            self._send_json(409, {"error": str(error)})
        except Exception as error:
            LOGGER.exception("Could not update continuous optimization: %s", error)
            self._send_json(503, {"error": "Could not update continuous optimization."})

    def _is_authorized(self):
        if not WEB_USERNAME and not WEB_PASSWORD:
            return True
        if not WEB_USERNAME or not WEB_PASSWORD:
            self._send_json(503, {"error": "Both SOFAR_WEB_USERNAME and SOFAR_WEB_PASSWORD must be configured."})
            return False
        authorization = self.headers.get("Authorization", "")
        scheme, separator, credentials = authorization.partition(" ")
        if scheme.lower() == "basic" and separator:
            try:
                decoded = base64.b64decode(credentials, validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                decoded = ""
            username, delimiter, password = decoded.partition(":")
            if (
                delimiter
                and hmac.compare_digest(username, WEB_USERNAME)
                and hmac.compare_digest(password, WEB_PASSWORD)
            ):
                return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Sofar Opti"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _send_status(self):
        try:
            self._send_json(200, CLIENT.get_status())
        except SofarCloudError as error:
            LOGGER.warning(
                "Status request failed at %s (API code=%s, HTTP status=%s, "
                "content type=%s, response format=%s)",
                error.stage,
                error.api_code,
                error.http_status,
                error.content_type,
                error.response_format,
            )
            self._send_json(503, error.as_dict())

    def _send_optimization(self, settings):
        try:
            result = calculate_optimization(settings, CLIENT, CLIENT.history_store)
            self._send_json(200, result)
        except ValueError as error:
            self._send_json(400, {"error": str(error), "stage": "validation"})
        except ForecastError as error:
            LOGGER.warning("Solar forecast failed: %s", error)
            self._send_json(502, {"error": str(error), "stage": "weather forecast"})
        except sqlite3.Error as error:
            LOGGER.exception("Could not read consumption history for optimization: %s", error)
            self._send_json(
                503,
                {
                    "error": "Could not read saved consumption history.",
                    "stage": "consumption history",
                },
            )
        except SofarCloudError as error:
            LOGGER.warning("Optimization status read failed at %s (API code=%s)", error.stage, error.api_code)
            self._send_json(503, error.as_dict())

    def _send_capabilities(self):
        try:
            self._send_json(200, CLIENT.get_energy_storage_schema())
        except SofarCloudError as error:
            LOGGER.warning("Command schema request failed at %s (API code=%s)", error.stage, error.api_code)
            self._send_json(503, error.as_dict())

    def _send_mode(self):
        try:
            self._send_json(200, CLIENT.read_energy_storage_mode())
        except SofarCloudError as error:
            LOGGER.warning("Mode read failed at %s (API code=%s)", error.stage, error.api_code)
            self._send_json(503, error.as_dict())

    def _send_discharge_status(self):
        self._send_discharge_status()

    def _send_file(self, path, content_type):
        try:
            content = path.read_bytes()
        except OSError:
            self.send_error(500, "Dashboard files could not be read.")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, status, payload):
        content = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format_string, *args):
        LOGGER.info("%s - %s", self.address_string(), format_string % args)


def main():
    try:
        is_loopback = WEB_HOST.lower() == "localhost" or ipaddress.ip_address(WEB_HOST).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback and (not WEB_USERNAME or not WEB_PASSWORD):
        raise SystemExit(
            "SOFAR_WEB_USERNAME and SOFAR_WEB_PASSWORD are required when "
            "SOFAR_WEB_HOST is not a loopback address."
        )
    if bool(WEB_USERNAME) != bool(WEB_PASSWORD):
        raise SystemExit("Set both SOFAR_WEB_USERNAME and SOFAR_WEB_PASSWORD, or leave both empty.")
    global HISTORY_STORE, TELEMETRY_SAMPLER, CONTINUOUS_OPTIMIZER
    HISTORY_STORE = HistoryStore(DATA_DIR / "history.sqlite3")
    CLIENT.history_store = HISTORY_STORE
    SCHEDULED_CHARGE.history_store = HISTORY_STORE
    MULTI_WINDOW_SCHEDULER.history_store = HISTORY_STORE
    TELEMETRY_SAMPLER = TelemetrySampler(CLIENT, HISTORY_STORE)

    def disable_continuous_plan():
        state = MULTI_WINDOW_SCHEDULER.get_state()
        is_continuous = state.get("continuous_optimization") is True
        session = None
        if is_continuous:
            session_id = state.get("continuous_session_id")
            session = (
                HISTORY_STORE.get_continuous_optimization_session(session_id)
                if session_id is not None
                else None
            )
            if session is None:
                session = HISTORY_STORE.get_latest_continuous_optimization_session()
        has_closed_session = bool(session and session.get("ended_at"))
        if state.get("status") in {
            "pending",
            "charging",
            "transitioning",
            "evaluating",
        }:
            MULTI_WINDOW_SCHEDULER.cancel(
                record_result=not (is_continuous and has_closed_session)
            )
        try:
            if is_continuous and state.get("plan_id") and has_closed_session:
                finalize_continuous_optimization_plan(
                    HISTORY_STORE,
                    state["plan_id"],
                    session.get("id"),
                    HISTORY_STORE.get_continuous_optimization_state().get(
                        "battery_model"
                    ),
                )
        finally:
            with MODE_CONTROL_LOCK:
                CLIENT.set_energy_storage_mode("self_use", confirmed=True)

    CONTINUOUS_OPTIMIZER = ContinuousOptimizationController(
        HISTORY_STORE,
        MULTI_WINDOW_SCHEDULER,
        lambda settings: calculate_optimization(settings, CLIENT, CLIENT.history_store),
        disable_continuous_plan,
        TIMEZONE,
        LOGGER,
    )
    server = ThreadingHTTPServer((WEB_HOST, WEB_PORT), DashboardHandler)
    SCHEDULED_CHARGE.start()
    MULTI_WINDOW_SCHEDULER.start()
    TELEMETRY_SAMPLER.start()
    CONTINUOUS_OPTIMIZER.start()
    LOGGER.info("Dashboard available at http://%s:%s", WEB_HOST, WEB_PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Shutting down dashboard.")
    finally:
        SCHEDULED_CHARGE.stop()
        MULTI_WINDOW_SCHEDULER.stop()
        TELEMETRY_SAMPLER.stop()
        CONTINUOUS_OPTIMIZER.stop()
        server.server_close()


if __name__ == "__main__":
    main()
