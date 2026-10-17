"""Tools MCP garmin_* (spec section 7.1 à 7.10)."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Callable

from src.analysis.series import ANALYSIS_TYPES, analyse, downsample, moving_time, pauses
from src.analysis.swim import analyse_swim
from src.analysis.swim import annotate as annotate_swim
from src.garmin_client import GarminAuthenticationError, GarminClient, GarminUnavailableError, classify_error
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


def _error(message: str, error_type: str = "invalid_input") -> dict[str, Any]:
    """Réponse d'erreur commune à tous les tools : `status` "error", `error` (booléen
    historique, conservé), `message` (message exact) et `error_type` : "auth" | "network" |
    "rate_limit" | "unknown" pour un échec d'accès à Garmin, "invalid_input" pour un paramètre
    refusé avant tout appel. `error_detail` regroupe {type, message}."""
    return {
        "error": True,
        "status": "error",
        "message": message,
        "error_type": error_type,
        "error_detail": {"type": error_type, "message": message},
    }


def _availability(block: dict[str, Any]) -> str:
    """Disponibilité d'un bloc : celle calculée par le client (sommeil, Body Battery, FC de
    repos distinguent "zero" de "empty"), sinon déduite de `status`."""
    if block.get("availability") in ("ok", "zero", "empty", "error"):
        return block["availability"]
    return {"ok": "ok", "no_data": "empty"}.get(block.get("status"), "error")


def _garmin_error(exc: Exception) -> dict[str, Any]:
    """Réponse d'erreur pour un échec d'accès à Garmin, typée d'après l'exception."""
    return _error(_garmin_error_message(exc), classify_error(exc))


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

    def _last_sync() -> dict[str, Any]:
        """Dernière synchro montre -> Garmin Connect, jamais bloquante : en cas d'échec
        d'accès, `status` "error" + `error_type` / `message`, `at` null."""
        empty = {"at": None, "age_minutes": None, "source": None, "device": None}
        try:
            sync = client.get_last_sync()
        except Exception as exc:
            return {**empty, "status": "error", "error_type": classify_error(exc), "message": _garmin_error_message(exc)}
        return {**empty, **sync} if isinstance(sync, dict) else {**empty, "status": "no_data"}

    async def garmin_sync() -> dict[str, Any]:
        """Force le rafraîchissement de toutes les données du jour (statut et charge
        d'entraînement, Body Battery, VO2max, FC repos, sommeil, activités des 7 derniers
        jours, seuil lactique, minutes d'intensité) et les écrit en cache — sauf les blocs
        `no_data`.

        Sortie : `status` "ok", `synced_at` (heure de CET appel, ISO Europe/Paris) et
        `last_sync` = heure de la dernière synchro de la MONTRE vers Garmin Connect
        (`at` ISO Europe/Paris, `age_minutes`, `source`, `device`) : si `at` est antérieur à
        une séance, celle-ci n'est pas encore arrivée chez Garmin. En cas d'échec :
        `status` "error", `error_type` ("auth" | "network" | "rate_limit" | "unknown"),
        `message` exact."""
        try:
            today = today_paris()
            week_start = monday_of_week(today)
            payloads = client.sync_all(today)
        except Exception as exc:
            logger.warning("garmin_sync failed: %s", exc)
            return _error(f"Impossible de se connecter à Garmin Connect: {exc}", classify_error(exc))

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
            # Pas de mise en cache d'une absence de donnée (cf. _fetch_cached).
            if isinstance(payload, dict) and payload.get("status") == "no_data":
                continue
            _store_cache(conn, type_, date_keys[type_], payload)

        synced_at = now_paris().isoformat()
        return {"status": "ok", "synced_at": synced_at, "last_sync": _last_sync()}

    def _fetch_cached(
        type_: str,
        key: str,
        fetch: Callable[[], dict[str, Any]],
        *,
        ignore_freshness: bool = False,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """Lecture cache -> sinon appel Garmin, avec le champ `status` commun (P3).

        Une réponse `no_data` n'est PAS mise en cache : une donnée qui arrive en retard
        (synchro tardive de la montre) doit apparaître dès l'appel suivant, pas une heure
        plus tard. Les payloads mis en cache avant l'ajout de `status` reçoivent "ok".
        """
        cached = None if force_refresh else _get_cached(conn, type_, key, ignore_freshness=ignore_freshness)
        if cached is None:
            try:
                cached = fetch()
            except Exception as exc:
                return _garmin_error(exc)
            if cached.get("status") != "no_data":
                _store_cache(conn, type_, key, cached)
        return {"status": "ok", **cached}

    async def garmin_get_training_status(force_refresh: bool = False) -> dict[str, Any]:
        """Statut d'entraînement Garmin : `training_status` (libellé Garmin, ex.
        "PRODUCTIVE_1"), `acute_load`, `chronic_load`, `date` (date réelle des données, jusqu'à
        7 jours en arrière). `status` : "ok" | "no_data" | erreur."""
        today = today_paris()
        return _fetch_cached(
            "training_status", today.isoformat(), lambda: client.get_training_status(today), force_refresh=force_refresh
        )

    async def garmin_get_body_battery(date: str | None = None, force_refresh: bool = False) -> dict[str, Any]:
        """Body Battery du jour (`date` YYYY-MM-DD, défaut aujourd'hui) : `at_wake` (niveau
        au réveil), `min` / `max` (niveaux extrêmes de la journée), `latest` + `latest_at`
        (dernier niveau mesuré et son horodatage), `charged` / `drained` (cumuls de points
        rechargés / consommés sur la journée, pas des niveaux), `value` (= `latest`, conservé
        pour compatibilité). Niveaux sur 0-100. Heures (ISO Europe/Paris) : `min_at`,
        `max_at`, `latest_at`, `at_wake_at` (fin de la nuit ; null si la nuit n'a pas été
        enregistrée). `availability` : "ok" | "zero" (points tous à 0) | "empty".
        `status` : "ok" | "no_data" (aucun point exploitable, valeurs null) | erreur
        (`error_type`, `message`)."""
        try:
            target_date = _parse_date(date)
        except ValueError:
            return _error(f"Date invalide : {date}")
        iso = target_date.isoformat()
        result = _fetch_cached("body_battery", iso, lambda: client.get_body_battery(iso), force_refresh=force_refresh)
        return result if result.get("error") else {"date": iso, **result}

    async def garmin_get_vo2max(force_refresh: bool = False) -> dict[str, Any]:
        """VO2max course (`running_vo2max`) et vélo (`cycling_vo2max`), `date` réelle de la
        dernière valeur (jusqu'à 7 jours en arrière). `status` : "ok" | "no_data" | erreur."""
        today = today_paris()
        return _fetch_cached("vo2max", today.isoformat(), lambda: client.get_vo2max(today), force_refresh=force_refresh)

    async def garmin_get_resting_hr(date: str | None = None, force_refresh: bool = False) -> dict[str, Any]:
        """FC de repos (bpm) du jour (`date` YYYY-MM-DD, défaut aujourd'hui), avec
        `measured_on` (jour de la mesure), `last_sync_at` (dernière synchro montre prise en
        compte, ISO Europe/Paris) et `provisoire` (true pour le jour en cours : valeur encore
        susceptible d'être recalculée). Garmin ne date la FC de repos qu'au jour, sans heure :
        `measured_on` + `last_sync_at` constituent l'horodatage disponible. `availability` :
        "ok" | "zero" (valeur 0 renvoyée : non exploitable) | "empty". `status` : "ok" |
        "no_data" (`resting_hr` null) | erreur (`error_type`, `message`)."""
        try:
            target_date = _parse_date(date)
        except ValueError:
            return _error(f"Date invalide : {date}")
        iso = target_date.isoformat()
        result = _fetch_cached("resting_hr", iso, lambda: client.get_resting_hr(iso), force_refresh=force_refresh)
        if result.get("error"):
            return result
        # La FC repos du jour en cours est recalculée par Garmin au fil des synchros : une
        # valeur lue le matin peut encore changer (cf. `last_sync_at`).
        return {"date": iso, "provisoire": target_date == today_paris(), **result}

    async def garmin_get_sleep(date: str | None = None, force_refresh: bool = False) -> dict[str, Any]:
        """Nuit se terminant le jour `date` (YYYY-MM-DD, défaut aujourd'hui) : durées en
        minutes (totale, profond, léger, paradoxal, éveillé) et `score`. `status` : "ok" |
        "no_data" (nuit non synchronisée : toutes les valeurs null, jamais 0) | erreur
        (`error_type`, `message`). `availability` : "ok" | "zero" (durée 0 renvoyée) |
        "empty" (aucune nuit enregistrée)."""
        try:
            target_date = _parse_date(date)
        except ValueError:
            return _error(f"Date invalide : {date}")
        iso = target_date.isoformat()
        result = _fetch_cached("sleep", iso, lambda: client.get_sleep(iso), force_refresh=force_refresh)
        return result if result.get("error") else {"date": iso, **result}

    async def garmin_get_recent_activities(days: int = 7, force_refresh: bool = False) -> dict[str, Any]:
        """Activités des `days` derniers jours (plus récentes en premier).

        `status` : "ok" | "no_data" (appel réussi, aucune activité sur la période ; jamais mis
        en cache) | "error" (échec d'accès : `error_type` + `message`, jamais une liste vide).
        `force_refresh=true` contourne le cache serveur et relit Garmin (à utiliser pour
        relire après une synchro tardive). `last_sync` : dernière synchro de la montre vers
        Garmin Connect (`at`, `age_minutes`, `source`) — une liste vide avec un `last_sync.at`
        antérieur à la séance prévue signifie « pas encore synchronisé », pas « repos ».
        Allures : `allure_*_min_km` (minutes décimales) + `allure_*_mmss` ("4:26")."""
        today = today_paris()

        def fetch() -> dict[str, Any]:
            activities = client.get_recent_activities(today - timedelta(days=days), fetch_limit=days * 4)
            return {"status": "ok" if activities else "no_data", "activities": activities}

        result = _fetch_cached(
            "recent_activities", f"{today.isoformat()}:days={days}", fetch, force_refresh=force_refresh
        )
        return result if result.get("error") else {**result, "last_sync": _last_sync()}

    async def garmin_get_training_load(force_refresh: bool = False) -> dict[str, Any]:
        """Charge aiguë/chronique, ratio (null si pas de charge chronique), `load_status`
        ("optimal" | "high" | "low" | "no_status"), cibles et ACWR. `status` : "ok" |
        "no_data" | erreur."""
        today = today_paris()
        return _fetch_cached(
            "training_load", today.isoformat(), lambda: client.get_training_load(today), force_refresh=force_refresh
        )

    async def garmin_get_training_load_balance(force_refresh: bool = False) -> dict[str, Any]:
        """Charge mensuelle par filière (aérobie faible / élevée / anaérobie) et cibles.
        `status` : "ok" | "no_data" | erreur."""
        today = today_paris()
        return _fetch_cached(
            "training_load_balance",
            today.isoformat(),
            lambda: client.get_training_load_balance(today),
            force_refresh=force_refresh,
        )

    async def garmin_get_lactate_threshold(force_refresh: bool = False) -> dict[str, Any]:
        """Seuil lactique : course (FC, allure min/km, puissance) et FTP vélo. `status` :
        "ok" | "no_data" | erreur."""
        today = today_paris()
        result = _fetch_cached(
            "lactate_threshold", today.isoformat(), client.get_lactate_threshold, force_refresh=force_refresh
        )
        return result if result.get("error") else {"date": today.isoformat(), **result}

    async def garmin_get_intensity_minutes(
        week_start_date: str | None = None, force_refresh: bool = False
    ) -> dict[str, Any]:
        """Minutes d'intensité modérées/vigoureuses de la semaine (lundi de `week_start_date`,
        défaut semaine courante) et objectif. `status` : "ok" | "no_data" | erreur."""
        try:
            week_start = monday_of_week(_parse_date(week_start_date)) if week_start_date else monday_of_week(today_paris())
        except ValueError:
            return _error(f"Date invalide : {week_start_date}")
        result = _fetch_cached(
            "intensity_minutes",
            week_start.isoformat(),
            lambda: client.get_intensity_minutes(week_start),
            force_refresh=force_refresh,
        )
        return result if result.get("error") else {"week_start_date": week_start.isoformat(), **result}

    async def garmin_get_activity_feedback(activity_id: str) -> dict[str, Any]:
        """Auto-évaluation de l'athlète saisie sur la montre en fin d'activité :
        `sensations` ("Très faible" | "Faible" | "Normal" | "Fort" | "Très fort", valeur brute
        Garmin 0/25/50/75/100 dans `sensations_brut`) et `effort_percu_sur_10` (RPE 1 à 10,
        10 = effort maximal) avec son libellé Garmin `effort_percu_libelle` ("Très léger",
        "Léger", "Modéré", "Difficile", "Très difficile", "Maximal").

        `status` : "ok" | "no_data" (rien saisi pour cette activité : valeurs null ; jamais mis
        en cache, pour voir une saisie faite plus tard dans l'app) | erreur. Cache 1h.
        """
        return _fetch_cached(
            "activity_feedback", str(activity_id), lambda: client.get_activity_feedback(activity_id)
        )

    async def garmin_get_activity_weather(activity_id: str) -> dict[str, Any]:
        """Météo d'une activité : températures en °C, `humidity_percent`, vent
        `wind_speed_kmh` / `wind_gust_kmh` (unité explicite dans `wind_speed_unit` = "km/h",
        unité native Garmin ; rafale null = aucune rafale signalée), `wind_direction_compass`,
        `condition`, station (`station_name`, `station_id`, `station_distance_km` entre la
        station et le départ, `station_far` true au-delà de 15 km : mesure peu
        représentative). Les champs `*_raw` historiques sont conservés. Cache sans
        expiration. `status` : "ok" | erreur."""
        key = str(activity_id)
        # Clé versionnée : les payloads v1 (sans km/h ni distance station) restent en base
        # mais ne sont plus servis.
        return _fetch_cached(
            "activity_weather_v2", key, lambda: client.get_activity_weather(activity_id), ignore_freshness=True
        )

    async def garmin_get_daily_snapshot(date: str | None = None) -> dict[str, Any]:
        """Vue quotidienne en un seul appel, pour l'analyse du jour (`date` YYYY-MM-DD,
        défaut aujourd'hui). Blocs, mêmes formats que les tools unitaires :
        `sleep`, `body_battery`, `resting_hr`, `training_status` (libellé, charges),
        `training_load` (aiguë/chronique, ratio, statut), `training_load_balance` (charge par
        filière), `lactate_threshold`, `vo2max`.

        Chaque bloc porte son `status` ("ok" | "no_data" | "error" + `message`) et son
        `availability` : "ok" | "zero" (valeur présente mais à 0, non exploitable) | "empty"
        (pas de donnée) | "error" — récapitulé dans `data_availability`. `status` global :
        "error" seulement si tous les blocs sont en erreur, sinon "ok" — `blocks_in_error` /
        `blocks_no_data` disent lesquels manquent. `last_sync` : dernière synchro de la montre
        vers Garmin Connect (`at`, `age_minutes`, `source`). Le seuil lactique est
        toujours la dernière valeur connue (pas datée) ; statut d'entraînement, charge et
        VO2max remontent jusqu'à 7 jours en arrière (champ `date` de chaque bloc).
        """
        try:
            target_date = _parse_date(date)
        except ValueError:
            return _error(f"Date invalide : {date}")
        try:
            blocks = client.get_daily_snapshot(target_date)
        except Exception as exc:
            return _garmin_error(exc)

        # Les blocs "ok" du jour alimentent le cache des tools unitaires (mêmes clés).
        if target_date == today_paris():
            for type_, payload in blocks.items():
                if payload.get("status") == "ok":
                    _store_cache(conn, type_, target_date.isoformat(), payload)

        in_error = [name for name, b in blocks.items() if b.get("status") == "error"]
        no_data = [name for name, b in blocks.items() if b.get("status") == "no_data"]
        blocks = {name: {**b, "availability": _availability(b)} for name, b in blocks.items()}
        return {
            "status": "error" if len(in_error) == len(blocks) else "ok",
            "date": target_date.isoformat(),
            "generated_at": now_paris().isoformat(),
            "last_sync": _last_sync(),
            "data_availability": {name: b["availability"] for name, b in blocks.items()},
            "blocks_in_error": in_error,
            "blocks_no_data": no_data,
            **blocks,
        }

    async def garmin_health_check() -> dict[str, Any]:
        """Contrôle de la chaîne d'accès Garmin, à appeler AVANT toute analyse (aucun
        argument, jamais mis en cache, ne lève jamais d'exception).

        Fait de vrais appels authentifiés à Garmin. Sortie :
        - `status` : "ok" | "error" ; `error` : null ou {`type`: "auth" | "network" |
          "rate_limit" | "unknown", `message`: message exact de Garmin} (jeton expiré, compte
          verrouillé, limite d'appels, réseau coupé) ;
        - `checked_at` : heure du contrôle (ISO 8601, Europe/Paris) ;
        - `last_sync` : dernière synchro de la MONTRE vers Garmin Connect (pas l'heure de
          l'appel) — `at` (ISO Europe/Paris ou null), `age_minutes`, `source`
          ("device_last_used" | "user_summary"), `device` ;
        - `latest_activity` : {`id`, `start` (ISO), `type`} ou null ;
        - `data_availability` : `activities` ("ok" | "empty" | "error"), `sleep`,
          `body_battery`, `resting_hr` ("ok" | "zero" | "empty" | "error") — "zero" = valeur
          présente mais à 0 (montre non portée : ne pas la lire comme une vraie mesure),
          "empty" = aucune donnée.
        Clés historiques conservées : `connection`, `last_activity`, `freshness`."""
        checked_at = now_paris().isoformat()
        try:
            health = client.get_health(today_paris())
        except Exception as exc:  # get_health ne lève pas ; filet de sécurité
            err = {"type": classify_error(exc), "message": _garmin_error_message(exc)}
            return {
                "status": "error",
                "error": err,
                "checked_at": checked_at,
                "connection": {"status": "error", "message": err["message"]},
                "last_sync": {"status": "error", "at": None, "age_minutes": None, "source": None, "device": None},
                "latest_activity": None,
                "data_availability": {k: "error" for k in ("activities", "sleep", "body_battery", "resting_hr")},
            }
        return {"checked_at": checked_at, **health}

    def _activity_details(activity_id: str) -> dict[str, Any]:
        """Séries de l'activité (cache sans expiration : une activité passée ne change plus).
        Lève l'exception Garmin telle quelle, à mapper par l'appelant."""
        key = str(activity_id)
        cached = _get_cached(conn, "activity_details", key, ignore_freshness=True)
        if cached is None:
            cached = client.get_activity_details(activity_id)
            _store_cache(conn, "activity_details", key, cached)
        return cached

    async def garmin_get_activity_details(activity_id: str, resolution_s: int | None = None) -> dict[str, Any]:
        """Séries temporelles d'une activité : `time_s` (temps écoulé, s), `heart_rate_bpm`,
        `pace_min_per_km` (minutes décimales), `elevation_m`.

        `resolution_s` (optionnel) : sous-échantillonne à 1 point par tranche de N secondes
        (médiane), ex. 5 -> ~5x moins de tokens. Sans ce paramètre : séries complètes
        (~30 000 tokens pour 1 h). Pour une analyse chiffrée, préférer
        `garmin_analyse_activity`, qui calcule côté serveur sans renvoyer les séries.
        """
        if resolution_s is not None and resolution_s < 1:
            return _error(f"resolution_s doit être >= 1 (reçu : {resolution_s})")
        try:
            details = _activity_details(activity_id)
        except Exception as exc:
            return _garmin_error(exc)
        # Pauses et temps en mouvement calculés sur la série complète (avant
        # sous-échantillonnage) : un trou de plus de 15 s entre deux points = pause montre.
        t = [x for x in details.get("time_s") or [] if x is not None]
        timing = {
            "elapsed_s": t[-1] - t[0] if t else None,
            "moving_s": moving_time(t) if t else None,
            "pauses": pauses(t),
        }
        if resolution_s is not None and resolution_s > 1:
            details = downsample(details, resolution_s)
        return {"status": "ok" if t else "no_data", **timing, **details}

    async def garmin_analyse_activity(
        activity_id: str, fc_seuil: float, allure_cible: float | None = None, type: str = "auto"
    ) -> dict[str, Any]:
        """Analyse chiffrée d'une activité calculée côté serveur (logique du script
        `analyse_series.py` de la Skill analyst), sans renvoyer les séries brutes.

        Entrée : `fc_seuil` (FC au seuil lactique, bpm), `allure_cible` (optionnelle, allure
        prévue des blocs en min/km décimales, ex. 4.0 = 4:00/km), `type` parmi
        "auto" | "fractionne" | "seuil" | "endurance".

        Sortie (JSON compact, quelques centaines de tokens, jamais de séries brutes) :
        - `elapsed_s` (temps écoulé, pauses comprises), `moving_s` (temps en mouvement),
          `pauses` [{`start_s`, `end_s`, `duration_s`}] ;
        - `vue_ensemble` : distance et allure moyenne calculées sur le temps en mouvement
          (`pace_min_per_km` + `pace_mmss`), FC, bascule effort/récup ;
        - `zones_fc` (% du temps par zone Friel) ;
        - `blocs` (début, durée, distance, allure brute et corrigée de la pente, FC, D+, écart
          à la cible) ;
        - `recuperations` entre blocs : FC fin de bloc, FC à +60 s,
          `variation_fc_60s_bpm` (signée : négative = redescente) et `variation_fc_60s`
          formatée ("−12 bpm", "+3 bpm"), `baisse_60s_bpm` historique (positive = baisse) ;
        - `derive_cardiaque` (découplage FC/allure sur l'effort continu le plus long s'il
          dépasse 40 min, sinon null).
        Allures en minutes décimales doublées d'un champ "M:SS" (`*_mmss`, `pace_mmss`).

        `status` : "ok", "no_data" (série sans allure, ex. natation en piscine -> utiliser
        garmin_get_swim_splits) ou erreur (`error: true`, `error_type`, `message`).
        """
        if type not in ANALYSIS_TYPES:
            return _error(f"type invalide : {type} (attendu : {', '.join(ANALYSIS_TYPES)})")
        if not fc_seuil or fc_seuil <= 0:
            return _error(f"fc_seuil invalide : {fc_seuil}")
        try:
            details = _activity_details(activity_id)
        except Exception as exc:
            return _garmin_error(exc)
        return {"activity_id": str(activity_id), **analyse(details, fc_seuil, allure_cible, type)}

    async def garmin_get_swim_splits(activity_id: str) -> dict[str, Any]:
        """Longueurs d'une séance de nage en piscine, regroupées en blocs.

        `splits` : une entrée par longueur — `length_idx`, `start_s` (depuis la 1re
        longueur), `duration_s`, `distance_m`, `avg_hr`, `swolf`, `stroke`,
        `pace_min_per_100m` + `pace_min_sec_per_100m` ("M:SS"), `block` (n° de bloc),
        `suspect` (true si la durée s'écarte de plus de -20 % / +40 % de la médiane de
        référence : virage mal détecté probable — la longueur est conservée),
        `suspect_reason` ("too_fast" | "too_slow" | null), `ecart_reference_pct`.

        `blocks` : longueurs consécutives séparées par un repos de plus de 10 s — numéros
        de première/dernière longueur, `nb_longueurs`, `distance_m`, `duree_nage_s`,
        `allure_min_100m` + `allure_mmss` (alias `pace_min_per_100m` + `pace_mmss`), `fc_moy`,
        `swolf_moy`, `nages`, `nb_suspectes`, `repos_avant_s`, `repos_apres_s`, `role`
        ("echauffement" = 1er bloc, "retour_au_calme" = dernier, "serie" entre les deux ;
        tout est "serie" s'il y a moins de 3 blocs).

        Noms normalisés partagés avec `garmin_analyse_swim` : `swim_time_s` (temps nagé hors
        repos), `rest_after_s`, `first_half_pace_mmss` / `second_half_pace_mmss` (null sous 4
        longueurs).

        `totaux` : `temps_nage_s` (hors repos), `temps_repos_s`, `temps_total_s`,
        `distance_m`.

        Pour analyser une séance (allures par bloc, dérive, CSS, longueurs aberrantes),
        utiliser `garmin_analyse_swim` plutôt que de lire ces longueurs à la main.

        `status` : "ok" | "no_data" (aucune longueur : eau libre, activité non nage) |
        erreur. Cache sans expiration (longueurs brutes ; blocs recalculés à chaque appel).
        """
        key = str(activity_id)

        def fetch() -> dict[str, Any]:
            splits = client.get_swim_splits(activity_id)
            return {"status": "ok" if splits else "no_data", "splits": splits}

        result = _fetch_cached("swim_splits", key, fetch, ignore_freshness=True)
        if result.get("error"):
            return result
        return {**result, **annotate_swim(result.get("splits") or [])}

    def _swim_error(base: dict[str, Any]) -> dict[str, Any]:
        """Erreur au format de garmin_analyse_swim : `error` = {type, message} (outil créé
        avec ce format ; les outils historiques gardent `error: true`)."""
        return {
            **base,
            "status": "error",
            "error": {"type": base["error_type"], "message": base["message"]},
        }

    async def garmin_analyse_swim(
        activity_id: str,
        css_blocks: list[int] | None = None,
        target_css_min_per_100m: float | None = None,
    ) -> dict[str, Any]:
        """Analyse chiffrée d'une séance de natation en piscine, calculée par le serveur. À
        utiliser pour toute analyse de natation, en remplacement de la lecture manuelle des
        splits (`garmin_get_swim_splits`). Renvoie uniquement des agrégats, jamais la liste
        des longueurs.

        Entrée : `activity_id` ; `css_blocks` (optionnel) = [n° du bloc long, n° du bloc
        court] pour imposer les deux chronos du test de vitesse critique ;
        `target_css_min_per_100m` (optionnel) = CSS visée en minutes décimales /100 m
        (1.6667 = 1:40).

        Règles : toutes les allures sont calculées sur le temps nagé (somme des longueurs),
        jamais sur le temps écoulé. La FC (capteur optique peu fiable dans l'eau) est
        renvoyée à titre informatif et n'entre dans aucun calcul.

        Sortie :
        - `overview` : `distance_m`, `nb_longueurs`, `swim_time_s` (temps nagé), `elapsed_s`
          (de la première longueur au dernier mur), `rest_s` (= elapsed − swim),
          `pace_min_per_100m` + `pace_mmss` (sur le temps nagé), `nb_blocks` ;
        - `blocks[]` (séries délimitées par un repos > 10 s) : `n`, `first_length`,
          `last_length`, `nb_lengths`, `distance_m`, `swim_time_s` (+ `swim_time_mmss`),
          `pace_min_per_100m` + `pace_mmss`, `first_half_pace_mmss`, `second_half_pace_mmss`,
          `drift_pct` ((2e moitié − 1re) / 1re × 100, null sous 4 longueurs), `hr_avg`,
          `hr_max`, `swolf_avg`, `rest_after_s` (null pour le dernier bloc), `suspect_count` ;
        - `suspect_lengths[]` : longueurs dont la durée s'écarte de la médiane de leur bloc de
          plus de max(3 × 1,4826 × MAD, 30 % de la médiane) — `length_idx`, `duration_s`,
          `block_median_s`, `reason` ("too_fast" | "too_slow") ; blocs de moins de 4 longueurs
          non testés ;
        - `css` : null (avec la raison dans `css_null_reason`) sauf si deux chronos sont
          identifiés — par défaut le premier bloc de 400 m puis le premier bloc de 200 m qui le
          suit. Contient `t_long_s`, `t_short_s` (+ "M:SS"), `css_s_per_100m`, `css_mmss`
          (= (t_long − t_court) / (d_long − d_court) × 100), `sensitivity_short_minus_5s_mmss`
          (CSS si le court avait été nagé 5 s plus vite), si une cible est donnée
          `gap_s_per_100m` et `gap_s_over_1900m`, et `control` : `short_vs_first_half_s`
          (chrono court − même distance au début du chrono long) et `short_not_faster` (vrai si
          l'écart est supérieur à −3 s : le court n'a pas été nagé plus vite, CSS probablement
          optimiste).

        `status` : "ok" | "no_data" (aucune longueur : eau libre, activité non nage) | "error"
        avec `error` = {`type` ("auth" | "network" | "rate_limit" | "unknown" |
        "invalid_input"), `message`}.
        """
        if css_blocks is not None and (
            not isinstance(css_blocks, list) or len(css_blocks) != 2 or not all(isinstance(n, int) for n in css_blocks)
        ):
            return _swim_error(_error(f"css_blocks invalide : {css_blocks} (attendu [n° bloc long, n° bloc court])"))
        if target_css_min_per_100m is not None and target_css_min_per_100m <= 0:
            return _swim_error(_error(f"target_css_min_per_100m invalide : {target_css_min_per_100m}"))

        def fetch() -> dict[str, Any]:
            splits = client.get_swim_splits(activity_id)
            return {"status": "ok" if splits else "no_data", "splits": splits}

        result = _fetch_cached("swim_splits", str(activity_id), fetch, ignore_freshness=True)
        if result.get("error"):
            return _swim_error(result)
        analysis = analyse_swim(result.get("splits") or [], css_blocks, target_css_min_per_100m)
        # Numéros de blocs imposés hors de la séance : erreur d'appel, pas une simple CSS absente.
        if (analysis.get("css_null_reason") or "").startswith("css_blocks invalide"):
            return _swim_error(_error(analysis["css_null_reason"]))
        return {"activity_id": str(activity_id), **analysis}

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
            return _error(f"Structure de séance invalide : {exc}")
        payload["workoutName"] = name

        if dry_run:
            return {"status": "dry_run", "payload": payload}

        try:
            target_date = _parse_date(date)
        except ValueError:
            return _error(f"Date invalide : {date}")

        try:
            existing = client.get_scheduled_workouts(target_date.year, target_date.month)
        except Exception as exc:
            return _garmin_error(exc)

        duplicate = _find_duplicate_scheduled_workout(existing, name=name, date_str=date)
        if duplicate is not None:
            return {"status": "already_scheduled", **duplicate}

        try:
            upload_result = client.upload_workout(payload)
        except Exception as exc:
            return _garmin_error(exc)

        workout_id = _extract_workout_id(upload_result)
        if workout_id is None:
            return {
                "error": True,
                "status": "error",
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
            "status": "error",
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
            return _error(f"Date invalide : {start_date or end_date}")

        if start > end:
            return _error(f"start_date ({start}) postérieure à end_date ({end})")

        workouts: dict[int, dict[str, Any]] = {}
        for year, month in _month_range(start, end):
            try:
                raw = client.get_scheduled_workouts(year, month)
            except Exception as exc:
                return _garmin_error(exc)
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

        return {"status": "ok", "workouts": sorted(workouts.values(), key=lambda w: w["date"] or "")}

    async def garmin_delete_workout(workout_id: int) -> dict[str, Any]:
        """Supprime une séance structurée (le template Garmin, ce qui retire aussi son
        occurrence programmée). `workout_id` : champ `workout_id` de `garmin_list_workouts`
        ou de `garmin_push_workout`. Sortie : `status` "ok" + `workout_id`, ou erreur."""
        try:
            client.delete_workout(workout_id)
        except Exception as exc:
            return _garmin_error(exc)
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
        "garmin_get_activity_feedback": garmin_get_activity_feedback,
        "garmin_get_activity_details": garmin_get_activity_details,
        "garmin_analyse_activity": garmin_analyse_activity,
        "garmin_get_swim_splits": garmin_get_swim_splits,
        "garmin_analyse_swim": garmin_analyse_swim,
        "garmin_push_workout": garmin_push_workout,
        "garmin_list_workouts": garmin_list_workouts,
        "garmin_delete_workout": garmin_delete_workout,
        "garmin_health_check": garmin_health_check,
        "garmin_get_daily_snapshot": garmin_get_daily_snapshot,
    }
