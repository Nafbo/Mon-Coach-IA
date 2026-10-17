"""Non-régression des schémas de réponse : chaque clé présente dans `baseline_keys.json`
(figé le 17/10/2026, avant la mission de fiabilisation) doit rester présente. Les ajouts
sont autorisés, jamais un renommage ni une suppression."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from src.db import get_connection, init_db
from src.tools import garmin_tools as gt
from tests.garmin_fakes import healthy_client

BASELINE = json.loads((Path(__file__).parent / "baseline_keys.json").read_text(encoding="utf-8"))

CALLS = {
    "garmin_sync": {},
    "garmin_get_training_status": {},
    "garmin_get_body_battery": {},
    "garmin_get_vo2max": {},
    "garmin_get_resting_hr": {},
    "garmin_get_sleep": {},
    "garmin_get_recent_activities": {"days": 7},
    "garmin_get_training_load": {},
    "garmin_get_training_load_balance": {},
    "garmin_get_lactate_threshold": {},
    "garmin_get_intensity_minutes": {},
    "garmin_get_activity_weather": {"activity_id": "1"},
    "garmin_get_activity_feedback": {"activity_id": "1"},
    "garmin_get_activity_details": {"activity_id": "1"},
    "garmin_analyse_activity": {"activity_id": "1", "fc_seuil": 174},
    "garmin_get_swim_splits": {"activity_id": "2"},
    "garmin_health_check": {},
    "garmin_get_daily_snapshot": {},
    "garmin_list_workouts": {},
}


@pytest.fixture
def tools(tmp_path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(gt, "today_paris", lambda: date(2026, 10, 17))
    conn = get_connection(":memory:")
    init_db(conn)
    yield gt.build_garmin_tools(healthy_client(tmp_path), conn)
    conn.close()


def test_baseline_covers_every_call() -> None:
    assert set(BASELINE) == set(CALLS)


@pytest.mark.parametrize("name", sorted(CALLS))
async def test_existing_keys_unchanged(tools: dict, name: str) -> None:
    result = await tools[name](**CALLS[name])
    expected = BASELINE[name]

    assert not result.get("error"), result
    assert set(expected["top"]) <= set(result), f"{name} : clés retirées {set(expected['top']) - set(result)}"
    for key, sub_keys in expected["nested"].items():
        assert isinstance(result[key], dict)
        assert set(sub_keys) <= set(result[key]), f"{name}.{key} : clés retirées {set(sub_keys) - set(result[key])}"
    for key, item_keys in expected.get("items", {}).items():
        assert result[key], f"{name}.{key} vide"
        assert set(item_keys) <= set(result[key][0]), f"{name}.{key}[] : clés retirées {set(item_keys) - set(result[key][0])}"
