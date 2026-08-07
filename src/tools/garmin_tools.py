"""Tools MCP garmin_* (spec section 7.1 à 7.10)."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from src.garmin_client import GarminAuthenticationError, GarminClient, GarminUnavailableError
from src.models import monday_of_week, today_paris

logger = logging.getLogger(__name__)

CACHE_FRESHNESS = timedelta(hours=1)


def _get_cached(conn: sqlite3.Connection, type_: str, date_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload_json, fetched_at FROM garmin_cache WHERE type = ? AND date = ? "
        "ORDER BY fetched_at DESC LIMIT 1",
        (type_, date_key),
    ).fetchone()
    if row is None:
        return None
    fetched_at = datetime.fromisoformat(row["fetched_at"])
    if datetime.now(timezone.utc) - fetched_at > CACHE_FRESHNESS:
        return None
    return json.loads(row["payload_json"])


def _store_cache(conn: sqlite3.Connection, type_: str, date_key: str, payload: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO garmin_cache (date, type, payload_json, fetched_at) VALUES (?, ?, ?, ?)",
        (date_key, type_, json.dumps(payload), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _parse_date(date_str: str | None) -> date:
    if date_str is None:
        return today_paris()
    return date.fromisoformat(date_str)


def _garmin_error_message(exc: Exception) -> str:
    if isinstance(exc, GarminAuthenticationError):
        return f"Identifiants Garmin invalides : {exc}"
    if isinstance(exc, GarminUnavailableError):
        return f"Garmin Connect injoignable : {exc}"
    return f"Erreur inattendue lors de l'appel à Garmin Connect : {exc}"


def build_garmin_tools(client: GarminClient, conn: sqlite3.Connection) -> dict[str, Callable[..., Any]]:
    """Construit les tools garmin_* liés à un `GarminClient` et une connexion DB donnés."""

    async def garmin_sync() -> dict[str, Any]:
        try:
            today = today_paris()
            week_start = monday_of_week(today)
            payloads = client.sync_all(today)
        except Exception as exc:
            logger.warning("garmin_sync failed: %s", exc)
            return {"error": True, "message": f"Impossible de se connecter à Garmin Connect: {exc}"}

        date_keys = {
            "training_status": today.isoformat(),
            "body_battery": today.isoformat(),
            "vo2max": today.isoformat(),
            "resting_hr": today.isoformat(),
            "sleep": today.isoformat(),
            "recent_activities": f"{today.isoformat()}:days=7",
            "training_load": today.isoformat(),
            "training_load_balance": today.isoformat(),
            "lactate_threshold": today.isoformat(),
            "intensity_minutes": week_start.isoformat(),
        }
        for type_, payload in payloads.items():
            _store_cache(conn, type_, date_keys[type_], payload)

        synced_at = datetime.now(timezone.utc).isoformat()
        return {"status": "ok", "synced_at": synced_at}

    async def garmin_get_training_status() -> dict[str, Any]:
        today = today_paris()
        cached = _get_cached(conn, "training_status", today.isoformat())
        if cached is None:
            try:
                cached = client.get_training_status(today)
            except Exception as exc:
                return {"error": True, "message": _garmin_error_message(exc)}
            _store_cache(conn, "training_status", today.isoformat(), cached)
        return cached

    async def garmin_get_body_battery(date: str | None = None) -> dict[str, Any]:
        try:
            target_date = _parse_date(date)
        except ValueError:
            return {"error": True, "message": f"Date invalide : {date}"}
        cached = _get_cached(conn, "body_battery", target_date.isoformat())
        if cached is None:
            try:
                cached = client.get_body_battery(target_date.isoformat())
            except Exception as exc:
                return {"error": True, "message": _garmin_error_message(exc)}
            _store_cache(conn, "body_battery", target_date.isoformat(), cached)
        return {"date": target_date.isoformat(), **cached}

    async def garmin_get_vo2max() -> dict[str, Any]:
        today = today_paris()
        cached = _get_cached(conn, "vo2max", today.isoformat())
        if cached is None:
            try:
                cached = client.get_vo2max(today)
            except Exception as exc:
                return {"error": True, "message": _garmin_error_message(exc)}
            _store_cache(conn, "vo2max", today.isoformat(), cached)
        return cached

    async def garmin_get_resting_hr(date: str | None = None) -> dict[str, Any]:
        try:
            target_date = _parse_date(date)
        except ValueError:
            return {"error": True, "message": f"Date invalide : {date}"}
        cached = _get_cached(conn, "resting_hr", target_date.isoformat())
        if cached is None:
            try:
                cached = client.get_resting_hr(target_date.isoformat())
            except Exception as exc:
                return {"error": True, "message": _garmin_error_message(exc)}
            _store_cache(conn, "resting_hr", target_date.isoformat(), cached)
        return {"date": target_date.isoformat(), **cached}

    async def garmin_get_sleep(date: str | None = None) -> dict[str, Any]:
        try:
            target_date = _parse_date(date)
        except ValueError:
            return {"error": True, "message": f"Date invalide : {date}"}
        cached = _get_cached(conn, "sleep", target_date.isoformat())
        if cached is None:
            try:
                cached = client.get_sleep(target_date.isoformat())
            except Exception as exc:
                return {"error": True, "message": _garmin_error_message(exc)}
            _store_cache(conn, "sleep", target_date.isoformat(), cached)
        return {"date": target_date.isoformat(), **cached}

    async def garmin_get_recent_activities(days: int = 7) -> dict[str, Any]:
        today = today_paris()
        cache_key = f"{today.isoformat()}:days={days}"
        cached = _get_cached(conn, "recent_activities", cache_key)
        if cached is None:
            try:
                activities = client.get_recent_activities(today - timedelta(days=days), fetch_limit=days * 4)
            except Exception as exc:
                return {"error": True, "message": _garmin_error_message(exc)}
            cached = {"activities": activities}
            _store_cache(conn, "recent_activities", cache_key, cached)
        return cached

    async def garmin_get_training_load() -> dict[str, Any]:
        today = today_paris()
        cached = _get_cached(conn, "training_load", today.isoformat())
        if cached is None:
            try:
                cached = client.get_training_load(today)
            except Exception as exc:
                return {"error": True, "message": _garmin_error_message(exc)}
            _store_cache(conn, "training_load", today.isoformat(), cached)
        return cached

    async def garmin_get_training_load_balance() -> dict[str, Any]:
        today = today_paris()
        cached = _get_cached(conn, "training_load_balance", today.isoformat())
        if cached is None:
            try:
                cached = client.get_training_load_balance(today)
            except Exception as exc:
                return {"error": True, "message": _garmin_error_message(exc)}
            _store_cache(conn, "training_load_balance", today.isoformat(), cached)
        return cached

    async def garmin_get_lactate_threshold() -> dict[str, Any]:
        today = today_paris()
        cached = _get_cached(conn, "lactate_threshold", today.isoformat())
        if cached is None:
            try:
                cached = client.get_lactate_threshold()
            except Exception as exc:
                return {"error": True, "message": _garmin_error_message(exc)}
            _store_cache(conn, "lactate_threshold", today.isoformat(), cached)
        return {"date": today.isoformat(), **cached}

    async def garmin_get_intensity_minutes(week_start_date: str | None = None) -> dict[str, Any]:
        try:
            week_start = monday_of_week(_parse_date(week_start_date)) if week_start_date else monday_of_week(today_paris())
        except ValueError:
            return {"error": True, "message": f"Date invalide : {week_start_date}"}
        cached = _get_cached(conn, "intensity_minutes", week_start.isoformat())
        if cached is None:
            try:
                cached = client.get_intensity_minutes(week_start)
            except Exception as exc:
                return {"error": True, "message": _garmin_error_message(exc)}
            _store_cache(conn, "intensity_minutes", week_start.isoformat(), cached)
        return {"week_start_date": week_start.isoformat(), **cached}

    return {
        "garmin_sync": garmin_sync,
        "garmin_get_training_status": garmin_get_training_status,
        "garmin_get_body_battery": garmin_get_body_battery,
        "garmin_get_vo2max": garmin_get_vo2max,
        "garmin_get_resting_hr": garmin_get_resting_hr,
        "garmin_get_sleep": garmin_get_sleep,
        "garmin_get_recent_activities": garmin_get_recent_activities,
        "garmin_get_training_load": garmin_get_training_load,
        "garmin_get_training_load_balance": garmin_get_training_load_balance,
        "garmin_get_lactate_threshold": garmin_get_lactate_threshold,
        "garmin_get_intensity_minutes": garmin_get_intensity_minutes,
    }
