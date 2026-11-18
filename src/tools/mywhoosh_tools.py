"""Tool MCP `mywhoosh_sync` : importe les sorties MyWhoosh dans Garmin Connect.

MyWhoosh n'a pas de synchro Garmin officielle. Pour chaque sortie : téléchargement du FIT,
contrôle de chevauchement avec les activités Garmin existantes, réécriture de l'appareil en
Edge 840 (pour que Garmin la compte dans la charge d'entraînement), puis import.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from src.fit_device import FitFormatError, _parse, heart_rate_series, inject_heart_rate, rewrite_as_edge_840, summarize
from src.garmin_client import ACTIVITY_TYPE_MAP, GarminClient
from src.models import PARIS_TZ
from src.mywhoosh_client import MyWhooshClient, MyWhooshError
from src.tools.garmin_tools import _error, _garmin_error

logger = logging.getLogger(__name__)

# Marge autour de la sortie MyWhoosh : une activité Garmin qui la chevauche (à cette marge
# près) est considérée comme la même séance — typiquement « Vélo d'intérieur » lancé sur la
# montre en parallèle. On ne se fie pas au nom, qui diffère toujours.
_OVERLAP_MARGIN = timedelta(minutes=5)
_MAX_COUNT = 10
# Sorties MyWhoosh téléchargées au plus pour trouver celle qui chevauche l'activité montre.
_MERGE_SCAN = 5
# Garmin met quelques secondes à traiter un import avant de le lister.
_ID_LOOKUP_ATTEMPTS = 4
_ID_LOOKUP_DELAY_S = 3


def _garmin_interval(act: dict[str, Any]) -> tuple[datetime, datetime] | None:
    start_raw = act.get("startTimeGMT")
    duration = act.get("duration") or act.get("elapsedDuration")
    if not isinstance(start_raw, str) or not isinstance(duration, (int, float)):
        return None
    try:
        start = datetime.fromisoformat(start_raw.replace(" ", "T")).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return start, start + timedelta(seconds=duration)


def find_overlapping(
    activities: list[dict[str, Any]], start: datetime, end: datetime, *, exclude_id: int | None = None
) -> dict[str, Any] | None:
    for act in activities:
        if exclude_id is not None and act.get("activityId") == exclude_id:
            continue
        interval = _garmin_interval(act)
        if interval is None:
            continue
        a_start, a_end = interval
        if a_start <= end + _OVERLAP_MARGIN and start - _OVERLAP_MARGIN <= a_end:
            type_key = (act.get("activityType") or {}).get("typeKey") or "unknown"
            return {
                "activity_id": act.get("activityId"),
                "nom": act.get("activityName"),
                "type": ACTIVITY_TYPE_MAP.get(type_key, type_key),
                "debut": a_start.astimezone(PARIS_TZ).isoformat(timespec="minutes"),
            }
    return None


def _imported_activity_id(response: Any) -> int | None:
    result = response.get("detailedImportResult") if isinstance(response, dict) else None
    successes = result.get("successes") if isinstance(result, dict) else None
    if isinstance(successes, list) and successes and isinstance(successes[0], dict):
        value = successes[0].get("internalId")
        return value if isinstance(value, int) else None
    return None


def _ride_label(ride: dict[str, Any]) -> str | None:
    for key in ("name", "title", "routeName", "worldName"):
        if isinstance(ride.get(key), str) and ride[key].strip():
            return ride[key].strip()
    return None


def build_mywhoosh_tools(
    garmin: GarminClient, mywhoosh: MyWhooshClient | None
) -> dict[str, Callable[..., Any]]:
    def _sync_one(ride: dict[str, Any], *, dry_run: bool, force: bool) -> dict[str, Any]:
        out: dict[str, Any] = {"nom": _ride_label(ride)}
        try:
            fit = mywhoosh.download_fit(ride)  # type: ignore[union-attr]
            summary = summarize(fit)
        except MyWhooshError as exc:
            return {**out, "statut": "erreur", "message": str(exc)}
        except FitFormatError as exc:
            return {**out, "statut": "erreur", "message": f"FIT MyWhoosh illisible : {exc}"}
        if summary.start_utc is None:
            return {**out, "statut": "erreur", "message": "heure de départ absente du FIT"}

        start = summary.start_utc
        end = start + timedelta(seconds=summary.elapsed_s or 0)
        out["debut"] = start.astimezone(PARIS_TZ).isoformat(timespec="minutes")
        out["duree_minutes"] = round(summary.elapsed_s / 60) if summary.elapsed_s else None

        try:
            day = start.astimezone(PARIS_TZ).date()
            existing = garmin.get_activities_by_date(day - timedelta(days=1), day + timedelta(days=1))
        except Exception as exc:
            return {**out, "statut": "erreur", "message": _garmin_error(exc)["message"]}
        overlap = find_overlapping(existing, start, end)
        if overlap and not force:
            return {**out, "statut": "deja_present", "activite_garmin": overlap}
        if dry_run:
            return {**out, "statut": "a_importer", "chevauche": overlap}

        return {**out, **_upload(fit, start, end)}

    def _upload(
        fit: bytes, start: datetime, end: datetime, *, exclude_id: int | None = None
    ) -> dict[str, Any]:
        """Réécrit en Edge 840, importe, puis renvoie {statut, activity_id | message}."""
        try:
            patched = rewrite_as_edge_840(fit)
        except FitFormatError as exc:
            return {"statut": "erreur", "message": f"réécriture du FIT impossible : {exc}"}

        fd, path = tempfile.mkstemp(prefix="mywhoosh_", suffix=".fit")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(patched)
            response = garmin.upload_activity_file(path)
        except Exception as exc:
            if "409" in str(exc):
                return {"statut": "deja_present", "message": "Garmin a déjà ce fichier (409)"}
            return {"statut": "erreur", "message": _garmin_error(exc)["message"]}
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        activity_id = _imported_activity_id(response)
        if activity_id is None:
            # Garmin traite souvent l'import en différé et ne renvoie pas encore l'id :
            # on le retrouve par chevauchement horaire, comme pour la détection de doublons.
            day = start.astimezone(PARIS_TZ).date()
            for attempt in range(_ID_LOOKUP_ATTEMPTS):
                if attempt:
                    time.sleep(_ID_LOOKUP_DELAY_S)
                try:
                    found = find_overlapping(
                        garmin.get_activities_by_date(day - timedelta(days=1), day + timedelta(days=1)),
                        start,
                        end,
                        exclude_id=exclude_id,
                    )
                except Exception:
                    break
                if found:
                    activity_id = found["activity_id"]
                    break
        return {"statut": "importe", "activity_id": activity_id}

    async def mywhoosh_sync(count: int = 1, dry_run: bool = False, force: bool = False) -> dict[str, Any]:
        """Importe dans Garmin Connect les `count` dernières sorties MyWhoosh (1 à 10, la
        plus récente d'abord), présentées comme enregistrées par un Edge 840 pour compter
        dans la charge d'entraînement. Une sortie déjà couverte par une activité Garmin qui
        la chevauche dans le temps (ex. « Vélo d'intérieur » lancé sur la montre en même
        temps) est sautée (`deja_present`), sauf `force=true`. `dry_run=true` : ne fait que
        lister ce qui serait importé. Statut par sortie : "importe" (+ `activity_id`
        Garmin, à passer à garmin_analyse_activity), "deja_present", "a_importer" (dry_run)
        ou "erreur" (+ `message`)."""
        if mywhoosh is None:
            return _error(
                "MyWhoosh non configuré : renseigner MYWHOOSH_EMAIL et MYWHOOSH_PASSWORD dans le .env",
                "auth",
            )
        if not 1 <= count <= _MAX_COUNT:
            return _error(f"count doit être entre 1 et {_MAX_COUNT}")
        try:
            rides = mywhoosh.list_activities(limit=count)
        except MyWhooshError as exc:
            return _error(f"MyWhoosh : {exc}", exc.error_type)
        if not rides:
            return {"status": "no_data", "sorties": []}
        results = [_sync_one(r, dry_run=dry_run, force=force) for r in rides[:count]]
        failed = all(r["statut"] == "erreur" for r in results)
        return {"status": "error" if failed else "ok", "dry_run": dry_run, "sorties": results}

    async def mywhoosh_merge_hr(garmin_activity_id: int, delete_original: bool = False) -> dict[str, Any]:
        """Fusionne une séance enregistrée en double : la FC de l'activité montre
        `garmin_activity_id` (ex. « Vélo d'intérieur ») est injectée dans le FIT de la sortie
        MyWhoosh qui la chevauche (puissance, cadence, distance), puis le tout est importé
        comme une seule activité Edge 840 — Garmin calcule alors charge et effet
        d'entraînement avec puissance ET FC. `delete_original=true` supprime ensuite
        DÉFINITIVEMENT l'activité montre (sinon la séance compte deux fois dans la charge) :
        ne le passer qu'après accord explicite de l'utilisateur. Sortie : `activity_id` de
        la nouvelle activité, `records_avec_fc` / `records_total`, `original_supprime`."""
        if mywhoosh is None:
            return _error(
                "MyWhoosh non configuré : renseigner MYWHOOSH_EMAIL et MYWHOOSH_PASSWORD dans le .env",
                "auth",
            )
        try:
            watch_fit = garmin.download_activity_fit(garmin_activity_id)
        except Exception as exc:
            return _garmin_error(exc)
        try:
            hr = heart_rate_series(watch_fit)
            watch = summarize(watch_fit)
        except FitFormatError as exc:
            return _error(f"FIT de la montre illisible : {exc}", "unknown")
        if not hr or watch.start_utc is None:
            return _error("l'activité Garmin ne contient pas de fréquence cardiaque exploitable")
        w_start = watch.start_utc
        w_end = w_start + timedelta(seconds=watch.elapsed_s or 0)

        try:
            rides = mywhoosh.list_activities(limit=_MAX_COUNT)
        except MyWhooshError as exc:
            return _error(f"MyWhoosh : {exc}", exc.error_type)
        match = None
        for ride in rides[:_MERGE_SCAN]:
            try:
                fit = mywhoosh.download_fit(ride)
                summary = summarize(fit)
            except (MyWhooshError, FitFormatError):
                continue
            if summary.start_utc is None:
                continue
            r_end = summary.start_utc + timedelta(seconds=summary.elapsed_s or 0)
            if summary.start_utc <= w_end + _OVERLAP_MARGIN and w_start - _OVERLAP_MARGIN <= r_end:
                match = (ride, fit, summary.start_utc, r_end)
                break
        if match is None:
            return _error("aucune sortie MyWhoosh récente ne chevauche cette activité", "invalid_input")
        ride, fit, r_start, r_end = match

        try:
            merged, filled = inject_heart_rate(fit, hr)
        except FitFormatError as exc:
            return _error(f"fusion impossible : {exc}", "unknown")
        total = sum(1 for m in _parse(merged)[2] if m.global_num == 20)
        result: dict[str, Any] = {
            "nom": _ride_label(ride),
            "debut": r_start.astimezone(PARIS_TZ).isoformat(timespec="minutes"),
            "records_avec_fc": filled,
            "records_total": total,
            **_upload(merged, r_start, r_end, exclude_id=garmin_activity_id),
            "original_supprime": False,
        }
        if result["statut"] != "importe":
            return {"status": "error", **result}
        if delete_original:
            try:
                garmin.delete_activity(garmin_activity_id)
                result["original_supprime"] = True
            except Exception as exc:
                result["message"] = f"activité fusionnée importée, mais suppression de l'originale échouée : {_garmin_error(exc)['message']}"
        return {"status": "ok", **result}

    return {"mywhoosh_sync": mywhoosh_sync, "mywhoosh_merge_hr": mywhoosh_merge_hr}
