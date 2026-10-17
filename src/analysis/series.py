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
# Équivalents vélo, en km/h (un écart de 20 s/km ne veut rien dire à 25 km/h) : centres
# effort / récup écartés d'au moins 5 km/h, écart « franc » (sans contrôle FC) dès 10 km/h.
_MIN_CLUSTER_GAP_KMH = 5.0
_STRONG_GAP_KMH = 10.0
# Vélo : pauses absorbées par défaut pour le découplage (feux, carrefours, ravitaillement
# court). La course garde son comportement historique (aucune fusion) sauf demande explicite.
DEFAULT_PAUSE_MERGE_VELO_S = 120
# Mode auto (pause_merge_s non fourni, vélo) : si la fenêtre couvre moins de 50 % du temps en
# mouvement d'une sortie de plus de 60 min, élargissement par paliers jusqu'à 600 s.
_VELO_AUTO_MERGE_STEPS_S = (300, 600)
_VELO_WIDEN_MIN_MOVING_S = 3600
# Fiabilité du découplage vélo (sans puissance ni correction de pente) :
# - au-delà de ±10 %, un découplage FC/vitesse traduit des conditions changeantes (relief,
#   vent, abri d'un groupe) plutôt qu'une dérive physiologique ;
# - une vitesse moyenne qui varie de plus de 15 % entre les moitiés rend le rapport FC/vitesse
#   non comparable (la FC n'est pas proportionnelle à la vitesse) ;
# - profil asymétrique : D+ d'une moitié > 2 × celui de l'autre ET écart >= 50 m (le minimum
#   absolu évite les faux positifs sur terrain plat, où 10 m contre 25 m ne change rien) ;
# - couverture < 50 % : la fenêtre ignore l'essentiel de la sortie.
_VELO_MAX_ABS_DECOUPLING_PCT = 10.0
_VELO_MAX_SPEED_GAP_PCT = 15.0
_VELO_DPLUS_RATIO = 2.0
_VELO_MIN_DPLUS_GAP_M = 50
_VELO_MIN_COVERAGE_PCT = 50.0
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


def _speed(pace: float | None) -> float | None:
    """Allure min/km -> vitesse km/h."""
    return 60 / pace if pace else None


def _bimodal_velo(paces: list[float], fast: float, slow: float) -> bool:
    """Vélo : deux régimes réels si les centres sont écartés d'au moins 5 km/h (le critère
    course en s/km n'a pas de sens à 25 km/h) et chaque groupe >= 10 % des points."""
    if _speed(fast) - _speed(slow) < _MIN_CLUSTER_GAP_KMH:
        return False
    threshold = (fast + slow) / 2
    share_fast = sum(1 for p in paces if p <= threshold) / len(paces)
    return _MIN_CLUSTER_SHARE <= share_fast <= 1 - _MIN_CLUSTER_SHARE


def _detect_blocks(
    t: list[int], hr: list, pace_smooth: list, valid_paces: list[float], fast: float, slow: float,
    segs: list[tuple[int, int]], type: str, velo: bool,
) -> tuple[list[tuple[int, int]], dict[str, Any]]:
    """Blocs d'effort + description de ce que `type` a réellement fait (`type_applique`)."""
    threshold = (fast + slow) / 2
    info: dict[str, Any] = {"demande": type}
    if type == "endurance":
        info["detection_blocs"] = "desactivee"
        info["effet"] = "aucun bloc recherché : séance traitée comme un effort continu"
        return [], info
    if type == "auto":
        bimodal = _bimodal_velo(valid_paces, fast, slow) if velo else _is_bimodal(valid_paces, fast, slow)
        info["detection_blocs"] = "automatique"
        info["allure_bimodale"] = bimodal
        if not bimodal:
            info["effet"] = (
                "aucun bloc : vitesse/allure trop régulière pour contenir des efforts distincts "
                "(résultat identique à type=\"endurance\")"
            )
            return [], info
    else:
        info["detection_blocs"] = "forcee"
    blocks: list[tuple[int, int]] = []
    for i, j in segs:
        blocks += [(i + a, i + b) for a, b in effort_blocks(t[i : j + 1], pace_smooth[i : j + 1], threshold)]
    blocks.sort()
    strong_gap = (
        _speed(fast) - _speed(slow) >= _STRONG_GAP_KMH if velo else slow - fast >= _STRONG_PACE_GAP_MIN_KM
    )
    if type == "auto" and blocks and not strong_gap:
        contrast = _hr_contrast(hr, blocks)
        info["contraste_fc_bpm"] = _r(contrast, 1)
        if contrast is not None and contrast < _MIN_BLOCK_HR_CONTRAST_BPM:
            info["effet"] = "blocs candidats écartés : FC à peine plus haute que le reste (arrêts, relief)"
            return [], info
    info["effet"] = f"{len(blocks)} bloc(s) d'effort retenu(s)"
    return blocks, info


