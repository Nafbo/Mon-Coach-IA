"""Natation en piscine : regroupement des longueurs en blocs et repérage des longueurs
aberrantes (fonctions pures, entrée = sortie de `GarminClient.get_swim_splits`).

Une longueur "suspecte" est généralement une erreur de détection de virage par la montre
(longueur coupée en deux, virage manqué) : elle est signalée, jamais supprimée.
"""

from __future__ import annotations

import statistics as st
from typing import Any

# Pause entre la fin d'une longueur et le début de la suivante au-delà de laquelle on
# considère un repos (= nouveau bloc). Un virage normal ne laisse pas de trou.
REST_GAP_S = 10
# Écart à la médiane de référence au-delà duquel une longueur est suspecte : 20 % plus
# rapide (ex. 18 s quand le bloc nage à 27 s/25 m) ou 40 % plus lente.
_FAST_RATIO = 0.80
_SLOW_RATIO = 1.40
# Nombre minimal de longueurs pour qu'une médiane de bloc serve de référence.
_MIN_REFERENCE = 3


def _mmss(decimal_minutes: float | None) -> str | None:
    if decimal_minutes is None:
        return None
    m, s = divmod(round(decimal_minutes * 60), 60)
    return f"{m}:{s:02d}"


def _end_s(length: dict[str, Any]) -> float | None:
    start, duration = length.get("start_s"), length.get("duration_s")
    return start + duration if isinstance(start, (int, float)) and isinstance(duration, (int, float)) else None


def group_blocks(splits: list[dict[str, Any]], rest_gap_s: float = REST_GAP_S) -> list[list[int]]:
    """Indices des longueurs, groupés en blocs séparés par un repos > `rest_gap_s`."""
    blocks: list[list[int]] = []
    for i, length in enumerate(splits):
        prev_end = _end_s(splits[i - 1]) if i else None
        start = length.get("start_s")
        new_block = not blocks or (
            prev_end is not None and isinstance(start, (int, float)) and start - prev_end > rest_gap_s
        )
        if new_block:
            blocks.append([i])
        else:
            blocks[-1].append(i)
    return blocks


def _reference(splits: list[dict[str, Any]], block: list[int], i: int) -> float | None:
    """Durée de référence d'une longueur : médiane des longueurs de même nage du bloc,
    sinon du bloc entier, sinon de toute la séance pour cette nage."""
    stroke = splits[i].get("stroke")
    pools = (
        [splits[k]["duration_s"] for k in block if splits[k].get("stroke") == stroke],
        [splits[k]["duration_s"] for k in block],
        [s["duration_s"] for s in splits if s.get("stroke") == stroke],
    )
    for pool in pools:
        values = [v for v in pool if isinstance(v, (int, float))]
        if len(values) >= _MIN_REFERENCE:
            return st.median(values)
    return None


