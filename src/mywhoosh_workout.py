"""Construction d'une séance home trainer au format JSON du Workout Builder MyWhoosh.

Format relevé dans le code du Workout Builder (workout.mywhoosh.com, fonction d'export JSON) :
une liste `WorkoutStepsArray` d'étapes, puissance en fraction de FTP (0.75 = 75 %), durées en
secondes, messages affichés pendant l'étape. Les répétitions sont aplaties en étapes
alternées partageant un même `IntervalId`.
"""

from __future__ import annotations

import math
import time
from typing import Any

SPORT_BIKE = 0  # SportsModeType attendu par l'API pour le vélo

_MAX_DURATION_S = 6 * 3600
_MAX_STEPS = 200
_PCT_RANGE = (0, 300)
_CADENCE_RANGE = (0, 200)


class WorkoutError(ValueError):
    """Séance refusée avant tout appel réseau (paramètre invalide)."""


def _num(block: dict[str, Any], key: str, label: str, *, lo: float, hi: float, required: bool = True) -> float | None:
    value = block.get(key)
    if value is None:
        if required:
            raise WorkoutError(f"{label} : `{key}` manquant")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
        raise WorkoutError(f"{label} : `{key}` doit être un nombre entre {lo} et {hi}")
    return value


def _duration(block: dict[str, Any], key: str, label: str) -> int:
    value = _num(block, key, label, lo=1, hi=_MAX_DURATION_S)
    return int(round(value))  # type: ignore[arg-type]


def _frac(pct: float | None) -> float:
    return round((pct or 0) / 100, 4)


def _messages(block: dict[str, Any], label: str, step_duration: int, *, interval: bool = False) -> list[dict[str, Any]]:
    raw = block.get("messages") or []
    if isinstance(block.get("message"), str):
        raw = [{"at_s": 0, "text": block["message"]}, *raw]
    out = []
    for i, msg in enumerate(raw, start=1):
        if not isinstance(msg, dict) or not isinstance(msg.get("text"), str) or not msg["text"].strip():
            raise WorkoutError(f"{label} : chaque message doit être {{at_s, text}}")
        at = int(msg.get("at_s") or 0)
        if not 0 <= at < step_duration:
            raise WorkoutError(f"{label} : message à {at} s hors de l'étape ({step_duration} s)")
        out.append({"Id": i, "Timeoffset" if interval else "Time": at, "Message": msg["text"].strip()})
    return out


