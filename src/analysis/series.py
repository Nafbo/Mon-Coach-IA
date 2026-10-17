"""Analyse des séries temporelles d'une activité (portage serveur de `analyse_series.py`,
livré avec la Skill "analyst").

Fonctions pures, sans I/O : l'entrée est exactement la sortie de
`GarminClient.get_activity_details` (`time_s`, `heart_rate_bpm`, `pace_min_per_km`,
`elevation_m`), la sortie un dict JSON compact (quelques centaines de tokens au lieu des
~30 000 des séries brutes).

Unités : temps en secondes, allures en minutes décimales par km (doublées d'un champ
`*_mmss` "M:SS"), distances en km, FC en bpm, dénivelé en m.
"""

from __future__ import annotations

import statistics as st
from typing import Any

# Intervalle entre deux échantillons au-delà duquel on considère que l'enregistrement s'est
# interrompu (pause montre / lap) — même seuil que le script d'origine.
PAUSE_GAP_S = 15
# Allures au-delà de 12 min/km = marche/arrêt : ignorées pour la segmentation.
_MAX_PACE_SEGMENTATION = 12
# Durée minimale d'un bloc d'effort.
_MIN_BLOCK_S = 25
# Effort continu minimal pour calculer un découplage cardiaque.
_MIN_DECOUPLING_S = 2400
# Bornes Friel en fraction de la FC seuil.
_ZONE_BOUNDS = [0.85, 0.90, 0.95, 1.00, 1.03]
_ZONE_NAMES = ["Z1 recup", "Z2 endurance", "Z3 tempo", "Z4 seuil", "Z5a", "Z5b+"]

ANALYSIS_TYPES = ("auto", "fractionne", "seuil", "endurance")


# ---------------------------------------------------------------- utilitaires


def mmss(pace: float | None) -> str | None:
    """4.25 (min/km décimal) -> "4:15"."""
    if pace is None:
        return None
    total = round(pace * 60)
    m, s = divmod(total, 60)
    return f"{m}:{s:02d}"


def _r(value: float | None, ndigits: int = 1) -> float | None:
    return round(value, ndigits) if value is not None else None