def annotate(splits: list[dict[str, Any]], rest_gap_s: float = REST_GAP_S) -> dict[str, Any]:
    """Renvoie {"splits": [... + suspect, suspect_reason, block, pace_mmss],
    "blocks": [... + role, repos_avant_s, repos_apres_s], "totaux": {temps de nage, temps de
    repos, temps total, distance}}."""
    blocks = group_blocks(splits, rest_gap_s)
    out_splits = [dict(s) for s in splits]

    for n, block in enumerate(blocks, 1):
        for i in block:
            length = out_splits[i]
            length["block"] = n
            ref = _reference(splits, block, i)
            duration = length.get("duration_s")
            reason = None
            if ref and isinstance(duration, (int, float)):
                ratio = duration / ref
                if ratio < _FAST_RATIO:
                    reason = "too_fast"
                elif ratio > _SLOW_RATIO:
                    reason = "too_slow"
                length["ecart_reference_pct"] = round((ratio - 1) * 100)
            length["suspect"] = reason is not None
            length["suspect_reason"] = reason

    block_list = []
    for n, block in enumerate(blocks, 1):
        lengths = [out_splits[i] for i in block]
        distance = sum(x["distance_m"] for x in lengths if isinstance(x.get("distance_m"), (int, float)))
        duration = sum(x["duration_s"] for x in lengths if isinstance(x.get("duration_s"), (int, float)))
        hrs = [x["avg_hr"] for x in lengths if x.get("avg_hr")]
        swolfs = [x["swolf"] for x in lengths if x.get("swolf")]
        strokes: dict[str, int] = {}
        for x in lengths:
            strokes[x.get("stroke") or "unknown"] = strokes.get(x.get("stroke") or "unknown", 0) + 1
        pace = duration / distance * 100 / 60 if distance else None
        next_start = out_splits[blocks[n][0]].get("start_s") if n < len(blocks) else None
        end = _end_s(lengths[-1])
        prev_end = _end_s(out_splits[blocks[n - 2][-1]]) if n > 1 else None
        halves = _half_paces(lengths)
        start = lengths[0].get("start_s")
        block_list.append(
            {
                "n": n,
                "premiere_longueur": lengths[0].get("length_idx"),
                "derniere_longueur": lengths[-1].get("length_idx"),
                "debut_s": lengths[0].get("start_s"),
                "nb_longueurs": len(lengths),
                "distance_m": round(distance),
                "duree_nage_s": round(duration, 1),
                "allure_min_100m": round(pace, 2) if pace else None,
                "allure_mmss": _mmss(pace),
                "pace_min_per_100m": round(pace, 2) if pace else None,
                "pace_mmss": _mmss(pace),
                "role": _role(n, len(blocks)),
                "fc_moy": round(st.mean(hrs)) if hrs else None,
                "swolf_moy": round(st.mean(swolfs), 1) if swolfs else None,
                "nages": strokes,
                "nb_suspectes": sum(1 for x in lengths if x["suspect"]),
                "repos_apres_s": round(next_start - end, 1) if next_start is not None and end is not None else None,
                "repos_avant_s": (
                    round(start - prev_end, 1) if start is not None and prev_end is not None else None
                ),
                # Noms partagés avec garmin_analyse_swim (temps nagé hors repos).
                "swim_time_s": round(duration, 1),
                "rest_after_s": (
                    round(next_start - end, 1) if next_start is not None and end is not None else None
                ),
                "first_half_pace_mmss": halves["first_half_pace_mmss"],
                "second_half_pace_mmss": halves["second_half_pace_mmss"],
            }
        )
    for length in out_splits:
        length["pace_mmss"] = _mmss(length.get("pace_min_per_100m"))
    swim_s = sum(x["duration_s"] for x in out_splits if isinstance(x.get("duration_s"), (int, float)))
    rest_s = sum(b["repos_apres_s"] for b in block_list if b["repos_apres_s"] is not None)
    totals = {
        "temps_nage_s": round(swim_s, 1),
        "temps_repos_s": round(rest_s, 1),
        "temps_total_s": round(swim_s + rest_s, 1),
        "distance_m": round(sum(b["distance_m"] for b in block_list)),
    }
    return {"splits": out_splits, "blocks": block_list, "totaux": totals}


def _role(n: int, total: int) -> str:
    """Rôle d'un bloc dans la séance : premier = échauffement, dernier = retour au calme,
    les autres = séries (avec 1 ou 2 blocs : tout est "serie")."""
    if total >= 3 and n == 1:
        return "echauffement"
    if total >= 3 and n == total:
        return "retour_au_calme"
    return "serie"


# ---------------------------------------------------------------------------------------
# Analyse complète d'une séance (tool `garmin_analyse_swim`) : agrégats uniquement.
# Règles : allures toujours calculées sur le temps nagé (somme des longueurs), jamais sur
# le temps écoulé ; la FC (capteur optique peu fiable dans l'eau) est informative et
# n'entre dans aucune décision.
# ---------------------------------------------------------------------------------------

# Longueurs minimales d'un bloc pour la dérive 1re/2e moitié et le test des aberrantes.
_MIN_LENGTHS_ANALYSIS = 4
# Seuil d'aberrance : max(3 × 1,4826 × MAD, 30 % de la médiane du bloc).
_MAD_K = 3 * 1.4826
_MIN_RELATIVE_DEVIATION = 0.30
# Contrôle CSS : si le chrono court n'est pas plus rapide d'au moins 3 s que le début du
# chrono long sur la même distance, il n'a pas été nagé à fond -> CSS probablement optimiste.
_CONTROL_MARGIN_S = -3.0
_CSS_SENSITIVITY_S = 5.0
_RACE_DISTANCE_M = 1900


def _sec_mmss(seconds: float | None) -> str | None:
    """Secondes (ex. 102.4 s/100 m) -> "1:42"."""
    if seconds is None:
        return None
    m, s = divmod(round(seconds), 60)
    return f"{m}:{s:02d}"


