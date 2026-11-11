"""Construction de payloads de séances structurées Garmin (course/vélo/natation).

La natation (`discipline="nage"`) a son propre schéma, voir `build_swim_workout` en fin de
module ; le schéma ci-dessous concerne course/vélo.

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
# PACE_ZONE=6 et non SPEED_ZONE=5 : `garminconnect.workout.TargetType` n'a pas d'attribut PACE
# (seulement OPEN=6, qui n'est pas ce qu'on veut), mais côté API Garmin réelle, un target de
# course borné en allure doit utiliser workoutTargetTypeId=6/"pace.zone" pour que l'app/montre
# affiche le résultat en min/km — 5/"speed.zone" l'affiche en km/h. Les bornes restent en m/s et
# le sens de l'inversion low/high est inchangé, seul le type de cible change.
_TARGET_TYPE_PACE_ZONE = 6

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
# DISTANCE=3 et non 1 : `garminconnect.workout.ConditionType.DISTANCE` vaut 1 dans la lib
# installée, mais l'API Garmin réelle traite 1 comme "lap.button" (fin manuelle) — un step
# borné en distance envoyé avec conditionTypeId=1 s'affichait comme "Appui sur touche Lap"
# sur l'app/montre au lieu de la distance cible. 3 est la valeur correcte côté serveur.
_CONDITION_TYPE_DISTANCE = 3
_CONDITION_TYPE_TIME = 2

# Clés reconnues à chaque niveau de `structure` — toute autre clé lève une erreur explicite
# (cf. docstring du module : c'est le fix d'un bug où une clé mal nommée pour le bloc de
# fractionné était silencieusement ignorée, sans aucune erreur ; propagé à tous les niveaux
# imbriqués après un incident similaire où `target_pace_min_per_km` — au lieu de la clé
# `target` attendue — était ignoré en silence, empêchant l'allure cible de remonter sur la montre).
_VALID_STRUCTURE_KEYS = {"discipline", "warmup", "blocks", "cooldown"}
_VALID_BLOCK_KEYS = {"repeat", "steps"}
_VALID_BLOCK_STEP_KEYS = {"type", "duration_sec", "distance_m", "target"}
_VALID_TIME_STEP_KEYS = {"duration_sec", "target"}
_VALID_TARGET_KEYS = {"type", "low", "high"}


def _validate_keys(d: dict[str, Any], valid_keys: set[str], context: str) -> None:
    unknown_keys = set(d) - valid_keys
    if unknown_keys:
        raise ValueError(
            f"Clé(s) non reconnue(s) dans {context} : {sorted(unknown_keys)} — clés attendues : "
            f"{sorted(valid_keys)}"
        )

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

    _validate_keys(target, _VALID_TARGET_KEYS, "target")
    target_type = target["type"]
    low = target["low"]
    high = target["high"]

    if target_type == "pace_min_per_km":
        # target.low = allure la plus rapide (min/km le plus petit) = vitesse la plus haute.
        value_one = 1000 / (high * 60)  # allure la plus lente -> vitesse la plus basse
        value_two = 1000 / (low * 60)  # allure la plus rapide -> vitesse la plus haute
        target_type_dict = {
            "workoutTargetTypeId": _TARGET_TYPE_PACE_ZONE,
            "workoutTargetTypeKey": "pace.zone",
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
    _validate_keys(raw_step, _VALID_BLOCK_STEP_KEYS, "step de bloc")
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
    en tête de ce module. `discipline="nage"` est délégué à `build_swim_workout` (schéma
    propre à la natation)."""
    if structure.get("discipline") == "nage":
        return build_swim_workout(structure)
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
            f"Discipline non supportée pour garmin_push_workout : {discipline!r} (attendu 'course', 'velo' ou 'nage')"
        )

    steps: list[ExecutableStep | RepeatGroup] = []
    order = 1

    warmup = structure.get("warmup")
    if warmup:
        _validate_keys(warmup, _VALID_TIME_STEP_KEYS, "warmup")
        steps.append(_build_time_step(create_warmup_step, warmup["duration_sec"], order, warmup.get("target")))
        order += 1

    for block in structure.get("blocks", []):
        _validate_keys(block, _VALID_BLOCK_KEYS, "block")
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
        _validate_keys(cooldown, _VALID_TIME_STEP_KEYS, "cooldown")
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


