"""Tests de src/tools/operational_tools.py avec une DB SQLite temporaire (en mémoire)."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from src.db import get_connection, init_db
from src.models import DayPlanInput, SessionInput
from src.tools import operational_tools as ot


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = get_connection(":memory:")
    init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def tools(conn: sqlite3.Connection) -> dict:
    return ot.build_operational_tools(conn)


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch: pytest.MonkeyPatch) -> date:
    fixed = date(2026, 8, 5)  # un mercredi
    monkeypatch.setattr(ot, "today_paris", lambda: fixed)
    return fixed


def _session(**overrides) -> SessionInput:
    base = dict(
        creneau="matin",
        discipline="course",
        objectif="endurance",
        description="footing facile",
        duree_minutes=45,
        distance_km=8.5,
        nutrition_avant=None,
        nutrition_apres=None,
    )
    base.update(overrides)
    return SessionInput(**base)


async def test_get_sessions_defaults_to_today(tools: dict, frozen_today: date) -> None:
    result = await tools["get_sessions"]()
    assert result == {"date": frozen_today.isoformat(), "sessions": []}


async def test_add_session_then_get_sessions(tools: dict) -> None:
    add_result = await tools["add_session"](
        date="2026-08-05",
        creneau="matin",
        discipline="course",
        objectif="endurance",
        description="footing facile",
        duree_minutes=45,
        distance_km=8.5,
        nutrition_avant=None,
        nutrition_apres=None,
    )
    assert add_result["status"] == "ok"
    assert isinstance(add_result["id"], int)

    result = await tools["get_sessions"](date="2026-08-05")
    assert result["date"] == "2026-08-05"
    assert len(result["sessions"]) == 1
    session = result["sessions"][0]
    assert session["discipline"] == "course"
    assert session["duree_minutes"] == 45


async def test_add_session_does_not_erase_existing_sessions(tools: dict) -> None:
    await tools["add_session"](
        date="2026-08-05", creneau="matin", discipline="course", objectif="a", description="a"
    )
    await tools["add_session"](
        date="2026-08-05", creneau="soir", discipline="nage", objectif="b", description="b"
    )
    result = await tools["get_sessions"](date="2026-08-05")
    assert len(result["sessions"]) == 2


async def test_set_sessions_replaces_existing(tools: dict) -> None:
    await tools["add_session"](
        date="2026-08-05", creneau="matin", discipline="course", objectif="a", description="a"
    )
    result = await tools["set_sessions"](date="2026-08-05", sessions=[_session(creneau="soir", discipline="velo")])
    assert result == {"status": "ok", "count": 1}

    sessions = (await tools["get_sessions"](date="2026-08-05"))["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["discipline"] == "velo"
    assert sessions[0]["creneau"] == "soir"


async def test_delete_session(tools: dict) -> None:
    add_result = await tools["add_session"](
        date="2026-08-05", creneau="matin", discipline="course", objectif="a", description="a"
    )
    session_id = add_result["id"]

    delete_result = await tools["delete_session"](id=session_id)
    assert delete_result == {"status": "ok"}

    sessions = (await tools["get_sessions"](date="2026-08-05"))["sessions"]
    assert sessions == []


async def test_delete_session_unknown_id_is_a_noop(tools: dict) -> None:
    result = await tools["delete_session"](id=999999)
    assert result == {"status": "ok"}


async def test_get_week_plan_has_seven_days_with_empty_lists(tools: dict) -> None:
    result = await tools["get_week_plan"](week_start_date="2026-08-03")
    assert result["week_start_date"] == "2026-08-03"
    assert len(result["days"]) == 7
    assert [d["date"] for d in result["days"]] == [
        "2026-08-03",
        "2026-08-04",
        "2026-08-05",
        "2026-08-06",
        "2026-08-07",
        "2026-08-08",
        "2026-08-09",
    ]
    assert all(d["sessions"] == [] for d in result["days"])


async def test_get_week_plan_defaults_to_current_week(tools: dict, frozen_today: date) -> None:
    result = await tools["get_week_plan"]()
    assert result["week_start_date"] == "2026-08-03"  # lundi de la semaine du 2026-08-05


async def test_set_week_plan_writes_correct_days(tools: dict) -> None:
    result = await tools["set_week_plan"](
        week_start_date="2026-08-03",
        days=[
            DayPlanInput(date="2026-08-04", sessions=[_session(creneau="matin", discipline="velo")]),
            DayPlanInput(date="2026-08-06", sessions=[_session(creneau="soir", discipline="nage")]),
        ],
    )
    assert result == {"status": "ok"}

    week = await tools["get_week_plan"](week_start_date="2026-08-03")
    by_date = {d["date"]: d["sessions"] for d in week["days"]}
    assert len(by_date["2026-08-04"]) == 1
    assert by_date["2026-08-04"][0]["discipline"] == "velo"
    assert len(by_date["2026-08-06"]) == 1
    assert by_date["2026-08-03"] == []
    assert by_date["2026-08-05"] == []


async def test_add_session_invalid_date_returns_error(tools: dict) -> None:
    result = await tools["add_session"](
        date="not-a-date", creneau="matin", discipline="course", objectif="a", description="a"
    )
    assert result["error"] is True
    assert "not-a-date" in result["message"]


async def test_add_session_invalid_creneau_returns_error(tools: dict) -> None:
    result = await tools["add_session"](
        date="2026-08-05", creneau="apres-midi", discipline="course", objectif="a", description="a"
    )
    assert result["error"] is True


async def test_get_sessions_invalid_date_returns_error(tools: dict) -> None:
    result = await tools["get_sessions"](date="05/08/2026")
    assert result["error"] is True


async def test_log_activity_feedback_inserts_row(tools: dict, conn: sqlite3.Connection) -> None:
    result = await tools["log_activity_feedback"](
        date="2026-08-05", discipline="course", smiley="😄", note=8
    )
    assert result == {"status": "ok"}

    row = conn.execute("SELECT * FROM activity_feedback WHERE date = ?", ("2026-08-05",)).fetchone()
    assert row is not None
    assert row["discipline"] == "course"
    assert row["ressenti"] == "😄"  # smiley stocké dans la colonne existante `ressenti`
    assert row["note"] == "8"  # note stockée en texte dans la colonne existante `note`


async def test_log_activity_feedback_is_append_only(tools: dict, conn: sqlite3.Connection) -> None:
    await tools["log_activity_feedback"](date="2026-08-05", discipline="course", smiley="😄", note=8)
    await tools["log_activity_feedback"](date="2026-08-05", discipline="course", smiley="😫", note=3)

    count = conn.execute("SELECT COUNT(*) AS n FROM activity_feedback").fetchone()["n"]
    assert count == 2


async def test_log_activity_feedback_invalid_date_returns_error(tools: dict) -> None:
    result = await tools["log_activity_feedback"](date="invalid", discipline="course", smiley="😄", note=8)
    assert result["error"] is True


async def test_log_activity_feedback_empty_smiley_returns_error(tools: dict) -> None:
    result = await tools["log_activity_feedback"](date="2026-08-05", discipline="course", smiley="", note=8)
    assert result["error"] is True


async def test_log_activity_feedback_note_out_of_range_returns_error(tools: dict) -> None:
    result = await tools["log_activity_feedback"](date="2026-08-05", discipline="course", smiley="😄", note=11)
    assert result["error"] is True

    result2 = await tools["log_activity_feedback"](date="2026-08-05", discipline="course", smiley="😄", note=-1)
    assert result2["error"] is True


async def test_log_activity_feedback_note_boundaries_are_valid(tools: dict) -> None:
    result_low = await tools["log_activity_feedback"](date="2026-08-05", discipline="course", smiley="😫", note=0)
    assert result_low == {"status": "ok"}

    result_high = await tools["log_activity_feedback"](date="2026-08-05", discipline="course", smiley="😄", note=10)
    assert result_high == {"status": "ok"}
