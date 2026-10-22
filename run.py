import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import socketio


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
ALLOWED_ORIGINS = {
    origin.strip()
    for origin in os.environ.get(
        "SOFAR_ALLOWED_ORIGINS",
        f"http://127.0.0.1:{WEB_PORT},http://localhost:{WEB_PORT}",
    ).split(",")
    if origin.strip()
}
WEB_ROOT = Path(__file__).parent / "web"
REQUEST_TIMEOUT = 15
COMMAND_TIMEOUT = 60
SOCKET_URL = "https://eu-socket.sofarcloud.com"

LOGIN_PATH = "/api/user/auth/he/login"
STATION_DETAIL_PATH = "/api/device/stationInfo/selectStationDetail"
LOGIN_SIGN = "H_3uwQ6lJ4g"
DETAIL_SIGN = "fgHFeEeOOMk"
COMMAND_SCHEMA_PATH = "/api/device/selectCmdParamByModel"
DEVICE_COMMAND_PATH = "/api/device/command"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger(__name__)


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
        "metrics": {
            "generation_power": realtime.get("generationPower"),
            "use_power": realtime.get("usePower"),
            "battery_soc": realtime.get("batterySoc"),
            "battery_power": realtime.get("batteryPower"),
            "grid_power": realtime.get("gridPower"),
        },
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


def build_mode_parameters(mode, power_w=None, today=None, start_time=None, current_rules=None):
    if mode == "self_use":
        return {"mode": "0"}
    if mode != "grid_charge":
        raise ValueError("Unsupported inverter mode.")
    if isinstance(power_w, bool) or not isinstance(power_w, int) or not 1 <= power_w <= 2000:
        raise ValueError("Charging power must be between 1 and 2000 W.")
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
    except (TypeError, ValueError):
        raise ValueError("Charging start time must use HH:MM format.") from None
    date_text = date.strftime("%m-%d")
    rules = [dict(rule) for rule in current_rules]
    for rule in rules:
        rule["touEnable"] = "0"
    charge_rule = rules[0]
    charge_rule.update(
        {
            "touEnable": "1",
            "touChargeStopSoc": "95",
            "touChargePower": str(power_w),
            "touStartChargeTimestamp": start_time,
            "touEndChargeTimestamp": "23:59",
            "touStartTimestamp": date_text,
            "touEndTimestamp": date_text,
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
                        "modes": [
                            {
                                "value": option.get("value"),
                                "label": option.get("key"),
                            }
                            for option in mode_field.get("optionItems", [])
                        ],
                        "fields": [field.get("fieldName") for field in fields],
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

    def set_energy_storage_mode(self, mode, power_w=None, confirmed=False):
        if not confirmed:
            raise SofarCloudError(
                "Confirm the mode change before sending it to the inverter.",
                "mode control",
            )
        schema = self.get_energy_storage_schema()
        supported_modes = {str(option["value"]) for option in schema["modes"]}
        mode_code = "0" if mode == "self_use" else "1" if mode == "grid_charge" else ""
        if mode_code not in supported_modes:
            raise SofarCloudError(
                "The live inverter schema does not support the requested mode.",
                "mode control",
            )
        if mode == "grid_charge":
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
            if state_of_charge >= 95:
                raise SofarCloudError(
                    "Battery SOC is already at or above the 95% charge target.",
                    "mode control",
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
                raise SofarCloudError(
                    "The inverter did not return its current Time-of-Use rules; the charge request was not sent.",
                    "mode control",
                )
            try:
                params = build_mode_parameters(
                    mode,
                    power_w,
                    current_rules=current_rules,
                )
            except ValueError as error:
                raise SofarCloudError(str(error), "mode control") from None
        else:
            params = build_mode_parameters(mode, power_w)
        if mode == "self_use" and current_mode == mode_code:
            return {"mode": mode_code, "verified": True, "changed": False}

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
        return {
            "mode": mode_code,
            "power_w": power_w if mode == "grid_charge" else None,
            "target_soc": 95 if mode == "grid_charge" else None,
            "start_time": params["energyStoreRuleList"][0]["touStartChargeTimestamp"]
            if mode == "grid_charge"
            else None,
            "verified": True,
            "changed": True,
        }

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


CLIENT = SofarCloudClient(USERNAME, PASSWORD, PLANT_ID, model=INVERTER_MODEL)


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
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._is_authorized():
            return
        if self.path != "/api/mode":
            self.send_error(404)
            return
        origin = self.headers.get("Origin")
        if origin and origin not in ALLOWED_ORIGINS:
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
        if not 0 < content_length <= 4096:
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
        if payload.get("confirmed") is not True:
            self._send_json(400, {"error": "Confirm the mode change before sending it."})
            return
        try:
            result = CLIENT.set_energy_storage_mode(
                payload.get("mode"),
                payload.get("power_w"),
                confirmed=payload.get("confirmed") is True,
            )
            self._send_json(200, result)
        except ValueError as error:
            self._send_json(400, {"error": str(error), "stage": "validation"})
        except SofarCloudError as error:
            LOGGER.warning("Mode control failed at %s (API code=%s)", error.stage, error.api_code)
            self._send_json(502, error.as_dict())

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
    server = ThreadingHTTPServer((WEB_HOST, WEB_PORT), DashboardHandler)
    LOGGER.info("Dashboard available at http://%s:%s", WEB_HOST, WEB_PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Shutting down dashboard.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
