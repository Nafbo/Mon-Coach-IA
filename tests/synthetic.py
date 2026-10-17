"""Séries synthétiques déterministes au format Garmin, pour tester l'analyse hors ligne.

Le sandbox de test n'a pas accès à connect.garmin.com : ces générateurs reproduisent la
forme des activités de référence (cf. scripts/capture_fixtures.py pour remplacer par des
séries réelles anonymisées quand un token Garmin valide est disponible) :

- `interval_run` : calqué sur 24725112038 (course du 14/10) — 1 échantillon/s jusqu'à
  3306 s, deux pauses 2558→2692 s et 2832→2921 s, échauffement + 5 × (3 min vite /
  2 min trot) + retour au calme.
- `endurance_run` : footing continu de 50 min, allure et FC bruitées, petites côtes.
"""

from __future__ import annotations

import random
from typing import Any

PAUSES_REF = [(2558, 2692), (2832, 2921)]
END_REF = 3306


def _pace_hr_interval(t: int) -> tuple[float, int]:
    if t < 900:
        return 5.5, 140
    if t < 2400:
        k = (t - 900) % 300
        return (4.25, 172) if k < 180 else (6.0, 150)
    return 5.75, 145


def interval_run(seed: int = 1) -> dict[str, list]:
    """Séries parsées (sortie de GarminClient.get_activity_details)."""
    rng = random.Random(seed)
    time_s, hr, pace, elev = [], [], [], []
    t = 0
    while t <= END_REF:
        pause = next((p for p in PAUSES_REF if p[0] < t < p[1]), None)
        if pause:
            t = pause[1]
            continue
        p, h = _pace_hr_interval(t)
        time_s.append(t)
        pace.append(round(p + rng.uniform(-0.05, 0.05), 2))
        hr.append(h + rng.randint(-2, 2))
        elev.append(round(60 + 5 * ((t // 600) % 2), 1))
        t += 1
    return {"time_s": time_s, "heart_rate_bpm": hr, "pace_min_per_km": pace, "elevation_m": elev}


def endurance_run(seed: int = 2, duration_s: int = 3000) -> dict[str, list]:
    rng = random.Random(seed)
    time_s, hr, pace, elev = [], [], [], []
    for t in range(duration_s + 1):
        hill = 0.15 if (t // 400) % 2 else -0.1  # côtes / descentes : ±6-9 s/km
        time_s.append(t)
        pace.append(round(5.33 + hill + rng.uniform(-0.12, 0.12), 2))
        hr.append(140 + t * 8 // duration_s + rng.randint(-3, 3))
        elev.append(round(50 + (t % 800) / 40, 1))
    return {"time_s": time_s, "heart_rate_bpm": hr, "pace_min_per_km": pace, "elevation_m": elev}


def to_garmin_details(series: dict[str, list]) -> dict[str, Any]:
    """Reconstruit un payload brut `get_activity_details` (metricDescriptors +
    activityDetailMetrics, vitesse en m/s) à partir de séries parsées — sert à tester le
    parsing du client sur les mêmes données que l'analyse."""
    descriptors = [
        {"key": "directHeartRate", "metricsIndex": 0},
        {"key": "sumElapsedDuration", "metricsIndex": 1},
        {"key": "directSpeed", "metricsIndex": 2},
        {"key": "directElevation", "metricsIndex": 3},
        {"key": "sumDistance", "metricsIndex": 4},
    ]
    points = []
    dist = 0.0
    prev_t = None
    for t, h, p, e in zip(series["time_s"], series["heart_rate_bpm"], series["pace_min_per_km"], series["elevation_m"]):
        speed = 1000 / (p * 60) if p else None
        if prev_t is not None and speed and t - prev_t <= 15:
            dist += speed * (t - prev_t)
        prev_t = t
        points.append({"metrics": [h, float(t), speed, e, round(dist, 1)]})
    return {"metricDescriptors": descriptors, "activityDetailMetrics": points}