# ---------------------------------------------------------------------------
# Natation (discipline "nage") — schéma propre, payload construit à la main
# ---------------------------------------------------------------------------
#
# Format calqué sur une vraie séance natation créée dans Garmin Connect et relue via
# /workout-service/workout/{id}, et sur /workout-service/workout/types (référentiel
# d'identifiants côté serveur). Écarts avec `garminconnect` 0.3.2 :
#   - sportTypeId natation = 4 côté API réelle ; `garminconnect.workout.SportType.SWIMMING`
#     vaut 3, qui est "other" côté Garmin. `SwimmingWorkout` n'est donc pas utilisé.
#   - le niveau d'effort ("intensité" dans l'app) est un *secondaryTargetType*
#     swim.instruction (id 18), `secondaryTargetValueOne` = instructionTypeId ;
#   - le repos est un step "rest" (id 5) borné en "fixed.rest" (id 8, secondes) ou en
#     "lap.button" (id 1 : repos libre jusqu'à l'appui sur Lap) ;
#   - `poolLength`/`poolLengthUnit` au niveau racine : la montre compte les longueurs.

_SWIM_SPORT_TYPE = {"sportTypeId": 4, "sportTypeKey": "swimming", "displayOrder": 4}
_METER_UNIT = {"unitId": 1, "unitKey": "meter", "factor": 100.0}

_SWIM_STEP_TYPES: dict[str, dict[str, Any]] = {
    "warmup": {"stepTypeId": 1, "stepTypeKey": "warmup", "displayOrder": 1},
    "cooldown": {"stepTypeId": 2, "stepTypeKey": "cooldown", "displayOrder": 2},
    "interval": _STEP_TYPE_INTERVAL,
    "recovery": _STEP_TYPE_RECOVERY,
    "rest": {"stepTypeId": 5, "stepTypeKey": "rest", "displayOrder": 5},
}

_SWIM_STROKES = {
    "any_stroke": 1, "backstroke": 2, "breaststroke": 3, "drill": 4, "fly": 5, "free": 6,
    "individual_medley": 7, "mixed": 8,
}
_SWIM_EQUIPMENT = {"fins": 1, "kickboard": 2, "paddles": 3, "pull_buoy": 4, "snorkel": 5}
_SWIM_DRILLS = {"kick": 1, "pull": 2, "drill": 3}
_SWIM_INTENSITIES = {
    "recovery": 1, "very_easy": 2, "easy": 3, "moderate": 4, "hard": 5, "very_hard": 6,
    "all_out": 7, "fast": 8, "ascend": 9, "descend": 10,
}

_VALID_SWIM_STRUCTURE_KEYS = {"discipline", "pool_length_m", "warmup", "blocks", "cooldown"}
_VALID_SWIM_BLOCK_KEYS = {"repeat", "steps", "skip_last_rest"}
_VALID_SWIM_STEP_KEYS = {
    "type", "distance_m", "duration_sec", "stroke", "equipment", "drill", "intensity", "target",
    "description",
}
_VALID_SWIM_TARGET_TYPES = {"pace_min_per_100m", "hr_bpm"}


def _lookup(table: dict[str, int], key: str, field: str) -> int:
    if key not in table:
        raise ValueError(f"Valeur de {field!r} non supportée : {key!r} — attendu : {sorted(table)}")
    return table[key]


def _build_swim_target(target: dict[str, Any] | None) -> dict[str, Any]:
    if not target:
        return {}
    _validate_keys(target, _VALID_TARGET_KEYS, "target")
    target_type = target["type"]
    if target_type not in _VALID_SWIM_TARGET_TYPES:
        raise ValueError(
            f"Type de target non supporté en natation : {target_type!r} — attendu : "
            f"{sorted(_VALID_SWIM_TARGET_TYPES)}"
        )
    if target_type == "hr_bpm":
        return _build_target(target)
    low, high = float(target["low"]), float(target["high"])
    # Même piège d'inversion qu'en course : low = allure la plus rapide = vitesse la plus haute.
    return {
        "targetType": {
            "workoutTargetTypeId": _TARGET_TYPE_PACE_ZONE,
            "workoutTargetTypeKey": "pace.zone",
            "displayOrder": 1,
        },
        "targetValueOne": round(100 / (high * 60), 3),
        "targetValueTwo": round(100 / (low * 60), 3),
    }