def merged_segments(
    t: list[int], segs: list[tuple[int, int]], pause_merge_s: float
) -> tuple[list[tuple[int, int]], list[list[dict[str, int]]]]:
    """Fusionne les segments séparés par une pause <= `pause_merge_s`. Renvoie les segments
    fusionnés et, pour chacun, la liste des pauses absorbées ({debut_s, duree_s})."""
    if not segs:
        return [], []
    merged = [list(segs[0])]
    absorbed: list[list[dict[str, int]]] = [[]]
    for i, j in segs[1:]:
        gap = t[i] - t[merged[-1][1]]
        if gap <= pause_merge_s:
            absorbed[-1].append({"debut_s": t[merged[-1][1]], "duree_s": gap})
            merged[-1][1] = j
        else:
            merged.append([i, j])
            absorbed.append([])
    return [tuple(m) for m in merged], absorbed


def _decoupling_moving_halves(t: list[int], hr: list, pa: list, i: int, j: int, velo: bool) -> float | None:
    """Découplage sur un segment pouvant contenir des pauses fusionnées : moitiés égales en
    TEMPS EN MOUVEMENT (une pause ne compte dans aucune moitié). Vélo : rapport FC / vitesse
    (km/h) sans correction de pente ; course : FC × allure, comme cardiac_decoupling."""
    total = moving_time(t, i, j)
    acc, k = 0, j
    for x in range(i, j):
        step = t[x + 1] - t[x]
        acc += step if step <= PAUSE_GAP_S else 0
        if acc >= total / 2:
            k = x + 1
            break

    def ratio(a: int, b: int) -> float | None:
        h = [x for x in hr[a : b + 1] if x]
        p = [x for x in pa[a : b + 1] if x and x < _MAX_PACE_SEGMENTATION]
        if not h or not p:
            return None
        if velo:
            return st.mean(h) / st.mean(60 / x for x in p)
        return st.mean(h) * st.mean(p)

    r1, r2 = ratio(i, k), ratio(k, j)
    return None if not (r1 and r2) else (r2 - r1) / r1 * 100


def _decoupling(
    t: list[int], hr: list, pa: list, blocks: list[tuple[int, int]], segs: list[tuple[int, int]],
    pause_merge_s: float | None, velo: bool,
) -> dict[str, Any] | None:
    """Découplage cardiaque sur le plus long effort continu >= 40 min. Sans `pause_merge_s`
    (comportement historique course) : plus long segment sans pause ou plus long bloc. Avec :
    segments fusionnés à travers les pauses courtes, longueur jugée en temps en mouvement."""
    if pause_merge_s is None:
        candidates = blocks if blocks else segs
        longest = max(candidates, key=lambda s: t[s[1]] - t[s[0]])
        if t[longest[1]] - t[longest[0]] < _MIN_DECOUPLING_S:
            return None
        d = cardiac_decoupling(t, hr, pa, *longest)
        if d is None:
            return None
        return {
            "debut_s": t[longest[0]],
            "duree_s": t[longest[1]] - t[longest[0]],
            "decouplage_pct": round(d, 1),
            "lecture": _decoupling_reading(d),
        }

    if blocks:
        candidates, absorbed = blocks, [[] for _ in blocks]
    else:
        candidates, absorbed = merged_segments(t, segs, pause_merge_s)
    best = max(range(len(candidates)), key=lambda n: moving_time(t, *candidates[n]))
    i, j = candidates[best]
    moving = moving_time(t, i, j)
    if moving < _MIN_DECOUPLING_S:
        return None
    d = _decoupling_moving_halves(t, hr, pa, i, j, velo)
    if d is None:
        return None
    return {
        "debut_s": t[i],
        "fin_s": t[j],
        "duree_s": t[j] - t[i],
        "duree_mouvement_s": moving,
        "pause_merge_s": pause_merge_s,
        "pauses_fusionnees": absorbed[best],
        "methode": (
            "FC / vitesse (km/h), 1re vs 2e moitié du temps en mouvement, sans correction de pente"
            if velo
            else "FC × allure, 1re vs 2e moitié du temps en mouvement"
        ),
        "decouplage_pct": round(d, 1),
        "lecture": _signed_decoupling_reading(d),
    }


