"""Construction de payloads de séances structurées Garmin (course/vélo), scope v1.

Fonctions pures : aucun appel réseau, aucune dépendance à `GarminClient`. Le payload produit
par `build_workout` est prêt pour `GarminClient.upload_workout` (via `.to_dict()` sous le
capot) — `garmin_push_workout` (src/tools/garmin_tools.py) écrase ensuite `workoutName` avec
le `name` fourni par l'appelant, `build_workout` ne le connaît pas.

Schéma exact de `structure` (seules clés reconnues au niveau racine, toute autre clé lève une
ValueError explicite plutôt que d'être silencieusement ignorée — cf. incident où `intervals`/
`repeats`/`repeat`/`steps`/`main` disparaissaient sans erreur) :

    {
      "discipline": "course" | "velo",
      "warmup":   {"duration_sec": <float>, "target": <target|absent>},   # optionnel
      "blocks": [                                                          # optionnel
        {
          "repeat": <int, defaut 1>,
          "steps": [
            {
              "type": "interval" | "recovery",
              "duration_sec": <float>,   # exactement l'un des deux
              "distance_m": <float>,     # (pas les deux, pas aucun des deux)
              "target": <target|absent>,
            },
            ...
          ],
        },
        ...
      ],
      "cooldown": {"duration_sec": <float>, "target": <target|absent>},   # optionnel
    }

`target` (optionnel, sur warmup/cooldown/n'importe quel step de bloc) :
    {"type": "pace_min_per_km" | "power_watts" | "hr_bpm", "low": <float>, "high": <float>}

`warmup`/`cooldown` sont toujours bornés en temps (`duration_sec` obligatoire — les helpers
`garminconnect` correspondants ne supportent pas la distance). Les steps `interval`/`recovery`
à l'intérieur d'un bloc peuvent être bornés en temps OU en distance, ex. pour un fractionné
"8x400m" : `{"type": "interval", "distance_m": 400, "target": {...}}`.
"""

from __future__ import annotations

from typing import Any

from garminconnect.workout import (
    CyclingWorkout,
    ExecutableStep,
    RepeatGroup,
    RunningWorkout,
    WorkoutSegment,
    create_cooldown_step,
    create_repeat_group,
    create_warmup_step,
)

_DISCIPLINE_WORKOUT_CLASS: dict[str, type] = {
    "course": RunningWorkout,
    "velo": CyclingWorkout,
}

# Répliqué depuis garminconnect.workout.RunningWorkout/CyclingWorkout (leurs Field
# default_factory ne sont pas récupérables proprement sans instancier l'objet).
_SPORT_TYPE: dict[str, dict[str, Any]] = {
    "course": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
    "velo": {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2},
}

# Identifiants numériques du protocole Garmin (pas un choix de `garminconnect`) : vérifiés
# stables entre garminconnect 0.3.2 et 0.3.9, alors que les noms d'attributs Python de
# `TargetType` ont été renommés entre ces deux versions (POWER -> POWER_ZONE,
# HEART_RATE -> HEART_RATE_ZONE, SPEED -> SPEED_ZONE, cf. README "Écarts volontaires").
# On n'importe donc plus `TargetType` de la lib, pour ne plus dépendre de ce nommage instable.
_TARGET_TYPE_NO_TARGET = 1
_TARGET_TYPE_POWER_ZONE = 2
_TARGET_TYPE_HEART_RATE_ZONE = 4
_TARGET_TYPE_SPEED_ZONE = 5

# Idem pour les types de step et de condition de fin : construits à la main pour les steps de
# bloc (interval/recovery) plutôt que via create_interval_step/create_recovery_step, qui ne
# bornent qu'en temps — pas de create_distance_interval_step dans la version installée en
# local (0.3.2, cf. README "Mise à jour en production").
_STEP_TYPE_INTERVAL = {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3}
_STEP_TYPE_RECOVERY = {"stepTypeId": 4, "stepTypeKey": "recovery", "displayOrder": 4}
_BLOCK_STEP_TYPES: dict[str, dict[str, Any]] = {
    "interval": _STEP_TYPE_INTERVAL,
    "recovery": _STEP_TYPE_RECOVERY,
}
_CONDITION_TYPE_DISTANCE = 1
_CONDITION_TYPE_TIME = 2

