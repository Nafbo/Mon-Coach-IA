"""Fiabilisation de l'accès : garmin_health_check, erreurs typées, disponibilité
ok/zero/empty/error et dernière synchro de la montre (mission du 17/10/2026)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from src.db import get_connection, init_db
from src.garmin_client import GarminClient
from src.models import PARIS_TZ
from src.tools import garmin_tools as gt
from tests.garmin_fakes import healthy_client

TODAY = date(2026, 10, 17)
HEALTH_KEYS = {"status", "error", "checked_at", "last_sync", "latest_activity", "data_availability"}


@pytest.fixture
def client(tmp_path) -> GarminClient:
    return healthy_client(tmp_path)


@pytest.fixture
def tools(client: GarminClient, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(gt, "today_paris", lambda: TODAY)
    conn = get_connection(":memory:")
    init_db(conn)
    yield gt.build_garmin_tools(client, conn)
    conn.close()


# -- garmin_health_check : forme complète -----------------------------------------------


async def test_health_ok_full_shape(tools: dict) -> None:
    result = await tools["garmin_health_check"]()

    assert HEALTH_KEYS <= set(result)
    assert result["status"] == "ok"
    assert result["error"] is None
    assert datetime.fromisoformat(result["checked_at"]).utcoffset() in (timedelta(hours=1), timedelta(hours=2))
    assert result["last_sync"]["source"] == "device_last_used"
    assert result["last_sync"]["at"].endswith("+02:00")
    assert isinstance(result["last_sync"]["age_minutes"], int)
    assert result["latest_activity"] == {
        "id": 24737274389,
        "start": "2026-10-15T19:41:12+02:00",
        "type": "course",
    }
    assert result["data_availability"] == {
        "activities": "ok",
        "sleep": "ok",
        "body_battery": "ok",
        "resting_hr": "ok",
    }


@pytest.mark.parametrize(
    ("exc", "expected_type"),
    [
        (GarminConnectAuthenticationError("Token expired"), "auth"),
        (GarminConnectConnectionError("API Error 401 - Failed to retrieve social profile"), "auth"),
        (GarminConnectTooManyRequestsError("Mobile login returned 429"), "rate_limit"),
        (GarminConnectConnectionError("Connection timed out"), "network"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_health_login_failure_is_typed_and_complete(tools: dict, client: GarminClient, exc, expected_type) -> None:
    client._logged_in = False
    client._garmin.login.side_effect = exc

    result = await tools["garmin_health_check"]()

    assert HEALTH_KEYS <= set(result)
    assert result["status"] == "error"
    assert result["error"]["type"] == expected_type
    assert str(exc) in result["error"]["message"]
    assert result["latest_activity"] is None
    assert set(result["data_availability"].values()) == {"error"}


async def test_health_expired_token_on_call(tools: dict, client: GarminClient) -> None:
    """Jeton refusé en cours de route (après re-login) : erreur typée auth, pas une liste vide."""
    client._garmin.get_activities.side_effect = GarminConnectAuthenticationError("Token expired")

    result = await tools["garmin_health_check"]()

    assert result["status"] == "error"
    assert result["error"] == {"type": "auth", "message": "Token expired"}
    assert result["data_availability"]["activities"] == "error"
    assert result["latest_activity"] is None
    assert result["data_availability"]["sleep"] == "ok"


async def test_health_never_raises(tools: dict, client: GarminClient, monkeypatch) -> None:
    monkeypatch.setattr(client, "get_health", lambda today: (_ for _ in ()).throw(RuntimeError("bug")))

    result = await tools["garmin_health_check"]()

    assert HEALTH_KEYS <= set(result)
    assert result["status"] == "error"
    assert result["error"]["type"] == "unknown"


async def test_health_zero_vs_empty(tools: dict, client: GarminClient) -> None:
    client._garmin.get_activities.return_value = []
    client._garmin.get_sleep_data.return_value = {"dailySleepDTO": {"sleepTimeSeconds": 0}}
    client._garmin.get_body_battery.return_value = [{"bodyBatteryValuesArray": [[1760652000000, 0], [1760652060000, 0]]}]
    client._garmin.get_rhr_day.return_value = {"allMetrics": {"metricsMap": {}}}

    result = await tools["garmin_health_check"]()

    assert result["status"] == "ok"
    assert result["latest_activity"] is None
    assert result["data_availability"] == {
        "activities": "empty",
        "sleep": "zero",
        "body_battery": "zero",
        "resting_hr": "empty",
    }


async def test_health_resting_hr_zero(tools: dict, client: GarminClient) -> None:
    client._garmin.get_rhr_day.return_value = {
        "allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [{"value": 0}]}}
    }
    client._garmin.get_sleep_data.return_value = {"dailySleepDTO": {}}

    result = await tools["garmin_health_check"]()

    assert result["data_availability"]["resting_hr"] == "zero"
    assert result["data_availability"]["sleep"] == "empty"


async def test_last_sync_falls_back_to_user_summary(tools: dict, client: GarminClient) -> None:
    client._garmin.get_device_last_used.return_value = {}

    result = await tools["garmin_health_check"]()

    assert result["last_sync"]["source"] == "user_summary"
    assert result["last_sync"]["at"] == "2026-10-17T07:12:00+02:00"


# -- statut explicite dans les outils de lecture ---------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected_type"),
    [
        (GarminConnectAuthenticationError("Token expired"), "auth"),
        (GarminConnectConnectionError("Connection reset by peer"), "network"),
        (GarminConnectTooManyRequestsError("429 Too Many Requests"), "rate_limit"),
    ],
)
async def test_recent_activities_error_never_looks_empty(tools: dict, client: GarminClient, exc, expected_type) -> None:
    client._garmin.get_activities.side_effect = exc

    result = await tools["garmin_get_recent_activities"](days=1)

    assert result["status"] == "error"
    assert result["error"] is True
    assert result["error_type"] == expected_type
    assert result["error_detail"] == {"type": expected_type, "message": result["message"]}
    assert "activities" not in result


async def test_recent_activities_empty_is_no_data_with_last_sync(tools: dict, client: GarminClient) -> None:
    client._garmin.get_activities.return_value = []

    result = await tools["garmin_get_recent_activities"](days=1)

    assert result["status"] == "no_data"
    assert result["activities"] == []
    assert result["last_sync"]["at"] is not None
    assert result["last_sync"]["device"] == "Forerunner 265"


async def test_recent_activities_last_sync_failure_does_not_hide_activities(tools: dict, client: GarminClient) -> None:
    client._garmin.get_device_last_used.side_effect = GarminConnectConnectionError("timeout")
    client._garmin.get_user_summary.side_effect = GarminConnectConnectionError("timeout")

    result = await tools["garmin_get_recent_activities"](days=7)

    assert result["status"] == "ok"
    assert len(result["activities"]) == 1
    assert result["last_sync"]["status"] == "error"
    assert result["last_sync"]["error_type"] == "network"


async def test_recent_activities_force_refresh(tools: dict, client: GarminClient) -> None:
    await tools["garmin_get_recent_activities"](days=7)
    await tools["garmin_get_recent_activities"](days=7)
    await tools["garmin_get_recent_activities"](days=7, force_refresh=True)

    assert client._garmin.get_activities.call_count == 2


async def test_sync_returns_last_sync(tools: dict) -> None:
    result = await tools["garmin_sync"]()

    assert result["status"] == "ok"
    assert result["last_sync"]["at"].endswith("+02:00")
    assert result["last_sync"]["source"] == "device_last_used"


async def test_sync_error_is_typed(tools: dict, client: GarminClient) -> None:
    client._garmin.get_training_status.side_effect = GarminConnectAuthenticationError("Token expired")

    result = await tools["garmin_sync"]()

    assert result["status"] == "error"
    assert result["error_type"] == "auth"


@pytest.mark.parametrize("tool", ["garmin_get_sleep", "garmin_get_body_battery", "garmin_get_resting_hr"])
async def test_wellness_network_error_is_typed(tools: dict, client: GarminClient, tool: str) -> None:
    for name in ("get_sleep_data", "get_body_battery", "get_rhr_day"):
        getattr(client._garmin, name).side_effect = GarminConnectConnectionError("Network is unreachable")

    result = await tools[tool]()

    assert result["status"] == "error"
    assert result["error_type"] == "network"
    assert "Network is unreachable" in result["message"]


async def test_wellness_zero_values_are_no_data(tools: dict, client: GarminClient) -> None:
    client._garmin.get_sleep_data.return_value = {"dailySleepDTO": {"sleepTimeSeconds": 0}}
    client._garmin.get_body_battery.return_value = [{"bodyBatteryValuesArray": [[1760652000000, 0]]}]
    client._garmin.get_rhr_day.return_value = {
        "allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [{"value": 0}]}}
    }

    sleep = await tools["garmin_get_sleep"]()
    bb = await tools["garmin_get_body_battery"]()
    rhr = await tools["garmin_get_resting_hr"]()

    for result in (sleep, bb, rhr):
        assert result["status"] == "no_data"
        assert result["availability"] == "zero"
    assert sleep["duration_minutes"] is None
    assert bb["value"] is None
    assert rhr["resting_hr"] is None


async def test_validation_error_type(tools: dict) -> None:
    result = await tools["garmin_get_sleep"](date="hier")
    assert result["error_type"] == "invalid_input"


# -- garmin_get_daily_snapshot ------------------------------------------------------------


async def test_snapshot_availability_and_last_sync(tools: dict, client: GarminClient) -> None:
    client._garmin.get_sleep_data.return_value = {"dailySleepDTO": {"sleepTimeSeconds": 0}}
    client._garmin.get_rhr_day.side_effect = GarminConnectConnectionError("timeout")

    result = await tools["garmin_get_daily_snapshot"]()

    assert result["last_sync"]["at"] is not None
    assert result["data_availability"]["sleep"] == "zero"
    assert result["data_availability"]["resting_hr"] == "error"
    assert result["data_availability"]["body_battery"] == "ok"
    assert result["data_availability"]["training_load"] == "ok"
    assert result["sleep"]["availability"] == "zero"


async def test_body_battery_times(tools: dict) -> None:
    result = await tools["garmin_get_body_battery"]()

    assert (result["min"], result["max"], result["latest"]) == (30, 88, 41)
    assert result["min_at"] == datetime.fromtimestamp(1760652000, tz=PARIS_TZ).isoformat()
    assert result["max_at"] == datetime.fromtimestamp(1760677200, tz=PARIS_TZ).isoformat()
    assert result["at_wake"] == 85
    assert result["at_wake_at"] == datetime.fromtimestamp(1760680800, tz=PARIS_TZ).isoformat()


async def test_body_battery_wake_time_null_without_night(tools: dict, client: GarminClient) -> None:
    client._garmin.get_user_summary.return_value = {"bodyBatteryAtWakeTime": None}

    result = await tools["garmin_get_body_battery"]()

    assert result["at_wake"] is None
    assert result["at_wake_at"] is None