def _build_swim_step(raw: dict[str, Any], step_order: int, default_type: str, pool_length_m: float) -> dict[str, Any]:
    _validate_keys(raw, _VALID_SWIM_STEP_KEYS, f"step natation ({default_type})")
    step_type_key = raw.get("type", default_type)
    step_type = _SWIM_STEP_TYPES.get(step_type_key)
    if step_type is None:
        raise ValueError(
            f"Type de step natation non supporté : {step_type_key!r} — attendu : {sorted(_SWIM_STEP_TYPES)}"
        )

    distance_m = raw.get("distance_m")
    duration_sec = raw.get("duration_sec")
    step: dict[str, Any] = {"type": "ExecutableStepDTO", "stepOrder": step_order, "stepType": dict(step_type)}

    if step_type_key == "rest":
        extra = {"stroke", "equipment", "drill", "intensity", "target", "distance_m"} & set(raw)
        if extra:
            raise ValueError(
                f"Un step 'rest' n'accepte que 'duration_sec'/'description', reçu aussi : {sorted(extra)}"
            )
        if duration_sec is None:
            # Repos libre : la montre attend l'appui sur Lap.
            step["endCondition"] = {
                "conditionTypeId": 1, "conditionTypeKey": "lap.button", "displayOrder": 1, "displayable": True,
            }
            step["endConditionValue"] = 0.0
        else:
            step["endCondition"] = {
                "conditionTypeId": 8, "conditionTypeKey": "fixed.rest", "displayOrder": 8, "displayable": True,
            }
            step["endConditionValue"] = float(duration_sec)
    else:
        if (distance_m is None) == (duration_sec is None):
            raise ValueError(
                f"Le step natation {step_type_key!r} doit fournir exactement l'un de 'distance_m' ou "
                "'duration_sec' (pas les deux, pas aucun des deux)"
            )
        if distance_m is not None:
            ratio = distance_m / pool_length_m
            if distance_m <= 0 or abs(ratio - round(ratio)) > 1e-6:
                raise ValueError(
                    f"distance_m={distance_m} n'est pas un multiple de la longueur du bassin "
                    f"({pool_length_m:g} m) : la montre compte des longueurs entières"
                )
            step["endCondition"] = {
                "conditionTypeId": _CONDITION_TYPE_DISTANCE, "conditionTypeKey": "distance",
                "displayOrder": 3, "displayable": True,
            }
            step["endConditionValue"] = float(distance_m)
            step["preferredEndConditionUnit"] = dict(_METER_UNIT)
        else:
            step["endCondition"] = {
                "conditionTypeId": _CONDITION_TYPE_TIME, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True,
            }
            step["endConditionValue"] = float(duration_sec)

        stroke_key = raw.get("stroke", "free")
        stroke_id = _lookup(_SWIM_STROKES, stroke_key, "stroke")
        step["strokeType"] = {"strokeTypeId": stroke_id, "strokeTypeKey": stroke_key, "displayOrder": stroke_id}
        equipment_key = raw.get("equipment")
        if equipment_key is not None:
            eq_id = _lookup(_SWIM_EQUIPMENT, equipment_key, "equipment")
            step["equipmentType"] = {"equipmentTypeId": eq_id, "equipmentTypeKey": equipment_key, "displayOrder": eq_id}
        drill_key = raw.get("drill")
        if drill_key is not None:
            drill_id = _lookup(_SWIM_DRILLS, drill_key, "drill")
            step["drillType"] = {"drillTypeId": drill_id, "drillTypeKey": drill_key, "displayOrder": drill_id}
        intensity_key = raw.get("intensity")
        if intensity_key is not None:
            step["secondaryTargetType"] = {
                "workoutTargetTypeId": 18, "workoutTargetTypeKey": "swim.instruction", "displayOrder": 18,
            }
            step["secondaryTargetValueOne"] = float(_lookup(_SWIM_INTENSITIES, intensity_key, "intensity"))
        step.update(_build_swim_target(raw.get("target")))

    if raw.get("description"):
        step["description"] = str(raw["description"])
    return step