# Clés reconnues au niveau racine de `structure` — toute autre clé lève une erreur explicite
# (cf. docstring du module : c'est le fix d'un bug où une clé mal nommée pour le bloc de
# fractionné était silencieusement ignorée, sans aucune erreur).
_VALID_STRUCTURE_KEYS = {"discipline", "warmup", "blocks", "cooldown"}

_PLACEHOLDER_WORKOUT_NAME = "Séance structurée"


def _build_target(target: dict[str, Any] | None) -> dict[str, Any]:
    """Convertit un `target` du schéma d'entrée vers le targetType/bornes Garmin.

    Retourne toujours un dict avec au moins `targetType`. `targetValueOne`/`targetValueTwo`
    ne sont présents que lorsqu'une zone est réellement imposée (absents pour NO_TARGET).

    Piège : l'allure (min/km) et la vitesse sont inversement proportionnelles. `target.low`
    (l'allure la plus rapide) correspond à la vitesse la plus haute, donc à `targetValueTwo`
    (la borne haute côté Garmin) — pas `targetValueOne`.
    """
    if not target:
        return {
            "targetType": {
                "workoutTargetTypeId": _TARGET_TYPE_NO_TARGET,
                "workoutTargetTypeKey": "no.target",
                "displayOrder": 1,
            }
        }

    target_type = target["type"]
    low = target["low"]
    high = target["high"]

    if target_type == "pace_min_per_km":
        # target.low = allure la plus rapide (min/km le plus petit) = vitesse la plus haute.
        value_one = 1000 / (high * 60)  # allure la plus lente -> vitesse la plus basse
        value_two = 1000 / (low * 60)  # allure la plus rapide -> vitesse la plus haute
        target_type_dict = {
            "workoutTargetTypeId": _TARGET_TYPE_SPEED_ZONE,
            "workoutTargetTypeKey": "speed.zone",
            "displayOrder": 1,
        }
    elif target_type == "power_watts":
        value_one, value_two = float(low), float(high)
        target_type_dict = {
            "workoutTargetTypeId": _TARGET_TYPE_POWER_ZONE,
            "workoutTargetTypeKey": "power.zone",
            "displayOrder": 1,
        }
    elif target_type == "hr_bpm":
        value_one, value_two = float(low), float(high)
        target_type_dict = {
            "workoutTargetTypeId": _TARGET_TYPE_HEART_RATE_ZONE,
            "workoutTargetTypeKey": "heart.rate.zone",
            "displayOrder": 1,
        }
    else:
        raise ValueError(f"Type de target non supporté : {target_type!r}")

    return {
        "targetType": target_type_dict,
        "targetValueOne": round(value_one, 3),
        "targetValueTwo": round(value_two, 3),
    }


def _build_time_step(creator, duration_sec: float, step_order: int, target: dict[str, Any] | None) -> ExecutableStep:
    """Pour warmup/cooldown : toujours bornés en temps (create_warmup_step/create_cooldown_step
    de garminconnect ne supportent que ça)."""
    parsed = _build_target(target)
    step = creator(duration_sec, step_order, target_type=parsed["targetType"])
    if "targetValueOne" in parsed:
        step.targetValueOne = parsed["targetValueOne"]
        step.targetValueTwo = parsed["targetValueTwo"]
    return step


def _build_block_step(raw_step: dict[str, Any], step_order: int) -> ExecutableStep:
    """Pour un step interval/recovery à l'intérieur d'un bloc : borné en temps
    (`duration_sec`) OU en distance (`distance_m`), exactement l'un des deux."""
    step_type_key = raw_step["type"]
    step_type = _BLOCK_STEP_TYPES.get(step_type_key)
    if step_type is None:
        raise ValueError(f"Type de step non supporté dans un bloc : {step_type_key!r}")

    duration_sec = raw_step.get("duration_sec")
    distance_m = raw_step.get("distance_m")
    if (duration_sec is None) == (distance_m is None):
        raise ValueError(
            f"Le step {step_type_key!r} doit fournir exactement l'un de 'duration_sec' ou "
            "'distance_m' (pas les deux, pas aucun des deux)"
        )

    if distance_m is not None:
        end_condition = {
            "conditionTypeId": _CONDITION_TYPE_DISTANCE,
            "conditionTypeKey": "distance",
            "displayOrder": 1,
            "displayable": True,
        }
        end_condition_value = distance_m
    else:
        end_condition = {
            "conditionTypeId": _CONDITION_TYPE_TIME,
            "conditionTypeKey": "time",
            "displayOrder": 2,
            "displayable": True,
        }
        end_condition_value = duration_sec

    parsed = _build_target(raw_step.get("target"))
    step = ExecutableStep(
        stepOrder=step_order,
        stepType=step_type,
        endCondition=end_condition,
        endConditionValue=end_condition_value,
        targetType=parsed["targetType"],
    )
    if "targetValueOne" in parsed:
        step.targetValueOne = parsed["targetValueOne"]
        step.targetValueTwo = parsed["targetValueTwo"]
    return step


