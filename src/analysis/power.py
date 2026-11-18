"""Analyse vélo en puissance (capteur, home trainer, sortie MyWhoosh importée).

Fonctions pures, appelées par `series.analyse` quand la série `power_w` existe. Sur un home
trainer, la vitesse est virtuelle : c'est la puissance qui mesure l'intensité. Les calculs
sont faits sur le TEMPS, pas sur le nombre de points (Garmin n'enregistre souvent qu'un point
toutes les 1 à 3 s) : chaque point vaut jusqu'au suivant, sauf au-delà de `PAUSE_GAP_S`
(pause, compté 1 s).

La FTP n'est jamais devinée : sans `ftp_w`, ni % FTP, ni IF, ni TSS, ni zones.
"""

from __future__ import annotations

import statistics as st
from typing import Any

# Seuils partagés avec l'analyse vitesse/FC (mêmes règles de pause, de durée minimale et de
# fiabilité du découplage).
from src.analysis.series import (
    DEFAULT_PAUSE_MERGE_VELO_S,
    PAUSE_GAP_S,
    _MIN_BLOCK_HR_CONTRAST_BPM,
    _MIN_CLUSTER_SHARE,
    _MIN_DECOUPLING_S,
    _VELO_AUTO_MERGE_STEPS_S,
    _VELO_MAX_ABS_DECOUPLING_PCT,
    _VELO_MIN_COVERAGE_PCT,
    _VELO_WIDEN_MIN_MOVING_S,
    _hr_contrast,
    _moving_midpoint,
    _r,
    _signed_decoupling_reading,
    effort_blocks,
    kmeans1d,
    merged_segments,
    moving_time,
)

# Zones de puissance de la Skill Vélo, en % de FTP (borne haute incluse) ; Z7 au-delà de 150 %.
POWER_ZONES = [
    ("Z1 recuperation", 55),
    ("Z2 endurance", 75),
    ("Z3 tempo", 90),
    ("Z4 seuil", 105),
    ("Z5 VO2max", 120),
    ("Z6 anaerobie", 150),
    ("Z7 neuromusculaire", None),
]
BEST_EFFORTS_S = {"1min": 60, "5min": 300, "10min": 600, "20min": 1200}
_NP_WINDOW_S = 30
_SMOOTH_HALF_S = 15  # lissage des blocs : moyenne sur ± 15 s
# Détection des blocs en mode auto : centres effort / récup écartés d'au moins 40 W ; écart
# « franc » (sans contrôle FC) dès 80 W — équivalents puissance des 5 / 10 km/h du vélo.
_MIN_POWER_GAP_W = 40
_STRONG_POWER_GAP_W = 80
# Fiabilité du découplage Pw:FC : la puissance moyenne ne doit pas varier de plus de 15 %
# entre les moitiés (la FC ne répond pas linéairement à un changement d'intensité).
_MAX_POWER_GAP_PCT = 15.0
# Verdict d'un bloc face à `puissance_cible_w` : écart relatif.
_TARGET_OK_PCT = 5.0
_TARGET_DRIFT_PCT = 10.0


def has_power(power: list) -> bool:
    return any(isinstance(p, (int, float)) and p > 0 for p in power)


def _weights(t: list[int]) -> list[int]:
    """Durée représentée par chaque point : jusqu'au suivant, 1 s après une pause ou en fin."""
    out = []
    for k in range(len(t)):
        dt = t[k + 1] - t[k] if k + 1 < len(t) else 1
        out.append(dt if 0 < dt <= PAUSE_GAP_S else 1)
    return out


def per_second(t: list[int], power: list) -> list[float]:
    """Puissance seconde par seconde sur le temps en mouvement (pauses exclues, absence de
    valeur = 0 W : roue libre)."""
    out: list[float] = []
    for p, w in zip(power, _weights(t)):
        out.extend([float(p) if isinstance(p, (int, float)) else 0.0] * w)
    return out


def _rolling_means(sec: list[float], window: int) -> list[float]:
    if len(sec) < window:
        return []
    acc = sum(sec[:window])
    out = [acc / window]
    for k in range(window, len(sec)):
        acc += sec[k] - sec[k - window]
        out.append(acc / window)
    return out


