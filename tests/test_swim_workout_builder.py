"""Tests purs du constructeur de séances natation (src/workout_builder.py, discipline "nage").

Les identifiants attendus viennent d'une vraie séance natation créée dans Garmin Connect et
relue via /workout-service/workout/{id}, et de /workout-service/workout/types.
"""

from __future__ import annotations

import pytest

from src.workout_builder import build_workout

SWIM_STRUCTURE = {
    "discipline": "nage",
    "pool_length_m": 25,
    "warmup": {"distance_m": 300, "intensity": "easy"},
    "blocks": [
        {
            "repeat": 4,
            "steps": [
                {"distance_m": 50, "drill": "drill", "description": "Rattrapé"},
                {"type": "rest", "duration_sec": 15},
            ],
        },
        {
            "repeat": 8,
            "skip_last_rest": True,
            "steps": [
                {"distance_m": 100, "target": {"type": "pace_min_per_100m", "low": 1.833, "high": 1.917}},
                {"type": "rest", "duration_sec": 20},
            ],
        },
    ],
    "cooldown": {"distance_m": 200, "stroke": "any_stroke", "equipment": "pull_buoy", "intensity": "easy"},
}


def test_swim_uses_real_garmin_sport_type_4_not_lib_value_3() -> None:
    payload = build_workout(SWIM_STRUCTURE)
    expected = {"sportTypeId": 4, "sportTypeKey": "swimming", "displayOrder": 4}
    assert payload["sportType"] == expected
    assert payload["workoutSegments"][0]["sportType"] == expected


def test_swim_pool_length_at_root() -> None:
    payload = build_workout(SWIM_STRUCTURE)
    assert payload["poolLength"] == 25.0
    assert payload["poolLengthUnit"] == {"unitId": 1, "unitKey": "meter", "factor": 100.0}


def test_swim_top_level_structure_and_step_orders() -> None:
    steps = build_workout(SWIM_STRUCTURE)["workoutSegments"][0]["workoutSteps"]
    assert [s["stepType"]["stepTypeKey"] for s in steps] == ["warmup", "repeat", "repeat", "cooldown"]
    orders = []

    def walk(items):
        for s in items:
            orders.append(s["stepOrder"])
            walk(s.get("workoutSteps", []))

    walk(steps)
    assert sorted(orders) == list(range(1, len(orders) + 1))
    assert steps[2]["numberOfIterations"] == 8
    assert steps[2]["skipLastRestStep"] is True
    assert steps[1]["skipLastRestStep"] is False


def test_swim_distance_step_matches_real_format() -> None:
    warmup = build_workout(SWIM_STRUCTURE)["workoutSegments"][0]["workoutSteps"][0]
    assert warmup["endCondition"]["conditionTypeId"] == 3
    assert warmup["endConditionValue"] == 300.0
    assert warmup["preferredEndConditionUnit"]["unitKey"] == "meter"
    assert warmup["strokeType"] == {"strokeTypeId": 6, "strokeTypeKey": "free", "displayOrder": 6}
    assert warmup["secondaryTargetType"]["workoutTargetTypeId"] == 18
    assert warmup["secondaryTargetValueOne"] == 3.0


def test_swim_drill_equipment_description() -> None:
    steps = build_workout(SWIM_STRUCTURE)["workoutSegments"][0]["workoutSteps"]
    drill = steps[1]["workoutSteps"][0]
    assert drill["drillType"] == {"drillTypeId": 3, "drillTypeKey": "drill", "displayOrder": 3}
    assert drill["description"] == "Rattrapé"
    assert drill["stepType"]["stepTypeKey"] == "interval"
    cooldown = steps[3]
    assert cooldown["equipmentType"]["equipmentTypeId"] == 4
    assert cooldown["strokeType"]["strokeTypeId"] == 1


