"""Wrapper autour de `garminconnect` : login, cache de session/token, parsing des payloads.

Les endpoints Garmin Connect utilisés ici (`connectapi` interne à `garminconnect`) ne sont pas
documentés officiellement par Garmin ; la forme des réponses JSON ci-dessous est basée sur la
structure connue de l'API à la date d'écriture. Après déploiement, lancer `garmin_sync` puis
inspecter le contenu brut de `garmin_cache` pour confirmer/ajuster le parsing si Garmin a fait
évoluer ses réponses (cf. README).
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, TypeVar

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from src.models import PARIS_TZ

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Table de correspondance des types d'activité Garmin -> vocabulaire du système.
# Les types Garmin non listés ici sont retournés tels quels (cf. spec section 7.7).
ACTIVITY_TYPE_MAP: dict[str, str] = {
    "running": "course",
    "trail_running": "course",
    "treadmill_running": "course",
    "track_running": "course",
    "indoor_running": "course",
    "street_running": "course",
    "cycling": "velo",
    "road_biking": "velo",
    "indoor_cycling": "velo",
    "virtual_ride": "velo",
    "mountain_biking": "velo",
    "gravel_cycling": "velo",
    "cyclocross": "velo",
    "track_cycling": "velo",
    "recumbent_cycling": "velo",
    "bmx": "velo",
    "e_bike_fitness": "velo",
    "e_bike_mountain": "velo",
    "lap_swimming": "nage",
    "open_water_swimming": "nage",
    "strength_training": "renfo",
}


# Types d'erreur d'accès exposés aux clients MCP (`error_type`) : permettent au coach de
# distinguer une session expirée d'une coupure réseau ou d'un blocage temporaire de Garmin.
ERROR_TYPES = ("auth", "network", "rate_limit", "unknown")


class GarminAuthenticationError(Exception):
    """Identifiants Garmin invalides, jeton expiré ou compte verrouillé."""

    error_type = "auth"


class GarminUnavailableError(Exception):
    """Garmin Connect injoignable ou en erreur (hors identifiants invalides).

    `error_type` : "network" (coupure, erreur HTTP, timeout), "rate_limit" (Garmin limite
    les appels, HTTP 429) ou "unknown" (exception non catégorisée)."""

    def __init__(self, message: str, error_type: str = "network") -> None:
        super().__init__(message)
        self.error_type = error_type


def classify_error(exc: BaseException) -> str:
    """Type d'erreur d'accès ("auth" | "network" | "rate_limit" | "unknown")."""
    explicit = getattr(exc, "error_type", None)
    if explicit in ERROR_TYPES:
        return explicit
    if isinstance(exc, GarminConnectAuthenticationError):
        return "auth"
    if isinstance(exc, GarminConnectTooManyRequestsError):
        return "rate_limit"
    if isinstance(exc, GarminConnectConnectionError):
        return _type_from_message(str(exc))
    return "unknown"


def _type_from_message(message: str) -> str:
    """Les erreurs HTTP de garminconnect arrivent sous forme de texte ("API Error 401 - ...",
    "Mobile login returned 429 ...") : le code HTTP tranche le type."""
    if "429" in message or "rate limit" in message.lower():
        return "rate_limit"
    if "401" in message or "403" in message:
        return "auth"
    return "network"


class GarminClient:
    """Wrapper haut niveau autour de `garminconnect.Garmin`."""

    def __init__(self, email: str, password: str, tokenstore_path: str) -> None:
        self._email = email
        self._password = password
        self._tokenstore_path = tokenstore_path
        self._garmin = Garmin(email=email, password=password)
        self._logged_in = False
        # Résumé quotidien partagé entre blocs pendant `get_daily_snapshot` (évite de
        # rappeler get_user_summary pour Body Battery puis FC repos).
        self._summary_override: dict[str, dict[str, Any]] = {}
        # Nuits déjà lues pendant `get_daily_snapshot` (sommeil puis heure de réveil de la
        # Body Battery) : None hors snapshot, pas de mémoire entre deux appels de tools.
        self._sleep_memo: dict[str, Any] | None = None

    # -- authentification -------------------------------------------------

    def _ensure_login(self) -> None:
        if self._logged_in:
            return
        try:
            self._garmin.login(tokenstore=self._tokenstore_path)
            self._logged_in = True
        # Les messages ci-dessous ne sont volontairement pas préfixés en français
        # ("Identifiants Garmin invalides : ", "Garmin Connect injoignable : ", etc.) :
        # ce préfixage est la responsabilité unique de `_garmin_error_message`
        # (src/tools/garmin_tools.py), pour éviter un double préfixage à l'affichage.
        except GarminConnectAuthenticationError as exc:
            raise GarminAuthenticationError(str(exc)) from exc
        except GarminConnectTooManyRequestsError as exc:
            raise GarminUnavailableError(
                f"Trop de tentatives de connexion, réessayer plus tard : {exc}", error_type="rate_limit"
            ) from exc
        except GarminConnectConnectionError as exc:
            kind = _type_from_message(str(exc))
            if kind == "auth":
                raise GarminAuthenticationError(str(exc)) from exc
            raise GarminUnavailableError(str(exc), error_type=kind) from exc
        except Exception as exc:  # défensif : ne jamais laisser fuiter une exception non catégorisée
            raise GarminUnavailableError(
                f"Erreur inattendue lors de la connexion : {exc}", error_type="unknown"
            ) from exc

    def _call(self, fn: Callable[..., T], *args: Any, _retry: bool = True, **kwargs: Any) -> T:
        self._ensure_login()
        try:
            return fn(*args, **kwargs)
        except GarminConnectAuthenticationError as exc:
            if _retry:
                self._logged_in = False
                return self._call(fn, *args, _retry=False, **kwargs)
            raise GarminAuthenticationError(str(exc)) from exc
        except (GarminConnectConnectionError, GarminConnectTooManyRequestsError) as exc:
            if _retry:
                self._logged_in = False
                return self._call(fn, *args, _retry=False, **kwargs)
            kind = classify_error(exc)
            if kind == "auth":
                raise GarminAuthenticationError(str(exc)) from exc
            raise GarminUnavailableError(str(exc), error_type=kind) from exc

    # -- données individuelles ---------------------------------------------

    # Garmin ne recalcule le training status/load qu'après une activité synchronisée : "aujourd'hui"
    # est très souvent vide. On remonte donc jusqu'à `max_lookback_days` en arrière et on renvoie la
    # date réelle des données trouvées (jamais silencieusement mélangée avec "aujourd'hui").
    def _fetch_training_status_with_fallback(
        self, today: date, max_lookback_days: int = 7
    ) -> tuple[dict[str, Any], str]:
        for offset in range(max_lookback_days + 1):
            d = today - timedelta(days=offset)
            raw = self._call(self._garmin.get_training_status, d.isoformat())
            if isinstance(raw, dict) and raw.get("mostRecentTrainingStatus"):
                return raw, d.isoformat()
        return {}, today.isoformat()

    def get_training_status(
        self, today: date, prefetched: tuple[dict[str, Any], str] | None = None
    ) -> dict[str, Any]:
        raw, found_date = prefetched or self._fetch_training_status_with_fallback(today)
        device_entry = _first_device_status(raw)
        status_label = device_entry.get("trainingStatusFeedbackPhrase") or device_entry.get("trainingStatus")
        acute_dto = device_entry.get("acuteTrainingLoadDTO") or {}
        acute = acute_dto.get("dailyTrainingLoadAcute") or acute_dto.get("acuteTrainingLoad")
        chronic = acute_dto.get("dailyTrainingLoadChronic") or acute_dto.get("chronicTrainingLoad")
        return {
            "status": "ok" if device_entry else "no_data",
            "date": found_date,
            # Libellé Garmin (ex. "PRODUCTIVE_1") : anciennement sous la clé "status", qui porte
            # désormais l'état de la donnée (ok/no_data/error), commun à tous les tools.
            "training_status": str(status_label) if status_label is not None else None,
            "acute_load": _to_int(acute, default=None),
            "chronic_load": _to_int(chronic, default=None),
        }

    def _user_summary(self, iso_date: str) -> dict[str, Any]:
        """Résumé quotidien Garmin (`get_user_summary`), utilisé en complément (valeur au
        réveil, dernière synchro). Jamais bloquant : {} en cas d'erreur ou de forme
        inattendue — les champs qui en dépendent valent alors null."""
        if iso_date in self._summary_override:
            return self._summary_override[iso_date]
        try:
            raw = self._call(self._garmin.get_user_summary, iso_date)
        except Exception as exc:  # défensif : donnée complémentaire, pas critique
            logger.info("get_user_summary(%s) indisponible : %s", iso_date, exc)
            return {}
        return raw if isinstance(raw, dict) else {}

    def get_body_battery(self, iso_date: str) -> dict[str, Any]:
        """Body Battery de la journée.

        - `value` : dernière valeur mesurée de la journée (= `latest`, conservé pour compat.) ;
        - `charged` / `drained` : total de points rechargés / consommés sur la journée
          (cumuls Garmin, pas des niveaux) ;
        - `at_wake` : niveau au réveil (résumé quotidien Garmin, null si indisponible) ;
        - `min` / `max` : niveaux extrêmes mesurés sur la journée, `min_at` / `max_at` leur
          horodatage ;
        - `latest_at` : horodatage (ISO Europe/Paris) de la dernière valeur ;
        - `at_wake_at` : heure du réveil (fin de nuit Garmin), null si la nuit n'a pas été
          enregistrée (montre non portée) ;
        - `availability` : "ok" | "zero" (points présents mais tous à 0 : donnée non
          exploitable) | "empty" (aucun point).
        """
        raw = self._call(self._garmin.get_body_battery, iso_date, iso_date)
        entry = raw[0] if isinstance(raw, list) and raw else {}
        points = [
            p
            for p in (entry.get("bodyBatteryValuesArray") or [])
            if isinstance(p, (list, tuple)) and len(p) >= 2 and p[1] is not None
        ]
        availability = _bb_availability(points)
        if availability != "ok":
            return {
                "status": "no_data",
                "availability": availability,
                "value": None,
                "charged": None,
                "drained": None,
                "at_wake": None,
                "at_wake_at": None,
                "min": None,
                "min_at": None,
                "max": None,
                "max_at": None,
                "latest": None,
                "latest_at": None,
            }
        levels = [p[1] for p in points]
        summary = self._user_summary(iso_date)
        at_wake = _to_int(summary.get("bodyBatteryAtWakeTime"), default=None)
        low = min(points, key=lambda p: p[1])
        high = max(points, key=lambda p: p[1])
        return {
            "status": "ok",
            "value": _to_int(levels[-1], default=None),
            "charged": _to_int(entry.get("charged"), default=None),
            "drained": _to_int(entry.get("drained"), default=None),
            "availability": "ok",
            "at_wake": at_wake,
            "at_wake_at": self._wake_time(iso_date) if at_wake is not None else None,
            "min": _to_int(low[1], default=None),
            "min_at": _epoch_ms_to_iso(low[0]),
            "max": _to_int(high[1], default=None),
            "max_at": _epoch_ms_to_iso(high[0]),
            "latest": _to_int(levels[-1], default=None),
            "latest_at": _epoch_ms_to_iso(points[-1][0]),
        }

    def _sleep_raw(self, iso_date: str) -> Any:
        if self._sleep_memo is not None and iso_date in self._sleep_memo:
            return self._sleep_memo[iso_date]
        raw = self._call(self._garmin.get_sleep_data, iso_date)
        if self._sleep_memo is not None:
            self._sleep_memo[iso_date] = raw
        return raw

    def _wake_time(self, iso_date: str) -> str | None:
        """Heure de fin de la nuit se terminant le jour donné (ISO Paris), null si absente."""
        try:
            raw = self._sleep_raw(iso_date)
        except Exception as exc:  # donnée complémentaire, jamais bloquante
            logger.info("get_sleep_data(%s) indisponible : %s", iso_date, exc)
            return None
        daily = ((raw or {}).get("dailySleepDTO") or {}) if isinstance(raw, dict) else {}
        return _epoch_ms_to_iso(daily.get("sleepEndTimestampGMT"))

    def get_vo2max(self, today: date, max_lookback_days: int = 7) -> dict[str, Any]:
        # Comme le training status/load, Garmin ne recalcule le VO2max qu'après une activité
        # synchronisée : "aujourd'hui" est très souvent vide. On remonte donc en arrière et on
        # renvoie la date réelle de la dernière valeur trouvée (jamais mélangée avec "aujourd'hui").
        for offset in range(max_lookback_days + 1):
            d = today - timedelta(days=offset)
            raw = self._call(self._garmin.get_max_metrics, d.isoformat())
            entry = raw[0] if isinstance(raw, list) and raw else (raw if isinstance(raw, dict) else {})
            generic = entry.get("generic") or {}
            cycling = entry.get("cycling") or {}
            running_vo2 = generic.get("vo2MaxValue") or generic.get("vo2MaxPreciseValue")
            cycling_vo2 = cycling.get("vo2MaxValue") or cycling.get("vo2MaxPreciseValue")
            if running_vo2 is not None or cycling_vo2 is not None:
                return {
                    "status": "ok",
                    "date": d.isoformat(),
                    "running_vo2max": _to_int(running_vo2, default=None),
                    "cycling_vo2max": _to_int(cycling_vo2, default=None),
                }
        return {"status": "no_data", "date": today.isoformat(), "running_vo2max": None, "cycling_vo2max": None}

    def get_resting_hr(self, iso_date: str) -> dict[str, Any]:
        raw = self._call(self._garmin.get_rhr_day, iso_date)
        metrics = ((raw or {}).get("allMetrics") or {}).get("metricsMap") or {}
        series = metrics.get("WELLNESS_RESTING_HEART_RATE") or []
        value = None
        measured_on = None
        for point in series:
            if isinstance(point, dict) and point.get("value") is not None:
                value = point["value"]
                measured_on = point.get("calendarDate") or iso_date
        # Garmin ne date la FC de repos qu'au jour (`calendarDate`), sans heure : c'est une
        # valeur journalière recalculée à chaque synchro (cf. `last_sync_at`).
        availability = "empty" if value is None else "zero" if value == 0 else "ok"
        if availability != "ok":
            return {
                "status": "no_data",
                "availability": availability,
                "resting_hr": None,
                "measured_on": None,
                "last_sync_at": None,
            }
        # La FC repos du jour est recalculée par Garmin au fil des synchros : `last_sync_at`
        # dit sur quelles données la valeur repose (une valeur du matin peut encore bouger).
        summary = self._user_summary(iso_date)
        return {
            "status": "ok",
            "availability": "ok",
            "resting_hr": _to_int(value, default=None),
            "measured_on": measured_on,
            "last_sync_at": _garmin_timestamp_to_iso(summary.get("lastSyncTimestampGMT")),
        }

    def get_sleep(self, iso_date: str) -> dict[str, Any]:
        raw = self._sleep_raw(iso_date)
        daily = (raw or {}).get("dailySleepDTO") or {}

        def minutes(seconds_key: str) -> int | None:
            seconds = daily.get(seconds_key)
            return int(seconds // 60) if isinstance(seconds, (int, float)) else None

        score = ((daily.get("sleepScores") or {}).get("overall") or {}).get("value")
        # Nuit non (encore) synchronisée : Garmin renvoie un dailySleepDTO sans
        # sleepTimeSeconds (ou à 0) — c'est une absence de donnée, pas une nuit de 0 min.
        availability = _sleep_availability(daily)
        if availability != "ok":
            return {
                "status": "no_data",
                "availability": availability,
                "duration_minutes": None,
                "deep_minutes": None,
                "light_minutes": None,
                "rem_minutes": None,
                "awake_minutes": None,
                "score": None,
            }
        return {
            "status": "ok",
            "availability": "ok",
            "duration_minutes": minutes("sleepTimeSeconds"),
            "deep_minutes": minutes("deepSleepSeconds"),
            "light_minutes": minutes("lightSleepSeconds"),
            "rem_minutes": minutes("remSleepSeconds"),
            "awake_minutes": minutes("awakeSleepSeconds"),
            "score": _to_int(score, default=None),
        }

    def get_recent_activities(self, since: date, fetch_limit: int) -> list[dict[str, Any]]:
        raw = self._call(self._garmin.get_activities, 0, max(fetch_limit, 20))
        activities = raw if isinstance(raw, list) else (raw.get("activities") if isinstance(raw, dict) else None) or []
        results: list[dict[str, Any]] = []
        for act in activities:
            if not isinstance(act, dict):
                continue
            start_local = act.get("startTimeLocal") or act.get("startTimeGMT") or ""
            act_date_str = str(start_local).split(" ")[0].split("T")[0]
            try:
                act_date = datetime.strptime(act_date_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            if act_date < since:
                continue
            type_key = (act.get("activityType") or {}).get("typeKey") or "unknown"
            normalized_type = ACTIVITY_TYPE_MAP.get(type_key, type_key)
            duration_seconds = act.get("duration")
            distance_m = act.get("distance")
            elevation_gain = act.get("elevationGain")

            allure_moyenne = None
            allure_rapide = None
            if normalized_type == "course":
                allure_moyenne = _pace_min_per_km(act.get("averageSpeed"))
                allure_rapide = _pace_min_per_km(act.get("maxSpeed"))

            piscine_longueur_m = None
            nb_longueurs = None
            swolf_moyen = None
            cadence_moyenne_brasses_min = None
            brasses_moyenne_longueur = None
            brasses_total = None
            meilleur_100m_sec = None
            allure_moyenne_min_100m = None
            if normalized_type == "nage":
                piscine_longueur_m = _pool_length_m(act.get("poolLength"), act.get("unitOfPoolLength"))
                nb_longueurs = _to_int(act.get("activeLengths"), default=None)
                swolf_moyen = _to_int(act.get("averageSwolf"), default=None)
                cadence_moyenne_brasses_min = _to_int(act.get("averageSwimCadenceInStrokesPerMinute"), default=None)
                brasses_moyenne_longueur = _round_or_none(act.get("avgStrokes"))
                brasses_total = _to_int(act.get("strokes"), default=None)
                meilleur_100m_sec = _round_or_none(act.get("fastestSplit_100"))
                allure_moyenne_min_100m = _pace_min_per_100m(act.get("averageSpeed"))

            results.append(
                {
                    "activity_id": act.get("activityId"),
                    "date": act_date.isoformat(),
                    "type": normalized_type,
                    "garmin_type": type_key,
                    "duration_minutes": int(duration_seconds // 60) if isinstance(duration_seconds, (int, float)) else None,
                    "distance_km": round(distance_m / 1000, 2) if isinstance(distance_m, (int, float)) else None,
                    "avg_hr": _to_int(act.get("averageHR"), default=None),
                    "max_hr": _to_int(act.get("maxHR"), default=None),
                    "denivele_m": _to_int(elevation_gain, default=None),
                    "allure_moyenne_min_km": allure_moyenne,
                    "allure_moyenne_mmss": _format_pace_mm_ss(allure_moyenne),
                    # Alias normalisés (même valeur que allure_moyenne_*), présents pour la course.
                    "pace_min_per_km": allure_moyenne,
                    "pace_mmss": _format_pace_mm_ss(allure_moyenne),
                    "allure_rapide_min_km": allure_rapide,
                    "allure_rapide_mmss": _format_pace_mm_ss(allure_rapide),
                    "effet_entrainement": act.get("trainingEffectLabel"),
                    "effet_aerobie": _round_or_none(act.get("aerobicTrainingEffect")),
                    "effet_aerobie_message": act.get("aerobicTrainingEffectMessage"),
                    "effet_anaerobie": _round_or_none(act.get("anaerobicTrainingEffect")),
                    "effet_anaerobie_message": act.get("anaerobicTrainingEffectMessage"),
                    "piscine_longueur_m": piscine_longueur_m,
                    "nb_longueurs": nb_longueurs,
                    "swolf_moyen": swolf_moyen,
                    "cadence_moyenne_brasses_min": cadence_moyenne_brasses_min,
                    "brasses_moyenne_longueur": brasses_moyenne_longueur,
                    "brasses_total": brasses_total,
                    "meilleur_100m_sec": meilleur_100m_sec,
                    "allure_moyenne_min_100m": allure_moyenne_min_100m,
                    "allure_moyenne_100m_mmss": _format_pace_mm_ss(allure_moyenne_min_100m),
                }
            )
            if normalized_type == "velo":
                # Vitesse = distance officielle / temps chronométré (pauses montre exclues),
                # comme la vitesse moyenne affichée par Garmin. Clés ajoutées au vélo
                # seulement : les sorties course et nage restent strictement inchangées.
                results[-1]["vitesse_moyenne_kmh"] = (
                    round(distance_m / duration_seconds * 3.6, 1)
                    if isinstance(distance_m, (int, float)) and distance_m > 0
                    and isinstance(duration_seconds, (int, float)) and duration_seconds > 0
                    else None
                )
                results[-1]["vitesse_max_kmh"] = _speed_kmh(act.get("maxSpeed"))
        results.sort(key=lambda a: a["date"], reverse=True)
        return results

    def get_training_load(
        self, today: date, prefetched: tuple[dict[str, Any], str] | None = None
    ) -> dict[str, Any]:
        raw, found_date = prefetched or self._fetch_training_status_with_fallback(today)
        device_entry = _first_device_status(raw)
        acute_dto = device_entry.get("acuteTrainingLoadDTO") or {}
        acute = _to_int(acute_dto.get("dailyTrainingLoadAcute") or acute_dto.get("acuteTrainingLoad"), default=None)
        chronic = _to_int(acute_dto.get("dailyTrainingLoadChronic") or acute_dto.get("chronicTrainingLoad"), default=None)
        if not chronic or acute is None:
            ratio = None
            load_status = "no_status"
        else:
            ratio = round(acute / chronic, 2)
            if ratio > 1.5:
                load_status = "high"
            elif ratio < 0.8:
                load_status = "low"
            else:
                load_status = "optimal"
        return {
            "status": "ok" if acute is not None or chronic is not None else "no_data",
            "date": found_date,
            "acute_load": acute,
            "chronic_load": chronic,
            "load_ratio": ratio,
            "load_status": load_status,
            "cible_charge_chronique_min": _round_or_none(acute_dto.get("minTrainingLoadChronic")),
            "cible_charge_chronique_max": _round_or_none(acute_dto.get("maxTrainingLoadChronic")),
            "acwr_pourcentage": _to_int(acute_dto.get("acwrPercent"), default=None),
            "acwr_statut": acute_dto.get("acwrStatus"),
        }

    def get_training_load_balance(
        self, today: date, prefetched: tuple[dict[str, Any], str] | None = None
    ) -> dict[str, Any]:
        """Répartition de charge mensuelle aérobie faible / aérobie élevée / anaérobique
        ("Training Load Focus" Garmin) — cf. mostRecentTrainingLoadBalance."""
        raw, found_date = prefetched or self._fetch_training_status_with_fallback(today)
        balance = raw.get("mostRecentTrainingLoadBalance") if isinstance(raw, dict) else None
        device_map = (balance or {}).get("metricsTrainingLoadBalanceDTOMap") or {}
        entry = next(iter(device_map.values()), {}) if device_map else {}
        return {
            "status": "ok" if entry else "no_data",
            "date": found_date,
            "aerobie_faible": _round_or_none(entry.get("monthlyLoadAerobicLow")),
            "aerobie_faible_cible_min": _to_int(entry.get("monthlyLoadAerobicLowTargetMin"), default=None),
            "aerobie_faible_cible_max": _to_int(entry.get("monthlyLoadAerobicLowTargetMax"), default=None),
            "aerobie_elevee": _round_or_none(entry.get("monthlyLoadAerobicHigh")),
            "aerobie_elevee_cible_min": _to_int(entry.get("monthlyLoadAerobicHighTargetMin"), default=None),
            "aerobie_elevee_cible_max": _to_int(entry.get("monthlyLoadAerobicHighTargetMax"), default=None),
            "charge_anaerobique": _round_or_none(entry.get("monthlyLoadAnaerobic")),
            "charge_anaerobique_cible_min": _to_int(entry.get("monthlyLoadAnaerobicTargetMin"), default=None),
            "charge_anaerobique_cible_max": _to_int(entry.get("monthlyLoadAnaerobicTargetMax"), default=None),
            "feedback": entry.get("trainingBalanceFeedbackPhrase"),
        }

    def get_lactate_threshold(self) -> dict[str, Any]:
        raw = self._call(self._garmin.get_lactate_threshold, latest=True)
        data = raw if isinstance(raw, dict) else {}

        speed_hr = data.get("speed_and_heart_rate") or {}
        hr = speed_hr.get("heartRate")
        raw_speed = speed_hr.get("speed")
        # Le champ "speed" de cet endpoit est à une échelle 10x inférieure au m/s standard
        # (validé contre un vrai compte : brut 0.369 -> ×10 -> 3.69 m/s -> 4:31/km, conforme
        # à l'allure de seuil affichée dans l'app Garmin, 4:30/km).
        pace_min_per_km = _pace_min_per_km(raw_speed * 10) if isinstance(raw_speed, (int, float)) else None

        # "power" ne contient que la dernière mesure calculée, pour un seul sport à la fois
        # (RUNNING ou CYCLING) — jamais les deux simultanément dans cette réponse.
        power_data = data.get("power") or {}
        sport = power_data.get("sport")
        running_power = None
        running_power_per_kg = None
        cycling_ftp = None
        if sport == "RUNNING":
            running_power = _to_int(power_data.get("functionalThresholdPower"), default=None)
            running_power_per_kg = _round_or_none(power_data.get("powerToWeight"), ndigits=2)
        elif sport == "CYCLING":
            cycling_ftp = _to_int(power_data.get("functionalThresholdPower"), default=None)

        has_data = any(v is not None for v in (hr, pace_min_per_km, running_power, cycling_ftp))
        return {
            "status": "ok" if has_data else "no_data",
            "running": {
                "heart_rate": _to_int(hr, default=None),
                "pace_min_per_km": pace_min_per_km,
                "pace_mmss": _format_pace_mm_ss(pace_min_per_km),
                "power_watts": running_power,
                "power_watts_per_kg": running_power_per_kg,
            },
            "cycling": {
                "ftp_watts": cycling_ftp,
            },
        }

    def get_activity_summary(self, activity_id: str | int) -> dict[str, Any]:
        """Résumé officiel d'une activité (`get_activity`) : `sport` ("course" | "velo" |
        "nage" | "renfo" | type Garmin brut), `type_garmin`, `distance_km` (distance officielle),
        `duree_s` (temps chronométré, pauses montre exclues), `moving_s` (temps en mouvement
        Garmin, arrêts sans pause aussi exclus), `elapsed_s`, `vitesse_moy_kmh`,
        `vitesse_max_kmh`. Valeurs absentes = null, jamais 0."""
        raw = self._call(self._garmin.get_activity, str(activity_id))
        data = raw if isinstance(raw, dict) else {}
        summary = data.get("summaryDTO") or {}
        type_key = (data.get("activityTypeDTO") or {}).get("typeKey")
        distance = summary.get("distance")

        def seconds(key: str) -> float | None:
            value = summary.get(key)
            return round(value, 1) if isinstance(value, (int, float)) and value > 0 else None

        return {
            "status": "ok" if type_key or summary else "no_data",
            "sport": ACTIVITY_TYPE_MAP.get(type_key, type_key) if type_key else None,
            "type_garmin": type_key,
            "distance_km": round(distance / 1000, 2) if isinstance(distance, (int, float)) and distance > 0 else None,
            "duree_s": seconds("duration"),
            "moving_s": seconds("movingDuration"),
            "elapsed_s": seconds("elapsedDuration"),
            "vitesse_moy_kmh": _speed_kmh(summary.get("averageSpeed")),
            "vitesse_max_kmh": _speed_kmh(summary.get("maxSpeed")),
        }

    def get_activity_feedback(self, activity_id: str | int) -> dict[str, Any]:
        """Auto-évaluation saisie sur la montre (ou dans l'app) en fin d'activité : sensations
        et effort perçu. Vérifié sur un vrai compte (17/10/2026) : `summaryDTO` de
        `get_activity` porte `directWorkoutFeel` (0/25/50/75/100) et `directWorkoutRpe`
        (10 à 100, soit l'effort perçu × 10) ; absents de la liste `get_activities`.
        `status` "no_data" si rien n'a été saisi pour cette activité."""
        raw = self._call(self._garmin.get_activity, str(activity_id))
        summary = ((raw or {}).get("summaryDTO") or {}) if isinstance(raw, dict) else {}
        feel = summary.get("directWorkoutFeel")
        rpe = summary.get("directWorkoutRpe")
        feel = feel if isinstance(feel, (int, float)) else None
        effort = round(rpe / 10) if isinstance(rpe, (int, float)) and rpe > 0 else None
        return {
            "status": "ok" if feel is not None or effort is not None else "no_data",
            "sensations": _feel_label(feel),
            "sensations_brut": _to_int(feel, default=None),
            "effort_percu_sur_10": effort,
            "effort_percu_libelle": _rpe_label(effort),
        }

    def get_activity_weather(self, activity_id: str | int) -> dict[str, Any]:
        raw = self._call(self._garmin.get_activity_weather, str(activity_id))
        data = raw if isinstance(raw, dict) else {}
        weather_type = data.get("weatherTypeDTO") or {}
        station = data.get("weatherStationDTO") or {}
        wind_raw = data.get("windSpeed")
        gust_raw = data.get("windGust")

        # Distance station <-> départ de la séance. Coordonnées de la station : celles du
        # payload météo si présentes, sinon table OACI locale ; départ : résumé d'activité.
        # Sans météo, Garmin renvoie un payload à null avec latitude/longitude = 0.0 (observé
        # sur 24737274389) : (0, 0) n'est pas une position, sinon distance "station" absurde.
        station_lat, station_lon = data.get("latitude"), data.get("longitude")
        if (
            not isinstance(station_lat, (int, float))
            or not isinstance(station_lon, (int, float))
            or (station_lat == 0 and station_lon == 0)
        ):
            station_lat, station_lon = _STATION_COORDS.get(str(station.get("id") or "").upper(), (None, None))
        has_weather = data.get("temp") is not None or bool(station)
        start_lat, start_lon = self._activity_start(activity_id) if has_weather else (None, None)
        distance_km = (
            round(_haversine_km(station_lat, station_lon, start_lat, start_lon), 1)
            if None not in (station_lat, station_lon, start_lat, start_lon)
            else None
        )
        return {
            "status": "ok" if has_weather else "no_data",
            # temp/apparentTemp/dewPoint : observés en °F sur un vrai compte (66 pour une
            # soirée d'août pluvieuse à Paris - incohérent en °C) — convertis en °C.
            "temp_celsius": _fahrenheit_to_celsius(data.get("temp")),
            "apparent_temp_celsius": _fahrenheit_to_celsius(data.get("apparentTemp")),
            "dew_point_celsius": _fahrenheit_to_celsius(data.get("dewPoint")),
            "humidity_percent": _to_int(data.get("relativeHumidity"), default=None),
            # windSpeed/windGust : déjà en km/h (confirmé contre l'app Garmin Connect),
            # contrairement aux températures en °F. Champs *_raw conservés pour compat.
            "wind_speed_raw": _to_int(data.get("windSpeed"), default=None),
            "wind_gust_raw": _to_int(data.get("windGust"), default=None),
            "wind_speed_kmh": _round_or_none(wind_raw),
            "wind_speed_unit": "km/h",
            # null = aucune rafale signalée par la station (pas une rafale nulle).
            "wind_gust_kmh": _round_or_none(gust_raw),
            "wind_direction_compass": data.get("windDirectionCompassPoint"),
            "condition": weather_type.get("desc"),
            "station_name": station.get("name"),
            "station_id": station.get("id"),
            "station_distance_km": distance_km,
            # Au-delà, la mesure peut s'écarter sensiblement des conditions sur le parcours.
            "station_far": distance_km > _STATION_FAR_KM if distance_km is not None else None,
        }

    def _activity_start(self, activity_id: str | int) -> tuple[float | None, float | None]:
        """Coordonnées de départ d'une activité (résumé Garmin), (None, None) si absentes
        ou si l'appel échoue — donnée complémentaire, jamais bloquante."""
        try:
            raw = self._call(self._garmin.get_activity, str(activity_id))
        except Exception as exc:
            logger.info("get_activity(%s) indisponible : %s", activity_id, exc)
            return None, None
        summary = (raw or {}).get("summaryDTO") or {} if isinstance(raw, dict) else {}
        lat, lon = summary.get("startLatitude"), summary.get("startLongitude")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            return float(lat), float(lon)
        return None, None

    def get_activity_details(self, activity_id: str | int) -> dict[str, Any]:
        raw = self._call(self._garmin.get_activity_details, str(activity_id))
        data = raw if isinstance(raw, dict) else {}
        descriptors = data.get("metricDescriptors") or []
        # La position d'un champ dans le tableau "metrics" de chaque point est donnée par
        # `metricsIndex` du descripteur correspondant — PAS par l'ordre de la liste
        # `metricDescriptors` elle-même (vérifié sur un vrai compte : l'ordre de la liste ne
        # correspond pas à l'ordre réel des index, ex. "directRunCadence" est listé en
        # premier mais son metricsIndex réel est 4).
        index_by_key = {d["key"]: d["metricsIndex"] for d in descriptors if "key" in d and "metricsIndex" in d}
        points = data.get("activityDetailMetrics") or []

        def _index_for(*candidate_keys: str) -> int | None:
            return next((index_by_key[k] for k in candidate_keys if k in index_by_key), None)

        time_idx = _index_for("sumElapsedDuration")
        hr_idx = _index_for("directHeartRate")
        # "directSpeed" n'est pas exposé sur toutes les activités (absent sur l'activité
        # utilisée pour valider ce parsing) — repli sur "directGradeAdjustedSpeed" (ajustée
        # au dénivelé), seul champ de vitesse disponible dans ce cas.
        speed_idx = _index_for("directSpeed", "directGradeAdjustedSpeed")
        elevation_idx = _index_for("directElevation")
        # Distance cumulée mesurée par Garmin (m) : plus fiable que l'intégration de l'allure
        # pour l'analyse (P2). Absente des payloads mis en cache avant son ajout.
        distance_idx = _index_for("sumDistance")

        def _value_at(metrics: list[Any] | None, idx: int | None) -> Any:
            return metrics[idx] if metrics is not None and idx is not None and idx < len(metrics) else None

        time_s: list[int | None] = []
        heart_rate_bpm: list[int | None] = []
        pace_min_per_km: list[float | None] = []
        elevation_m: list[float | None] = []
        distance_m: list[float | None] = []
        for point in points:
            metrics = point.get("metrics") if isinstance(point, dict) else None
            metrics = metrics if isinstance(metrics, list) else None
            time_s.append(_to_int(_value_at(metrics, time_idx), default=None))
            heart_rate_bpm.append(_to_int(_value_at(metrics, hr_idx), default=None))
            pace_min_per_km.append(_pace_min_per_km(_value_at(metrics, speed_idx)))
            elevation_m.append(_round_or_none(_value_at(metrics, elevation_idx)))
            distance_m.append(_round_or_none(_value_at(metrics, distance_idx)))

        return {
            "time_s": time_s,
            "heart_rate_bpm": heart_rate_bpm,
            "pace_min_per_km": pace_min_per_km,
            "elevation_m": elevation_m,
            "distance_m": distance_m,
        }

    def get_swim_splits(self, activity_id: str | int) -> list[dict[str, Any]]:
        """Longueurs de piscine d'une activité de nage (`get_activity_details` ne contient
        pas d'allure continue en piscine : pas de vitesse GPS, Garmin ne calcule l'allure
        que par longueur). Vérifié sur un vrai compte : `get_activity_typed_splits` renvoie
        une liste vide pour la nage — c'est `get_activity_splits` (lapDTOs -> lengthDTOs)
        qui contient les données par longueur. `lengthIndex` est déjà global sur toute
        l'activité (pas de redémarrage à 1 par lap), réutilisé tel quel comme `length_idx`.

        Retourne une liste vide (pas d'exception) si l'activité n'a pas de longueurs
        exploitables — cas attendu en eau libre (`open_water_swimming`), où il n'y a pas de
        mur pour marquer les longueurs. Non vérifié sur un vrai compte eau libre (aucune
        activité de ce type disponible au moment de l'implémentation) : à confirmer.
        """
        raw = self._call(self._garmin.get_activity_splits, str(activity_id))
        data = raw if isinstance(raw, dict) else {}
        laps = data.get("lapDTOs") or []

        lengths: list[dict[str, Any]] = []
        for lap in laps:
            if not isinstance(lap, dict):
                continue
            for length in lap.get("lengthDTOs") or []:
                if isinstance(length, dict):
                    lengths.append(length)

        if not lengths:
            return []

        first_start = _parse_garmin_gmt(lengths[0].get("startTimeGMT"))

        results: list[dict[str, Any]] = []
        for length in lengths:
            distance = length.get("distance")
            duration = length.get("duration")
            start = _parse_garmin_gmt(length.get("startTimeGMT"))
            start_s = (
                _to_int((start - first_start).total_seconds(), default=None)
                if start is not None and first_start is not None
                else None
            )
            pace_min_per_100m = None
            if isinstance(duration, (int, float)) and isinstance(distance, (int, float)) and distance > 0:
                pace_min_per_100m = round(duration / distance * 100 / 60, 2)

            results.append(
                {
                    "length_idx": _to_int(length.get("lengthIndex"), default=None),
                    "start_s": start_s,
                    "duration_s": _round_or_none(duration, ndigits=1),
                    "distance_m": _round_or_none(distance, ndigits=1),
                    "avg_hr": _to_int(length.get("averageHR"), default=None),
                    "swolf": _to_int(length.get("averageSWOLF"), default=None),
                    "stroke": length.get("swimStroke"),
                    "pace_min_per_100m": pace_min_per_100m,
                    "pace_min_sec_per_100m": _format_pace_mm_ss(pace_min_per_100m),
                }
            )
        return results

    def upload_workout(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call(self._garmin.upload_workout, payload)

    def schedule_workout(self, workout_id: int | str, date_str: str) -> dict[str, Any]:
        return self._call(self._garmin.schedule_workout, workout_id, date_str)

    def get_scheduled_workouts(self, year: int, month: int) -> Any:
        return self._call(self._garmin.get_scheduled_workouts, year, month)

    def delete_workout(self, workout_id: int | str) -> Any:
        """Supprime le workout (le template/la définition de séance), pas une occurrence de
        calendrier isolée : Garmin distingue "workout" (template, identifié par `workoutId`,
        ce que `upload_workout`/`schedule_workout` manipulent) et "scheduled workout"
        (occurrence programmée à une date, avec son propre id, supprimable seule via
        `unschedule_workout` côté lib — non exposé ici, pas de besoin identifié). Supprimer le
        template retire aussi son occurrence programmée. `workout_id` doit venir de
        `upload_workout`/`garmin_push_workout` ou de `garmin_list_workouts` (champ
        `workoutId` des entrées de calendrier), jamais du champ `id` d'une entrée de calendrier
        (c'est l'id de l'occurrence, un nombre différent) — confirmé sur un vrai compte."""
        return self._call(self._garmin.delete_workout, workout_id)

    def get_intensity_minutes(self, week_start: date) -> dict[str, Any]:
        week_end = week_start + timedelta(days=6)
        raw = self._call(self._garmin.get_weekly_intensity_minutes, week_start.isoformat(), week_end.isoformat())
        entries = raw if isinstance(raw, list) else []
        dict_entries = [e for e in entries if isinstance(e, dict)]
        # Aucune entrée = semaine sans donnée (null) ; entrées présentes = vrai total (0 possible).
        moderate = sum(_to_int(e.get("moderateValue"), default=0) for e in dict_entries) if dict_entries else None
        vigorous = sum(_to_int(e.get("vigorousValue"), default=0) for e in dict_entries) if dict_entries else None

        goal = 0
        for entry in entries:
            if isinstance(entry, dict) and entry.get("weeklyGoal") is not None:
                goal = _to_int(entry.get("weeklyGoal"), default=0)
                break
        if goal == 0:
            # Repli sur l'endpoint journalier : son champ objectif s'appelle `weekGoal`
            # (vérifié contre un vrai compte Garmin, cf. README "Limites connues").
            goal_raw = self._call(self._garmin.get_intensity_minutes_data, week_start.isoformat())
            if isinstance(goal_raw, dict):
                goal = _to_int(goal_raw.get("weekGoal") or goal_raw.get("weeklyGoal"), default=0)

        return {
            "status": "ok" if dict_entries else "no_data",
            "moderate_minutes": moderate,
            "vigorous_minutes": vigorous,
            "goal_minutes": goal or None,
        }

    # -- état de santé du connecteur -----------------------------------------

    def get_last_sync(self) -> dict[str, Any]:
        """Heure de la dernière synchro de la montre vers Garmin Connect (pas l'heure de
        l'appel) : `at` (ISO Europe/Paris), `age_minutes`, `source`
        ("device_last_used" = dernier envoi de la montre, vérifié sur un vrai compte ;
        "user_summary" = repli sur le résumé quotidien), `device`, `status`.
        Lève l'exception Garmin en cas d'échec d'accès."""
        raw = self._call(self._garmin.get_device_last_used)
        data = raw if isinstance(raw, dict) else {}
        at = _epoch_ms_to_iso(data.get("lastUsedDeviceUploadTime"))
        source = "device_last_used"
        if at is None:
            summary = self._user_summary(datetime.now(PARIS_TZ).date().isoformat())
            at = _garmin_timestamp_to_iso(summary.get("lastSyncTimestampGMT"))
            source = "user_summary" if at else None
        return {
            "status": "ok" if at else "no_data",
            "at": at,
            "age_minutes": _age_minutes(at),
            "source": source,
            "device": data.get("lastUsedDeviceName"),
        }

    def get_health(self, today: date) -> dict[str, Any]:
        """Contrôle de la chaîne d'accès (tool `garmin_health_check`). Ne lève jamais :
        toute erreur est renvoyée dans la réponse.

        Fait de vrais appels authentifiés (dernière synchro, dernière activité, sommeil, Body
        Battery, FC de repos) : un jeton expiré, un compte verrouillé ou une limite d'appels
        donnent `status: "error"` avec `error: {type, message}` (message exact de Garmin).
        Chaque sous-contrôle est isolé : un endpoint en erreur n'empêche pas les autres de
        répondre. Clés historiques (`connection`, `last_activity`, `freshness`) conservées.
        """
        iso_today = today.isoformat()
        errors: list[dict[str, str]] = []

        def fail(exc: BaseException) -> dict[str, Any]:
            err = {"type": classify_error(exc), "message": str(exc)}
            errors.append(err)
            return {"status": "error", "message": err["message"], "error_type": err["type"]}

        try:
            self._ensure_login()
        except Exception as exc:
            failure = fail(exc)
            return {
                "status": "error",
                "error": errors[0],
                "connection": {"status": "error", "message": failure["message"]},
                "last_sync": {"status": "error", "at": None, "age_minutes": None, "source": None, "device": None},
                "latest_activity": None,
                "data_availability": {k: "error" for k in ("activities", "sleep", "body_battery", "resting_hr")},
            }

        def check(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
            try:
                return fn()
            except Exception as exc:  # défensif : un sous-contrôle ne doit jamais tout casser
                return fail(exc)

        def last_activity() -> dict[str, Any]:
            raw = self._call(self._garmin.get_activities, 0, 1)
            acts = raw if isinstance(raw, list) else []
            if not acts or not isinstance(acts[0], dict):
                return {"status": "no_data", "activity_id": None, "start_local": None, "type": None}
            act = acts[0]
            type_key = (act.get("activityType") or {}).get("typeKey")
            return {
                "status": "ok",
                "activity_id": act.get("activityId"),
                "start_local": act.get("startTimeLocal"),
                "type": ACTIVITY_TYPE_MAP.get(type_key, type_key),
            }

        def sleep() -> dict[str, Any]:
            raw = self._call(self._garmin.get_sleep_data, iso_today)
            daily = (raw or {}).get("dailySleepDTO") or {}
            availability = _sleep_availability(daily)
            return {
                "status": "ok" if availability == "ok" else "no_data",
                "availability": availability,
                "sleep_end": _epoch_ms_to_iso(daily.get("sleepEndTimestampGMT")),
            }

        def body_battery() -> dict[str, Any]:
            raw = self._call(self._garmin.get_body_battery, iso_today, iso_today)
            entry = raw[0] if isinstance(raw, list) and raw else {}
            points = [
                p
                for p in (entry.get("bodyBatteryValuesArray") or [])
                if isinstance(p, (list, tuple)) and len(p) >= 2 and p[1] is not None
            ]
            availability = _bb_availability(points)
            return {
                "status": "ok" if availability == "ok" else "no_data",
                "availability": availability,
                "last_point_at": _epoch_ms_to_iso(points[-1][0]) if points else None,
            }

        def resting_hr() -> dict[str, Any]:
            data = self.get_resting_hr(iso_today)
            return {
                "status": data["status"],
                "availability": data["availability"],
                "date": iso_today if data["status"] == "ok" else None,
            }

        sync = check(self.get_last_sync)
        activity = check(last_activity)
        freshness = {"sleep": check(sleep), "body_battery": check(body_battery), "resting_hr": check(resting_hr)}

        latest = None
        if activity.get("status") == "ok":
            latest = {
                "id": activity["activity_id"],
                "start": _local_to_iso(activity["start_local"]),
                "type": activity["type"],
            }
        activities_availability = {"ok": "ok", "no_data": "empty"}.get(activity["status"], "error")
        return {
            "status": "error" if errors else "ok",
            "error": errors[0] if errors else None,
            "connection": {"status": "ok"},
            "last_sync": {**{"at": None, "age_minutes": None, "source": None, "device": None}, **sync},
            "last_activity": activity,
            "latest_activity": latest,
            "freshness": freshness,
            "data_availability": {
                "activities": activities_availability,
                **{name: block.get("availability", "error") for name, block in freshness.items()},
            },
        }

    # -- synchronisation complète -------------------------------------------

    def get_daily_snapshot(self, day: date) -> dict[str, Any]:
        """Vue quotidienne en un appel (tool `garmin_get_daily_snapshot`) : sommeil, Body
        Battery, FC repos, statut et charge d'entraînement, charge par filière, seuil
        lactique, VO2max. Chaque bloc porte son propre `status` ("ok" | "no_data" |
        "error" + `message`) : un endpoint en échec n'empêche pas les autres de répondre.

        Le payload training status n'est récupéré qu'une fois pour statut, charge et
        filières ; le résumé quotidien (Body Battery au réveil, dernière synchro) une fois
        pour Body Battery et FC repos.
        """
        iso = day.isoformat()

        def block(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
            try:
                return fn()
            except Exception as exc:  # défensif : un bloc en erreur ne casse pas le snapshot
                return {"status": "error", "message": str(exc)}

        try:
            training_raw: tuple[dict[str, Any], str] | Exception = self._fetch_training_status_with_fallback(day)
        except Exception as exc:
            training_raw = exc

        def from_training(parse: Callable[..., dict[str, Any]]) -> Callable[[], dict[str, Any]]:
            def run() -> dict[str, Any]:
                if isinstance(training_raw, Exception):
                    raise training_raw
                return parse(day, prefetched=training_raw)

            return run

        summary = self._user_summary(iso)
        self._summary_override = {iso: summary}
        self._sleep_memo = {}
        try:
            snapshot = {
                "sleep": block(lambda: self.get_sleep(iso)),
                "body_battery": block(lambda: self.get_body_battery(iso)),
                "resting_hr": block(lambda: self.get_resting_hr(iso)),
                "training_status": block(from_training(self.get_training_status)),
                "training_load": block(from_training(self.get_training_load)),
                "training_load_balance": block(from_training(self.get_training_load_balance)),
                "lactate_threshold": block(self.get_lactate_threshold),
                "vo2max": block(lambda: self.get_vo2max(day)),
            }
        finally:
            self._summary_override = {}
            self._sleep_memo = None
        return snapshot

    def sync_all(self, today: date) -> dict[str, Any]:
        """Rafraîchit toutes les données du jour (appelé par le tool `garmin_sync`)."""
        iso_today = today.isoformat()
        week_start = today - timedelta(days=today.weekday())
        return {
            "training_status": self.get_training_status(today),
            "body_battery": self.get_body_battery(iso_today),
            "vo2max": self.get_vo2max(today),
            "resting_hr": self.get_resting_hr(iso_today),
            "sleep": self.get_sleep(iso_today),
            "recent_activities": {"activities": self.get_recent_activities(today - timedelta(days=7), fetch_limit=30)},
            "training_load": self.get_training_load(today),
            "training_load_balance": self.get_training_load_balance(today),
            "lactate_threshold": self.get_lactate_threshold(),
            "intensity_minutes": self.get_intensity_minutes(week_start),
        }


def _first_device_status(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    status_data = (raw.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData") or {}
    if not status_data:
        return {}
    return next(iter(status_data.values()), {}) or {}


def _to_int(value: Any, *, default: int | None) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    return default


def _pace_min_per_km(speed_m_per_s: Any) -> float | None:
    """Convertit une vitesse (m/s, telle que renvoyée par garminconnect) en allure min/km.

    Note : le résumé d'activité de Garmin n'expose que averageSpeed et maxSpeed (pas de
    "minSpeed") — il n'y a donc pas d'équivalent "allure la plus lente" à ce niveau.
    """
    if not isinstance(speed_m_per_s, (int, float)) or speed_m_per_s <= 0:
        return None
    return round(1000 / speed_m_per_s / 60, 2)


def _speed_kmh(speed_m_per_s: Any) -> float | None:
    """Vitesse m/s -> km/h (1 décimale) ; null si absente ou nulle."""
    if not isinstance(speed_m_per_s, (int, float)) or speed_m_per_s <= 0:
        return None
    return round(speed_m_per_s * 3.6, 1)


def _pace_min_per_100m(speed_m_per_s: Any) -> float | None:
    """Convertit une vitesse (m/s) en allure min/100m (convention natation)."""
    if not isinstance(speed_m_per_s, (int, float)) or speed_m_per_s <= 0:
        return None
    return round(100 / speed_m_per_s / 60, 2)


def _round_or_none(value: Any, ndigits: int = 1) -> float | None:
    return round(value, ndigits) if isinstance(value, (int, float)) else None


def _fahrenheit_to_celsius(value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    return round((value - 32) * 5 / 9, 1)


def _format_pace_mm_ss(decimal_minutes: float | None) -> str | None:
    """Convertit une allure en minutes décimales (ex. `1.74`) en `"M:SS"` (ex. `"1:44"`) —
    `1.74` se lit comme "1,74 minute", pas "1 minute 74", format peu naturel à afficher tel
    quel pour une allure de nage/course."""
    if decimal_minutes is None:
        return None
    total_seconds = round(decimal_minutes * 60)
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


_STATION_FAR_KM = 15.0
# Coordonnées des stations météo (aéroports, code OACI) autour des lieux d'entraînement
# habituels — repli quand le payload météo Garmin ne donne pas la position de la station.
_STATION_COORDS: dict[str, tuple[float, float]] = {
    "LFPV": (48.7742, 2.2017),  # Villacoublay-Vélizy
    "LFPO": (48.7233, 2.3794),  # Paris-Orly
    "LFPG": (49.0097, 2.5479),  # Paris-Charles-de-Gaulle
    "LFPB": (48.9694, 2.4414),  # Paris-Le Bourget
    "LFPN": (48.7519, 2.1061),  # Toussus-le-Noble
    "LFPT": (49.0967, 2.0408),  # Pontoise-Cormeilles
}


# Échelle "Sensations" de la montre (directWorkoutFeel) : 5 crans de 25.
_FEEL_LABELS = {0: "Très faible", 25: "Faible", 50: "Normal", 75: "Fort", 100: "Très fort"}


def _feel_label(value: float | None) -> str | None:
    if value is None:
        return None
    return _FEEL_LABELS[min(_FEEL_LABELS, key=lambda k: abs(k - value))]


def _rpe_label(effort: int | None) -> str | None:
    """Libellés de l'échelle d'effort perçu (RPE 1-10) affichée par Garmin."""
    if effort is None:
        return None
    if effort <= 1:
        return "Très léger"
    if effort <= 3:
        return "Léger"
    if effort <= 6:
        return "Modéré"
    if effort <= 8:
        return "Difficile"
    if effort == 9:
        return "Très difficile"
    return "Maximal"


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _garmin_timestamp_to_iso(value: Any) -> str | None:
    """Timestamp Garmin GMT, en epoch ms ou en chaîne "2026-10-17T06:20:00.0" -> ISO Paris."""
    if isinstance(value, (int, float)):
        return _epoch_ms_to_iso(value)
    parsed = _parse_garmin_gmt(value)
    if parsed is None:
        return None
    return parsed.replace(tzinfo=timezone.utc).astimezone(PARIS_TZ).isoformat()


def _sleep_availability(daily: dict[str, Any]) -> str:
    """"ok" (nuit mesurée), "zero" (durée présente mais à 0) ou "empty" (aucune donnée)."""
    seconds = daily.get("sleepTimeSeconds")
    if not isinstance(seconds, (int, float)):
        return "empty"
    return "ok" if seconds > 0 else "zero"


def _bb_availability(points: list) -> str:
    """"ok", "zero" (points présents mais tous à 0) ou "empty" (aucun point)."""
    if not points:
        return "empty"
    return "ok" if any(p[1] for p in points) else "zero"


def _age_minutes(iso: str | None) -> int | None:
    if iso is None:
        return None
    return int((datetime.now(PARIS_TZ) - datetime.fromisoformat(iso)).total_seconds() // 60)


def _local_to_iso(value: Any) -> str | None:
    """Heure locale Garmin ("2026-10-17 09:42:59", fuseau de l'appareil = Paris) -> ISO
    8601 avec décalage Europe/Paris."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=PARIS_TZ).isoformat()
    except ValueError:
        return None


def _epoch_ms_to_iso(value: Any) -> str | None:
    """Timestamp Garmin en millisecondes epoch (UTC) -> ISO 8601 en Europe/Paris."""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, tz=PARIS_TZ).isoformat()


def _parse_garmin_gmt(value: Any) -> datetime | None:
    """Parse un timestamp `startTimeGMT` façon Garmin (ex. "2026-08-20T17:01:37.0").

    `datetime.fromisoformat` refuse un nombre de décimales != 3/6 selon la version de Python
    (le "0" à une seule décimale ici fait échouer le parsing sur certaines versions) —
    `strptime` avec `%f` est plus tolérant sur le nombre de chiffres fournis.
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f")
    except ValueError:
        return None


def _pool_length_m(pool_length: Any, unit: Any) -> float | None:
    """Convertit `poolLength` (exprimé dans l'unité de base de `unitOfPoolLength`) en mètres.

    Ex. observé sur un vrai compte : poolLength=2500.0, unitOfPoolLength={"unitKey": "meter",
    "factor": 100.0} -> bassin de 25 m (2500 / 100).
    """
    if not isinstance(pool_length, (int, float)):
        return None
    factor = (unit or {}).get("factor") if isinstance(unit, dict) else None
    length = pool_length / factor if isinstance(factor, (int, float)) and factor else pool_length
    unit_key = (unit or {}).get("unitKey") if isinstance(unit, dict) else None
    if unit_key == "yard":
        length *= 0.9144
    return round(length, 2)