def _moving_midpoint(t: list[int], i: int, j: int) -> int:
    """Indice qui partage [i, j] en deux moitiés égales de TEMPS EN MOUVEMENT."""
    total = moving_time(t, i, j)
    acc = 0
    for x in range(i, j):
        step = t[x + 1] - t[x]
        acc += step if step <= PAUSE_GAP_S else 0
        if acc >= total / 2:
            return x + 1
    return j


def _half_metrics(t: list[int], pa: list, el: list, a: int, b: int) -> dict[str, Any]:
    """Vitesse moyenne (km/h), D+ cumulé et altitude moyenne d'une moitié de fenêtre."""
    speeds = [60 / p for p in pa[a : b + 1] if p and p < _MAX_PACE_SEGMENTATION]
    els = [e for e in el[a : b + 1] if e is not None]
    dplus = sum(max(0.0, els[k + 1] - els[k]) for k in range(len(els) - 1)) if len(els) > 1 else None
    return {
        "duree_mouvement_s": moving_time(t, a, b),
        "vitesse_moy_kmh": round(st.mean(speeds), 1) if speeds else None,
        "dplus_m": round(dplus) if dplus is not None else None,
        "altitude_moy_m": round(st.mean(els)) if els else None,
    }


def _reliability_reasons(d: float, first: dict[str, Any], second: dict[str, Any], coverage_pct: float) -> list[str]:
    """Règles de fiabilité du découplage vélo (sans capteur de puissance ni correction de
    pente) ; liste vide = fiable."""
    reasons = []
    if abs(d) > _VELO_MAX_ABS_DECOUPLING_PCT:
        reasons.append(
            f"découplage de {d:+.1f} % hors de la plage plausible (±{_VELO_MAX_ABS_DECOUPLING_PCT:.0f} %)"
        )
    v1, v2 = first["vitesse_moy_kmh"], second["vitesse_moy_kmh"]
    if v1 and v2 and abs(v2 - v1) / v1 * 100 > _VELO_MAX_SPEED_GAP_PCT:
        reasons.append(
            f"vitesse moyenne très différente entre les moitiés ({v1:.1f} puis {v2:.1f} km/h, "
            f"écart > {_VELO_MAX_SPEED_GAP_PCT:.0f} %) : conditions (relief, vent, groupe) non comparables"
        )
    d1, d2 = first["dplus_m"], second["dplus_m"]
    if d1 is not None and d2 is not None:
        low, high = min(d1, d2), max(d1, d2)
        if high - low >= _VELO_MIN_DPLUS_GAP_M and high > _VELO_DPLUS_RATIO * low:
            reasons.append(
                f"profil asymétrique (D+ {d1} m puis {d2} m) : sans correction de pente, la FC par "
                "km/h n'est pas comparable entre les moitiés"
            )
    if coverage_pct < _VELO_MIN_COVERAGE_PCT:
        reasons.append(
            f"fenêtre couvrant {coverage_pct:.0f} % du temps en mouvement seulement "
            f"(< {_VELO_MIN_COVERAGE_PCT:.0f} %) : l'essentiel de la sortie est ignoré"
        )
    return reasons


