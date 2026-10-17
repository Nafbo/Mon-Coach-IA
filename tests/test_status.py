"""P3 : distinction erreur / absence de donnée / valeur réelle, et garmin_health_check."""

from __future__ import annotations

import sqlite3
from datetime import date
from unittest.mock import MagicMock

import pytest
from garminconnect import GarminConnectAuthenticationError, GarminConnectConnectionError

from src.db import get_connection, init_db
from src.garmin_client import GarminClient
from src.tools import garmin_tools as gt

TODAY = date(2026, 10, 17)


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = get_connection(":memory:")
    init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def client(tmp_path) -> GarminClient:
    gc = GarminClient("a@b.com", "pw", str(tmp_path / "tokenstore"))
    gc._garmin = MagicMock()
    gc._logged_in = True
    return gc


@pytest.fixture
def tools(client: GarminClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(gt, "today_paris", lambda: TODAY)
    return gt.build_garmin_tools(client, conn)


# -- no_data : valeurs null, jamais 0 ------------------------------------------------


async def test_p3_sleep_not_synced_is_no_data_with_nulls(tools: dict, client: GarminClient) -> None:
    client._garmin.get_sleep_data.return_value = {"dailySleepDTO": {"sleepTimeSeconds": None, "sleepScores": {}}}

    result = await tools["garmin_get_sleep"]()

    assert result["status"] == "no_data"
    assert result["duration_minutes"] is None
    assert result["deep_minutes"] is None
    assert result["score"] is None


async def test_p3_body_battery_empty_is_no_data(tools: dict, client: GarminClient) -> None:
    client._garmin.get_body_battery.return_value = []

    result = await tools["garmin_get_body_battery"]()

    assert result["date"] == "2026-10-17"
    assert result["status"] == "no_data"
    assert all(result[k] is None for k in ("value", "charged", "drained", "at_wake", "min", "max", "latest"))


async def test_p3_resting_hr_empty_is_no_data(tools: dict, client: GarminClient) -> None:
    client._garmin.get_rhr_day.return_value = {"allMetrics": {"metricsMap": {}}}

    result = await tools["garmin_get_resting_hr"]()

    assert result["status"] == "no_data"
    assert result["resting_hr"] is None


async def test_p3_real_value_is_ok(tools: dict, client: GarminClient) -> None:
    client._garmin.get_rhr_day.return_value = {
        "allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [{"value": 46.0}]}}
    }

    result = await tools["garmin_get_resting_hr"]()

    assert result["status"] == "ok"
    assert result["resting_hr"] == 46


async def test_p3_no_data_is_not_cached(tools: dict, client: GarminClient, conn: sqlite3.Connection) -> None:
    """Une nuit synchronisée en retard doit apparaître dès l'appel suivant."""
    client._garmin.get_sleep_data.side_effect = [
        {"dailySleepDTO": {}},
        {"dailySleepDTO": {"sleepTimeSeconds": 25200}},
    ]

    first = await tools["garmin_get_sleep"]()
    second = await tools["garmin_get_sleep"]()

    assert first["status"] == "no_data"
    assert second["status"] == "ok"
    assert second["duration_minutes"] == 420
    rows = conn.execute("SELECT COUNT(*) AS n FROM garmin_cache WHERE type = 'sleep'").fetchone()
    assert rows["n"] == 1


async def test_p3_force_refresh_bypasses_cache(tools: dict, client: GarminClient) -> None:
    client._garmin.get_activities.side_effect = [
        [{"activityId": 1, "startTimeLocal": "2026-10-16 18:00:00", "activityType": {"typeKey": "running"}}],
        [
            {"activityId": 2, "startTimeLocal": "2026-10-17 07:00:00", "activityType": {"typeKey": "running"}},
            {"activityId": 1, "startTimeLocal": "2026-10-16 18:00:00", "activityType": {"typeKey": "running"}},
        ],
    ]

    first = await tools["garmin_get_recent_activities"]()
    cached = await tools["garmin_get_recent_activities"]()
    refreshed = await tools["garmin_get_recent_activities"](force_refresh=True)

    assert len(first["activities"]) == len(cached["activities"]) == 1
    assert len(refreshed["activities"]) == 2


# -- error : message exact -------------------------------------------------------------


async def test_p3_error_has_status_and_exact_message(tools: dict, client: GarminClient) -> None:
    client._garmin.get_body_battery.side_effect = GarminConnectConnectionError("HTTP 503 Service Unavailable")

    result = await tools["garmin_get_body_battery"]()

    assert result["status"] == "error"
    assert result["error"] is True
    assert result["message"] == "Garmin Connect injoignable : HTTP 503 Service Unavailable"


async def test_p3_invalid_date_is_error(tools: dict) -> None:
    result = await tools["garmin_get_sleep"](date="17/10/2026")
    assert result == {"error": True, "status": "error", "message": "Date invalide : 17/10/2026"}


# -- garmin_health_check -------------------------------------------------------------


def _healthy(client: GarminClient) -> None:
    client._garmin.get_device_last_used.return_value = {
        "lastUsedDeviceName": "Forerunner 265",
        "lastUsedDeviceUploadTime": 1760682000000,  # 2025-10-17T08:20:00+02:00
    }
    client._garmin.get_activities.return_value = [
        {"activityId": 24748453127, "startTimeLocal": "2026-10-16 12:00:00", "activityType": {"typeKey": "lap_swimming"}}
    ]
    client._garmin.get_sleep_data.return_value = {
        "dailySleepDTO": {"sleepTimeSeconds": 25000, "sleepEndTimestampGMT": 1760680800000}
    }
    client._garmin.get_body_battery.return_value = [{"bodyBatteryValuesArray": [[1760680800000, 70], [1760682000000, 72]]}]
    client._garmin.get_rhr_day.return_value = {
        "allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [{"value": 46}]}}
    }


async def test_p3_health_check_all_ok(tools: dict, client: GarminClient) -> None:
    _healthy(client)

    result = await tools["garmin_health_check"]()

    assert result["status"] == "ok"
    assert result["connection"] == {"status": "ok"}
    assert result["last_sync"]["device"] == "Forerunner 265"
    assert result["last_sync"]["at"].endswith("+02:00")
    assert result["last_activity"] == {
        "status": "ok",
        "activity_id": 24748453127,
        "start_local": "2026-10-16 12:00:00",
        "type": "nage",
    }
    assert result["freshness"]["sleep"]["status"] == "ok"
    assert result["freshness"]["body_battery"]["last_point_at"] == result["last_sync"]["at"]
    assert result["freshness"]["resting_hr"] == {"status": "ok", "date": "2026-10-17"}
    assert "checked_at" in result


async def test_p3_health_check_isolates_failing_endpoint(tools: dict, client: GarminClient) -> None:
    _healthy(client)
    client._garmin.get_body_battery.side_effect = GarminConnectConnectionError("timeout")
    client._garmin.get_sleep_data.return_value = {"dailySleepDTO": {}}

    result = await tools["garmin_health_check"]()

    assert result["status"] == "error"
    assert result["freshness"]["body_battery"]["status"] == "error"
    assert "timeout" in result["freshness"]["body_battery"]["message"]
    assert result["freshness"]["sleep"] == {"status": "no_data", "sleep_end": None}
    assert result["last_activity"]["status"] == "ok"


async def test_p3_health_check_login_failure(tools: dict, client: GarminClient) -> None:
    client._logged_in = False
    client._garmin.login.side_effect = GarminConnectAuthenticationError("401")

    result = await tools["garmin_health_check"]()

    assert result["status"] == "error"
    assert result["connection"]["status"] == "error"
    assert "401" in result["connection"]["message"]


async def test_p3_sync_does_not_cache_no_data(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gt, "today_paris", lambda: TODAY)
    fake = MagicMock(spec=GarminClient)
    fake.sync_all.return_value = {"sleep": {"status": "no_data"}, "resting_hr": {"status": "ok", "resting_hr": 46}}
    tools = gt.build_garmin_tools(fake, conn)

    await tools["garmin_sync"]()

    types = {r["type"] for r in conn.execute("SELECT type FROM garmin_cache")}
    assert types == {"resting_hr"}