def normalized_power(sec: list[float]) -> float | None:
    rolling = _rolling_means(sec, _NP_WINDOW_S)
    if not rolling:
        return None
    return st.mean(x**4 for x in rolling) ** 0.25


def best_efforts(sec: list[float]) -> dict[str, int | None]:
    return {
        label: (round(max(r)) if (r := _rolling_means(sec, s)) else None) for label, s in BEST_EFFORTS_S.items()
    }


def power_zones(sec: list[float], ftp_w: float) -> list[dict[str, Any]]:
    counts = [0] * len(POWER_ZONES)
    for p in sec:
        pct = p * 100 / ftp_w  # p / ftp * 100 donnerait 55.00000000000001 pour 55 %
        idx = next((n for n, (_, hi) in enumerate(POWER_ZONES) if hi is not None and pct <= hi), len(POWER_ZONES) - 1)
        counts[idx] += 1
    total = len(sec) or 1
    lows = [0] + [hi for _, hi in POWER_ZONES[:-1]]
    return [
        {
            "zone": name,
            "pct_ftp": f"> {low} %" if hi is None else (f"<= {hi} %" if low == 0 else f"{low + 1}-{hi} %"),
            "watts": f"> {round(low * ftp_w / 100)} W" if hi is None else f"<= {round(hi * ftp_w / 100)} W",
            "pct": round(c / total * 100, 1),
            "duree_s": c,
        }
        for (name, hi), low, c in zip(POWER_ZONES, lows, counts)
    ]


def _weighted_mean(t: list[int], xs: list, i: int, j: int, *, zero_is_missing: bool = False) -> float | None:
    w = _weights(t)
    num = den = 0.0
    for k in range(i, j + 1):
        x = xs[k]
        if not isinstance(x, (int, float)) or (zero_is_missing and x <= 0):
            if zero_is_missing:
                continue
            x = 0.0
        num += x * w[k]
        den += w[k]
    return num / den if den else None


def _smoothed(t: list[int], power: list) -> list[float | None]:
    """Moyenne glissante sur ± 15 s de TEMPS (indépendante de la fréquence d'échantillonnage)."""
    out: list[float | None] = []
    lo = 0
    hi = 0
    acc = 0.0
    n = 0
    vals = [float(p) if isinstance(p, (int, float)) else 0.0 for p in power]
    for k in range(len(t)):
        while hi < len(t) and t[hi] <= t[k] + _SMOOTH_HALF_S:
            acc += vals[hi]
            n += 1
            hi += 1
        while t[lo] < t[k] - _SMOOTH_HALF_S:
            acc -= vals[lo]
            n -= 1
            lo += 1
        out.append(acc / n if n else None)
    return out


def detect_power_blocks(
    t: list[int], hr: list, power: list, segs: list[tuple[int, int]], type: str
) -> tuple[list[tuple[int, int]], dict[str, Any]]:
    """Blocs d'effort sur la puissance lissée (même logique que la détection sur la vitesse :
    2-means, garde-fous du mode auto, contrôle de FC si l'écart n'est pas franc)."""
    info: dict[str, Any] = {"demande": type, "base_detection": "puissance"}
    if type == "endurance":
        info["detection_blocs"] = "desactivee"
        info["effet"] = "aucun bloc recherché : séance traitée comme un effort continu"
        return [], info
    sm = _smoothed(t, power)
    valid = [x for x in sm if x is not None]
    low, high = kmeans1d(valid)
    if low is None:
        info["detection_blocs"] = "automatique" if type == "auto" else "forcee"
        info["effet"] = "aucun bloc : série trop courte"
        return [], info
    threshold = (low + high) / 2
    info["centre_effort_w"] = round(high)
    info["centre_recup_w"] = round(low)
    if type == "auto":
        share = sum(1 for x in valid if x >= threshold) / len(valid)
        bimodal = high - low >= _MIN_POWER_GAP_W and _MIN_CLUSTER_SHARE <= share <= 1 - _MIN_CLUSTER_SHARE
        info["detection_blocs"] = "automatique"
        # Clé historique de la détection sur la vitesse ; ici, bimodalité de la PUISSANCE.
        info["allure_bimodale"] = bimodal
        info["puissance_bimodale"] = bimodal
        if not bimodal:
            info["effet"] = (
                "aucun bloc : puissance trop régulière pour contenir des efforts distincts "
                "(résultat identique à type=\"endurance\")"
            )
            return [], info
    else:
        info["detection_blocs"] = "forcee"
    negated = [-x if x is not None else None for x in sm]
    blocks: list[tuple[int, int]] = []
    for i, j in segs:
        blocks += [(i + a, i + b) for a, b in effort_blocks(t[i : j + 1], negated[i : j + 1], -threshold)]
    blocks.sort()
    if type == "auto" and blocks and high - low < _STRONG_POWER_GAP_W and any(hr):
        contrast = _hr_contrast(hr, blocks)
        info["contraste_fc_bpm"] = _r(contrast, 1)
        if contrast is not None and contrast < _MIN_BLOCK_HR_CONTRAST_BPM:
            info["effet"] = "blocs candidats écartés : FC à peine plus haute que le reste"
            return [], info
    info["effet"] = f"{len(blocks)} bloc(s) d'effort retenu(s)"
    return blocks, info


