"""Tests purs de src/workout_builder.py (aucun appel réseau, aucun GarminClient)."""

from __future__ import annotations

import pytest

from src.workout_builder import _build_target, build_workout

# Fixtures reprises telles quelles du brief.

COURSE_STRUCTURE = {
    "discipline": "course",
    "warmup": {"duration_sec": 900},
    "blocks": [
        {
            "repeat": 6,
            "steps": [
                {"type": "interval", "duration_sec": 90, "target": {"type": "pace_min_per_km", "low": 3.33, "high": 3.5}},
                {"type": "recovery", "duration_sec": 90},
            ],
        }
    ],
    "cooldown": {"duration_sec": 600},
}

VELO_STRUCTURE = {
    "discipline": "velo",
    "warmup": {"duration_sec": 900},
    "blocks": [
        {
            "repeat": 6,
            "steps": [
                {"type": "interval", "duration_sec": 240, "target": {"type": "power_watts", "low": 265, "high": 300}},
                {"type": "recovery", "duration_sec": 240},
            ],
        }
    ],
    "cooldown": {"duration_sec": 600},
}


# ---------------------------------------------------------------------------
# _build_target : sens de conversion allure -> vitesse (piège explicitement signalé)
# ---------------------------------------------------------------------------


def test_build_target_pace_low_maps_to_target_value_two() -> None:
    """target.low (allure la plus rapide) doit donner la vitesse la plus haute (targetValueTwo),
    pas targetValueOne — piège explicitement signalé dans le brief."""
    result = _build_target({"type": "pace_min_per_km", "low": 3.33, "high": 3.5})

    assert result["targetValueOne"] < result["targetValueTwo"]
    # low=3.33 (rapide) -> vitesse haute -> targetValueTwo ; high=3.5 (lent) -> targetValueOne
    expected_value_two = round(1000 / (3.33 * 60), 3)
    expected_value_one = round(1000 / (3.5 * 60), 3)
    assert result["targetValueTwo"] == expected_value_two
    assert result["targetValueOne"] == expected_value_one


def test_build_target_power_no_inversion() -> None:
    result = _build_target({"type": "power_watts", "low": 265, "high": 300})

    assert result["targetValueOne"] == 265.0
    assert result["targetValueTwo"] == 300.0


def test_build_target_hr_no_inversion() -> None:
    result = _build_target({"type": "hr_bpm", "low": 140, "high": 160})

    assert result["targetValueOne"] == 140.0
    assert result["targetValueTwo"] == 160.0


def test_build_target_none_returns_no_target() -> None:
    result = _build_target(None)

    assert result == {
        "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
    }
    assert "targetValueOne" not in result


def test_build_target_unsupported_type_raises() -> None:
    with pytest.raises(ValueError):
        _build_target({"type": "cadence_spm", "low": 80, "high": 90})


# ---------------------------------------------------------------------------
# build_workout : fixtures du brief
# ---------------------------------------------------------------------------


def test_build_workout_course_uses_running_sport_type() -> None:
    payload = build_workout(COURSE_STRUCTURE)

    assert payload["sportType"] == {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}


def test_build_workout_velo_uses_cycling_sport_type() -> None:
    payload = build_workout(VELO_STRUCTURE)

    assert payload["sportType"] == {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2}


def test_build_workout_course_total_duration() -> None:
    payload = build_workout(COURSE_STRUCTURE)

    # 900 (warmup) + 6*(90+90) (blocs) + 600 (cooldown)
    assert payload["estimatedDurationInSecs"] == 900 + 6 * 180 + 600


def test_build_workout_velo_total_duration() -> None:
    payload = build_workout(VELO_STRUCTURE)

    # 900 (warmup) + 6*(240+240) (blocs) + 600 (cooldown)
    assert payload["estimatedDurationInSecs"] == 900 + 6 * 480 + 600


def test_build_workout_course_step_structure() -> None:
    payload = build_workout(COURSE_STRUCTURE)
    segment = payload["workoutSegments"][0]
    steps = segment["workoutSteps"]

    # warmup, repeat group, cooldown
    assert len(steps) == 3
    warmup_step, repeat_group, cooldown_step = steps

    assert warmup_step["stepType"]["stepTypeKey"] == "warmup"
    assert warmup_step["endConditionValue"] == 900
    assert warmup_step["targetType"]["workoutTargetTypeKey"] == "no.target"
    assert "targetValueOne" not in warmup_step

    assert repeat_group["type"] == "RepeatGroupDTO"
    assert repeat_group["numberOfIterations"] == 6
    assert len(repeat_group["workoutSteps"]) == 2
    interval_step, recovery_step = repeat_group["workoutSteps"]
    assert interval_step["stepType"]["stepTypeKey"] == "interval"
    assert interval_step["endConditionValue"] == 90
    assert interval_step["targetType"]["workoutTargetTypeKey"] == "speed.zone"
    assert interval_step["targetValueOne"] < interval_step["targetValueTwo"]
    assert recovery_step["stepType"]["stepTypeKey"] == "recovery"
    assert "targetValueOne" not in recovery_step

    assert cooldown_step["stepType"]["stepTypeKey"] == "cooldown"
    assert cooldown_step["endConditionValue"] == 600


def test_build_workout_velo_power_target_not_inverted() -> None:
    payload = build_workout(VELO_STRUCTURE)
    repeat_group = payload["workoutSegments"][0]["workoutSteps"][1]
    interval_step = repeat_group["workoutSteps"][0]

    assert interval_step["targetType"]["workoutTargetTypeKey"] == "power.zone"
    assert interval_step["targetValueOne"] == 265.0
    assert interval_step["targetValueTwo"] == 300.0


def test_build_workout_unsupported_discipline_raises() -> None:
    structure = {**COURSE_STRUCTURE, "discipline": "nage"}

    with pytest.raises(ValueError):
        build_workout(structure)


def test_build_workout_unsupported_block_step_type_raises() -> None:
    structure = {
        "discipline": "course",
        "blocks": [{"repeat": 1, "steps": [{"type": "warmup", "duration_sec": 60}]}],
    }

    with pytest.raises(ValueError):
        build_workout(structure)


def test_build_workout_block_without_repeat_defaults_to_one_iteration() -> None:
    structure = {
        "discipline": "course",
        "blocks": [{"steps": [{"type": "interval", "duration_sec": 60}]}],
    }

    payload = build_workout(structure)

    repeat_group = payload["workoutSegments"][0]["workoutSteps"][0]
    assert repeat_group["numberOfIterations"] == 1
    assert payload["estimatedDurationInSecs"] == 60