def _swim_step_estimates(raw: dict[str, Any]) -> tuple[float, float]:
    """(distance m, durée s) best-effort d'un step natation, pour les champs d'affichage
    `estimatedDistanceInMeters`/`estimatedDurationInSecs`. Un step en distance sans allure
    cible contribue 0 s plutôt qu'une hypothèse fausse."""
    distance = float(raw.get("distance_m") or 0)
    duration = raw.get("duration_sec")
    if duration is not None:
        return distance, float(duration)
    target = raw.get("target")
    if distance and target and target.get("type") == "pace_min_per_100m":
        return distance, distance / 100 * (target["low"] + target["high"]) / 2 * 60
    return distance, 0.0


def build_swim_workout(structure: dict[str, Any]) -> dict[str, Any]:
    """Construit le payload Garmin d'une séance de natation en piscine (prêt pour
    `GarminClient.upload_workout`). Schéma documenté dans `garmin_push_workout`
    (src/tools/garmin_tools.py)."""
    _validate_keys(structure, _VALID_SWIM_STRUCTURE_KEYS, "structure (natation)")
    pool_length_m = float(structure.get("pool_length_m", 25))
    if pool_length_m <= 0:
        raise ValueError(f"pool_length_m doit être > 0, reçu {pool_length_m}")

    steps: list[dict[str, Any]] = []
    order = 1
    total_distance = 0.0
    total_duration = 0.0

    def add_single(raw: dict[str, Any], default_type: str) -> None:
        nonlocal order, total_distance, total_duration
        steps.append(_build_swim_step(raw, order, default_type, pool_length_m))
        order += 1
        d, t = _swim_step_estimates(raw)
        total_distance += d
        total_duration += t

    warmup = structure.get("warmup")
    if warmup:
        if "type" in warmup:
            raise ValueError("Clé 'type' non reconnue dans warmup (le type est implicite)")
        add_single({"type": "warmup", **warmup}, "warmup")

    for block in structure.get("blocks", []):
        _validate_keys(block, _VALID_SWIM_BLOCK_KEYS, "block (natation)")
        raw_steps = block.get("steps") or []
        if not raw_steps:
            raise ValueError("Un block natation doit contenir au moins un step")
        repeat = int(block.get("repeat", 1))
        if repeat < 1:
            raise ValueError(f"repeat doit être >= 1, reçu {repeat}")
        if repeat == 1:
            # Pas de groupe "1x" sur la montre : steps posés à plat, comme dans l'app Garmin.
            for raw in raw_steps:
                add_single(raw, "interval")
            continue
        group_order = order
        order += 1
        children = []
        for raw in raw_steps:
            children.append(_build_swim_step(raw, order, "interval", pool_length_m))
            order += 1
            d, t = _swim_step_estimates(raw)
            total_distance += d * repeat
            total_duration += t * repeat
        steps.append({
            "type": "RepeatGroupDTO",
            "stepOrder": group_order,
            "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
            "numberOfIterations": repeat,
            "workoutSteps": children,
            "endCondition": {
                "conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False,
            },
            "endConditionValue": float(repeat),
            "skipLastRestStep": bool(block.get("skip_last_rest", False)),
            "smartRepeat": False,
        })

    cooldown = structure.get("cooldown")
    if cooldown:
        if "type" in cooldown:
            raise ValueError("Clé 'type' non reconnue dans cooldown (le type est implicite)")
        add_single({"type": "cooldown", **cooldown}, "cooldown")

    if not steps:
        raise ValueError("Séance natation vide : fournir au moins warmup, blocks ou cooldown")

    return {
        "workoutName": _PLACEHOLDER_WORKOUT_NAME,
        "sportType": dict(_SWIM_SPORT_TYPE),
        "estimatedDurationInSecs": int(total_duration),
        "estimatedDistanceInMeters": total_distance,
        "poolLength": pool_length_m,
        "poolLengthUnit": dict(_METER_UNIT),
        "workoutSegments": [
            {"segmentOrder": 1, "sportType": dict(_SWIM_SPORT_TYPE), "workoutSteps": steps},
        ],
    }
