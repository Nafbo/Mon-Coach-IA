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


# -- analyse, natation, formats -------------------------------------------------------------

from src.analysis import series as an  # noqa: E402
from src.analysis import swim  # noqa: E402
from tests.synthetic import END_REF, PAUSES_REF, interval_run, swim_session  # noqa: E402


@pytest.mark.parametrize(("value", "text"), [(-12, "−12 bpm"), (3, "+3 bpm"), (0, "0 bpm")])
def test_bpm_delta_format(value: int, text: str) -> None:
    assert an.format_bpm_delta(value) == text
    assert "--" not in an.format_bpm_delta(value)


def test_analysis_signed_recovery_and_pauses() -> None:
    details = interval_run()
    details["heart_rate_bpm"] = [
        185 if 900 <= t < 2400 and (t - 900) % 300 >= 180 else h
        for t, h in zip(details["time_s"], details["heart_rate_bpm"])
    ]

    result = an.analyse(details, 177)
    recup = result["recuperations"][0]

    assert recup["variation_fc_60s_bpm"] > 0
    assert recup["variation_fc_60s"].startswith("+")
    assert result["elapsed_s"] == END_REF
    assert result["moving_s"] == 3083
    assert [(p["start_s"], p["duration_s"]) for p in result["pauses"]] == [(2558, 134), (2832, 89)]


def test_analysis_drop_is_negative_with_typographic_minus() -> None:
    recup = an.analyse(interval_run(), 177)["recuperations"][0]

    assert recup["variation_fc_60s_bpm"] < 0
    assert recup["variation_fc_60s"].startswith("−")
    assert recup["variation_fc_60s_bpm"] == -recup["baisse_60s_bpm"]


def test_analysis_pace_aliases_and_no_raw_series() -> None:
    result = an.analyse(interval_run(), 177)

    assert result["vue_ensemble"]["pace_mmss"] == result["vue_ensemble"]["allure_moy_mmss"]
    assert result["blocs"][0]["pace_min_per_km"] == result["blocs"][0]["allure"]
    for key in ("time_s", "heart_rate_bpm", "pace_min_per_km", "elevation_m"):
        assert key not in result


def test_swim_roles_rest_and_totals() -> None:
    result = swim.annotate(swim_session())
    blocks = result["blocks"]

    assert blocks[0]["role"] == "echauffement"
    assert blocks[-1]["role"] == "retour_au_calme"
    assert {b["role"] for b in blocks[1:-1]} == {"serie"}
    assert blocks[0]["repos_avant_s"] is None
    assert blocks[1]["repos_avant_s"] == blocks[0]["repos_apres_s"]
    assert blocks[0]["pace_mmss"] == blocks[0]["allure_mmss"]
    totals = result["totaux"]
    assert totals["distance_m"] == 1800
    assert totals["temps_nage_s"] == pytest.approx(sum(s["duration_s"] for s in swim_session()), abs=0.5)
    assert totals["temps_repos_s"] == pytest.approx(30 + 7 * 20 + 30 + 30, abs=8)
    assert totals["temps_total_s"] == pytest.approx(totals["temps_nage_s"] + totals["temps_repos_s"], abs=0.2)
    assert all("pace_mmss" in s for s in result["splits"])


def test_weather_unit_and_course_pace_aliases(client: GarminClient) -> None:
    weather = client.get_activity_weather("1")
    activity = client.get_recent_activities(date(2026, 10, 10), fetch_limit=20)[0]

    assert weather["wind_speed_unit"] == "km/h"
    assert weather["wind_speed_kmh"] == 2
    assert activity["pace_min_per_km"] == activity["allure_moyenne_min_km"]
    assert activity["pace_mmss"] == activity["allure_moyenne_mmss"] == "6:31"
