"""Construction de payloads de séances structurées Garmin (course/vélo), scope v1.

Fonctions pures : aucun appel réseau, aucune dépendance à `GarminClient`. Le payload produit
par `build_workout` est prêt pour `GarminClient.upload_workout` (via `.to_dict()` sous le
capot) — `garmin_push_workout` (src/tools/garmin_tools.py) écrase ensuite `workoutName` avec
le `name` fourni par l'appelant, `build_workout` ne le connaît pas.
"""

from __future__ import annotations

from typing import Any, Callable

from garminconnect.workout import (
    CyclingWorkout,
    ExecutableStep,
    RepeatGroup,
    RunningWorkout,
    WorkoutSegment,
    create_cooldown_step,
    create_interval_step,
    create_recovery_step,
    create_repeat_group,
    create_warmup_step,
)

_DISCIPLINE_WORKOUT_CLASS: dict[str, type] = {
    "course": RunningWorkout,
    "velo": CyclingWorkout,
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

# Répliqué depuis garminconnect.workout.RunningWorkout/CyclingWorkout (leurs Field
# default_factory ne sont pas récupérables proprement sans instancier l'objet).
_SPORT_TYPE: dict[str, dict[str, Any]] = {
    "course": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
    "velo": {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2},
}

_BLOCK_STEP_BUILDERS: dict[str, Callable[..., ExecutableStep]] = {
    "interval": create_interval_step,
    "recovery": create_recovery_step,
}

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


def _build_step(
    creator: Callable[..., ExecutableStep], duration_sec: float, step_order: int, target: dict[str, Any] | None
) -> ExecutableStep:
    parsed = _build_target(target)
    step = creator(duration_sec, step_order, target_type=parsed["targetType"])
    if "targetValueOne" in parsed:
        step.targetValueOne = parsed["targetValueOne"]
        step.targetValueTwo = parsed["targetValueTwo"]
    return step


def _total_duration_seconds(structure: dict[str, Any]) -> int:
    total = 0.0
    warmup = structure.get("warmup")
    if warmup:
        total += warmup["duration_sec"]
    for block in structure.get("blocks", []):
        block_total = sum(s["duration_sec"] for s in block.get("steps", []))
        total += block_total * block.get("repeat", 1)
    cooldown = structure.get("cooldown")
    if cooldown:
        total += cooldown["duration_sec"]
    return int(total)


def build_workout(structure: dict[str, Any]) -> dict[str, Any]:
    """Dispatcher : construit un RunningWorkout/CyclingWorkout à partir de `structure` et
    retourne son `.to_dict()` (JSON prêt pour `GarminClient.upload_workout`)."""
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
        steps.append(_build_step(create_warmup_step, warmup["duration_sec"], order, warmup.get("target")))
        order += 1

    for block in structure.get("blocks", []):
        block_steps: list[ExecutableStep] = []
        for raw_step in block.get("steps", []):
            builder = _BLOCK_STEP_BUILDERS.get(raw_step["type"])
            if builder is None:
                raise ValueError(f"Type de step non supporté dans un bloc : {raw_step['type']!r}")
            block_steps.append(_build_step(builder, raw_step["duration_sec"], order, raw_step.get("target")))
            order += 1
        # Un bloc sans `repeat` (ou repeat=1) est traité par le même mécanisme qu'un bloc
        # répété (create_repeat_group avec iterations=1) — pas de cas particulier.
        steps.append(create_repeat_group(iterations=block.get("repeat", 1), workout_steps=block_steps, step_order=order))
        order += 1

    cooldown = structure.get("cooldown")
    if cooldown:
        steps.append(_build_step(create_cooldown_step, cooldown["duration_sec"], order, cooldown.get("target")))
        order += 1

    workout = workout_cls(
        workoutName=_PLACEHOLDER_WORKOUT_NAME,
        estimatedDurationInSecs=_total_duration_seconds(structure),
        workoutSegments=[
            WorkoutSegment(segmentOrder=1, sportType=_SPORT_TYPE[discipline], workoutSteps=steps),
        ],
    )
    return workout.to_dict()
