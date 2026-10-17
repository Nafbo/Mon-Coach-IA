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