def _pace(swim_s: float, distance_m: float) -> tuple[float | None, str | None]:
    """Allure en minutes décimales /100 m et "M:SS", sur le temps nagé."""
    if not distance_m:
        return None, None
    per_100 = swim_s / distance_m * 100
    return round(per_100 / 60, 2), _sec_mmss(per_100)


def _num(values: list[Any]) -> list[float]:
    return [v for v in values if isinstance(v, (int, float))]


def _half_paces(lengths: list[dict[str, Any]]) -> dict[str, Any]:
    """Allures 1re / 2e moitié du bloc (temps nagé) et dérive en %, null sous 4 longueurs."""
    if len(lengths) < _MIN_LENGTHS_ANALYSIS:
        return {"first_half_pace_mmss": None, "second_half_pace_mmss": None, "drift_pct": None}
    mid = len(lengths) // 2
    paces = []
    for half in (lengths[:mid], lengths[mid:]):
        swim = sum(_num([x.get("duration_s") for x in half]))
        dist = sum(_num([x.get("distance_m") for x in half]))
        paces.append(swim / dist * 100 if dist else None)
    p1, p2 = paces
    return {
        "first_half_pace_mmss": _sec_mmss(p1),
        "second_half_pace_mmss": _sec_mmss(p2),
        "drift_pct": round((p2 - p1) / p1 * 100, 1) if p1 and p2 is not None else None,
    }


