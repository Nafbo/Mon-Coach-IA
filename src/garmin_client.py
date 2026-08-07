"""Wrapper autour de `garminconnect` : login, cache de session/token, parsing des payloads.

Les endpoints Garmin Connect utilisés ici (`connectapi` interne à `garminconnect`) ne sont pas
documentés officiellement par Garmin ; la forme des réponses JSON ci-dessous est basée sur la
structure connue de l'API à la date d'écriture. Après déploiement, lancer `garmin_sync` puis
inspecter le contenu brut de `garmin_cache` pour confirmer/ajuster le parsing si Garmin a fait
évoluer ses réponses (cf. README).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Callable, TypeVar

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

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
    "lap_swimming": "nage",
    "open_water_swimming": "nage",
    "strength_training": "renfo",
}


class GarminAuthenticationError(Exception):
    """Identifiants Garmin invalides."""


class GarminUnavailableError(Exception):
    """Garmin Connect injoignable ou en erreur (hors identifiants invalides)."""


class GarminClient:
    """Wrapper haut niveau autour de `garminconnect.Garmin`."""

    def __init__(self, email: str, password: str, tokenstore_path: str) -> None:
        self._email = email
        self._password = password
        self._tokenstore_path = tokenstore_path
        self._garmin = Garmin(email=email, password=password)
        self._logged_in = False

    # -- authentification -------------------------------------------------

    def _ensure_login(self) -> None:
        if self._logged_in:
            return
        try:
            self._garmin.login(tokenstore=self._tokenstore_path)
            self._logged_in = True
        except GarminConnectAuthenticationError as exc:
            raise GarminAuthenticationError(f"Identifiants Garmin invalides : {exc}") from exc
        except GarminConnectTooManyRequestsError as exc:
            raise GarminUnavailableError(
                f"Trop de tentatives de connexion à Garmin Connect, réessayer plus tard : {exc}"
            ) from exc
        except GarminConnectConnectionError as exc:
            raise GarminUnavailableError(f"Garmin Connect injoignable : {exc}") from exc
        except Exception as exc:  # défensif : ne jamais laisser fuiter une exception non catégorisée
            raise GarminUnavailableError(
                f"Erreur inattendue lors de la connexion à Garmin Connect : {exc}"
            ) from exc

    def _call(self, fn: Callable[..., T], *args: Any, _retry: bool = True, **kwargs: Any) -> T:
        self._ensure_login()
        try:
            return fn(*args, **kwargs)
        except GarminConnectAuthenticationError as exc:
            if _retry:
                self._logged_in = False
                return self._call(fn, *args, _retry=False, **kwargs)
            raise GarminAuthenticationError(f"Identifiants Garmin invalides : {exc}") from exc
        except (GarminConnectConnectionError, GarminConnectTooManyRequestsError) as exc:
            if _retry:
                self._logged_in = False
                return self._call(fn, *args, _retry=False, **kwargs)
            raise GarminUnavailableError(f"Garmin Connect injoignable : {exc}") from exc

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

    def get_training_status(self, today: date) -> dict[str, Any]:
        raw, found_date = self._fetch_training_status_with_fallback(today)
        device_entry = _first_device_status(raw)
        status_label = device_entry.get("trainingStatusFeedbackPhrase") or device_entry.get("trainingStatus")
        acute_dto = device_entry.get("acuteTrainingLoadDTO") or {}
        acute = acute_dto.get("dailyTrainingLoadAcute") or acute_dto.get("acuteTrainingLoad")
        chronic = acute_dto.get("dailyTrainingLoadChronic") or acute_dto.get("chronicTrainingLoad")
        return {
            "date": found_date,
            "status": str(status_label) if status_label is not None else "unknown",
            "acute_load": _to_int(acute, default=0),
            "chronic_load": _to_int(chronic, default=0),
        }

    def get_body_battery(self, iso_date: str) -> dict[str, Any]:
        raw = self._call(self._garmin.get_body_battery, iso_date, iso_date)
        entry = raw[0] if isinstance(raw, list) and raw else {}
        values = entry.get("bodyBatteryValuesArray") or []
        latest_value = None
        for point in reversed(values):
            if isinstance(point, (list, tuple)) and len(point) >= 2 and point[1] is not None:
                latest_value = point[1]
                break
        return {
            "value": _to_int(latest_value, default=0),
            "charged": _to_int(entry.get("charged"), default=0),
            "drained": _to_int(entry.get("drained"), default=0),
        }

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
                    "date": d.isoformat(),
                    "running_vo2max": _to_int(running_vo2, default=None),
                    "cycling_vo2max": _to_int(cycling_vo2, default=None),
                }
        return {"date": today.isoformat(), "running_vo2max": None, "cycling_vo2max": None}

    def get_resting_hr(self, iso_date: str) -> dict[str, Any]:
        raw = self._call(self._garmin.get_rhr_day, iso_date)
        metrics = ((raw or {}).get("allMetrics") or {}).get("metricsMap") or {}
        series = metrics.get("WELLNESS_RESTING_HEART_RATE") or []
        value = None
        for point in series:
            if isinstance(point, dict) and point.get("value") is not None:
                value = point["value"]
        return {"resting_hr": _to_int(value, default=0)}

    def get_sleep(self, iso_date: str) -> dict[str, Any]:
        raw = self._call(self._garmin.get_sleep_data, iso_date)
        daily = (raw or {}).get("dailySleepDTO") or {}

        def minutes(seconds_key: str) -> int:
            seconds = daily.get(seconds_key)
            return int(seconds // 60) if isinstance(seconds, (int, float)) else 0

        score = ((daily.get("sleepScores") or {}).get("overall") or {}).get("value")
        return {
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
                    "date": act_date.isoformat(),
                    "type": normalized_type,
                    "garmin_type": type_key,
                    "duration_minutes": int(duration_seconds // 60) if isinstance(duration_seconds, (int, float)) else 0,
                    "distance_km": round(distance_m / 1000, 2) if isinstance(distance_m, (int, float)) else None,
                    "avg_hr": _to_int(act.get("averageHR"), default=None),
                    "max_hr": _to_int(act.get("maxHR"), default=None),
                    "denivele_m": _to_int(elevation_gain, default=None),
                    "allure_moyenne_min_km": allure_moyenne,
                    "allure_rapide_min_km": allure_rapide,
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
                }
            )
        results.sort(key=lambda a: a["date"], reverse=True)
        return results

    def get_training_load(self, today: date) -> dict[str, Any]:
        raw, found_date = self._fetch_training_status_with_fallback(today)
        device_entry = _first_device_status(raw)
        acute_dto = device_entry.get("acuteTrainingLoadDTO") or {}
        acute = _to_int(acute_dto.get("dailyTrainingLoadAcute") or acute_dto.get("acuteTrainingLoad"), default=0)
        chronic = _to_int(acute_dto.get("dailyTrainingLoadChronic") or acute_dto.get("chronicTrainingLoad"), default=0)
        if chronic == 0:
            ratio = 0.0
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

    def get_training_load_balance(self, today: date) -> dict[str, Any]:
        """Répartition de charge mensuelle aérobie faible / aérobie élevée / anaérobique
        ("Training Load Focus" Garmin) — cf. mostRecentTrainingLoadBalance."""
        raw, found_date = self._fetch_training_status_with_fallback(today)
        balance = raw.get("mostRecentTrainingLoadBalance") if isinstance(raw, dict) else None
        device_map = (balance or {}).get("metricsTrainingLoadBalanceDTOMap") or {}
        entry = next(iter(device_map.values()), {}) if device_map else {}
        return {
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

        return {
            "running": {
                "heart_rate": _to_int(hr, default=None),
                "pace_min_per_km": pace_min_per_km,
                "power_watts": running_power,
                "power_watts_per_kg": running_power_per_kg,
            },
            "cycling": {
                "ftp_watts": cycling_ftp,
            },
        }

    def get_intensity_minutes(self, week_start: date) -> dict[str, Any]:
        week_end = week_start + timedelta(days=6)
        raw = self._call(self._garmin.get_weekly_intensity_minutes, week_start.isoformat(), week_end.isoformat())
        entries = raw if isinstance(raw, list) else []
        moderate = sum(_to_int(e.get("moderateValue"), default=0) for e in entries if isinstance(e, dict))
        vigorous = sum(_to_int(e.get("vigorousValue"), default=0) for e in entries if isinstance(e, dict))

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

        return {"moderate_minutes": moderate, "vigorous_minutes": vigorous, "goal_minutes": goal}

    # -- synchronisation complète -------------------------------------------

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


def _pace_min_per_100m(speed_m_per_s: Any) -> float | None:
    """Convertit une vitesse (m/s) en allure min/100m (convention natation)."""
    if not isinstance(speed_m_per_s, (int, float)) or speed_m_per_s <= 0:
        return None
    return round(100 / speed_m_per_s / 60, 2)


def _round_or_none(value: Any, ndigits: int = 1) -> float | None:
    return round(value, ndigits) if isinstance(value, (int, float)) else None


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
