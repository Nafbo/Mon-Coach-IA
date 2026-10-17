"""Tests sur les séries réelles anonymisées des activités de référence.

Les fixtures `tests/fixtures/<activity_id>.json.gz` sont produites par
`python scripts/capture_fixtures.py 24725112038 24748453127` (token Garmin valide requis,
lecture seule). Tant qu'elles n'existent pas, ces tests sont ignorés : les tests sur séries
synthétiques (test_analysis.py, test_swim.py) couvrent la même logique hors ligne.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.analysis import series as an
from src.analysis import swim
from src.garmin_client import GarminClient

FIXTURES = Path(__file__).parent / "fixtures"
RUN_ID = "24725112038"
SWIM_ID = "24748453127"


def _client_from_fixture(activity_id: str, tmp_path: Path) -> GarminClient:
    path = FIXTURES / f"{activity_id}.json.gz"
    if not path.exists():
        pytest.skip(f"fixture absente : {path.name} (cf. scripts/capture_fixtures.py)")
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        payload = json.load(fh)
    client = GarminClient("a@b.com", "pw", str(tmp_path / "tokens"))
    client._garmin = MagicMock()
    client._logged_in = True
    client._garmin.get_activity_details.return_value = payload["details"]
    client._garmin.get_activity_splits.return_value = payload["splits"]
    client._garmin.get_activity.return_value = payload["activity"]
    return client


def test_ref_run_pauses_moving_time_and_distance(tmp_path: Path) -> None:
    client = _client_from_fixture(RUN_ID, tmp_path)

    result = an.analyse(client.get_activity_details(RUN_ID), fc_seuil=177)
    vue = result["vue_ensemble"]

    pauses = [(p["debut_s"], p["fin_s"]) for p in vue["pauses"]]
    assert len(pauses) == 2
    for (start, end), (ref_start, ref_end) in zip(pauses, [(2558, 2692), (2832, 2921)]):
        assert abs(start - ref_start) <= 3 and abs(end - ref_end) <= 3
    assert vue["duree_ecoulee_s"] == pytest.approx(3306, abs=5)
    assert vue["duree_mouvement_s"] == pytest.approx(51 * 60, abs=90)  # Garmin : 51 min
    assert vue["distance_km"] == pytest.approx(10.18, abs=0.1)
    assert all(r["baisse_60s_bpm"] is None or isinstance(r["baisse_60s_bpm"], int) for r in result["recuperations"])


def test_ref_swim_72_lengths_and_suspects(tmp_path: Path) -> None:
    client = _client_from_fixture(SWIM_ID, tmp_path)

    result = swim.annotate(client.get_swim_splits(SWIM_ID))

    assert len(result["splits"]) == 72
    suspects = {s["length_idx"] for s in result["splits"] if s["suspect"]}
    assert {21, 59} <= suspects
    assert sum(b["nb_longueurs"] for b in result["blocks"]) == 72


EASY_RUN_ID = "24737274389"  # footing du 15/10 : 6,05 km, 9 arrêts, 100 % Z1


def test_ref_threshold_session_blocks(tmp_path: Path) -> None:
    client = _client_from_fixture(RUN_ID, tmp_path)

    result = an.analyse(client.get_activity_details(RUN_ID), fc_seuil=174)

    long_blocks = [b for b in result["blocs"] if b["duree_s"] >= 300]
    assert len(long_blocks) == 3
    for b in long_blocks:
        assert 4.2 <= b["allure"] <= 4.4  # 4:12-4:24/km
        assert b["fc_moy"] >= 160
    assert len(json.dumps(result)) < 8000


def test_ref_easy_run_with_stops_has_no_false_blocks(tmp_path: Path) -> None:
    client = _client_from_fixture(EASY_RUN_ID, tmp_path)
    summary = json.loads(gzip.open(FIXTURES / f"{EASY_RUN_ID}.json.gz", "rt").read())["activity"]["summaryDTO"]

    result = an.analyse(client.get_activity_details(EASY_RUN_ID), fc_seuil=174)
    vue = result["vue_ensemble"]

    assert result["blocs"] == []
    assert vue["seance_continue"] is True
    assert len(vue["pauses"]) == 9
    assert vue["duree_ecoulee_s"] == pytest.approx(summary["elapsedDuration"], abs=2)
    assert vue["duree_mouvement_s"] == pytest.approx(summary["movingDuration"], abs=40)
    assert vue["distance_km"] == pytest.approx(summary["distance"] / 1000, abs=0.02)
    # Forcer le type reste possible.
    assert an.analyse(client.get_activity_details(EASY_RUN_ID), 174, type="fractionne")["blocs"]


def test_ref_easy_run_empty_weather_is_no_data(tmp_path: Path) -> None:
    client = _client_from_fixture(EASY_RUN_ID, tmp_path)
    client._garmin.get_activity_weather.return_value = {
        "temp": None,
        "windSpeed": None,
        "latitude": 0.0,
        "longitude": 0.0,
        "weatherStationDTO": None,
        "weatherTypeDTO": None,
    }

    weather = client.get_activity_weather(EASY_RUN_ID)

    assert weather["status"] == "no_data"
    assert weather["station_distance_km"] is None
    assert weather["station_far"] is None