def smooth(xs: list[float | None], w: int = 5) -> list[float | None]:
    """Médiane glissante, tolérante aux trous."""
    out: list[float | None] = []
    for i in range(len(xs)):
        window = [x for x in xs[max(0, i - w // 2) : i + w // 2 + 1] if x is not None]
        out.append(st.median(window) if window else None)
    return out


def kmeans1d(vals: list[float], iters: int = 25) -> tuple[float | None, float | None]:
    """2-means sur une série 1D. Renvoie (centre_rapide, centre_lent)."""
    vs = sorted(v for v in vals if v is not None)
    if len(vs) < 4:
        return None, None
    a, b = vs[len(vs) // 10], vs[-max(1, len(vs) // 10)]
    for _ in range(iters):
        ga = [v for v in vs if abs(v - a) <= abs(v - b)]
        gb = [v for v in vs if abs(v - a) > abs(v - b)]
        if not ga or not gb:
            break
        na, nb = st.mean(ga), st.mean(gb)
        if abs(na - a) < 1e-6 and abs(nb - b) < 1e-6:
            break
        a, b = na, nb
    return (a, b) if a < b else (b, a)


def slope_cost(grade_pct: float) -> float:
    """Facteur de coût énergétique relatif au plat (approximation Minetti simplifiée)."""
    g = max(-12.0, min(18.0, grade_pct))
    return 1 + 0.030 * g if g >= 0 else 1 + 0.012 * g


# ------------------------------------------------------------------- chargement


def load_series(details: dict[str, Any]) -> tuple[list[int], list, list, list]:
    """Aligne les séries sur `time_s` et retire les points sans temps."""
    t_raw = details.get("time_s") or []
    n = len(t_raw)

    def pad(key: str) -> list:
        xs = list(details.get(key) or [])
        return (xs + [None] * n)[:n]

    hr, pa, el = pad("heart_rate_bpm"), pad("pace_min_per_km"), pad("elevation_m")
    keep = [i for i in range(n) if t_raw[i] is not None]
    return [t_raw[i] for i in keep], [hr[i] for i in keep], [pa[i] for i in keep], [el[i] for i in keep]


# --------------------------------------------------------------- segmentation


def continuous_segments(t: list[int], gap_s: int = PAUSE_GAP_S) -> list[tuple[int, int]]:
    """Coupe la série là où l'enregistrement s'interrompt (pauses / laps)."""
    if not t:
        return []
    segs, start = [], 0
    for i in range(1, len(t)):
        if t[i] - t[i - 1] > gap_s:
            segs.append((start, i - 1))
            start = i
    segs.append((start, len(t) - 1))
    return segs


def effort_blocks(t: list[int], pace_smooth: list, threshold: float, min_s: int = _MIN_BLOCK_S) -> list[tuple[int, int]]:
    """Regroupe les échantillons plus rapides que `threshold` en blocs d'effort."""
    marks = [p is not None and p <= threshold for p in pace_smooth]
    blocks, start = [], None
    for i, m in enumerate(marks):
        if m and start is None:
            start = i
        elif not m and start is not None:
            if t[i - 1] - t[start] >= min_s:
                blocks.append((start, i - 1))
            start = None
    if start is not None and t[-1] - t[start] >= min_s:
        blocks.append((start, len(t) - 1))
    return blocks


# ------------------------------------------------------------------- métriques


def _distance_km(t: list[int], pa: list, i: int, j: int) -> float:
    dist = 0.0
    for k in range(i, j):
        p = pa[k]
        if p and p > 0:
            dist += ((t[k + 1] - t[k]) / 60.0) / p
    return dist


def block_stats(t: list[int], hr: list, pa: list, el: list, i: int, j: int) -> dict[str, Any]:
    dt = t[j] - t[i]
    dist = _distance_km(t, pa, i, j)
    hrs = [h for h in hr[i : j + 1] if h]
    els = [e for e in el[i : j + 1] if e is not None]
    dplus = sum(max(0, els[k + 1] - els[k]) for k in range(len(els) - 1)) if len(els) > 1 else 0
    grade = ((els[-1] - els[0]) / (dist * 1000) * 100) if (els and dist > 0.02) else 0.0
    pace = (dt / 60.0) / dist if dist > 0.02 else None
    flat = pace / slope_cost(grade) if pace else None
    return {
        "debut_s": t[i],
        "duree_s": dt,
        "dist_km": round(dist, 2),
        "allure": _r(pace, 2),
        "allure_mmss": mmss(pace),
        "allure_plat": _r(flat, 2),
        "allure_plat_mmss": mmss(flat),
        "fc_moy": _r(st.mean(hrs), 0) if hrs else None,
        "fc_max": max(hrs) if hrs else None,
        "dplus_m": round(dplus),
        "pente_pct": round(grade, 1),
    }


def cardiac_decoupling(t: list[int], hr: list, pa: list, i: int, j: int) -> float | None:
    """Découplage FC/allure entre 1re et 2e moitié (en %). > 5 % = dérive marquée."""
    mid = (t[i] + t[j]) // 2
    k = next((x for x in range(i, j + 1) if t[x] >= mid), j)

    def ratio(a: int, b: int) -> float | None:
        h = [x for x in hr[a : b + 1] if x]
        p = [x for x in pa[a : b + 1] if x and x < _MAX_PACE_SEGMENTATION]
        if not h or not p:
            return None
        return st.mean(h) * st.mean(p)  # FC × allure ~ FC / vitesse

    r1, r2 = ratio(i, k), ratio(k, j)
    return None if not (r1 and r2) else (r2 - r1) / r1 * 100


def hr_zones(hr: list, lthr: float) -> list[dict[str, Any]]:
    """Répartition des échantillons par zone FC (bornes Friel basées sur la FC seuil)."""
    counts = [0] * len(_ZONE_NAMES)
    for h in hr:
        if not h:
            continue
        counts[sum(1 for b in _ZONE_BOUNDS if h / lthr >= b)] += 1
    total = sum(counts) or 1
    return [{"zone": name, "pct": round(c / total * 100, 1)} for name, c in zip(_ZONE_NAMES, counts)]


def _recoveries(t: list[int], hr: list, blocks: list[tuple[int, int]]) -> list[dict[str, Any]]:
    out = []
    for n in range(len(blocks) - 1):
        end, nxt = blocks[n][1], blocks[n + 1][0]
        hr_end = next((hr[k] for k in range(end, max(end - 8, 0), -1) if hr[k]), None)
        k60 = next((k for k in range(end, nxt + 1) if t[k] >= t[end] + 60), None)
        hr60 = hr[k60] if k60 is not None and hr[k60] else None
        out.append(
            {
                "apres_bloc": n + 1,
                "duree_s": t[nxt] - t[end],
                "fc_fin_bloc": hr_end,
                "fc_60s": hr60,
                "baisse_60s_bpm": (hr_end - hr60) if (hr_end and hr60) else None,
            }
        )
    return out


def _verdict(ecart_s: float) -> str:
    if abs(ecart_s) <= 5:
        return "conforme"
    if abs(ecart_s) <= 15:
        return "derive"
    return "hors cible"


def _decoupling_reading(d: float) -> str:
    return "stable" if d < 5 else "derive moderee" if d < 8 else "derive marquee"


# ------------------------------------------------------------------------ main


def analyse(
    details: dict[str, Any], fc_seuil: float, allure_cible: float | None = None, type: str = "auto"
) -> dict[str, Any]:
    """Analyse complète d'une activité ; renvoie un dict JSON compact.

    `status` vaut "no_data" si la série est trop courte / sans allure (ex. natation en
    piscine, sans vitesse continue — utiliser garmin_get_swim_splits).
    """
    t, hr, pa, el = load_series(details)
    pace_smooth = smooth(pa, 5)
    fast, slow = kmeans1d([p for p in pace_smooth if p and p < _MAX_PACE_SEGMENTATION])
    if fast is None:
        return {"status": "no_data", "message": "Série trop courte ou sans allure pour être segmentée."}
    threshold = (fast + slow) / 2

    duration = t[-1] - t[0]
    dist = _distance_km(t, pa, 0, len(t) - 1)
    hrs = [h for h in hr if h]
    avg_pace = duration / 60 / dist if dist else None

    segs = continuous_segments(t)
    blocks: list[tuple[int, int]] = []
    for i, j in segs:
        blocks += [(i + a, i + b) for a, b in effort_blocks(t[i : j + 1], pace_smooth[i : j + 1], threshold)]
    blocks.sort()

    block_list = []
    for n, (i, j) in enumerate(blocks, 1):
        s = {"n": n, **block_stats(t, hr, pa, el, i, j)}
        if allure_cible and s["allure"]:
            ecart = (s["allure"] - allure_cible) * 60
            s["ecart_cible_s_km"] = round(ecart)
            s["verdict"] = _verdict(ecart)
        block_list.append(s)

    decoupling = None
    longest = max(segs, key=lambda s: t[s[1]] - t[s[0]])
    if t[longest[1]] - t[longest[0]] >= _MIN_DECOUPLING_S:
        d = cardiac_decoupling(t, hr, pa, *longest)
        if d is not None:
            decoupling = {
                "debut_s": t[longest[0]],
                "duree_s": t[longest[1]] - t[longest[0]],
                "decouplage_pct": round(d, 1),
                "lecture": _decoupling_reading(d),
            }

    return {
        "status": "ok",
        "parametres": {"fc_seuil": fc_seuil, "allure_cible": allure_cible, "type": type},
        "vue_ensemble": {
            "duree_s": duration,
            "distance_km": round(dist, 2),
            "allure_moy": _r(avg_pace, 2),
            "allure_moy_mmss": mmss(avg_pace),
            "fc_moy": _r(st.mean(hrs), 0) if hrs else None,
            "fc_max": max(hrs) if hrs else None,
            "fc_max_pct_seuil": round(max(hrs) / fc_seuil * 100) if hrs else None,
            "bascule_effort_recup": _r(threshold, 2),
            "bascule_effort_recup_mmss": mmss(threshold),
            "centre_rapide_mmss": mmss(fast),
            "centre_lent_mmss": mmss(slow),
            "nb_segments": len(segs),
        },
        "zones_fc": [z for z in hr_zones(hr, fc_seuil) if z["pct"] >= 0.5],
        "blocs": block_list,
        "recuperations": _recoveries(t, hr, blocks),
        "derive_cardiaque": decoupling,
    }


def downsample(details: dict[str, Any], resolution_s: int) -> dict[str, Any]:
    """Sous-échantillonne les séries : 1 point par tranche de `resolution_s` secondes
    (médiane de chaque série sur la tranche, temps = premier échantillon de la tranche).
    Les clés non-série du dict sont conservées telles quelles."""
    t = details.get("time_s") or []
    series_keys = [k for k, v in details.items() if isinstance(v, list) and len(v) == len(t) and k != "time_s"]
    buckets: dict[int, list[int]] = {}
    for i, ti in enumerate(t):
        if ti is not None:
            buckets.setdefault(ti // resolution_s, []).append(i)

    out: dict[str, Any] = {k: v for k, v in details.items() if k not in series_keys and k != "time_s"}
    out["time_s"] = [t[idx[0]] for idx in buckets.values()]
    for key in series_keys:
        col = details[key]
        values = []
        for idx in buckets.values():
            xs = [col[i] for i in idx if col[i] is not None]
            if not xs:
                values.append(None)
            elif all(isinstance(x, int) for x in xs):
                values.append(round(st.median(xs)))
            else:
                values.append(round(st.median(xs), 2))
        out[key] = values
    out["resolution_s"] = resolution_s
    return out