def _decoupling_velo(
    t: list[int], hr: list, pa: list, el: list, blocks: list[tuple[int, int]], segs: list[tuple[int, int]],
    pause_merge_s: float | None,
) -> dict[str, Any] | None:
    """Découplage cardiaque vélo : FC / vitesse, 1re vs 2e moitié du temps en mouvement du plus
    long effort continu (>= 40 min), sans correction de pente, avec indicateurs de fiabilité.

    `pause_merge_s` None = mode auto : fusion des pauses <= 120 s, puis élargissement par
    paliers (300 s, 600 s) tant que la fenêtre couvre moins de 50 % du temps en mouvement
    d'une sortie de plus de 60 min. Valeur explicite = appliquée telle quelle."""
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
        return None
    d = _decoupling_moving_halves(t, hr, pa, i, j, True)
    if d is None:
        return None

    k = _moving_midpoint(t, i, j)
    first, second = _half_metrics(t, pa, el, i, k), _half_metrics(t, pa, el, k, j)
    coverage = moving / total_moving * 100 if total_moving else 0.0
    reasons = _reliability_reasons(d, first, second, coverage)
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
        "moities": {"premiere": first, "seconde": second},
        "methode": "FC / vitesse (km/h), 1re vs 2e moitié du temps en mouvement, sans correction de pente",
        "decouplage_pct": round(d, 1),
        "fiable": not reasons,
        "raisons_fiabilite": reasons,
        "lecture": _signed_decoupling_reading(d) if not reasons else "non interprétable",
    }


def _signed_decoupling_reading(d: float) -> str:
    """Lecture d'un découplage calculé à travers les pauses (vélo ou `pause_merge_s`) : comme
    la lecture historique, mais une valeur nettement négative (2e moitié plus « économique »)
    n'est pas « stable » : sans correction de pente, c'est le signe d'un relief ou d'un vent
    différents entre les deux moitiés, pas d'une forme exceptionnelle."""
    if d <= -5:
        return "negatif marque (relief ou vent probablement differents entre les moities)"
    return _decoupling_reading(d)


def _velo_block(s: dict[str, Any], vitesse_cible_kmh: float | None) -> dict[str, Any]:
    """Bloc vélo : vitesse en km/h à la place des allures (et sans allure « à plat » : la
    correction de pente de la course ne s'applique pas au vélo)."""
    speed = _speed(s["allure"])
    out = {
        "n": s["n"],
        "debut_s": s["debut_s"],
        "duree_s": s["duree_s"],
        "dist_km": s["dist_km"],
        "vitesse_kmh": _r(speed, 1),
        "fc_moy": s["fc_moy"],
        "fc_max": s["fc_max"],
        "dplus_m": s["dplus_m"],
        "pente_pct": s["pente_pct"],
    }
    if vitesse_cible_kmh and speed:
        ecart = speed - vitesse_cible_kmh
        out["ecart_cible_kmh"] = round(ecart, 1)
        out["verdict"] = "conforme" if abs(ecart) <= 1 else "derive" if abs(ecart) <= 2.5 else "hors cible"
    return out


