"""Tools MCP opérationnels : plan de semaine, séances du jour, feedback (spec section 7.11 à 7.17)."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Callable

from src.models import PARIS_TZ, DayPlanInput, SessionInput, monday_of_week, today_paris


def _now_iso() -> str:
    return datetime.now(PARIS_TZ).isoformat()


def _parse_date(date_str: str) -> date:
    return date.fromisoformat(date_str)


def _row_to_session(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "creneau": row["creneau"],
        "discipline": row["discipline"],
        "objectif": row["objectif"],
        "description": row["description"],
        "duree_minutes": row["duree_minutes"],
        "distance_km": row["distance_km"],
        "nutrition_avant": row["nutrition_avant"],
        "nutrition_apres": row["nutrition_apres"],
    }


def _fetch_sessions(conn: sqlite3.Connection, date_str: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM sessions WHERE date = ? ORDER BY creneau, id",
        (date_str,),
    ).fetchall()
    return [_row_to_session(row) for row in rows]


def _replace_sessions(conn: sqlite3.Connection, date_str: str, sessions: list[SessionInput]) -> int:
    conn.execute("DELETE FROM sessions WHERE date = ?", (date_str,))
    created_at = _now_iso()
    for session in sessions:
        conn.execute(
            """
            INSERT INTO sessions
                (date, creneau, discipline, objectif, description, duree_minutes,
                 distance_km, nutrition_avant, nutrition_apres, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                date_str,
                session.creneau,
                session.discipline,
                session.objectif,
                session.description,
                session.duree_minutes,
                session.distance_km,
                session.nutrition_avant,
                session.nutrition_apres,
                created_at,
            ),
        )
    conn.commit()
    return len(sessions)


def build_operational_tools(conn: sqlite3.Connection) -> dict[str, Callable[..., Any]]:
    """Construit les tools opérationnels liés à une connexion DB donnée."""

    async def get_week_plan(week_start_date: str | None = None) -> dict[str, Any]:
        try:
            week_start = monday_of_week(_parse_date(week_start_date)) if week_start_date else monday_of_week(today_paris())
        except ValueError:
            return {"error": True, "message": f"Date invalide : {week_start_date}"}

        days = []
        for offset in range(7):
            day_date = week_start + timedelta(days=offset)
            days.append({"date": day_date.isoformat(), "sessions": _fetch_sessions(conn, day_date.isoformat())})
        return {"week_start_date": week_start.isoformat(), "days": days}

    async def set_week_plan(week_start_date: str, days: list[DayPlanInput]) -> dict[str, Any]:
        try:
            _parse_date(week_start_date)
            for day in days:
                _parse_date(day.date)
        except ValueError as exc:
            return {"error": True, "message": f"Date invalide : {exc}"}

        for day in days:
            _replace_sessions(conn, day.date, day.sessions)
        return {"status": "ok"}

    async def get_sessions(date: str | None = None) -> dict[str, Any]:
        try:
            target_date = _parse_date(date) if date else today_paris()
        except ValueError:
            return {"error": True, "message": f"Date invalide : {date}"}
        return {"date": target_date.isoformat(), "sessions": _fetch_sessions(conn, target_date.isoformat())}

    async def set_sessions(date: str, sessions: list[SessionInput]) -> dict[str, Any]:
        try:
            _parse_date(date)
        except ValueError:
            return {"error": True, "message": f"Date invalide : {date}"}
        count = _replace_sessions(conn, date, sessions)
        return {"status": "ok", "count": count}

    async def add_session(
        date: str,
        creneau: str,
        discipline: str,
        objectif: str,
        description: str,
        duree_minutes: int | None = None,
        distance_km: float | None = None,
        nutrition_avant: str | None = None,
        nutrition_apres: str | None = None,
    ) -> dict[str, Any]:
        try:
            _parse_date(date)
        except ValueError:
            return {"error": True, "message": f"Date invalide : {date}"}
        if creneau not in ("matin", "soir"):
            return {"error": True, "message": f"creneau invalide : {creneau!r} (attendu 'matin' ou 'soir')"}

        cursor = conn.execute(
            """
            INSERT INTO sessions
                (date, creneau, discipline, objectif, description, duree_minutes,
                 distance_km, nutrition_avant, nutrition_apres, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                date,
                creneau,
                discipline,
                objectif,
                description,
                duree_minutes,
                distance_km,
                nutrition_avant,
                nutrition_apres,
                _now_iso(),
            ),
        )
        conn.commit()
        return {"status": "ok", "id": cursor.lastrowid}

    async def delete_session(id: int) -> dict[str, Any]:
        conn.execute("DELETE FROM sessions WHERE id = ?", (id,))
        conn.commit()
        return {"status": "ok"}

    async def log_activity_feedback(
        date: str,
        discipline: str,
        smiley: str,
        note: int,
    ) -> dict[str, Any]:
        try:
            _parse_date(date)
        except ValueError:
            return {"error": True, "message": f"Date invalide : {date}"}
        if not smiley:
            return {"error": True, "message": "smiley ne doit pas être vide"}
        if not (0 <= note <= 10):
            return {"error": True, "message": f"note invalide : {note} (attendu un entier entre 0 et 10)"}

        # Stocké dans les colonnes existantes ressenti/note du schéma (section 6, non modifié) :
        # ressenti <- smiley, note <- str(note).
        conn.execute(
            "INSERT INTO activity_feedback (date, discipline, ressenti, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (date, discipline, smiley, str(note), _now_iso()),
        )
        conn.commit()
        return {"status": "ok"}

    return {
        "get_week_plan": get_week_plan,
        "set_week_plan": set_week_plan,
        "get_sessions": get_sessions,
        "set_sessions": set_sessions,
        "add_session": add_session,
        "delete_session": delete_session,
        "log_activity_feedback": log_activity_feedback,
    }