def block_power(
    t: list[int], power: list, cadence: list, i: int, j: int, ftp_w: float | None, cible_w: float | None
) -> dict[str, Any]:
    avg = _weighted_mean(t, power, i, j)
    values = [p for p in power[i : j + 1] if isinstance(p, (int, float))]
    out: dict[str, Any] = {
        "puissance_moy_w": _r(avg, 0),
        "puissance_max_w": max(values) if values else None,
        "pct_ftp": round(avg / ftp_w * 100) if avg is not None and ftp_w else None,
        "cadence_moy_rpm": _r(_weighted_mean(t, cadence, i, j, zero_is_missing=True), 0) if cadence else None,
    }
    if cible_w and avg is not None:
        ecart = avg - cible_w
        pct = ecart / cible_w * 100
        out["ecart_cible_w"] = round(ecart)
        out["ecart_cible_pct"] = round(pct, 1)
        out["verdict_puissance"] = (
            "conforme" if abs(pct) <= _TARGET_OK_PCT else "derive" if abs(pct) <= _TARGET_DRIFT_PCT else "hors cible"
        )
    return out


def _power_reasons(d: float, p1: float, p2: float, coverage_pct: float) -> list[str]:
    reasons = []
    if abs(d) > _VELO_MAX_ABS_DECOUPLING_PCT:
        reasons.append(f"découplage de {d:+.1f} % hors de la plage plausible (±{_VELO_MAX_ABS_DECOUPLING_PCT:.0f} %)")
    if p1 and abs(p2 - p1) / p1 * 100 > _MAX_POWER_GAP_PCT:
        reasons.append(
            f"puissance moyenne très différente entre les moitiés ({p1:.0f} puis {p2:.0f} W, écart > "
            f"{_MAX_POWER_GAP_PCT:.0f} %) : intensités non comparables"
        )
    if coverage_pct < _VELO_MIN_COVERAGE_PCT:
        reasons.append(
            f"fenêtre couvrant {coverage_pct:.0f} % du temps en mouvement seulement "
            f"(< {_VELO_MIN_COVERAGE_PCT:.0f} %) : l'essentiel de la sortie est ignoré"
        )
    return reasons


