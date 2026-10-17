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
# Garde-fous de détection de blocs en mode "auto" : sans eux, le 2-means coupe toujours
# la série en deux, même sur un footing à allure régulière (côtes, bruit GPS) -> faux blocs.
# Écart minimal entre allure d'effort et allure de récup, et part minimale de chaque groupe.
_MIN_CLUSTER_GAP_MIN_KM = 20 / 60
_MIN_CLUSTER_SHARE = 0.10
# Sur une sortie facile entrecoupée d'arrêts (feux, pauses), l'allure est bimodale sans qu'il
# y ait d'effort : en mode "auto", les blocs ne sont retenus que si leur FC moyenne dépasse
# d'au moins 10 bpm celle du reste de la séance, ou si l'écart d'allure atteint 60 s/km
# (efforts courts type 30/30, où la FC n'a pas le temps de monter). Validé sur séries réelles :
# seuil 3 × 6'30 (24725112038) -> +29 bpm ; footing avec arrêts (24737274389) -> +7 bpm.
_MIN_BLOCK_HR_CONTRAST_BPM = 10
_STRONG_PACE_GAP_MIN_KM = 60 / 60
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


def load_series(details: dict[str, Any]) -> tuple[list[int], list, list, list, list]:
    """Aligne les séries sur `time_s` et retire les points sans temps. Renvoie
    (temps, FC, allure, altitude, distance cumulée en m — None si non exposée)."""
    t_raw = details.get("time_s") or []
    n = len(t_raw)

    def pad(key: str) -> list:
        xs = list(details.get(key) or [])
        return (xs + [None] * n)[:n]

    hr, pa, el, di = pad("heart_rate_bpm"), pad("pace_min_per_km"), pad("elevation_m"), pad("distance_m")
    keep = [i for i in range(n) if t_raw[i] is not None]
    return tuple([col[i] for i in keep] for col in (t_raw, hr, pa, el, di))  # type: ignore[return-value]


def pauses(t: list[int], gap_s: int = PAUSE_GAP_S) -> list[dict[str, int]]:
    """Interruptions d'enregistrement (> `gap_s`) : début, fin et durée en secondes depuis le
    départ, en double nommage (`debut_s`/`fin_s`/`duree_s` historiques, `start_s`/`end_s`/
    `duration_s`)."""
    return [
        {
            "debut_s": t[k],
            "fin_s": t[k + 1],
            "duree_s": t[k + 1] - t[k],
            "start_s": t[k],
            "end_s": t[k + 1],
            "duration_s": t[k + 1] - t[k],
        }
        for k in range(len(t) - 1)
        if t[k + 1] - t[k] > gap_s
    ]


def moving_time(t: list[int], i: int = 0, j: int | None = None, gap_s: int = PAUSE_GAP_S) -> int:
    """Temps en mouvement entre les indices i et j : somme des pas <= `gap_s`."""
    j = len(t) - 1 if j is None else j
    return sum(t[k + 1] - t[k] for k in range(i, j) if t[k + 1] - t[k] <= gap_s)


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


def _distance_km(t: list[int], pa: list, i: int, j: int, di: list | None = None) -> float:
    """Distance entre i et j. Utilise la distance cumulée Garmin (`sumDistance`) quand elle
    est disponible ; sinon intègre l'allure, en excluant les pauses (> PAUSE_GAP_S) — les
    compter revenait à "courir" pendant la pause à l'allure du dernier point."""
    if di is not None and di[i] is not None and di[j] is not None:
        return max(0.0, (di[j] - di[i]) / 1000)
    dist = 0.0
    for k in range(i, j):
        p, step = pa[k], t[k + 1] - t[k]
        if p and p > 0 and step <= PAUSE_GAP_S:
            dist += (step / 60.0) / p
    return dist


def block_stats(t: list[int], hr: list, pa: list, el: list, i: int, j: int, di: list | None = None) -> dict[str, Any]:
    dt = moving_time(t, i, j)
    dist = _distance_km(t, pa, i, j, di)
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
        "pace_min_per_km": _r(pace, 2),
        "pace_mmss": mmss(pace),
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
    """Récupération entre blocs. `baisse_60s_bpm` est signée : positive = la FC a baissé,
    négative = elle est remontée (le script d'origine affichait alors "--2 bpm").
    `duree_s` = temps en mouvement ; `inclut_pause` signale une pause montre dans la récup."""
    out = []
    for n in range(len(blocks) - 1):
        end, nxt = blocks[n][1], blocks[n + 1][0]
        hr_end = next((hr[k] for k in range(end, max(end - 8, 0), -1) if hr[k]), None)
        k60 = next((k for k in range(end, nxt + 1) if t[k] >= t[end] + 60), None)
        hr60 = hr[k60] if k60 is not None and hr[k60] else None
        delta = (hr_end - hr60) if (hr_end and hr60) else None
        out.append(
            {
                "apres_bloc": n + 1,
                "duree_s": moving_time(t, end, nxt),
                "inclut_pause": any(t[k + 1] - t[k] > PAUSE_GAP_S for k in range(end, nxt)),
                "fc_fin_bloc": hr_end,
                "fc_60s": hr60,
                "baisse_60s_bpm": delta,
                "fc_remonte": delta < 0 if delta is not None else None,
                # Variation de FC sur 60 s (FC à +60 s moins FC de fin de bloc) : négative =
                # redescente. Formatée avec un vrai signe ("−12 bpm", "+3 bpm"), jamais "--N".
                "variation_fc_60s_bpm": -delta if delta is not None else None,
                "variation_fc_60s": format_bpm_delta(-delta) if delta is not None else None,
            }
        )
    return out