def build_workout(
    name: str,
    blocks: list[dict[str, Any]],
    *,
    description: str | None = None,
    ftp_w: int | None = None,
    workout_id: int | None = None,
) -> dict[str, Any]:
    """Séance MyWhoosh à partir de blocs simples (pourcentages de FTP) :

    - `warmup` / `cooldown` / `ramp` : `duration_s`, `start_pct`, `end_pct` ;
    - `steady` : `duration_s`, `power_pct` ;
    - `intervals` : `repeat`, `on_s`, `on_pct`, `off_s`, `off_pct` ;
    - `free` : `duration_s` (roue libre, sans consigne de puissance).

    Optionnels sur chaque bloc : `cadence` (rpm ; `off_cadence` pour la récup des
    intervalles), `message` (texte affiché au début) ou `messages` [{at_s, text}].
    """
    if not isinstance(name, str) or not name.strip():
        raise WorkoutError("`name` est obligatoire")
    if not isinstance(blocks, list) or not blocks:
        raise WorkoutError("`blocks` doit contenir au moins un bloc")

    steps: list[dict[str, Any]] = []
    interval_id = 0

    def step(step_type: str, duration: int, *, power: float = 0, start: float = 0, end: float = 0,
             cadence: float | None = None, messages: list | None = None, interval: int = 0) -> None:
        steps.append({
            "IntervalId": interval,
            "StepType": step_type,
            "Id": len(steps) + 1,
            "WorkoutMessage": messages or [],
            "Rpm": int(cadence or 0),
            "Power": power,
            "Pace": 0,
            "StartPower": start,
            "EndPower": end,
            "Time": duration,
            "IsManualGrade": False,
            "ManualGradeValue": 0,
            "ShowAveragePower": True,
            "FlatRoad": 0,
        })

    for idx, block in enumerate(blocks, start=1):
        if not isinstance(block, dict):
            raise WorkoutError(f"bloc {idx} : un objet est attendu")
        kind = block.get("type")
        label = f"bloc {idx} ({kind})"
        cadence = _num(block, "cadence", label, lo=_CADENCE_RANGE[0], hi=_CADENCE_RANGE[1], required=False)
        if kind in ("warmup", "cooldown", "ramp"):
            duration = _duration(block, "duration_s", label)
            start = _num(block, "start_pct", label, lo=_PCT_RANGE[0], hi=_PCT_RANGE[1])
            end = _num(block, "end_pct", label, lo=_PCT_RANGE[0], hi=_PCT_RANGE[1])
            step("E_CoolDown" if kind == "cooldown" else "E_WarmUp", duration, start=_frac(start), end=_frac(end),
                 cadence=cadence, messages=_messages(block, label, duration))
        elif kind == "steady":
            duration = _duration(block, "duration_s", label)
            power = _num(block, "power_pct", label, lo=_PCT_RANGE[0], hi=_PCT_RANGE[1])
            step("E_Normal", duration, power=_frac(power), cadence=cadence, messages=_messages(block, label, duration))
        elif kind == "free":
            duration = _duration(block, "duration_s", label)
            step("E_FreeRide", duration, messages=_messages(block, label, duration))
        elif kind == "intervals":
            repeat = _num(block, "repeat", label, lo=1, hi=50)
            on_s = _duration(block, "on_s", label)
            off_s = _duration(block, "off_s", label)
            on_pct = _num(block, "on_pct", label, lo=_PCT_RANGE[0], hi=_PCT_RANGE[1])
            off_pct = _num(block, "off_pct", label, lo=_PCT_RANGE[0], hi=_PCT_RANGE[1])
            off_cadence = _num(block, "off_cadence", label, lo=_CADENCE_RANGE[0], hi=_CADENCE_RANGE[1], required=False)
            interval_id += 1
            on_messages = _messages(block, label, on_s, interval=True)
            for rep in range(int(repeat)):  # type: ignore[arg-type]
                step("E_Normal", on_s, power=_frac(on_pct), cadence=cadence,
                     messages=on_messages if rep == 0 else [], interval=interval_id)
                step("E_Normal", off_s, power=_frac(off_pct), cadence=off_cadence or cadence, interval=interval_id)
        else:
            raise WorkoutError(f"bloc {idx} : type inconnu {kind!r} (warmup, steady, intervals, ramp, cooldown, free)")

    if len(steps) > _MAX_STEPS:
        raise WorkoutError(f"séance trop longue : {len(steps)} étapes (max {_MAX_STEPS})")
    total = sum(s["Time"] for s in steps)
    if total > _MAX_DURATION_S:
        raise WorkoutError("séance de plus de 6 h")

    intensity, tss = _intensity(steps)
    kcal = _kcal(steps, ftp_w) if ftp_w else 0
    clean = name.strip()
    return {
        "Id": workout_id or int(time.time() * 1000),
        "Name": clean,
        "Description": (description or "").strip() or f"mywhoosh - {clean}",
        "Mode": "E_Ride",
        "ERGMode": "E_OFF",
        "IsRecovery": False,
        "IsIntervals": interval_id > 0,
        "FTPMode": "E_NoFTP",
        "IsTT": False,
        "IsTSS": False,
        "IsIF": False,
        "FTPMultiplier": 1,
        "StressPoint": 0,
        "Time": total,
        "CustomTagDescription": "2",
        "CategoryId": 200000000,
        "SubcategoryId": 1,
        "Type": "E_Normal",
        "DisplayType": "E_byWatts",
        "StepCount": len(steps),
        "IsFavorite": False,
        "CompletedCount": 0,
        "WorkoutSteps": [],
        "AuthorName": "Mon Coach'IA",
        "WokoutAssociationId": 0,  # sic, orthographe de l'API
        "IF": intensity,
        "TSS": tss,
        "KJ": kcal,
        "WorkoutStepsArray": steps,
    }


def _step_fraction(s: dict[str, Any]) -> float:
    if s["StepType"] == "E_FreeRide":
        return 0.5
    if s["StepType"] in ("E_WarmUp", "E_CoolDown"):
        return (s["StartPower"] + s["EndPower"]) / 2
    return s["Power"]


def _intensity(steps: list[dict[str, Any]]) -> tuple[float, float]:
    """IF (moyenne d'ordre 4 des paliers, proche de la NP) et TSS estimés."""
    total = sum(s["Time"] for s in steps)
    if total == 0:
        return 0.0, 0.0
    mean4 = sum(_step_fraction(s) ** 4 * s["Time"] for s in steps) / total
    intensity = math.sqrt(math.sqrt(mean4))
    return round(intensity, 2), round(total / 3600 * intensity**2 * 100, 1)


def _kcal(steps: list[dict[str, Any]], ftp_w: int) -> float:
    """Même estimation que le Workout Builder (travail mécanique / 4,184 / 25 %)."""
    joules = sum(_step_fraction(s) * ftp_w * s["Time"] for s in steps)
    return round(joules / 1000 / 4.184 / 0.25, 1)


def summarize(workout: dict[str, Any]) -> dict[str, Any]:
    """Résumé lisible pour la réponse du tool (dry_run)."""
    return {
        "nom": workout["Name"],
        "duree_minutes": round(workout["Time"] / 60),
        "etapes": workout["StepCount"],
        "if_estime": workout["IF"],
        "tss_estime": workout["TSS"],
        "deroule": [
            {
                "type": s["StepType"].removeprefix("E_"),
                "duree_s": s["Time"],
                **(
                    {"de_pct": round(s["StartPower"] * 100), "a_pct": round(s["EndPower"] * 100)}
                    if s["StepType"] in ("E_WarmUp", "E_CoolDown")
                    else {"pct_ftp": round(s["Power"] * 100)}
                    if s["StepType"] == "E_Normal"
                    else {}
                ),
                **({"rpm": s["Rpm"]} if s["Rpm"] else {}),
            }
            for s in workout["WorkoutStepsArray"]
        ],
    }