def _estimate_step_seconds(raw_step: dict[str, Any]) -> float:
    """Estimation best-effort pour `estimatedDurationInSecs` (champ d'affichage Garmin, pas
    une valeur exacte). Pour un step en distance sans cible d'allure, impossible d'estimer un
    temps à partir de la seule distance -> contribution nulle plutôt qu'une hypothèse fausse."""
    duration_sec = raw_step.get("duration_sec")
    if duration_sec is not None:
        return duration_sec
    distance_m = raw_step.get("distance_m") or 0
    target = raw_step.get("target")
    if target and target.get("type") == "pace_min_per_km":
        avg_pace_min_per_km = (target["low"] + target["high"]) / 2
        return distance_m / 1000 * avg_pace_min_per_km * 60
    return 0.0


def _total_duration_seconds(structure: dict[str, Any]) -> int:
    total = 0.0
    warmup = structure.get("warmup")
    if warmup:
        total += warmup["duration_sec"]
    for block in structure.get("blocks", []):
        block_total = sum(_estimate_step_seconds(s) for s in block.get("steps", []))
        total += block_total * block.get("repeat", 1)
    cooldown = structure.get("cooldown")
    if cooldown:
        total += cooldown["duration_sec"]
    return int(total)


def build_workout(structure: dict[str, Any]) -> dict[str, Any]:
    """Dispatcher : construit un RunningWorkout/CyclingWorkout à partir de `structure` et
    retourne son `.to_dict()` (JSON prêt pour `GarminClient.upload_workout`). Schéma complet
    en tête de ce module."""
    unknown_keys = set(structure) - _VALID_STRUCTURE_KEYS
    if unknown_keys:
        raise ValueError(
            f"Clé(s) non reconnue(s) dans structure : {sorted(unknown_keys)} — clés attendues : "
            f"{sorted(_VALID_STRUCTURE_KEYS)}. Le fractionné se fournit sous la clé 'blocks' "
            "(liste de {'repeat': int, 'steps': [...]}), pas 'intervals'/'repeats'/'repeat'/"
            "'steps'/'main'."
        )

    discipline = structure.get("discipline")
    workout_cls = _DISCIPLINE_WORKOUT_CLASS.get(discipline)
    if workout_cls is None:
        raise ValueError(
            f"Discipline non supportée pour garmin_push_workout : {discipline!r} (attendu 'course' ou 'velo')"
        )

    steps: list[ExecutableStep | RepeatGroup] = []
    order = 1

    warmup = structure.get("warmup")
    if warmup:
        steps.append(_build_time_step(create_warmup_step, warmup["duration_sec"], order, warmup.get("target")))
        order += 1

    for block in structure.get("blocks", []):
        block_steps: list[ExecutableStep] = []
        for raw_step in block.get("steps", []):
            block_steps.append(_build_block_step(raw_step, order))
            order += 1
        # Un bloc sans `repeat` (ou repeat=1) est traité par le même mécanisme qu'un bloc
        # répété (create_repeat_group avec iterations=1) — pas de cas particulier.
        steps.append(create_repeat_group(iterations=block.get("repeat", 1), workout_steps=block_steps, step_order=order))
        order += 1

    cooldown = structure.get("cooldown")
    if cooldown:
        steps.append(_build_time_step(create_cooldown_step, cooldown["duration_sec"], order, cooldown.get("target")))
        order += 1

    workout = workout_cls(
        workoutName=_PLACEHOLDER_WORKOUT_NAME,
        estimatedDurationInSecs=_total_duration_seconds(structure),
        workoutSegments=[
            WorkoutSegment(segmentOrder=1, sportType=_SPORT_TYPE[discipline], workoutSteps=steps),
        ],
    )
    return workout.to_dict()