def _hr_contrast(hr: list, blocks: list[tuple[int, int]]) -> float | None:
    """FC moyenne dans les blocs moins FC moyenne hors blocs (bpm), None sans FC."""
    inside = set()
    for i, j in blocks:
        inside.update(range(i, j + 1))
    hr_in = [hr[k] for k in inside if hr[k]]
    hr_out = [hr[k] for k in range(len(hr)) if k not in inside and hr[k]]
    if not hr_in or not hr_out:
        return None
    return st.mean(hr_in) - st.mean(hr_out)


def format_bpm_delta(value: int) -> str:
    """-12 -> "−12 bpm" (signe moins typographique), 3 -> "+3 bpm", 0 -> "0 bpm"."""
    if value > 0:
        return f"+{value} bpm"
    if value < 0:
        return f"−{-value} bpm"
    return "0 bpm"


def _is_bimodal(paces: list[float], fast: float, slow: float) -> bool:
    """Vrai si la série contient réellement deux régimes (effort / récup) : centres écartés
    d'au moins 20 s/km et chaque groupe >= 10 % des points."""
    if slow - fast < _MIN_CLUSTER_GAP_MIN_KM:
        return False
    threshold = (fast + slow) / 2
    share_fast = sum(1 for p in paces if p <= threshold) / len(paces)
    return _MIN_CLUSTER_SHARE <= share_fast <= 1 - _MIN_CLUSTER_SHARE


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
    t, hr, pa, el, di = load_series(details)
    has_distance = any(d is not None for d in di)
    pace_smooth = smooth(pa, 5)
    valid_paces = [p for p in pace_smooth if p and p < _MAX_PACE_SEGMENTATION]
    fast, slow = kmeans1d(valid_paces)
    if fast is None:
        return {"status": "no_data", "message": "Série trop courte ou sans allure pour être segmentée."}
    threshold = (fast + slow) / 2

    elapsed = t[-1] - t[0]
    moving = moving_time(t)
    dist = _distance_km(t, pa, 0, len(t) - 1, di if has_distance else None)
    hrs = [h for h in hr if h]
    avg_pace = moving / 60 / dist if dist else None

    # Détection des blocs : "endurance" -> aucune ; "fractionne"/"seuil" -> toujours ;
    # "auto" -> seulement si l'allure est réellement bimodale (pas de faux blocs sur un
    # footing régulier).
    if type == "endurance":
        detect = False
    elif type == "auto":
        detect = _is_bimodal(valid_paces, fast, slow)
    else:
        detect = True

    segs = continuous_segments(t)
    blocks: list[tuple[int, int]] = []
    if detect:
        for i, j in segs:
            blocks += [(i + a, i + b) for a, b in effort_blocks(t[i : j + 1], pace_smooth[i : j + 1], threshold)]
        blocks.sort()
        if type == "auto" and blocks and slow - fast < _STRONG_PACE_GAP_MIN_KM:
            contrast = _hr_contrast(hr, blocks)
            if contrast is not None and contrast < _MIN_BLOCK_HR_CONTRAST_BPM:
                blocks = []

    block_list = []
    for n, (i, j) in enumerate(blocks, 1):
        s = {"n": n, **block_stats(t, hr, pa, el, i, j, di if has_distance else None)}
        if allure_cible and s["allure"]:
            ecart = (s["allure"] - allure_cible) * 60
            s["ecart_cible_s_km"] = round(ecart)
            s["verdict"] = _verdict(ecart)
        block_list.append(s)

    # Découplage cardiaque : uniquement sur un effort continu >= 40 min — le plus long
    # segment sans pause si aucun bloc n'est détecté, sinon le plus long bloc (un segment
    # mêlant efforts et récups n'a pas de découplage interprétable).
    decoupling = None
    candidates = blocks if blocks else segs
    longest = max(candidates, key=lambda s: t[s[1]] - t[s[0]])
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
        # Temps écoulé (pauses comprises) vs temps en mouvement : distance et allure moyenne
        # de `vue_ensemble` sont calculées sur le temps en mouvement.
        "elapsed_s": elapsed,
        "moving_s": moving,
        "pauses": pauses(t),
        "vue_ensemble": {
            "duree_s": moving,
            "duree_mouvement_s": moving,
            "duree_ecoulee_s": elapsed,
            "pauses": pauses(t),
            "source_distance": "garmin" if has_distance else "integration_allure",
            "seance_continue": not blocks,
            "distance_km": round(dist, 2),
            "allure_moy": _r(avg_pace, 2),
            "allure_moy_mmss": mmss(avg_pace),
            "pace_min_per_km": _r(avg_pace, 2),
            "pace_mmss": mmss(avg_pace),
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