def analyse(
    details: dict[str, Any],
    fc_seuil: float,
    allure_cible: float | None = None,
    type: str = "auto",
    *,
    sport: str | None = None,
    summary: dict[str, Any] | None = None,
    pause_merge_s: float | None = None,
    vitesse_cible_kmh: float | None = None,
) -> dict[str, Any]:
    """Analyse complète d'une activité ; renvoie un dict JSON compact.

    `sport` ("course" | "velo" | ...) choisit l'expression des résultats : km/h, distance
    officielle et découplage à travers les pauses courtes pour le vélo ; comportement
    historique (min/km) sinon. `summary` = résumé officiel Garmin de l'activité
    (`GarminClient.get_activity_summary`), optionnel. `status` vaut "no_data" si la série
    est trop courte / sans allure (ex. natation en piscine — utiliser garmin_analyse_swim).
    """
    velo = sport == "velo"
    summary = summary or {}
    t, hr, pa, el, di = load_series(details)
    has_distance = any(d is not None for d in di)
    pace_smooth = smooth(pa, 5)
    valid_paces = [p for p in pace_smooth if p and p < _MAX_PACE_SEGMENTATION]
    fast, slow = kmeans1d(valid_paces)
    if fast is None:
        return {"status": "no_data", "sport": sport, "message": "Série trop courte ou sans allure pour être segmentée."}
    threshold = (fast + slow) / 2

    elapsed = t[-1] - t[0]
    moving = moving_time(t)
    dist = _distance_km(t, pa, 0, len(t) - 1, di if has_distance else None)
    hrs = [h for h in hr if h]
    avg_pace = moving / 60 / dist if dist else None

    segs = continuous_segments(t)
    blocks, type_info = _detect_blocks(t, hr, pace_smooth, valid_paces, fast, slow, segs, type, velo)

    block_list = []
    for n, (i, j) in enumerate(blocks, 1):
        s = {"n": n, **block_stats(t, hr, pa, el, i, j, di if has_distance else None)}
        if velo:
            block_list.append(_velo_block(s, vitesse_cible_kmh))
            continue
        if allure_cible and s["allure"]:
            ecart = (s["allure"] - allure_cible) * 60
            s["ecart_cible_s_km"] = round(ecart)
            s["verdict"] = _verdict(ecart)
        block_list.append(s)

    if velo:
        decoupling = _decoupling_velo(t, hr, pa, el, blocks, segs, pause_merge_s)
    else:
        decoupling = _decoupling(t, hr, pa, blocks, segs, pause_merge_s, velo)
    official_km = summary.get("distance_km")
    common_fc = {
        "fc_moy": _r(st.mean(hrs), 0) if hrs else None,
        "fc_max": max(hrs) if hrs else None,
        "fc_max_pct_seuil": round(max(hrs) / fc_seuil * 100) if hrs else None,
    }

    if velo:
        chrono_s = summary.get("duree_s") or moving
        distance = official_km if official_km is not None else dist
        vue = {
            "duree_s": moving,
            "duree_mouvement_s": moving,
            "duree_ecoulee_s": elapsed,
            "duree_chrono_garmin_s": summary.get("duree_s"),
            "pauses": pauses(t),
            "source_distance": "garmin_officielle" if official_km is not None else (
                "garmin" if has_distance else "integration_allure"
            ),
            "seance_continue": not blocks,
            "distance_km": round(distance, 2),
            "distance_officielle_km": official_km,
            "distance_integree_km": round(dist, 2),
            "vitesse_moy_kmh": round(distance / chrono_s * 3600, 1) if chrono_s else None,
            "vitesse_max_kmh": summary.get("vitesse_max_kmh"),
            **common_fc,
            "bascule_effort_recup_kmh": _r(_speed(threshold), 1),
            "centre_rapide_kmh": _r(_speed(fast), 1),
            "centre_lent_kmh": _r(_speed(slow), 1),
            "nb_segments": len(segs),
        }
        parametres = {
            "fc_seuil": fc_seuil,
            "vitesse_cible_kmh": vitesse_cible_kmh,
            "type": type,
            # Valeur réellement appliquée au découplage (mode auto : 120 s, élargi si besoin).
            "pause_merge_s": decoupling["pause_merge_s"] if decoupling else (
                pause_merge_s if pause_merge_s is not None else DEFAULT_PAUSE_MERGE_VELO_S
            ),
            "pause_merge_mode": "explicite" if pause_merge_s is not None else "auto",
        }
    else:
        vue = {
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
            **common_fc,
            "bascule_effort_recup": _r(threshold, 2),
            "bascule_effort_recup_mmss": mmss(threshold),
            "centre_rapide_mmss": mmss(fast),
            "centre_lent_mmss": mmss(slow),
            "nb_segments": len(segs),
        }
        if official_km is not None:
            vue["distance_officielle_km"] = official_km
        parametres = {"fc_seuil": fc_seuil, "allure_cible": allure_cible, "type": type}
        if pause_merge_s is not None:
            parametres["pause_merge_s"] = pause_merge_s

    return {
        "status": "ok",
        "sport": sport,
        "parametres": parametres,
        "fc_seuil_utilisee": fc_seuil,
        "type_applique": type_info,
        # Temps écoulé (pauses comprises) vs temps en mouvement : distance et allure moyenne
        # de `vue_ensemble` sont calculées sur le temps en mouvement.
        "elapsed_s": elapsed,
        "moving_s": moving,
        "pauses": pauses(t),
        "vue_ensemble": vue,
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
