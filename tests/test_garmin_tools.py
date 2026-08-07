"""Tests de src/tools/garmin_tools.py et src/garmin_client.py, garminconnect mocké (pas d'appel réel)."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from src.db import get_connection, init_db
from src.garmin_client import GarminAuthenticationError, GarminClient, GarminUnavailableError
from src.tools import garmin_tools as gt

# ---------------------------------------------------------------------------
# Tests de src/tools/garmin_tools.py : orchestration, cache, mapping d'erreurs.
# Le GarminClient est ici un double de test (spec'd sur la vraie classe) : garminconnect
# n'est jamais sollicité.
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = get_connection(":memory:")
    init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def fake_client() -> MagicMock:
    return MagicMock(spec=GarminClient)


@pytest.fixture
def tools(fake_client: MagicMock, conn: sqlite3.Connection) -> dict:
    return gt.build_garmin_tools(fake_client, conn)


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch: pytest.MonkeyPatch) -> date:
    fixed = date(2026, 8, 5)
    monkeypatch.setattr(gt, "today_paris", lambda: fixed)
    return fixed


async def test_training_status_fetches_then_uses_cache(tools: dict, fake_client: MagicMock) -> None:
    fake_client.get_training_status.return_value = {
        "date": "2026-08-05",
        "status": "PRODUCTIVE",
        "acute_load": 450,
        "chronic_load": 400,
    }

    first = await tools["garmin_get_training_status"]()
    second = await tools["garmin_get_training_status"]()

    assert first == {"date": "2026-08-05", "status": "PRODUCTIVE", "acute_load": 450, "chronic_load": 400}
    assert second == first
    fake_client.get_training_status.assert_called_once_with(date(2026, 8, 5))


async def test_cache_expired_triggers_refetch(tools: dict, fake_client: MagicMock, conn: sqlite3.Connection) -> None:
    fake_client.get_training_status.return_value = {
        "date": "2026-08-05",
        "status": "MAINTAINING",
        "acute_load": 100,
        "chronic_load": 120,
    }

    stale_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    conn.execute(
        "INSERT INTO garmin_cache (date, type, payload_json, fetched_at) VALUES (?, ?, ?, ?)",
        ("2026-08-05", "training_status", '{"status": "OLD", "acute_load": 1, "chronic_load": 1}', stale_time),
    )
    conn.commit()

    result = await tools["garmin_get_training_status"]()

    assert result["status"] == "MAINTAINING"
    fake_client.get_training_status.assert_called_once()


async def test_garmin_sync_forces_real_call_even_with_fresh_cache(
    tools: dict, fake_client: MagicMock, conn: sqlite3.Connection
) -> None:
    conn.execute(
        "INSERT INTO garmin_cache (date, type, payload_json, fetched_at) VALUES (?, ?, ?, ?)",
        ("2026-08-05", "training_status", '{"status": "CACHED"}', datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()

    fake_client.sync_all.return_value = {
        "training_status": {"date": "2026-08-05", "status": "PRODUCTIVE", "acute_load": 1, "chronic_load": 1},
        "body_battery": {"value": 80, "charged": 10, "drained": 5},
        "vo2max": {"running_vo2max": 52, "cycling_vo2max": None},
        "resting_hr": {"resting_hr": 48},
        "sleep": {
            "duration_minutes": 420,
            "deep_minutes": 90,
            "light_minutes": 250,
            "rem_minutes": 70,
            "awake_minutes": 10,
            "score": 80,
        },
        "recent_activities": {"activities": []},
        "training_load": {
            "date": "2026-08-05",
            "acute_load": 1,
            "chronic_load": 1,
            "load_ratio": 1.0,
            "load_status": "optimal",
        },
        "training_load_balance": {
            "date": "2026-08-05",
            "aerobie_faible": 392.6,
            "aerobie_faible_cible_min": 305,
            "aerobie_faible_cible_max": 706,
            "aerobie_elevee": 720.5,
            "aerobie_elevee_cible_min": 428,
            "aerobie_elevee_cible_max": 829,
            "charge_anaerobique": 44.9,
            "charge_anaerobique_cible_min": 133,
            "charge_anaerobique_cible_max": 400,
            "feedback": "ANAEROBIC_SHORTAGE",
        },
        "lactate_threshold": {"running": {"heart_rate": None, "pace_min_per_km": None}, "cycling": {"ftp_watts": None}},
        "intensity_minutes": {"moderate_minutes": 0, "vigorous_minutes": 0, "goal_minutes": 150},
    }

    result = await tools["garmin_sync"]()

    assert result["status"] == "ok"
    assert "synced_at" in result
    fake_client.sync_all.assert_called_once()

    rows = conn.execute("SELECT type, date FROM garmin_cache ORDER BY id").fetchall()
    types = {row["type"] for row in rows}
    assert types == {
        "training_status",
        "body_battery",
        "vo2max",
        "resting_hr",
        "sleep",
        "recent_activities",
        "training_load",
        "training_load_balance",
        "lactate_threshold",
        "intensity_minutes",
    }


async def test_garmin_sync_error_returns_error_dict(tools: dict, fake_client: MagicMock) -> None:
    fake_client.sync_all.side_effect = GarminAuthenticationError("Identifiants Garmin invalides : bad creds")

    result = await tools["garmin_sync"]()

    assert result["error"] is True
    assert "Impossible de se connecter à Garmin Connect" in result["message"]


async def test_body_battery_default_date_is_today(tools: dict, fake_client: MagicMock, frozen_today: date) -> None:
    fake_client.get_body_battery.return_value = {"value": 65, "charged": 40, "drained": 20}

    result = await tools["garmin_get_body_battery"]()

    assert result["date"] == frozen_today.isoformat()
    fake_client.get_body_battery.assert_called_once_with(frozen_today.isoformat())


async def test_body_battery_explicit_date(tools: dict, fake_client: MagicMock) -> None:
    fake_client.get_body_battery.return_value = {"value": 65, "charged": 40, "drained": 20}

    result = await tools["garmin_get_body_battery"](date="2026-07-01")

    assert result["date"] == "2026-07-01"
    fake_client.get_body_battery.assert_called_once_with("2026-07-01")


async def test_body_battery_invalid_date_returns_error(tools: dict, fake_client: MagicMock) -> None:
    result = await tools["garmin_get_body_battery"](date="not-a-date")

    assert result["error"] is True
    fake_client.get_body_battery.assert_not_called()


async def test_resting_hr_authentication_error(tools: dict, fake_client: MagicMock) -> None:
    fake_client.get_resting_hr.side_effect = GarminAuthenticationError("bad creds")

    result = await tools["garmin_get_resting_hr"]()

    assert result == {"error": True, "message": "Identifiants Garmin invalides : bad creds"}


async def test_sleep_unavailable_error(tools: dict, fake_client: MagicMock) -> None:
    fake_client.get_sleep.side_effect = GarminUnavailableError("Garmin Connect injoignable")

    result = await tools["garmin_get_sleep"]()

    assert result["error"] is True
    assert "injoignable" in result["message"]


async def test_recent_activities_default_days(tools: dict, fake_client: MagicMock, frozen_today: date) -> None:
    fake_client.get_recent_activities.return_value = [
        {"date": "2026-08-04", "type": "course", "duration_minutes": 30, "distance_km": 5.0, "avg_hr": 140, "max_hr": 160}
    ]

    result = await tools["garmin_get_recent_activities"]()

    assert result == {"activities": fake_client.get_recent_activities.return_value}
    called_since, kwargs = fake_client.get_recent_activities.call_args
    assert called_since[0] == frozen_today - timedelta(days=7)


async def test_intensity_minutes_defaults_to_current_week_monday(
    tools: dict, fake_client: MagicMock, frozen_today: date
) -> None:
    fake_client.get_intensity_minutes.return_value = {"moderate_minutes": 50, "vigorous_minutes": 20, "goal_minutes": 150}

    result = await tools["garmin_get_intensity_minutes"]()

    assert result["week_start_date"] == "2026-08-03"  # lundi de la semaine du 2026-08-05
    fake_client.get_intensity_minutes.assert_called_once_with(date(2026, 8, 3))


async def test_training_load_balance_uses_client_and_caches(
    tools: dict, fake_client: MagicMock, frozen_today: date
) -> None:
    fake_client.get_training_load_balance.return_value = {
        "date": "2026-08-03",
        "aerobie_faible": 392.6,
        "aerobie_faible_cible_min": 305,
        "aerobie_faible_cible_max": 706,
        "aerobie_elevee": 720.5,
        "aerobie_elevee_cible_min": 428,
        "aerobie_elevee_cible_max": 829,
        "charge_anaerobique": 44.9,
        "charge_anaerobique_cible_min": 133,
        "charge_anaerobique_cible_max": 400,
        "feedback": "ANAEROBIC_SHORTAGE",
    }

    first = await tools["garmin_get_training_load_balance"]()
    second = await tools["garmin_get_training_load_balance"]()

    assert first["feedback"] == "ANAEROBIC_SHORTAGE"
    assert first["date"] == "2026-08-03"  # date réellement trouvée, pas "aujourd'hui"
    assert second == first
    fake_client.get_training_load_balance.assert_called_once_with(frozen_today)


async def test_training_load_balance_error_handling(tools: dict, fake_client: MagicMock) -> None:
    fake_client.get_training_load_balance.side_effect = GarminUnavailableError("Garmin Connect injoignable")

    result = await tools["garmin_get_training_load_balance"]()

    assert result["error"] is True
    assert "injoignable" in result["message"]


async def test_vo2max_uses_client_and_caches(tools: dict, fake_client: MagicMock, frozen_today: date) -> None:
    fake_client.get_vo2max.return_value = {"date": "2026-08-03", "running_vo2max": 52, "cycling_vo2max": None}

    first = await tools["garmin_get_vo2max"]()
    second = await tools["garmin_get_vo2max"]()

    assert first == {"date": "2026-08-03", "running_vo2max": 52, "cycling_vo2max": None}
    assert second == first
    fake_client.get_vo2max.assert_called_once_with(frozen_today)


# ---------------------------------------------------------------------------
# Tests de src/garmin_client.py : parsing des réponses garminconnect (mockées),
# normalisation des types d'activité, distinction des erreurs d'authentification.
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path) -> GarminClient:
    gc = GarminClient("a@b.com", "pw", str(tmp_path / "tokenstore"))
    gc._garmin = MagicMock()
    gc._logged_in = True  # évite tout appel à login()
    return gc


def test_get_training_status_parses_acute_chronic_load(client: GarminClient) -> None:
    client._garmin.get_training_status.return_value = {
        "mostRecentTrainingStatus": {
            "latestTrainingStatusData": {
                "1234": {
                    "trainingStatusFeedbackPhrase": "PRODUCTIVE_1",
                    "acuteTrainingLoadDTO": {
                        "dailyTrainingLoadAcute": 480,
                        "dailyTrainingLoadChronic": 410,
                    },
                }
            }
        }
    }

    result = client.get_training_status(date(2026, 8, 5))

    assert result == {"date": "2026-08-05", "status": "PRODUCTIVE_1", "acute_load": 480, "chronic_load": 410}


def test_get_training_status_missing_data_falls_back(client: GarminClient) -> None:
    client._garmin.get_training_status.return_value = {}

    result = client.get_training_status(date(2026, 8, 5))

    assert result == {"date": "2026-08-05", "status": "unknown", "acute_load": 0, "chronic_load": 0}


def test_get_training_status_looks_back_when_today_empty(client: GarminClient) -> None:
    def side_effect(iso_date: str) -> dict:
        if iso_date == "2026-08-03":
            return {
                "mostRecentTrainingStatus": {
                    "latestTrainingStatusData": {
                        "1234": {"trainingStatusFeedbackPhrase": "PRODUCTIVE_2", "acuteTrainingLoadDTO": {}}
                    }
                }
            }
        return {}

    client._garmin.get_training_status.side_effect = side_effect

    result = client.get_training_status(date(2026, 8, 5))

    assert result["date"] == "2026-08-03"
    assert result["status"] == "PRODUCTIVE_2"
    assert client._garmin.get_training_status.call_count == 3  # 05, 04, puis 03 (trouvé)


def test_get_body_battery_reads_latest_value_from_array(client: GarminClient) -> None:
    client._garmin.get_body_battery.return_value = [
        {
            "charged": 45,
            "drained": 60,
            "bodyBatteryValuesArray": [[1000, 70], [2000, 65], [3000, 58]],
        }
    ]

    result = client.get_body_battery("2026-08-05")

    assert result == {"value": 58, "charged": 45, "drained": 60}


def test_get_sleep_converts_seconds_to_minutes(client: GarminClient) -> None:
    client._garmin.get_sleep_data.return_value = {
        "dailySleepDTO": {
            "sleepTimeSeconds": 25200,
            "deepSleepSeconds": 5400,
            "lightSleepSeconds": 14000,
            "remSleepSeconds": 4500,
            "awakeSleepSeconds": 1300,
            "sleepScores": {"overall": {"value": 78}},
        }
    }

    result = client.get_sleep("2026-08-05")

    assert result == {
        "duration_minutes": 420,
        "deep_minutes": 90,
        "light_minutes": 233,
        "rem_minutes": 75,
        "awake_minutes": 21,
        "score": 78,
    }


def test_get_recent_activities_normalizes_types_and_filters_by_date(client: GarminClient) -> None:
    client._garmin.get_activities.return_value = [
        {
            "startTimeLocal": "2026-08-05 07:00:00",
            "activityType": {"typeKey": "trail_running"},
            "duration": 1800,
            "distance": 5230.0,
            "averageHR": 145,
            "maxHR": 172,
            "elevationGain": 224.0,
            "averageSpeed": 2.358,
            "maxSpeed": 3.07,
            "trainingEffectLabel": "TEMPO",
            "aerobicTrainingEffect": 3.2,
            "aerobicTrainingEffectMessage": "IMPACTING_TEMPO_22",
            "anaerobicTrainingEffect": 0.0,
            "anaerobicTrainingEffectMessage": "NO_ANAEROBIC_BENEFIT_0",
        },
        {
            "startTimeLocal": "2026-08-01 07:00:00",  # trop ancien, doit être exclu
            "activityType": {"typeKey": "cycling"},
            "duration": 3600,
            "distance": 30000.0,
        },
        {
            "startTimeLocal": "2026-08-04 18:00:00",
            "activityType": {"typeKey": "some_future_garmin_type"},
            "duration": 600,
            "distance": None,
        },
    ]

    result = client.get_recent_activities(since=date(2026, 8, 3), fetch_limit=20)

    assert len(result) == 2
    assert result[0]["date"] == "2026-08-05"
    assert result[0]["type"] == "course"
    assert result[0]["garmin_type"] == "trail_running"
    assert result[0]["distance_km"] == 5.23
    assert result[0]["denivele_m"] == 224
    assert result[0]["allure_moyenne_min_km"] == 7.07
    assert result[0]["allure_rapide_min_km"] == 5.43
    assert result[0]["effet_entrainement"] == "TEMPO"
    assert result[0]["effet_aerobie"] == 3.2
    assert result[0]["effet_aerobie_message"] == "IMPACTING_TEMPO_22"
    assert result[0]["effet_anaerobie"] == 0.0
    assert result[0]["effet_anaerobie_message"] == "NO_ANAEROBIC_BENEFIT_0"
    assert result[1]["type"] == "some_future_garmin_type"  # type non reconnu : passé tel quel
    assert result[1]["allure_moyenne_min_km"] is None  # pas une "course" normalisée
    assert result[1]["denivele_m"] is None  # elevationGain absent
    assert result[0]["piscine_longueur_m"] is None  # pas une "nage"


def test_get_recent_activities_swim_fields(client: GarminClient) -> None:
    # Formes réelles observées sur un compte Garmin (cf. README).
    client._garmin.get_activities.return_value = [
        {
            "startTimeLocal": "2026-07-30 19:31:42",
            "activityType": {"typeKey": "lap_swimming"},
            "duration": 1923.88,
            "distance": 1275.0,
            "averageHR": 124,
            "maxHR": 151,
            "trainingEffectLabel": "RECOVERY",
            "poolLength": 2500.0,
            "unitOfPoolLength": {"unitId": 1, "unitKey": "meter", "factor": 100.0},
            "activeLengths": 51,
            "averageSwolf": 53.0,
            "averageSwimCadenceInStrokesPerMinute": 25.0,
            "avgStrokes": 15.7,
            "strokes": 802.0,
            "fastestSplit_100": 125.08,
            "averageSpeed": 0.663,
        }
    ]

    result = client.get_recent_activities(since=date(2026, 7, 1), fetch_limit=20)

    assert len(result) == 1
    swim = result[0]
    assert swim["type"] == "nage"
    assert swim["piscine_longueur_m"] == 25.0
    assert swim["nb_longueurs"] == 51
    assert swim["swolf_moyen"] == 53
    assert swim["cadence_moyenne_brasses_min"] == 25
    assert swim["brasses_moyenne_longueur"] == 15.7
    assert swim["brasses_total"] == 802
    assert swim["meilleur_100m_sec"] == 125.1
    assert swim["allure_moyenne_min_100m"] == 2.51
    # Pas de course : allure/dénivelé restent null.
    assert swim["allure_moyenne_min_km"] is None
    assert swim["denivele_m"] is None


def test_recent_activities_allure_100m_null_for_non_swim(client: GarminClient) -> None:
    client._garmin.get_activities.return_value = [
        {
            "startTimeLocal": "2026-08-05 07:00:00",
            "activityType": {"typeKey": "running"},
            "duration": 1800,
            "distance": 5000.0,
            "averageSpeed": 2.5,
        }
    ]

    result = client.get_recent_activities(since=date(2026, 8, 1), fetch_limit=20)

    assert result[0]["allure_moyenne_min_100m"] is None


def test_pool_length_converts_yards_to_meters(client: GarminClient) -> None:
    client._garmin.get_activities.return_value = [
        {
            "startTimeLocal": "2026-07-30 19:31:42",
            "activityType": {"typeKey": "lap_swimming"},
            "duration": 1000,
            "distance": 1000.0,
            "poolLength": 2500.0,
            "unitOfPoolLength": {"unitKey": "yard", "factor": 100.0},
        }
    ]

    result = client.get_recent_activities(since=date(2026, 7, 1), fetch_limit=20)

    assert result[0]["piscine_longueur_m"] == round(25 * 0.9144, 2)


def test_get_intensity_minutes_reads_goal_from_weekly_entry(client: GarminClient) -> None:
    # Formes réelles observées sur un compte Garmin (cf. README "Limites connues") :
    # get_weekly_intensity_minutes renvoie une seule entrée agrégée avec son propre "weeklyGoal".
    client._garmin.get_weekly_intensity_minutes.return_value = [
        {"calendarDate": "2026-08-03", "weeklyGoal": 150, "moderateValue": 5, "vigorousValue": 36}
    ]

    result = client.get_intensity_minutes(date(2026, 8, 3))

    assert result == {"moderate_minutes": 5, "vigorous_minutes": 36, "goal_minutes": 150}
    client._garmin.get_intensity_minutes_data.assert_not_called()


def test_get_intensity_minutes_falls_back_to_daily_week_goal_field(client: GarminClient) -> None:
    # Si l'entrée hebdomadaire ne porte pas de "weeklyGoal", repli sur get_intensity_minutes_data
    # dont le champ objectif réel s'appelle "weekGoal" (et non "weeklyGoal").
    client._garmin.get_weekly_intensity_minutes.return_value = [
        {"calendarDate": "2026-08-03", "moderateValue": 5, "vigorousValue": 36}
    ]
    client._garmin.get_intensity_minutes_data.return_value = {"weekGoal": 150}

    result = client.get_intensity_minutes(date(2026, 8, 3))

    assert result == {"moderate_minutes": 5, "vigorous_minutes": 36, "goal_minutes": 150}


def test_get_training_load_status_classification(client: GarminClient) -> None:
    client._garmin.get_training_status.return_value = {
        "mostRecentTrainingStatus": {
            "latestTrainingStatusData": {
                "1234": {"acuteTrainingLoadDTO": {"dailyTrainingLoadAcute": 700, "dailyTrainingLoadChronic": 400}}
            }
        }
    }

    result = client.get_training_load(date(2026, 8, 5))

    assert result["load_ratio"] == 1.75
    assert result["load_status"] == "high"


def test_get_training_load_reads_target_range_and_acwr(client: GarminClient) -> None:
    # Forme réelle observée sur un compte Garmin.
    client._garmin.get_training_status.return_value = {
        "mostRecentTrainingStatus": {
            "latestTrainingStatusData": {
                "1234": {
                    "acuteTrainingLoadDTO": {
                        "dailyTrainingLoadAcute": 412,
                        "dailyTrainingLoadChronic": 301,
                        "minTrainingLoadChronic": 240.8,
                        "maxTrainingLoadChronic": 451.5,
                        "acwrPercent": 57,
                        "acwrStatus": "OPTIMAL",
                    }
                }
            }
        }
    }

    result = client.get_training_load(date(2026, 8, 5))

    assert result["cible_charge_chronique_min"] == 240.8
    assert result["cible_charge_chronique_max"] == 451.5
    assert result["acwr_pourcentage"] == 57
    assert result["acwr_statut"] == "OPTIMAL"


def test_get_training_load_no_chronic_data_is_no_status(client: GarminClient) -> None:
    client._garmin.get_training_status.return_value = {}

    result = client.get_training_load(date(2026, 8, 5))

    assert result == {
        "date": "2026-08-05",
        "acute_load": 0,
        "chronic_load": 0,
        "load_ratio": 0.0,
        "load_status": "no_status",
        "cible_charge_chronique_min": None,
        "cible_charge_chronique_max": None,
        "acwr_pourcentage": None,
        "acwr_statut": None,
    }


def test_get_training_load_balance_parses_real_shape(client: GarminClient) -> None:
    # Forme réelle observée sur un compte Garmin ("Training Load Focus").
    client._garmin.get_training_status.return_value = {
        "mostRecentTrainingStatus": {"latestTrainingStatusData": {"1234": {}}},
        "mostRecentTrainingLoadBalance": {
            "metricsTrainingLoadBalanceDTOMap": {
                "3496379594": {
                    "monthlyLoadAerobicLow": 392.6215,
                    "monthlyLoadAerobicHigh": 720.48975,
                    "monthlyLoadAnaerobic": 44.898575,
                    "monthlyLoadAerobicLowTargetMin": 305,
                    "monthlyLoadAerobicLowTargetMax": 706,
                    "monthlyLoadAerobicHighTargetMin": 428,
                    "monthlyLoadAerobicHighTargetMax": 829,
                    "monthlyLoadAnaerobicTargetMin": 133,
                    "monthlyLoadAnaerobicTargetMax": 400,
                    "trainingBalanceFeedbackPhrase": "ANAEROBIC_SHORTAGE",
                }
            }
        },
    }

    result = client.get_training_load_balance(date(2026, 8, 5))

    assert result == {
        "date": "2026-08-05",
        "aerobie_faible": 392.6,
        "aerobie_faible_cible_min": 305,
        "aerobie_faible_cible_max": 706,
        "aerobie_elevee": 720.5,
        "aerobie_elevee_cible_min": 428,
        "aerobie_elevee_cible_max": 829,
        "charge_anaerobique": 44.9,
        "charge_anaerobique_cible_min": 133,
        "charge_anaerobique_cible_max": 400,
        "feedback": "ANAEROBIC_SHORTAGE",
    }


def test_get_training_load_balance_missing_data(client: GarminClient) -> None:
    client._garmin.get_training_status.return_value = {}

    result = client.get_training_load_balance(date(2026, 8, 5))

    assert result == {
        "date": "2026-08-05",
        "aerobie_faible": None,
        "aerobie_faible_cible_min": None,
        "aerobie_faible_cible_max": None,
        "aerobie_elevee": None,
        "aerobie_elevee_cible_min": None,
        "aerobie_elevee_cible_max": None,
        "charge_anaerobique": None,
        "charge_anaerobique_cible_min": None,
        "charge_anaerobique_cible_max": None,
        "feedback": None,
    }


def test_get_lactate_threshold_handles_missing_values(client: GarminClient) -> None:
    client._garmin.get_lactate_threshold.return_value = {}

    result = client.get_lactate_threshold()

    assert result == {
        "running": {"heart_rate": None, "pace_min_per_km": None, "power_watts": None, "power_watts_per_kg": None},
        "cycling": {"ftp_watts": None},
    }


def test_get_lactate_threshold_parses_real_running_shape(client: GarminClient) -> None:
    # Forme réelle observée sur un compte Garmin : "speed" est à une échelle 10x
    # inférieure au m/s (0.36944341 -> 4:31/km, validé contre l'app Garmin : 4:30/km).
    client._garmin.get_lactate_threshold.return_value = {
        "speed_and_heart_rate": {
            "calendarDate": "2026-08-03T19:18:15.496",
            "speed": 0.36944341,
            "heartRate": 177,
            "heartRateCycling": None,
        },
        "power": {
            "calendarDate": "2026-07-29T20:44:32.0",
            "sport": "RUNNING",
            "functionalThresholdPower": 444,
            "weight": 86.0,
            "powerToWeight": 5.162790697674419,
        },
    }

    result = client.get_lactate_threshold()

    assert result["running"]["heart_rate"] == 177
    assert result["running"]["pace_min_per_km"] == 4.51
    assert result["running"]["power_watts"] == 444
    assert result["running"]["power_watts_per_kg"] == 5.16
    assert result["cycling"]["ftp_watts"] is None  # "power" ici concerne la course, pas le vélo


def test_get_lactate_threshold_parses_real_cycling_shape(client: GarminClient) -> None:
    client._garmin.get_lactate_threshold.return_value = {
        "speed_and_heart_rate": {"speed": None, "heartRate": None, "heartRateCycling": 165},
        "power": {"sport": "CYCLING", "functionalThresholdPower": 250, "weight": 86.0, "powerToWeight": 2.9},
    }

    result = client.get_lactate_threshold()

    assert result["cycling"]["ftp_watts"] == 250
    assert result["running"]["power_watts"] is None


def test_get_vo2max_reads_generic_and_cycling(client: GarminClient) -> None:
    client._garmin.get_max_metrics.return_value = [{"generic": {"vo2MaxValue": 52}, "cycling": {"vo2MaxValue": 48}}]

    result = client.get_vo2max(date(2026, 8, 5))

    assert result == {"date": "2026-08-05", "running_vo2max": 52, "cycling_vo2max": 48}


def test_get_vo2max_looks_back_when_today_empty(client: GarminClient) -> None:
    def side_effect(iso_date: str):
        if iso_date == "2026-08-03":
            return [{"generic": {"vo2MaxValue": 52}, "cycling": None}]
        return {"generic": None, "cycling": None}

    client._garmin.get_max_metrics.side_effect = side_effect

    result = client.get_vo2max(date(2026, 8, 5))

    assert result == {"date": "2026-08-03", "running_vo2max": 52, "cycling_vo2max": None}
    assert client._garmin.get_max_metrics.call_count == 3  # 05, 04, puis 03 (trouvé)


def test_get_vo2max_no_data_within_lookback_returns_nulls(client: GarminClient) -> None:
    client._garmin.get_max_metrics.return_value = {}

    result = client.get_vo2max(date(2026, 8, 5))

    assert result == {"date": "2026-08-05", "running_vo2max": None, "cycling_vo2max": None}


def test_login_maps_authentication_error(tmp_path) -> None:
    gc = GarminClient("a@b.com", "wrong-pw", str(tmp_path / "tokenstore"))
    gc._garmin = MagicMock()
    gc._garmin.login.side_effect = GarminConnectAuthenticationError("401 Unauthorized")

    with pytest.raises(GarminAuthenticationError):
        gc.get_training_status(date(2026, 8, 5))


def test_login_maps_connection_error_to_unavailable(tmp_path) -> None:
    gc = GarminClient("a@b.com", "pw", str(tmp_path / "tokenstore"))
    gc._garmin = MagicMock()
    gc._garmin.login.side_effect = GarminConnectConnectionError("timeout")

    with pytest.raises(GarminUnavailableError):
        gc.get_training_status(date(2026, 8, 5))


def test_login_maps_too_many_requests_to_unavailable(tmp_path) -> None:
    gc = GarminClient("a@b.com", "pw", str(tmp_path / "tokenstore"))
    gc._garmin = MagicMock()
    gc._garmin.login.side_effect = GarminConnectTooManyRequestsError("429")

    with pytest.raises(GarminUnavailableError):
        gc.get_training_status(date(2026, 8, 5))