def test_swim_fixed_rest_and_lap_button_rest() -> None:
    rest = build_workout(SWIM_STRUCTURE)["workoutSegments"][0]["workoutSteps"][1]["workoutSteps"][1]
    assert rest["stepType"]["stepTypeId"] == 5
    assert rest["endCondition"]["conditionTypeKey"] == "fixed.rest"
    assert rest["endConditionValue"] == 15.0
    assert "strokeType" not in rest

    free_rest = build_workout(
        {"discipline": "nage", "blocks": [{"steps": [{"distance_m": 100}, {"type": "rest"}]}]}
    )["workoutSegments"][0]["workoutSteps"][1]
    assert free_rest["endCondition"]["conditionTypeKey"] == "lap.button"


def test_swim_pace_target_inverted_like_running() -> None:
    interval = build_workout(SWIM_STRUCTURE)["workoutSegments"][0]["workoutSteps"][2]["workoutSteps"][0]
    assert interval["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    # low=1.833 min/100m (le plus rapide) -> vitesse haute -> targetValueTwo.
    assert interval["targetValueTwo"] == round(100 / (1.833 * 60), 3)
    assert interval["targetValueOne"] == round(100 / (1.917 * 60), 3)
    assert interval["targetValueTwo"] > interval["targetValueOne"]


def test_swim_estimates() -> None:
    payload = build_workout(SWIM_STRUCTURE)
    assert payload["estimatedDistanceInMeters"] == 300 + 4 * 50 + 8 * 100 + 200
    # Seuls les repos fixes et les 100 avec allure sont estimables.
    expected = 4 * 15 + 8 * (100 / 100 * (1.833 + 1.917) / 2 * 60 + 20)
    assert payload["estimatedDurationInSecs"] == int(expected)


def test_swim_block_repeat_one_is_flattened() -> None:
    steps = build_workout(
        {"discipline": "nage", "blocks": [{"steps": [{"distance_m": 200}, {"type": "rest", "duration_sec": 30}]}]}
    )["workoutSegments"][0]["workoutSteps"]
    assert [s["type"] for s in steps] == ["ExecutableStepDTO", "ExecutableStepDTO"]


def test_swim_distance_must_be_multiple_of_pool_length() -> None:
    with pytest.raises(ValueError, match="multiple"):
        build_workout({"discipline": "nage", "pool_length_m": 25, "warmup": {"distance_m": 110}})
    build_workout({"discipline": "nage", "pool_length_m": 50, "warmup": {"distance_m": 400}})
    with pytest.raises(ValueError, match="multiple"):
        build_workout({"discipline": "nage", "pool_length_m": 50, "warmup": {"distance_m": 75}})


@pytest.mark.parametrize(
    "structure",
    [
        {"discipline": "nage", "warmup": {"distance_m": 100, "nage": "free"}},
        {"discipline": "nage", "intervals": []},
        {"discipline": "nage", "blocks": [{"repeats": 4, "steps": [{"distance_m": 50}]}]},
        {"discipline": "nage", "blocks": [{"steps": [{"distance_m": 50, "stroke": "crawl"}]}]},
        {"discipline": "nage", "blocks": [{"steps": [{"distance_m": 50, "equipment": "palmes"}]}]},
        {"discipline": "nage", "blocks": [{"steps": [{"distance_m": 50, "intensity": "max"}]}]},
        {"discipline": "nage", "blocks": [{"steps": [{"distance_m": 50, "duration_sec": 60}]}]},
        {"discipline": "nage", "blocks": [{"steps": [{"stroke": "free"}]}]},
        {"discipline": "nage", "blocks": [{"steps": [{"type": "rest", "distance_m": 25}]}]},
        {"discipline": "nage", "blocks": [{"steps": [{"distance_m": 50,
                                                       "target": {"type": "pace_min_per_km", "low": 1, "high": 2}}]}]},
        {"discipline": "nage", "blocks": [{"steps": []}]},
        {"discipline": "nage"},
    ],
)
def test_swim_rejects_invalid_structures(structure: dict) -> None:
    with pytest.raises(ValueError):
        build_workout(structure)
