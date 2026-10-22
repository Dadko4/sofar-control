import unittest
import base64
from email.message import Message
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import addinfourl
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta
from threading import Event
from zoneinfo import ZoneInfo

from run import (
    TIMEZONE,
    DischargeWatchdog,
    SofarCloudClient,
    SofarCloudError,
    build_mode_parameters,
    estimate_discharge,
    load_env_file,
    parse_station_status,
    sign_sofar_request,
    supports_passive_discharge,
)


def sample_tou_rules():
    return [
        {
            "touId": str(index),
            "ruleId": str(index),
            "touEnable": "1" if index < 2 else "0",
            "touStartChargeTimestamp": "22:00" if index == 0 else "16:00",
            "touEndChargeTimestamp": "05:45" if index == 0 else "18:45",
            "touChargeStopSoc": "90" if index == 0 else "60",
            "touChargePower": "900" if index == 0 else "800",
            "touEnableWeek": "1,2,3,4,5,6,7",
            "touStartTimestamp": "01-01",
            "touEndTimestamp": "12-31",
            "touModeControl": "0",
            "touChargeBatteryControl": "0",
        }
        for index in range(4)
    ]


class SofarCloudClientTests(unittest.TestCase):
    def test_status_maps_station_realtime_values(self):
        result = parse_station_status(
            {
                "stationName": "Jacovce",
                "stationState": 2,
                "stationBatteryCapacity": 5,
                "stationBatteryCapacityUnit": "kWh",
                "stationRealTimeVo": {
                    "generationPower": 1.25,
                    "usePower": 0.8,
                    "batterySoc": 72,
                    "batteryPower": -0.4,
                    "chargePower": 0,
                    "disChargePower": -0.4,
                    "gridPower": 0.05,
                    "dataTime": "1792669677",
                    "generationValue": 2.7,
                    "generationValueUnit": "kWh",
                    "useValue": 4.3,
                    "useValueUnit": "kWh",
                    "gridValue": 0.2,
                    "gridValueUnit": "kWh",
                    "buyValue": 4.4,
                    "buyValueUnit": "kWh",
                    "chargeValue": 3.6,
                    "chargeValueUnit": "kWh",
                    "disChargeValue": 1.4,
                    "disChargeValueUnit": "kWh",
                },
            },
            "HYD 5KTL-3PH",
        )

        self.assertEqual(result["station_name"], "Jacovce")
        self.assertEqual(result["model"], "HYD 5KTL-3PH")
        self.assertEqual(result["battery_capacity"], {"value": 5, "unit": "kWh"})
        self.assertEqual(
            result["metrics"],
            {
                "generation_power": 1.25,
                "use_power": 0.8,
                "battery_soc": 72,
                "battery_power": -0.4,
                "charge_power": 0,
                "discharge_power": -0.4,
                "grid_power": 0.05,
            },
        )
        self.assertEqual(result["data_time"], "1792669677")
        self.assertEqual(
            result["daily_energy"],
            {
                "generation": {"value": 2.7, "unit": "kWh"},
                "consumption": {"value": 4.3, "unit": "kWh"},
                "grid_export": {"value": 0.2, "unit": "kWh"},
                "grid_import": {"value": 4.4, "unit": "kWh"},
                "battery_charge": {"value": 3.6, "unit": "kWh"},
                "battery_discharge": {"value": 1.4, "unit": "kWh"},
            },
        )

    def test_status_logs_in_and_reads_station_detail(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        responses = [
            {"code": "0", "data": {"accessToken": "test-token"}},
            {
                "code": "0",
                "data": {
                    "stationName": "Test plant",
                    "stationRealTimeVo": {"batterySoc": 55},
                },
            },
        ]

        with patch.object(client, "_request_json", side_effect=responses) as request:
            status = client.get_status()

        self.assertEqual(status["station_name"], "Test plant")
        self.assertEqual(status["metrics"]["battery_soc"], 55)
        self.assertEqual(
            request.call_args_list[0].args[:3],
            ("POST", "/api/user/auth/he/login", "login"),
        )
        self.assertEqual(
            request.call_args_list[1].args[:3],
            (
                "POST",
                "/api/device/stationInfo/selectStationDetail?stationId=plant-123",
                "station detail",
            ),
        )
        self.assertEqual(request.call_args_list[1].args[2], "station detail")
        self.assertEqual(request.call_args_list[1].kwargs["headers"]["authorization"], "test-token")
        self.assertEqual(request.call_args_list[1].kwargs["headers"]["request-from"], "app")
        self.assertEqual(request.call_args_list[1].kwargs["payload"], {})

    def test_missing_credentials_fail_without_network_access(self):
        client = SofarCloudClient("", "", "plant-123")

        with patch.object(client, "_request_json") as request:
            with self.assertRaisesRegex(SofarCloudError, "SOFAR_USERNAME"):
                client.get_status()

        request.assert_not_called()

    def test_missing_plant_id_fails_before_login(self):
        client = SofarCloudClient("test@example.com", "test-password", "")

        with patch.object(client, "_request_json") as request:
            with self.assertRaisesRegex(SofarCloudError, "SOFAR_PLANT_ID") as raised:
                client.get_status()

        self.assertEqual(raised.exception.stage, "configuration")
        request.assert_not_called()

    def test_non_json_detail_response_includes_safe_diagnostics(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        client._access_token = "test-token"
        client._token_expires_at = 9999999999
        headers = Message()
        headers["Content-Type"] = "text/html; charset=utf-8"
        response = addinfourl(
            BytesIO(b"<html><body>login required</body></html>"),
            headers,
            "https://example.invalid",
            code=200,
        )

        with patch("run.urlopen", return_value=response):
            with self.assertRaises(SofarCloudError) as raised:
                client.get_status()

        self.assertEqual(raised.exception.stage, "station detail")
        self.assertEqual(raised.exception.http_status, 200)
        self.assertEqual(raised.exception.content_type, "text/html; charset=utf-8")
        self.assertEqual(raised.exception.response_format, "html")
        self.assertNotIn("login required", str(raised.exception))

    def test_env_file_loads_values_without_overriding_environment(self):
        with TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "SOFAR_TEST_FROM_FILE='file value'\n"
                "SOFAR_TEST_FROM_ENV=file value\n"
                "# ignored comment\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"SOFAR_TEST_FROM_ENV": "existing value"}):
                load_env_file(env_path)
                import os

                self.assertEqual(os.environ["SOFAR_TEST_FROM_FILE"], "file value")
                self.assertEqual(os.environ["SOFAR_TEST_FROM_ENV"], "existing value")
                os.environ.pop("SOFAR_TEST_FROM_FILE", None)

    def test_login_rejects_unsuccessful_api_response(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")

        with patch.object(client, "_request_json", return_value={"code": "666666"}):
            with self.assertRaisesRegex(SofarCloudError, "login failed") as raised:
                client.get_status()

        self.assertEqual(raised.exception.stage, "login")
        self.assertEqual(raised.exception.api_code, "666666")


    def test_api_error_response_does_not_expose_credentials(self):
        client = SofarCloudClient("private-user", "private-password", "plant-123")

        with patch.object(
            client,
            "_request_json",
            return_value={"code": "101021", "message": "private-user is invalid"},
        ):
            with self.assertRaises(SofarCloudError) as raised:
                client.get_status()

        self.assertEqual(
            raised.exception.as_dict(),
            {
                "error": "SofarCloud login failed.",
                "stage": "login",
                "api_code": "101021",
            },
        )

    def test_charge_mode_parameters_are_today_only_and_soc_capped(self):
        from datetime import date

        params = build_mode_parameters(
            "grid_charge",
            2000,
            date(2026, 10, 22),
            "11:16",
            sample_tou_rules(),
        )

        self.assertEqual(params["mode"], "1")
        rules = params["energyStoreRuleList"]
        self.assertEqual(len(rules), 4)
        self.assertEqual(rules[0]["touEnable"], "1")
        self.assertEqual(rules[0]["touChargeStopSoc"], "95")
        self.assertEqual(rules[0]["touChargePower"], "2000")
        self.assertEqual(rules[0]["touStartChargeTimestamp"], "11:16")
        self.assertEqual(rules[0]["touEndChargeTimestamp"], "23:59")
        self.assertEqual(rules[0]["touStartTimestamp"], "10-22")
        self.assertEqual(rules[0]["touEndTimestamp"], "10-22")
        self.assertEqual(rules[0]["touEnableWeek"], "1,2,3,4,5,6,7")
        self.assertEqual([rule["touEnable"] for rule in rules[1:]], ["0", "0", "0"])
        self.assertEqual(sample_tou_rules()[0]["touEnable"], "1")

    def test_charge_power_is_limited_to_two_kw(self):
        for power in (0, -1, 2001, True, 1000.5):
            with self.subTest(power=power):
                with self.assertRaisesRegex(ValueError, "between 1 and 2000 W"):
                    build_mode_parameters("grid_charge", power)

    def test_charge_plan_rejects_invalid_start_time_and_missing_rules(self):
        for start_time, rules in (("25:99", sample_tou_rules()), ("11:30", None)):
            with self.subTest(start_time=start_time, rules=rules):
                with self.assertRaises(ValueError):
                    build_mode_parameters(
                        "grid_charge",
                        2000,
                        start_time=start_time,
                        current_rules=rules,
                    )

    def test_discharge_estimate_uses_configured_capacity_and_soc_floor(self):
        estimate = estimate_discharge(73, 2500, 5)

        self.assertEqual(estimate["energy_kwh"], 2.15)
        self.assertEqual(estimate["duration_seconds"], 3096)
        self.assertEqual(estimate["duration_minutes"], 51.6)

    def test_discharge_estimate_rejects_soc_at_floor_and_invalid_power(self):
        for soc, power in ((30, 2500), (float("nan"), 2500), (73, 2501), (73, 99)):
            with self.subTest(soc=soc, power=power):
                with self.assertRaises(ValueError):
                    estimate_discharge(soc, power, 5)

    def test_passive_discharge_is_supported_only_for_known_model_and_fields(self):
        fields = ["passiveManualBup", "passiveManualBlo"]

        self.assertTrue(supports_passive_discharge("HYD 5-20KTL-3PH", fields))
        self.assertFalse(supports_passive_discharge("HYD 5KTL-3PH", fields))
        self.assertFalse(supports_passive_discharge("HYD 5-20KTL-3PH", fields[:1]))

    def test_discharge_mode_parameters_use_negative_passive_power(self):
        params = build_mode_parameters("grid_discharge", 2500)

        self.assertEqual(
            params,
            {
                "mode": "3",
                "passiveManualBup": -2500,
                "passiveManualBlo": -2500,
            },
        )

    def test_passive_discharge_power_is_limited(self):
        for power in (99, 2501, True, 1000.5):
            with self.subTest(power=power):
                with self.assertRaisesRegex(ValueError, "between 100 and 2500 W"):
                    build_mode_parameters("grid_discharge", power)

    def test_request_sign_uses_only_scalar_fields(self):
        self.assertEqual(
            sign_sofar_request({"a": "value", "nested": {"secret": "excluded"}}),
            sign_sofar_request({"a": "value"}),
        )
        self.assertEqual(
            sign_sofar_request({"b": 2, "a": "value"}),
            sign_sofar_request({"a": "value", "b": 2}),
        )

    def test_mode_change_requires_confirmation_before_network_access(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        with patch.object(client, "get_energy_storage_schema") as schema:
            with self.assertRaisesRegex(SofarCloudError, "Confirm the mode change"):
                client.set_energy_storage_mode("grid_charge", 2000)
        schema.assert_not_called()

    def test_self_use_request_is_idempotent_when_already_active(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        schema = {
            "modes": [{"value": "0"}, {"value": "1"}],
            "fields": [],
        }
        with patch.object(client, "get_energy_storage_schema", return_value=schema), \
                patch.object(client, "read_energy_storage_mode", return_value={"mode": "0"}), \
                patch.object(client, "_execute_command") as command:
            result = client.set_energy_storage_mode("self_use", confirmed=True)

        self.assertEqual(result, {"mode": "0", "verified": True, "changed": False})
        command.assert_not_called()

    def test_charge_is_blocked_at_target_soc_before_any_write(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        schema = {
            "modes": [{"value": "0"}, {"value": "1"}],
            "fields": [
                "touEnable",
                "touChargeStopSoc",
                "touChargePower",
                "touStartChargeTimestamp",
                "touEndChargeTimestamp",
                "touStartTimestamp",
                "touEndTimestamp",
                "touEnableWeek",
            ],
        }
        with patch.object(client, "get_energy_storage_schema", return_value=schema), \
                patch.object(
                    client,
                    "get_status",
                    return_value={"metrics": {"battery_soc": 95}},
                ), \
                patch.object(client, "read_energy_storage_mode") as read_mode, \
                patch.object(client, "_execute_command") as command:
            with self.assertRaisesRegex(SofarCloudError, "at or above the 95%"):
                client.set_energy_storage_mode("grid_charge", 2000, confirmed=True)

        read_mode.assert_not_called()
        command.assert_not_called()

    def test_charge_is_blocked_when_soc_is_not_finite(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        schema = {
            "modes": [{"value": "0"}, {"value": "1"}],
            "fields": [
                "touEnable",
                "touChargeStopSoc",
                "touChargePower",
                "touStartChargeTimestamp",
                "touEndChargeTimestamp",
                "touStartTimestamp",
                "touEndTimestamp",
                "touEnableWeek",
            ],
        }
        with patch.object(client, "get_energy_storage_schema", return_value=schema), \
                patch.object(
                    client,
                    "get_status",
                    return_value={"metrics": {"battery_soc": "nan"}},
                ), \
                patch.object(client, "read_energy_storage_mode") as read_mode:
            with self.assertRaisesRegex(SofarCloudError, "SOC is invalid"):
                client.set_energy_storage_mode("grid_charge", 2000, confirmed=True)

        read_mode.assert_not_called()

    def test_charge_sends_bounded_today_only_schedule_after_confirmation(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        client._access_token = "test-token"
        client._token_expires_at = 9999999999
        original_rules = sample_tou_rules()
        schema = {
            "modes": [{"value": "0"}, {"value": "1"}],
            "fields": [
                "touEnable",
                "touChargeStopSoc",
                "touChargePower",
                "touStartChargeTimestamp",
                "touEndChargeTimestamp",
                "touStartTimestamp",
                "touEndTimestamp",
                "touEnableWeek",
            ],
        }
        socket_client = MagicMock()
        socket_client.connected = True
        command = MagicMock(return_value={"result": 1})
        mode_reads = []

        def read_mode():
            if not mode_reads:
                mode_reads.append(True)
                return {"mode": "0", "energyStoreRuleList": original_rules}
            updated_rules = command.call_args.args[0]["updateValue"]["energyStoreRuleList"]
            return {"mode": "1", "energyStoreRuleList": updated_rules}

        with patch.object(client, "get_energy_storage_schema", return_value=schema), \
                patch.object(
                    client,
                    "get_status",
                    return_value={"metrics": {"battery_soc": 24}},
                ), \
                patch.object(
                    client,
                    "read_energy_storage_mode",
                    side_effect=read_mode,
                ), \
                patch("run.socketio.Client", return_value=socket_client), \
                patch.object(client, "_execute_command", command):
            result = client.set_energy_storage_mode(
                "grid_charge",
                2000,
                confirmed=True,
            )

        params = command.call_args.args[0]["params"]
        charge_rule = params["energyStoreRuleList"][0]
        self.assertEqual(params["mode"], "1")
        self.assertEqual(charge_rule["touChargeStopSoc"], "95")
        self.assertEqual(charge_rule["touChargePower"], "2000")
        self.assertEqual(charge_rule["touStartTimestamp"], charge_rule["touEndTimestamp"])
        self.assertRegex(charge_rule["touStartChargeTimestamp"], r"^\d{2}:\d{2}$")
        self.assertEqual(charge_rule["touEndChargeTimestamp"], "23:59")
        self.assertEqual([rule["touEnable"] for rule in params["energyStoreRuleList"][1:]], ["0", "0", "0"])
        self.assertEqual(command.call_args.args[0]["originValue"]["energyStoreRuleList"], original_rules)
        self.assertEqual(result["mode"], "1")
        self.assertTrue(result["verified"])
        socket_client.connect.assert_called_once()
        socket_client.disconnect.assert_called_once()

    def test_charge_is_not_reported_successful_when_schedule_was_ignored(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        client._access_token = "test-token"
        client._token_expires_at = 9999999999
        original_rules = sample_tou_rules()
        schema = {
            "modes": [{"value": "0"}, {"value": "1"}],
            "fields": [
                "touEnable",
                "touChargeStopSoc",
                "touChargePower",
                "touStartChargeTimestamp",
                "touEndChargeTimestamp",
                "touStartTimestamp",
                "touEndTimestamp",
                "touEnableWeek",
            ],
        }
        with patch.object(client, "get_energy_storage_schema", return_value=schema), \
                patch.object(client, "get_status", return_value={"metrics": {"battery_soc": 24}}), \
                patch.object(
                    client,
                    "read_energy_storage_mode",
                    side_effect=[
                        {"mode": "1", "energyStoreRuleList": original_rules},
                        {"mode": "1", "energyStoreRuleList": original_rules},
                    ],
                ), \
                patch("run.socketio.Client"), \
                patch.object(client, "_execute_command", return_value={"result": 1}):
            with self.assertRaisesRegex(
                SofarCloudError,
                "requested charging schedule was not applied",
            ) as raised:
                client.set_energy_storage_mode("grid_charge", 2000, confirmed=True)

        self.assertEqual(raised.exception.stage, "mode verification")

    def test_discharge_is_blocked_at_or_below_the_soc_floor_before_any_write(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        schema = {
            "model": "HYD 5-20KTL-3PH",
            "modes": [{"value": "0"}, {"value": "1"}, {"value": "2"}],
            "fields": ["passiveManualBup", "passiveManualBlo"],
        }
        with patch("run.ENABLE_PASSIVE_DISCHARGE", True), \
                patch.object(client, "get_energy_storage_schema", return_value=schema), \
                patch.object(
                    client,
                    "get_status",
                    return_value={"metrics": {"battery_soc": 30}},
                ), \
                patch.object(client, "read_energy_storage_mode") as read_mode, \
                patch.object(client, "_execute_command") as command:
            with self.assertRaisesRegex(SofarCloudError, "above 30%"):
                client.set_energy_storage_mode(
                    "grid_discharge",
                    2500,
                    confirmed=True,
                )

        read_mode.assert_not_called()
        command.assert_not_called()

    def test_discharge_sends_and_verifies_passive_power_even_when_mode_is_not_listed(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        client._access_token = "test-token"
        client._token_expires_at = 9999999999
        schema = {
            "model": "HYD 5-20KTL-3PH",
            "modes": [{"value": "0"}, {"value": "1"}, {"value": "2"}],
            "fields": ["passiveManualBup", "passiveManualBlo"],
        }
        socket_client = MagicMock()
        socket_client.connected = True
        command = MagicMock(return_value={"result": 1})
        mode_reads = []

        def read_mode():
            if not mode_reads:
                mode_reads.append(True)
                return {"mode": "0"}
            update = command.call_args.args[0]["updateValue"]
            return {
                "mode": "3",
                "passiveManualBup": update["passiveManualBup"],
                "passiveManualBlo": update["passiveManualBlo"],
            }

        with patch("run.ENABLE_PASSIVE_DISCHARGE", True), \
                patch.object(client, "get_energy_storage_schema", return_value=schema), \
                patch.object(
                    client,
                    "get_status",
                    return_value={"metrics": {"battery_soc": 73}},
                ), \
                patch.object(client, "read_energy_storage_mode", side_effect=read_mode), \
                patch("run.socketio.Client", return_value=socket_client), \
                patch.object(client, "_execute_command", command):
            result = client.set_energy_storage_mode(
                "grid_discharge",
                2500,
                confirmed=True,
            )

        params = command.call_args.args[0]["params"]
        self.assertEqual(params["mode"], "3")
        self.assertEqual(params["passiveManualBup"], -2500)
        self.assertEqual(params["passiveManualBlo"], -2500)
        self.assertEqual(command.call_args.args[0]["updateValue"]["mode"], "3")
        self.assertEqual(result["control_mode"], "passive")
        self.assertEqual(result["mode"], "3")
        self.assertEqual(result["target_soc"], 30)
        self.assertEqual(result["start_soc"], 73)
        self.assertEqual(result["estimated_energy_kwh"], 2.15)
        self.assertEqual(result["estimated_duration_minutes"], 51.6)
        self.assertTrue(result["verified"])
        socket_client.connect.assert_called_once()
        socket_client.disconnect.assert_called_once()

    def test_passive_discharge_is_blocked_for_unrecognized_model_before_write(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        schema = {
            "model": "HYD 5KTL-3PH",
            "modes": [{"value": "0"}, {"value": "1"}, {"value": "2"}],
            "fields": ["passiveManualBup", "passiveManualBlo"],
        }
        with patch("run.ENABLE_PASSIVE_DISCHARGE", True), \
                patch.object(client, "get_energy_storage_schema", return_value=schema), \
                patch.object(client, "get_status") as get_status, \
                patch.object(client, "read_energy_storage_mode") as read_mode, \
                patch.object(client, "_execute_command") as command:
            with self.assertRaisesRegex(SofarCloudError, "unavailable"):
                client.set_energy_storage_mode(
                    "grid_discharge",
                    2500,
                    confirmed=True,
                )

        get_status.assert_not_called()
        read_mode.assert_not_called()
        command.assert_not_called()

    def test_passive_discharge_is_disabled_by_default_before_write(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        schema = {
            "model": "HYD 5-20KTL-3PH",
            "modes": [{"value": "0"}, {"value": "1"}, {"value": "2"}],
            "fields": ["passiveManualBup", "passiveManualBlo"],
        }
        with patch("run.ENABLE_PASSIVE_DISCHARGE", False), \
                patch.object(client, "get_energy_storage_schema", return_value=schema), \
                patch.object(client, "get_status") as get_status, \
                patch.object(client, "read_energy_storage_mode") as read_mode, \
                patch.object(client, "_execute_command") as command:
            with self.assertRaisesRegex(SofarCloudError, "disabled"):
                client.set_energy_storage_mode(
                    "grid_discharge",
                    2500,
                    confirmed=True,
                )

        get_status.assert_not_called()
        read_mode.assert_not_called()
        command.assert_not_called()

    def test_passive_discharge_readback_must_match_signed_power(self):
        client = SofarCloudClient("test@example.com", "test-password", "plant-123")
        self.assertTrue(
            client._passive_discharge_matches(
                {
                    "passiveManualBup": "-2500",
                    "passiveManualBlo": -2500.0,
                },
                {
                    "passiveManualBup": -2500,
                    "passiveManualBlo": -2500,
                },
            )
        )
        self.assertFalse(
            client._passive_discharge_matches(
                {"passiveManualBup": 2500, "passiveManualBlo": -2500},
                {"passiveManualBup": -2500, "passiveManualBlo": -2500},
            )
        )

    def test_discharge_watchdog_returns_to_self_use_at_soc_floor(self):
        watchdog = DischargeWatchdog()
        client = MagicMock()
        client.get_status.return_value = {"metrics": {"battery_soc": 30}}
        deadline = (
            datetime.now(ZoneInfo(TIMEZONE)) + timedelta(minutes=5)
        ).isoformat()

        watchdog._monitor(client, deadline, Event())

        client.set_energy_storage_mode.assert_called_once_with(
            "self_use",
            confirmed=True,
        )
        state = watchdog.get_state()
        self.assertFalse(state["active"])
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["stop_reason"], "target_soc_reached")

    def test_discharge_watchdog_fails_closed_when_soc_cannot_be_read(self):
        watchdog = DischargeWatchdog()
        client = MagicMock()
        client.get_status.side_effect = SofarCloudError(
            "Telemetry unavailable.",
            "station detail",
        )
        deadline = (
            datetime.now(ZoneInfo(TIMEZONE)) + timedelta(minutes=5)
        ).isoformat()

        watchdog._monitor(client, deadline, Event())

        client.set_energy_storage_mode.assert_called_once_with(
            "self_use",
            confirmed=True,
        )
        self.assertEqual(
            watchdog.get_state()["stop_reason"],
            "telemetry_unavailable",
        )


class DashboardAuthenticationTests(unittest.TestCase):
    def test_basic_auth_accepts_configured_credentials(self):
        from run import DashboardHandler

        credentials = base64.b64encode(b"operator:private-password").decode("ascii")
        handler = unittest.mock.Mock()
        handler.headers = {"Authorization": f"Basic {credentials}"}
        with patch("run.WEB_USERNAME", "operator"), patch(
            "run.WEB_PASSWORD", "private-password"
        ):
            self.assertTrue(DashboardHandler._is_authorized(handler))
        handler.send_response.assert_not_called()

    def test_basic_auth_rejects_missing_credentials_with_challenge(self):
        from run import DashboardHandler

        handler = unittest.mock.Mock()
        handler.headers = {}
        with patch("run.WEB_USERNAME", "operator"), patch(
            "run.WEB_PASSWORD", "private-password"
        ):
            self.assertFalse(DashboardHandler._is_authorized(handler))
        handler.send_response.assert_called_once_with(401)
        handler.send_header.assert_any_call("WWW-Authenticate", 'Basic realm="Sofar Opti"')

    def test_non_loopback_bind_requires_authentication(self):
        from run import main

        with patch("run.WEB_HOST", "0.0.0.0"), patch(
            "run.WEB_USERNAME", ""
        ), patch("run.WEB_PASSWORD", ""), patch("run.ThreadingHTTPServer") as server:
            with self.assertRaisesRegex(SystemExit, "required when SOFAR_WEB_HOST"):
                main()
        server.assert_not_called()


if __name__ == "__main__":
    unittest.main()