def _suspects_mad(lengths: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Longueurs dont la durée s'écarte de la médiane du bloc de plus de
    max(3 × 1,4826 × MAD, 30 % de la médiane). Bloc de moins de 4 longueurs : non testé."""
    durations = _num([x.get("duration_s") for x in lengths])
    if len(lengths) < _MIN_LENGTHS_ANALYSIS or len(durations) < _MIN_LENGTHS_ANALYSIS:
        return []
    median = st.median(durations)
    mad = st.median([abs(d - median) for d in durations])
    threshold = max(_MAD_K * mad, _MIN_RELATIVE_DEVIATION * median)
    out = []
    for x in lengths:
        d = x.get("duration_s")
        if isinstance(d, (int, float)) and abs(d - median) > threshold:
            out.append(
                {
                    "length_idx": x.get("length_idx"),
                    "duration_s": d,
                    "block_median_s": round(median, 1),
                    "reason": "too_fast" if d < median else "too_slow",
                }
            )
    return out


def _css(
    blocks: list[dict[str, Any]],
    raw_blocks: list[list[dict[str, Any]]],
    css_blocks: list[int] | None,
    target_min_per_100m: float | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Vitesse critique à partir de deux chronos (long puis court) : (css, raison si null)."""
    if css_blocks is not None:
        valid = len(css_blocks) == 2 and all(isinstance(n, int) and 1 <= n <= len(blocks) for n in css_blocks)
        if not valid:
            return None, f"css_blocks invalide : {css_blocks} (attendu [n° bloc long, n° bloc court], de 1 à {len(blocks)})"
        i_long, i_short = css_blocks[0] - 1, css_blocks[1] - 1
    else:
        i_long = next((i for i, b in enumerate(blocks) if b["distance_m"] == 400), None)
        if i_long is None:
            return None, "aucun bloc de 400 m dans la séance"
        i_short = next((i for i, b in enumerate(blocks) if i > i_long and b["distance_m"] == 200), None)
        if i_short is None:
            return None, "aucun bloc de 200 m après le bloc de 400 m"
    long_b, short_b = blocks[i_long], blocks[i_short]
    d_long, d_short = long_b["distance_m"], short_b["distance_m"]
    if d_long <= d_short:
        return None, f"le bloc long ({d_long} m) doit être plus long que le bloc court ({d_short} m)"
    t_long, t_short = long_b["swim_time_s"], short_b["swim_time_s"]
    css_s = (t_long - t_short) / (d_long - d_short) * 100
    sensitivity = (t_long - (t_short - _CSS_SENSITIVITY_S)) / (d_long - d_short) * 100

    # Contrôle : chrono court comparé au début du chrono long sur la même distance.
    covered, first_part = 0.0, 0.0
    for x in raw_blocks[i_long]:
        if covered >= d_short:
            break
        covered += x.get("distance_m") or 0
        first_part += x.get("duration_s") or 0
    control = None
    if covered == d_short:
        diff = round(t_short - first_part, 1)
        control = {
            "first_half_of_long_s": round(first_part, 1),
            "short_vs_first_half_s": diff,
            "short_not_faster": diff > _CONTROL_MARGIN_S,
        }

    css = {
        "long_block": i_long + 1,
        "short_block": i_short + 1,
        "d_long_m": d_long,
        "d_short_m": d_short,
        "t_long_s": round(t_long, 1),
        "t_short_s": round(t_short, 1),
        "t_long_mmss": _sec_mmss(t_long),
        "t_short_mmss": _sec_mmss(t_short),
        "css_s_per_100m": round(css_s, 1),
        "css_min_per_100m": round(css_s / 60, 3),
        "css_mmss": _sec_mmss(css_s),
        "sensitivity_short_minus_5s_mmss": _sec_mmss(sensitivity),
        "control": control,
    }
    if target_min_per_100m:
        gap = css_s - target_min_per_100m * 60
        css["target_mmss"] = _sec_mmss(target_min_per_100m * 60)
        css["gap_s_per_100m"] = round(gap, 1)
        css["gap_s_over_1900m"] = round(gap * _RACE_DISTANCE_M / 100)
    return css, None


def analyse_swim(
    splits: list[dict[str, Any]],
    css_blocks: list[int] | None = None,
    target_css_min_per_100m: float | None = None,
    rest_gap_s: float = REST_GAP_S,
) -> dict[str, Any]:
    """Analyse d'une séance de natation en piscine : `overview`, `blocks`,
    `suspect_lengths`, `css` (+ `css_null_reason`). Aucune liste de longueurs en sortie."""
    if not splits:
        return {"status": "no_data", "message": "Aucune longueur exploitable (eau libre ou activité non nage)."}

    raw_blocks = [[splits[i] for i in idx] for idx in group_blocks(splits, rest_gap_s)]
    blocks = []
    suspects: list[dict[str, Any]] = []
    for n, lengths in enumerate(raw_blocks, 1):
        swim_s = sum(_num([x.get("duration_s") for x in lengths]))
        dist = sum(_num([x.get("distance_m") for x in lengths]))
        pace, pace_mmss = _pace(swim_s, dist)
        hrs = _num([x.get("avg_hr") for x in lengths])
        swolfs = _num([x.get("swolf") for x in lengths])
        end = _end_s(lengths[-1])
        next_start = raw_blocks[n][0].get("start_s") if n < len(raw_blocks) else None
        block_suspects = _suspects_mad(lengths)
        suspects += block_suspects
        blocks.append(
            {
                "n": n,
                "first_length": lengths[0].get("length_idx"),
                "last_length": lengths[-1].get("length_idx"),
                "nb_lengths": len(lengths),
                "distance_m": round(dist),
                "swim_time_s": round(swim_s, 1),
                "swim_time_mmss": _sec_mmss(swim_s),
                "pace_min_per_100m": pace,
                "pace_mmss": pace_mmss,
                **_half_paces(lengths),
                "hr_avg": round(st.mean(hrs)) if hrs else None,
                "hr_max": max(hrs) if hrs else None,
                "swolf_avg": round(st.mean(swolfs), 1) if swolfs else None,
                "rest_after_s": round(next_start - end, 1) if next_start is not None and end is not None else None,
                "suspect_count": len(block_suspects),
            }
        )

    swim_total = sum(b["swim_time_s"] for b in blocks)
    dist_total = sum(b["distance_m"] for b in blocks)
    first_start = splits[0].get("start_s")
    last_end = _end_s(splits[-1])
    elapsed = last_end - first_start if first_start is not None and last_end is not None else None
    pace, pace_mmss = _pace(swim_total, dist_total)
    css, reason = _css(blocks, raw_blocks, css_blocks, target_css_min_per_100m)
    return {
        "status": "ok",
        "overview": {
            "distance_m": dist_total,
            "nb_longueurs": len(splits),
            "swim_time_s": round(swim_total, 1),
            "elapsed_s": round(elapsed, 1) if elapsed is not None else None,
            "rest_s": round(elapsed - swim_total, 1) if elapsed is not None else None,
            "pace_min_per_100m": pace,
            "pace_mmss": pace_mmss,
            "nb_blocks": len(blocks),
        },
        "blocks": blocks,
        "suspect_lengths": suspects,
        "css": css,
        "css_null_reason": reason,
    }
