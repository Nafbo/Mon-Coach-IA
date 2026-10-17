"""Capture les réponses Garmin brutes d'une activité et les enregistre, anonymisées, comme
fixtures de test hors ligne (tests/fixtures/<activity_id>.json.gz).

Lecture seule : aucun appel d'écriture vers Garmin. Réutilise le tokenstore existant
(data/garmin_tokens/), comme inspect_new_endpoints.py.

Usage (depuis la racine du repo, venv activé) :
    python scripts/capture_fixtures.py 24725112038 24748453127

Anonymisation : les champs GPS (latitude/longitude, polylignes), identifiants de compte et
noms sont retirés ou remplacés ; les séries (temps, FC, vitesse, altitude, distance) et les
longueurs de nage sont conservées telles quelles.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = ROOT / "tests" / "fixtures"

# Clés retirées partout (récursivement) : position, identité, appareil.
_DROPPED_KEYS = {
    "geoPolylineDTO",
    "polyline",
    "ownerId",
    "ownerDisplayName",
    "ownerFullName",
    "ownerProfileImageUrlSmall",
    "ownerProfileImageUrlMedium",
    "ownerProfileImageUrlLarge",
    "userInfoDto",
    "fullname",
    "fullName",
    "displayName",
    "userProfilePk",
    "userProfileId",
    "deviceId",
    "locationName",
    "activityName",
    "description",
}
# Clés de coordonnées GPS : remplacées par None (garde la forme du payload).
_GPS_KEY_FRAGMENTS = ("latitude", "longitude")
# Descripteurs de métriques GPS dans activityDetailMetrics : valeurs remplacées par None.
_GPS_METRIC_KEYS = {"directLatitude", "directLongitude"}


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in _DROPPED_KEYS:
                continue
            if any(frag in k.lower() for frag in _GPS_KEY_FRAGMENTS):
                out[k] = None
                continue
            out[k] = _scrub(v)
        return out
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _scrub_details(details: dict[str, Any]) -> dict[str, Any]:
    descriptors = details.get("metricDescriptors") or []
    gps_idx = {d["metricsIndex"] for d in descriptors if d.get("key") in _GPS_METRIC_KEYS and "metricsIndex" in d}
    for point in details.get("activityDetailMetrics") or []:
        metrics = point.get("metrics") if isinstance(point, dict) else None
        if isinstance(metrics, list):
            for idx in gps_idx:
                if idx < len(metrics):
                    metrics[idx] = None
    return _scrub(details)


def capture(api: Any, activity_id: str) -> Path:
    payload = {
        "activity_id": activity_id,
        "activity": _scrub(api.get_activity(activity_id)),
        "details": _scrub_details(api.get_activity_details(activity_id, maxchart=100_000)),
        "splits": _scrub(api.get_activity_splits(activity_id)),
    }
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / f"{activity_id}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return path


def main() -> None:
    load_dotenv(ROOT / ".env")
    from garminconnect import Garmin

    db_path = os.environ.get("DB_PATH", "./data/coach.db")
    tokenstore = str((ROOT / db_path).resolve().parent / "garmin_tokens")
    api = Garmin(email=os.environ["GARMIN_EMAIL"], password=os.environ["GARMIN_PASSWORD"])
    api.login(tokenstore=tokenstore)
    for activity_id in sys.argv[1:]:
        print(capture(api, activity_id))


if __name__ == "__main__":
    main()
