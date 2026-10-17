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


# Longueurs aberrantes relevées sur 24748453127 (n° de longueur -> durée en s).
SWIM_SUSPECTS_REF = {21: 18.0, 59: 20.3}


def swim_session() -> list[dict[str, Any]]:
    """Séance de nage calquée sur 24748453127 (16/10) : 72 longueurs de 25 m, sortie de
    `GarminClient.get_swim_splits`. Structure : 200 m échauffement (8 L à 30 s), 8 × 100 m
    (4 L à 27 s, 20 s de repos), 400 m (16 L à 28 s), 400 m retour au calme (16 L à 29 s) ;
    30 s de repos entre les parties. Longueurs 21 (18,0 s) et 59 (20,3 s) aberrantes."""
    plan: list[tuple[int, float, float]] = [(8, 30.0, 30.0)]  # (longueurs, durée, repos après)
    plan += [(4, 27.0, 20.0)] * 7 + [(4, 27.0, 30.0)]
    plan += [(16, 28.0, 30.0), (16, 29.0, 0.0)]
    rng = random.Random(3)
    splits: list[dict[str, Any]] = []
    t = 0.0
    idx = 1
    for n_lengths, duration, rest in plan:
        for _ in range(n_lengths):
            d = SWIM_SUSPECTS_REF.get(idx, round(duration + rng.uniform(-0.8, 0.8), 1))
            pace = round(d / 25 * 100 / 60, 2)
            splits.append(
                {
                    "length_idx": idx,
                    "start_s": int(t),
                    "duration_s": d,
                    "distance_m": 25.0,
                    "avg_hr": 130 + rng.randint(0, 15),
                    "swolf": int(d) + 16,
                    "stroke": "FREESTYLE",
                    "pace_min_per_100m": pace,
                    "pace_min_sec_per_100m": f"{int(pace)}:{round((pace - int(pace)) * 60):02d}",
                }
            )
            t += d
            idx += 1
        t += rest
    return splits
