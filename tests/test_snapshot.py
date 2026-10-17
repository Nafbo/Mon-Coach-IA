"""P5 : garmin_get_daily_snapshot."""

from __future__ import annotations

import sqlite3
from datetime import date
from unittest.mock import MagicMock

import pytest
from garminconnect import GarminConnectConnectionError

from src.db import get_connection, init_db
from src.garmin_client import GarminClient
from src.tools import garmin_tools as gt

TODAY = date(2026, 10, 17)

_TRAINING_RAW = {
    "mostRecentTrainingStatus": {
        "latestTrainingStatusData": {
            "1": {
                "trainingStatusFeedbackPhrase": "PRODUCTIVE_1",
                "acuteTrainingLoadDTO": {"dailyTrainingLoadAcute": 480, "dailyTrainingLoadChronic": 400},
            }
        }
    },
    "mostRecentTrainingLoadBalance": {
        "metricsTrainingLoadBalanceDTOMap": {"1": {"monthlyLoadAerobicLow": 390.0, "monthlyLoadAnaerobic": 45.0}}
    },
}


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = get_connection(":memory:")
    init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def client(tmp_path) -> GarminClient:
    gc = GarminClient("a@b.com", "pw", str(tmp_path / "tokenstore"))
    g = gc._garmin = MagicMock()
    gc._logged_in = True
    g.get_user_summary.return_value = {"bodyBatteryAtWakeTime": 82, "lastSyncTimestampGMT": "2026-10-17T05:00:00.0"}
    g.get_sleep_data.return_value = {"dailySleepDTO": {"sleepTimeSeconds": 26000, "sleepScores": {"overall": {"value": 81}}}}
    g.get_body_battery.return_value = [{"charged": 60, "drained": 40, "bodyBatteryValuesArray": [[1760680800000, 82]]}]
    g.get_rhr_day.return_value = {"allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [{"value": 46}]}}}
    g.get_training_status.return_value = _TRAINING_RAW
    g.get_lactate_threshold.return_value = {"speed_and_heart_rate": {"heartRate": 177, "speed": 0.369}, "power": {}}
    g.get_max_metrics.return_value = [{"generic": {"vo2MaxValue": 54}}]
    g.get_device_last_used.return_value = {"lastUsedDeviceName": "Forerunner 265", "lastUsedDeviceUploadTime": 1760682000000}
    return gc


@pytest.fixture
def tools(client: GarminClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(gt, "today_paris", lambda: TODAY)
    return gt.build_garmin_tools(client, conn)


async def test_p5_snapshot_contains_all_blocks(tools: dict) -> None:
    result = await tools["garmin_get_daily_snapshot"]()

    assert result["status"] == "ok"
    assert result["date"] == "2026-10-17"
    assert result["blocks_in_error"] == [] and result["blocks_no_data"] == []
    assert result["sleep"]["score"] == 81
    assert result["body_battery"]["at_wake"] == 82
    assert result["resting_hr"]["resting_hr"] == 46
    assert result["training_status"]["training_status"] == "PRODUCTIVE_1"
    assert result["training_load"]["load_ratio"] == 1.2
    assert result["training_load"]["load_status"] == "optimal"
    assert result["training_load_balance"]["charge_anaerobique"] == 45.0
    assert result["lactate_threshold"]["running"]["heart_rate"] == 177
    assert result["vo2max"]["running_vo2max"] == 54


async def test_p5_snapshot_minimises_garmin_calls(tools: dict, client: GarminClient) -> None:
    await tools["garmin_get_daily_snapshot"]()

    g = client._garmin
    # Une seule remontée training status (trouvée dès aujourd'hui) pour 3 blocs.
    assert g.get_training_status.call_count == 1
    assert g.get_user_summary.call_count == 1
    total = sum(
        getattr(g, name).call_count
        for name in (
            "get_user_summary",
            "get_sleep_data",
            "get_body_battery",
            "get_rhr_day",
            "get_training_status",
            "get_lactate_threshold",
            "get_max_metrics",
            "get_device_last_used",
        )
    )
    # 7 appels de données + 1 pour l'heure de dernière synchro de la montre.
    assert total == 8


async def test_p5_failing_block_is_isolated(tools: dict, client: GarminClient) -> None:
    client._garmin.get_sleep_data.side_effect = GarminConnectConnectionError("HTTP 500")
    client._garmin.get_rhr_day.return_value = {}

    result = await tools["garmin_get_daily_snapshot"]()

    assert result["status"] == "ok"
    assert result["sleep"]["status"] == "error"
    assert "HTTP 500" in result["sleep"]["message"]
    assert result["resting_hr"]["status"] == "no_data"
    assert result["blocks_in_error"] == ["sleep"]
    assert result["blocks_no_data"] == ["resting_hr"]
    assert result["body_battery"]["status"] == "ok"


async def test_p5_training_failure_marks_three_blocks(tools: dict, client: GarminClient) -> None:
    client._garmin.get_training_status.side_effect = GarminConnectConnectionError("timeout")

    result = await tools["garmin_get_daily_snapshot"]()

    assert set(result["blocks_in_error"]) == {"training_status", "training_load", "training_load_balance"}
    assert result["vo2max"]["status"] == "ok"


async def test_p5_snapshot_feeds_unit_tool_cache(tools: dict, client: GarminClient) -> None:
    await tools["garmin_get_daily_snapshot"]()
    client._garmin.get_training_status.reset_mock()

    load = await tools["garmin_get_training_load"]()

    assert load["load_ratio"] == 1.2
    client._garmin.get_training_status.assert_not_called()


async def test_p5_invalid_date(tools: dict) -> None:
    result = await tools["garmin_get_daily_snapshot"](date="hier")
    assert result["status"] == "error"
