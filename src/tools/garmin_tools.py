"""Tools MCP garmin_* (spec section 7.1 à 7.10)."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Callable

from src.analysis.series import ANALYSIS_TYPES, analyse, downsample, moving_time, pauses
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


def _error(message: str) -> dict[str, Any]:
    """Réponse d'erreur commune à tous les tools : `status` "error" (P3) + `error`/`message`
    historiques, conservés pour la rétrocompatibilité."""
    return {"error": True, "status": "error", "message": message}


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
            return _error(f"Impossible de se connecter à Garmin Connect: {exc}")

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
        return {"status": "ok", "synced_at": synced_at}

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
                return _error(_garmin_error_message(exc))
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
        pour compatibilité). Niveaux sur 0-100. `status` : "ok" | "no_data" (aucun point
        mesuré, valeurs null) | erreur."""
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
        susceptible d'être recalculée). `status` : "ok" | "no_data" (`resting_hr` null) | erreur."""
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
        "no_data" (nuit non synchronisée : toutes les valeurs null, jamais 0) | erreur."""
        try:
            target_date = _parse_date(date)
        except ValueError:
            return _error(f"Date invalide : {date}")
        iso = target_date.isoformat()
        result = _fetch_cached("sleep", iso, lambda: client.get_sleep(iso), force_refresh=force_refresh)
        return result if result.get("error") else {"date": iso, **result}

    async def garmin_get_recent_activities(days: int = 7, force_refresh: bool = False) -> dict[str, Any]:
        """Activités des `days` derniers jours (plus récentes en premier). `status` : "ok" |
        "no_data" (aucune activité sur la période ; jamais mis en cache, pour voir tout de
        suite une activité synchronisée en retard) | erreur."""
        today = today_paris()

        def fetch() -> dict[str, Any]:
            activities = client.get_recent_activities(today - timedelta(days=days), fetch_limit=days * 4)
            return {"status": "ok" if activities else "no_data", "activities": activities}

        return _fetch_cached("recent_activities", f"{today.isoformat()}:days={days}", fetch, force_refresh=force_refresh)

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

    async def garmin_get_activity_weather(activity_id: str) -> dict[str, Any]:
        """Météo d'une activité : températures en °C, `humidity_percent`, vent
        `wind_speed_kmh` / `wind_gust_kmh` (km/h, convertis depuis des mph supposés, cf.
        `wind_unit_source` ; rafale null = aucune rafale signalée), `wind_direction_compass`,
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

    async def garmin_health_check() -> dict[str, Any]:
        """État du connecteur Garmin, à appeler en début de routine planifiée : `connection`
        (login Garmin), `last_sync` (dernier envoi de la montre : `at` ISO Paris, `device`),
        `last_activity` (dernière activité reçue : id, début, type), `freshness` par métrique
        du jour (`sleep.sleep_end`, `body_battery.last_point_at`, `resting_hr.date`), chacun
        avec son `status` ("ok" | "no_data" | "error" + `message`). Jamais mis en cache.
        `checked_at` : horodatage du contrôle (Europe/Paris)."""
        try:
            health = client.get_health(today_paris())
        except Exception as exc:
            return _error(_garmin_error_message(exc))
        return {"checked_at": now_paris().isoformat(), **health}

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
            return _error(_garmin_error_message(exc))
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

        Sortie (JSON compact) : `vue_ensemble` (durée, distance, allure, FC, bascule
        effort/récup), `zones_fc` (% du temps par zone Friel), `blocs` (début, durée,
        distance, allure brute et corrigée de la pente, FC, D+, écart à la cible),
        `recuperations` entre blocs (FC fin de bloc, FC à +60 s, baisse en bpm),
        `derive_cardiaque` (découplage FC/allure sur l'effort continu le plus long s'il
        dépasse 40 min, sinon null). Allures doublées d'un champ `*_mmss` ("M:SS").

        `status` : "ok", "no_data" (série sans allure, ex. natation en piscine -> utiliser
        garmin_get_swim_splits) ou erreur (`error: true`, `message`).
        """
        if type not in ANALYSIS_TYPES:
            return _error(f"type invalide : {type} (attendu : {', '.join(ANALYSIS_TYPES)})")
        if not fc_seuil or fc_seuil <= 0:
            return _error(f"fc_seuil invalide : {fc_seuil}")
        try:
            details = _activity_details(activity_id)
        except Exception as exc:
            return _error(_garmin_error_message(exc))
        return {"activity_id": str(activity_id), **analyse(details, fc_seuil, allure_cible, type)}

    async def garmin_get_swim_splits(activity_id: str) -> dict[str, Any]:
        key = str(activity_id)

        def fetch() -> dict[str, Any]:
            splits = client.get_swim_splits(activity_id)
            return {"status": "ok" if splits else "no_data", "splits": splits}

        return _fetch_cached("swim_splits", key, fetch, ignore_freshness=True)

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
            return _error(_garmin_error_message(exc))

        duplicate = _find_duplicate_scheduled_workout(existing, name=name, date_str=date)
        if duplicate is not None:
            return {"status": "already_scheduled", **duplicate}

        try:
            upload_result = client.upload_workout(payload)
        except Exception as exc:
            return _error(_garmin_error_message(exc))

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
                return _error(_garmin_error_message(exc))
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
        try:
            client.delete_workout(workout_id)
        except Exception as exc:
            return _error(_garmin_error_message(exc))
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
        "garmin_analyse_activity": garmin_analyse_activity,
        "garmin_get_swim_splits": garmin_get_swim_splits,
        "garmin_push_workout": garmin_push_workout,
        "garmin_list_workouts": garmin_list_workouts,
        "garmin_delete_workout": garmin_delete_workout,
        "garmin_health_check": garmin_health_check,
    }