def power_decoupling(
    t: list[int], hr: list, power: list, blocks: list[tuple[int, int]], segs: list[tuple[int, int]],
    pause_merge_s: float | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Découplage Pw:FC (puissance / FC, 1re vs 2e moitié du temps en mouvement du plus long
    effort continu >= 40 min), mêmes fenêtres et fusion de pauses que le découplage vélo.
    Renvoie (découplage, raison si null)."""
    if not any(isinstance(h, (int, float)) and h > 0 for h in hr):
        return None, "FC absente : découplage puissance/FC impossible"
    total_moving = moving_time(t)
    auto = pause_merge_s is None

    def window(threshold: float) -> tuple[int, int, list[dict[str, int]], int]:
        if blocks:
            candidates, absorbed = blocks, [[] for _ in blocks]
        else:
            candidates, absorbed = merged_segments(t, segs, threshold)
        best = max(range(len(candidates)), key=lambda n: moving_time(t, *candidates[n]))
        i, j = candidates[best]
        return i, j, absorbed[best], moving_time(t, i, j)

    applied = DEFAULT_PAUSE_MERGE_VELO_S if auto else pause_merge_s
    i, j, absorbed, moving = window(applied)
    if auto and not blocks and total_moving > _VELO_WIDEN_MIN_MOVING_S:
        for step in _VELO_AUTO_MERGE_STEPS_S:
            if moving / total_moving * 100 >= _VELO_MIN_COVERAGE_PCT:
                break
            applied = step
            i, j, absorbed, moving = window(step)
    if moving < _MIN_DECOUPLING_S:
        return None, f"effort continu le plus long de {moving // 60} min (< {_MIN_DECOUPLING_S // 60} min)"

    k = _moving_midpoint(t, i, j)
    halves = []
    for a, b in ((i, k), (k, j)):
        p = _weighted_mean(t, power, a, b)
        h = _weighted_mean(t, hr, a, b, zero_is_missing=True)
        halves.append((a, b, p, h))
    (_, _, p1, h1), (_, _, p2, h2) = halves
    if not (p1 and p2 and h1 and h2):
        return None, "FC ou puissance manquante sur une moitié de la fenêtre"
    ef1, ef2 = p1 / h1, p2 / h2
    d = (ef1 - ef2) / ef1 * 100  # > 0 : la FC monte pour une même puissance (dérive)
    coverage = moving / total_moving * 100 if total_moving else 0.0
    reasons = _power_reasons(d, p1, p2, coverage)
    moities = {
        name: {
            "duree_mouvement_s": moving_time(t, a, b),
            "puissance_moy_w": round(p),
            "fc_moy": round(h),
            "rapport_w_par_bpm": round(p / h, 3),
        }
        for name, (a, b, p, h) in zip(("premiere", "seconde"), halves)
    }
    return {
        "debut_s": t[i],
        "fin_s": t[j],
        "duree_s": t[j] - t[i],
        "duree_mouvement_s": moving,
        "couverture_pct": round(coverage, 1),
        "pause_merge_s": applied,
        "pause_merge_mode": "auto" if auto else "explicite",
        "fusion_elargie": auto and applied > DEFAULT_PAUSE_MERGE_VELO_S,
        "pauses_fusionnees": absorbed,
        "moities": moities,
        "methode": "puissance / FC (Pw:FC), 1re vs 2e moitié du temps en mouvement",
        "decouplage_pct": round(d, 1),
        "fiable": not reasons,
        "raisons_fiabilite": reasons,
        "lecture": _signed_decoupling_reading(d) if not reasons else "non interprétable",
    }, None


def overview(sec: list[float], cadence_t: list[int], cadence: list, ftp_w: float | None) -> dict[str, Any]:
    """Champs puissance de `vue_ensemble`."""
    avg = st.mean(sec) if sec else None
    np_ = normalized_power(sec)
    cad = _weighted_mean(cadence_t, cadence, 0, len(cadence) - 1, zero_is_missing=True) if cadence else None
    out: dict[str, Any] = {
        "puissance_moy_w": _r(avg, 0),
        "puissance_normalisee_w": _r(np_, 0),
        "vi": round(np_ / avg, 2) if np_ and avg else None,
        "puissance_max_w": round(max(sec)) if sec else None,
        "cadence_moy_rpm": _r(cad, 0),
        "travail_kj": round(sum(sec) / 1000) if sec else None,
        "ftp_utilisee_w": ftp_w,
    }
    if ftp_w and np_:
        intensity = np_ / ftp_w
        out["if"] = round(intensity, 2)
        out["tss"] = round(len(sec) * np_ * intensity / (ftp_w * 3600) * 100, 1)
        out["puissance_moy_pct_ftp"] = round(avg / ftp_w * 100) if avg else None
    else:
        out["if"] = None
        out["tss"] = None
        out["message_ftp"] = (
            "ftp_w non fourni : % FTP, IF, TSS et zones de puissance non calculés "
            "(la FTP n'est jamais devinée par le serveur)"
        )
    return out

