"""Faux compte Garmin réaliste (réponses brutes garminconnect mockées), partagé par les tests
de non-régression et de disponibilité des données."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.garmin_client import GarminClient
from tests.synthetic import interval_run, swim_session, to_garmin_details

TRAINING_RAW = {
    "mostRecentTrainingStatus": {
        "latestTrainingStatusData": {
            "1": {
                "trainingStatusFeedbackPhrase": "PRODUCTIVE_1",
                "acuteTrainingLoadDTO": {"dailyTrainingLoadAcute": 480, "dailyTrainingLoadChronic": 400},
            }
        }
    },
    "mostRecentTrainingLoadBalance": {
        "metricsTrainingLoadBalanceDTOMap": {"1": {"monthlyLoadAerobicLow": 390.0, "monthlyLoadAnaerobic": 45.0}}
    },
}


def _swim_splits_raw() -> dict:
    lengths = [
        {
            "lengthIndex": s["length_idx"],
            "startTimeGMT": f"2026-10-16T18:00:00.0" if i == 0 else None,
            "duration": s["duration_s"],
            "distance": 25.0,
            "averageHR": s["avg_hr"],
            "averageSWOLF": s["swolf"],
            "swimStroke": "FREESTYLE",
        }
        for i, s in enumerate(swim_session())
    ]
    # startTimeGMT absolu pour chaque longueur (le client calcule start_s par différence).
    from datetime import datetime, timedelta

    t0 = datetime(2026, 10, 16, 18, 0, 0)
    for length, s in zip(lengths, swim_session()):
        length["startTimeGMT"] = (t0 + timedelta(seconds=s["start_s"])).strftime("%Y-%m-%dT%H:%M:%S.0")
    return {"lapDTOs": [{"lengthDTOs": lengths}]}


def healthy_client(tmp_path) -> GarminClient:
    client = GarminClient("a@b.com", "pw", str(tmp_path / "tokenstore"))
    g = client._garmin = MagicMock()
    client._logged_in = True
    g.get_training_status.return_value = TRAINING_RAW
    g.get_body_battery.return_value = [
        {"charged": 60, "drained": 40, "bodyBatteryValuesArray": [[1760652000000, 30], [1760677200000, 88], [1760702400000, 41]]}
    ]
    g.get_user_summary.return_value = {"bodyBatteryAtWakeTime": 85, "lastSyncTimestampGMT": "2026-10-17T05:12:00.0"}
    g.get_max_metrics.return_value = [{"generic": {"vo2MaxValue": 54}}]
    g.get_rhr_day.return_value = {
        "allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [{"value": 46, "calendarDate": "2026-10-17"}]}}
    }
    g.get_sleep_data.return_value = {
        "dailySleepDTO": {
            "sleepTimeSeconds": 26000,
            "deepSleepSeconds": 5400,
            "lightSleepSeconds": 14000,
            "remSleepSeconds": 4500,
            "awakeSleepSeconds": 1300,
            "sleepEndTimestampGMT": 1760680800000,
            "sleepScores": {"overall": {"value": 81}},
        }
    }
    g.get_activities.return_value = [
        {
            "activityId": 24737274389,
            "startTimeLocal": "2026-10-15 19:41:12",
            "activityType": {"typeKey": "running"},
            "duration": 2361.9,
            "distance": 6045.3,
            "averageSpeed": 2.56,
            "maxSpeed": 3.4,
            "averageHR": 119,
            "maxHR": 140,
        }
    ]
    g.get_lactate_threshold.return_value = {"speed_and_heart_rate": {"heartRate": 174, "speed": 0.383}, "power": {}}
    g.get_weekly_intensity_minutes.return_value = [{"moderateValue": 50, "vigorousValue": 20, "weeklyGoal": 150}]
    g.get_activity_weather.return_value = {
        "temp": 64,
        "apparentTemp": 64,
        "dewPoint": 48,
        "relativeHumidity": 56,
        "windSpeed": 2,
        "windGust": None,
        "windDirectionCompassPoint": "se",
        "weatherStationDTO": {"id": "LFPV", "name": "Villacoublay"},
        "weatherTypeDTO": {"desc": "Fair"},
    }
    g.get_activity.return_value = {
        "summaryDTO": {"startLatitude": 48.80, "startLongitude": 2.17, "directWorkoutFeel": 75, "directWorkoutRpe": 70}
    }
    g.get_activity_details.return_value = to_garmin_details(interval_run())
    g.get_activity_splits.return_value = _swim_splits_raw()
    g.get_device_last_used.return_value = {
        "lastUsedDeviceName": "Forerunner 265",
        "lastUsedDeviceUploadTime": 1760682000000,
    }
    g.get_scheduled_workouts.return_value = {"calendarItems": []}
    return client
