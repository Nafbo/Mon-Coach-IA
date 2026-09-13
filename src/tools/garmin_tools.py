"""Tools MCP garmin_* (spec section 7.1 à 7.10)."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Callable

from src.garmin_client import GarminAuthenticationError, GarminClient, GarminUnavailableError
from src.models import monday_of_week, now_paris, today_paris
from src.workout_builder import build_workout

logger = logging.getLogger(__name__)

CACHE_FRESHNESS = timedelta(hours=1)


def _get_cached(
    conn: sqlite3.Connection, type_: str, date_key: str, *, ignore_freshness: bool = False
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload_json, fetched_at FROM garmin_cache WHERE type = ? AND date = ? "
        "ORDER BY fetched_at DESC LIMIT 1",
        (type_, date_key),
    ).fetchone()
    if row is None:
        return None
    if not ignore_freshness:
        fetched_at = datetime.fromisoformat(row["fetched_at"])
        if now_paris() - fetched_at > CACHE_FRESHNESS:
            return None
    return json.loads(row["payload_json"])


def _store_cache(conn: sqlite3.Connection, type_: str, date_key: str, payload: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO garmin_cache (date, type, payload_json, fetched_at) VALUES (?, ?, ?, ?)",
        (date_key, type_, json.dumps(payload), now_paris().isoformat()),
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


# Forme de `get_scheduled_workouts` non validée contre un vrai compte (cf. README "Limites
# connues") : parsing défensif tolérant plusieurs clés plausibles plutôt qu'une hypothèse
# unique non vérifiée.
def _iter_scheduled_entries(raw: Any) -> list[dict[str, Any]]:
    # "calendarItems" confirmé sur un vrai compte (cf. README) — plus besoin de deviner
    # d'autres clés jamais observées en pratique.
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    if isinstance(raw, dict):
        value = raw.get("calendarItems")
        if isinstance(value, list):
            return [e for e in value if isinstance(e, dict)]
    return []


# Valeurs d'`itemType` confirmées sur un vrai compte (cf. README "Limites connues") :
# "workout" = séance structurée programmée (créée via garmin_push_workout, seule qui nous
# intéresse ici) ; "activity" = activité déjà réalisée ; "event" = échéance/course objectif
# (ex. "Odyssea") ; "nap" = repère de calendrier sans rapport (sommeil). Liste blanche plutôt
# que liste noire ("!= activity" uniquement, comme avant) : plus sûr si Garmin ajoute d'autres
# itemType à l'avenir, et corrige au passage un faux négatif potentiel de anti-doublon contre
# "event"/"nap" (jamais déclenché en pratique, mais latent).
_SCHEDULED_WORKOUT_ITEM_TYPE = "workout"


def _iter_pushed_workout_entries(raw: Any) -> list[dict[str, Any]]:
    return [e for e in _iter_scheduled_entries(raw) if e.get("itemType") == _SCHEDULED_WORKOUT_ITEM_TYPE]


def _extract_workout_id(raw: Any) -> int | None:
    # "workoutId" avant "id" : confirmé sur un vrai compte que ce sont deux identifiants
    # différents sur une entrée de calendrier — "id" est l'occurrence programmée, "workoutId"
    # le template (celui attendu par garmin_delete_workout/schedule_workout). Sur la réponse
    # d'upload_workout (pas une entrée de calendrier), seul "workoutId" existe.
    if isinstance(raw, dict):
        value = raw.get("workoutId") or raw.get("id")
        if isinstance(value, (int, str)):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def _find_duplicate_scheduled_workout(raw: Any, *, name: str, date_str: str) -> dict[str, Any] | None:
    for entry in _iter_pushed_workout_entries(raw):
        entry_name = entry.get("title") or entry.get("workoutName") or entry.get("name")
        entry_date = entry.get("date") or entry.get("calendarDate")
        if entry_name == name and entry_date == date_str:
            return {"workout_id": _extract_workout_id(entry), "scheduled_date": entry_date}
    return None


# sportTypeKey observé sur les entrées de calendrier itemType="workout" (confirmé sur un vrai
# compte) : reprend exactement les clés que build_workout écrit lui-même (cf. _SPORT_TYPE dans
# src/workout_builder.py) — une clé absente de cette table est retournée telle quelle plutôt
# que de lever une erreur (même tolérance que ACTIVITY_TYPE_MAP dans src/garmin_client.py).
_SPORT_TYPE_KEY_TO_DISCIPLINE = {"running": "course", "cycling": "velo"}


def _scheduled_workout_summary(entry: dict[str, Any]) -> dict[str, Any]:
    sport_type_key = entry.get("sportTypeKey")
    return {
        "workout_id": _extract_workout_id(entry),
        "name": entry.get("title") or entry.get("workoutName") or entry.get("name"),
        "date": entry.get("date") or entry.get("calendarDate"),
        "discipline": _SPORT_TYPE_KEY_TO_DISCIPLINE.get(sport_type_key, sport_type_key),
    }


def _month_range(start: date, end: date) -> list[tuple[int, int]]:
    """Liste des (year, month) couvrant [start, end] inclus — get_scheduled_workouts
    n'interroge Garmin que mois par mois, pas de plage de dates directe côté API."""
    months = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append((year, month))
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return months


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

        synced_at = now_paris().isoformat()
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

    async def garmin_get_activity_weather(activity_id: str) -> dict[str, Any]:
        key = str(activity_id)
        cached = _get_cached(conn, "activity_weather", key, ignore_freshness=True)
        if cached is None:
            try:
                cached = client.get_activity_weather(activity_id)
            except Exception as exc:
                return {"error": True, "message": _garmin_error_message(exc)}
            _store_cache(conn, "activity_weather", key, cached)
        return cached

    async def garmin_get_activity_details(activity_id: str) -> dict[str, Any]:
        key = str(activity_id)
        cached = _get_cached(conn, "activity_details", key, ignore_freshness=True)
        if cached is None:
            try:
                cached = client.get_activity_details(activity_id)
            except Exception as exc:
                return {"error": True, "message": _garmin_error_message(exc)}
            _store_cache(conn, "activity_details", key, cached)
        return cached

    async def garmin_get_swim_splits(activity_id: str) -> dict[str, Any]:
        key = str(activity_id)
        cached = _get_cached(conn, "swim_splits", key, ignore_freshness=True)
        if cached is None:
            try:
                splits = client.get_swim_splits(activity_id)
            except Exception as exc:
                return {"error": True, "message": _garmin_error_message(exc)}
            cached = {"splits": splits}
            _store_cache(conn, "swim_splits", key, cached)
        return cached

    async def garmin_push_workout(
        date: str, name: str, structure: dict[str, Any], dry_run: bool = True
    ) -> dict[str, Any]:
        """Construit et programme une séance structurée (course ou vélo) sur la montre Garmin.

        `structure` (clés reconnues au niveau racine : `discipline`, `warmup`, `blocks`,
        `cooldown` — toute autre clé, ex. `intervals`/`repeats`/`repeat`/`steps`/`main`,
        renvoie une erreur explicite plutôt que d'être silencieusement ignorée) :

            {
              "discipline": "course" | "velo",
              "warmup":   {"duration_sec": <float>, "target": <target|absent>},   # optionnel
              "blocks": [                                                          # optionnel
                {
                  "repeat": <int, defaut 1>,
                  "steps": [
                    {
                      "type": "interval" | "recovery",
                      "duration_sec": <float>,   # exactement l'un des deux :
                      "distance_m": <float>,     # duration_sec OU distance_m, pas les deux
                      "target": <target|absent>,
                    },
                    ...
                  ],
                },
                ...
              ],
              "cooldown": {"duration_sec": <float>, "target": <target|absent>},   # optionnel
            }

        `target` (optionnel, sur warmup/cooldown/n'importe quel step de bloc) :
            {"type": "pace_min_per_km" | "power_watts" | "hr_bpm", "low": <float>, "high": <float>}

        Exemple — fractionné course 8x400m à 3:55-4:00/km, récup 200m trot, warmup 15min,
        cooldown 10min :

            {
              "discipline": "course",
              "warmup": {"duration_sec": 900},
              "blocks": [
                {
                  "repeat": 8,
                  "steps": [
                    {"type": "interval", "distance_m": 400,
                     "target": {"type": "pace_min_per_km", "low": 3.917, "high": 4.0}},
                    {"type": "recovery", "distance_m": 200}
                  ]
                }
              ],
              "cooldown": {"duration_sec": 600}
            }

        `warmup`/`cooldown` sont toujours bornés en temps (`duration_sec` obligatoire). Les
        steps `interval`/`recovery` à l'intérieur d'un bloc peuvent être bornés en temps OU en
        distance.

        `dry_run=True` (défaut) : construit le payload sans appel d'écriture réel, à inspecter
        avant tout envoi réel avec `dry_run=False`.
        """
        try:
            payload = build_workout(structure)
        except (KeyError, ValueError) as exc:
            return {"error": True, "message": f"Structure de séance invalide : {exc}"}
        payload["workoutName"] = name

        if dry_run:
            return {"status": "dry_run", "payload": payload}

        try:
            target_date = _parse_date(date)
        except ValueError:
            return {"error": True, "message": f"Date invalide : {date}"}

        try:
            existing = client.get_scheduled_workouts(target_date.year, target_date.month)
        except Exception as exc:
            return {"error": True, "message": _garmin_error_message(exc)}

        duplicate = _find_duplicate_scheduled_workout(existing, name=name, date_str=date)
        if duplicate is not None:
            return {"status": "already_scheduled", **duplicate}

        try:
            upload_result = client.upload_workout(payload)
        except Exception as exc:
            return {"error": True, "message": _garmin_error_message(exc)}

        workout_id = _extract_workout_id(upload_result)
        if workout_id is None:
            return {
                "error": True,
                "message": f"Réponse d'upload inattendue, impossible d'y trouver un workout_id : {upload_result}",
            }

        last_exc: Exception | None = None
        for _attempt in range(3):
            try:
                client.schedule_workout(workout_id, date)
                return {"status": "ok", "workout_id": workout_id, "scheduled_date": date}
            except Exception as exc:
                last_exc = exc

        return {
            "error": True,
            "message": (
                f"Séance créée (workout_id={workout_id}) mais échec de la programmation "
                f"après 3 tentatives : {last_exc}"
            ),
            "workout_id": workout_id,
        }

    async def garmin_list_workouts(start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
        """Liste les séances structurées actuellement programmées sur le calendrier Garmin
        (celles créées via `garmin_push_workout`) — pas la bibliothèque d'exercices Garmin en
        général.

        Chaque entrée : `workout_id` (utilisable tel quel avec `garmin_delete_workout`),
        `name`, `date` (YYYY-MM-DD), `discipline` (`course`/`velo`/clé Garmin brute si
        inconnue).

        Précision Garmin "workout" vs "scheduled workout" : un `workout` est le
        template/définition de la séance (identifié par `workoutId`) ; un `scheduled workout`
        est son occurrence sur le calendrier à une date donnée (identifiée par un `id` de
        calendrier différent). `garmin_delete_workout` supprime le *template* — ce qui retire
        aussi son occurrence programmée. `workout_id` ci-dessous est bien le `workoutId` du
        template, pas l'`id` de l'occurrence de calendrier.

        `start_date`/`end_date` (YYYY-MM-DD, optionnels) filtrent sur la date programmée. Sans
        filtre : fenêtre par défaut de J-30 à J+60 (l'API Garmin n'expose le calendrier que
        mois par mois — un historique complet illimité n'est pas interrogeable en un seul
        appel).
        """
        try:
            start = _parse_date(start_date) if start_date else today_paris() - timedelta(days=30)
            end = _parse_date(end_date) if end_date else today_paris() + timedelta(days=60)
        except ValueError:
            return {"error": True, "message": f"Date invalide : {start_date or end_date}"}

        if start > end:
            return {"error": True, "message": f"start_date ({start}) postérieure à end_date ({end})"}

        workouts: dict[int, dict[str, Any]] = {}
        for year, month in _month_range(start, end):
            try:
                raw = client.get_scheduled_workouts(year, month)
            except Exception as exc:
                return {"error": True, "message": _garmin_error_message(exc)}
            for entry in _iter_pushed_workout_entries(raw):
                entry_date_str = entry.get("date") or entry.get("calendarDate")
                try:
                    entry_date = date.fromisoformat(entry_date_str) if entry_date_str else None
                except ValueError:
                    entry_date = None
                if entry_date is None or not (start <= entry_date <= end):
                    continue
                summary = _scheduled_workout_summary(entry)
                if summary["workout_id"] is not None:
                    workouts[summary["workout_id"]] = summary

        return {"workouts": sorted(workouts.values(), key=lambda w: w["date"] or "")}

    async def garmin_delete_workout(workout_id: int) -> dict[str, Any]:
        try:
            client.delete_workout(workout_id)
        except Exception as exc:
            return {"error": True, "message": _garmin_error_message(exc)}
        return {"status": "ok", "workout_id": workout_id}

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
        "garmin_get_activity_weather": garmin_get_activity_weather,
        "garmin_get_activity_details": garmin_get_activity_details,
        "garmin_get_swim_splits": garmin_get_swim_splits,
        "garmin_push_workout": garmin_push_workout,
        "garmin_list_workouts": garmin_list_workouts,
        "garmin_delete_workout": garmin_delete_workout,
    }
