"""P4 : réponses non ambiguës (allures M:SS, Body Battery, météo, pauses, FC repos)."""

from __future__ import annotations

import sqlite3
from datetime import date
from unittest.mock import MagicMock

import pytest

from src.db import get_connection, init_db
from src.garmin_client import GarminClient
from src.tools import garmin_tools as gt
from tests.synthetic import END_REF, PAUSES_REF, interval_run

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
    gc._garmin.get_user_summary.return_value = {}
    gc._logged_in = True
    return gc


@pytest.fixture
def tools(client: GarminClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(gt, "today_paris", lambda: TODAY)
    return gt.build_garmin_tools(client, conn)


# -- allures M:SS ---------------------------------------------------------------------


def test_p4_recent_activities_pace_mmss(client: GarminClient) -> None:
    speed_443 = 1000 / (4.43 * 60)  # 4,43 min/km décimales = 4:26/km
    client._garmin.get_activities.return_value = [
        {
            "activityId": 24725112038,
            "startTimeLocal": "2026-10-14 18:30:00",
            "activityType": {"typeKey": "running"},
            "averageSpeed": speed_443,
            "maxSpeed": 1000 / (3.5 * 60),
        }
    ]

    act = client.get_recent_activities(date(2026, 10, 10), fetch_limit=20)[0]

    assert act["allure_moyenne_min_km"] == 4.43  # champ historique conservé
    assert act["allure_moyenne_mmss"] == "4:26"
    assert act["allure_rapide_mmss"] == "3:30"


def test_p4_swim_pace_mmss(client: GarminClient) -> None:
    client._garmin.get_activities.return_value = [
        {
            "activityId": 24748453127,
            "startTimeLocal": "2026-10-16 12:00:00",
            "activityType": {"typeKey": "lap_swimming"},
            "averageSpeed": 100 / (1.74 * 60),
        }
    ]

    act = client.get_recent_activities(date(2026, 10, 10), fetch_limit=20)[0]

    assert act["allure_moyenne_min_100m"] == 1.74
    assert act["allure_moyenne_100m_mmss"] == "1:44"


def test_p4_lactate_threshold_pace_mmss(client: GarminClient) -> None:
    client._garmin.get_lactate_threshold.return_value = {
        "speed_and_heart_rate": {"heartRate": 177, "speed": 0.36944341},
        "power": {},
    }

    running = client.get_lactate_threshold()["running"]

    assert running["pace_min_per_km"] == 4.51
    assert running["pace_mmss"] == "4:31"


# -- Body Battery ----------------------------------------------------------------------


async def test_p4_body_battery_semantics(tools: dict, client: GarminClient) -> None:
    client._garmin.get_body_battery.return_value = [
        {
            "charged": 62,
            "drained": 48,
            "bodyBatteryValuesArray": [[1760652000000, 30], [1760677200000, 88], [1760702400000, 41]],
        }
    ]
    client._garmin.get_user_summary.return_value = {"bodyBatteryAtWakeTime": 85}

    result = await tools["garmin_get_body_battery"]()

    assert result["at_wake"] == 85
    assert result["min"] == 30
    assert result["max"] == 88
    assert result["latest"] == result["value"] == 41
    assert result["latest_at"].startswith("2025-10-17T14:00:00")
    assert (result["charged"], result["drained"]) == (62, 48)


async def test_p4_body_battery_summary_failure_is_not_blocking(tools: dict, client: GarminClient) -> None:
    client._garmin.get_body_battery.return_value = [{"bodyBatteryValuesArray": [[1760652000000, 30]]}]
    client._garmin.get_user_summary.side_effect = RuntimeError("boom")

    result = await tools["garmin_get_body_battery"]()

    assert result["status"] == "ok"
    assert result["at_wake"] is None


# -- météo -----------------------------------------------------------------------------


def _weather(client: GarminClient, *, start: tuple[float, float] | None, payload_coords: bool = False) -> None:
    payload = {
        "temp": 59,
        "windSpeed": 2,
        "windGust": None,
        "windDirectionCompassPoint": "sw",
        "weatherStationDTO": {"id": "LFPV", "name": "Villacoublay"},
        "weatherTypeDTO": {"desc": "Cloudy"},
    }
    if payload_coords:
        payload.update({"latitude": 48.85, "longitude": 2.35})
    client._garmin.get_activity_weather.return_value = payload
    summary = {"startLatitude": start[0], "startLongitude": start[1]} if start else {}
    client._garmin.get_activity.return_value = {"summaryDTO": summary}


def test_p4_weather_wind_in_kmh_and_gust_null(client: GarminClient) -> None:
    _weather(client, start=(48.80, 2.17))

    result = client.get_activity_weather("24725112038")

    assert result["wind_speed_raw"] == 2  # champ historique conservé
    assert result["wind_speed_kmh"] == 2  # unité native Garmin : km/h, pas de conversion
    assert result["wind_gust_kmh"] is None


def test_p4_weather_station_distance_close(client: GarminClient) -> None:
    _weather(client, start=(48.80, 2.17))  # ~3,6 km de Villacoublay

    result = client.get_activity_weather("24725112038")

    assert result["station_id"] == "LFPV"
    assert 2 < result["station_distance_km"] < 6
    assert result["station_far"] is False


def test_p4_weather_station_far_is_flagged(client: GarminClient) -> None:
    _weather(client, start=(48.40, 2.70))  # Fontainebleau, ~50 km

    result = client.get_activity_weather("1")

    assert result["station_distance_km"] > 15
    assert result["station_far"] is True


def test_p4_weather_payload_coords_take_precedence(client: GarminClient) -> None:
    _weather(client, start=(48.85, 2.35), payload_coords=True)

    result = client.get_activity_weather("1")

    assert result["station_distance_km"] == 0.0


def test_p4_weather_without_start_coords(client: GarminClient) -> None:
    _weather(client, start=None)  # ex. séance sur tapis : pas de GPS

    result = client.get_activity_weather("1")

    assert result["station_distance_km"] is None
    assert result["station_far"] is None


# -- détails d'activité : temps écoulé vs en mouvement ---------------------------------


async def test_p4_activity_details_exposes_pauses(tools: dict, client: GarminClient, monkeypatch) -> None:
    monkeypatch.setattr(client, "get_activity_details", lambda _id: interval_run())

    full = await tools["garmin_get_activity_details"]("24725112038")
    small = await tools["garmin_get_activity_details"]("24725112038", resolution_s=5)

    for result in (full, small):
        assert result["elapsed_s"] == END_REF
        assert result["moving_s"] == 3083
        assert [(p["debut_s"], p["fin_s"]) for p in result["pauses"]] == PAUSES_REF


# -- FC repos : horodatage ---------------------------------------------------------------


async def test_p4_resting_hr_timestamp_and_provisional(tools: dict, client: GarminClient) -> None:
    client._garmin.get_rhr_day.return_value = {
        "allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [{"value": 46, "calendarDate": "2026-10-17"}]}}
    }
    client._garmin.get_user_summary.return_value = {"lastSyncTimestampGMT": "2026-10-17T05:12:00.0"}

    today = await tools["garmin_get_resting_hr"]()
    past = await tools["garmin_get_resting_hr"](date="2026-10-13")

    assert today["resting_hr"] == 46
    assert today["measured_on"] == "2026-10-17"
    assert today["last_sync_at"] == "2026-10-17T07:12:00+02:00"
    assert today["provisoire"] is True
    assert past["provisoire"] is False
