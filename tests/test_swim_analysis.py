"""garmin_analyse_swim : analyse de natation côté serveur (mission du 17/10/2026)."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from garminconnect import GarminConnectAuthenticationError, GarminConnectConnectionError

from src.analysis import swim
from src.db import get_connection, init_db
from src.garmin_client import GarminClient
from src.tools import garmin_tools as gt

FIXTURES = Path(__file__).parent / "fixtures"
CSS_TEST_ID = "24752973255"  # test CSS du 17/10 : 400 m en 6:45, 200 m en 3:20


def _length(idx: int, start: float, duration: float, hr: int = 140) -> dict:
    return {
        "length_idx": idx,
        "start_s": int(start),
        "duration_s": duration,
        "distance_m": 25.0,
        "avg_hr": hr,
        "swolf": int(duration) + 12,
        "stroke": "FREESTYLE",
        "pace_min_per_100m": round(duration / 25 * 100 / 60, 2),
    }


def _session(blocks: list[tuple[list[float], float]]) -> list[dict]:
    """[(durées des longueurs, repos après)] -> splits au format get_swim_splits."""
    out, t, idx = [], 0.0, 1
    for durations, rest in blocks:
        for d in durations:
            out.append(_length(idx, t, d))
            t += d
            idx += 1
        t += rest
    return out


# Reconstitution de la structure du test CSS du 17/10 (mêmes chronos de référence) :
# 400 m en 404,8 s dont 200,3 s sur le premier 200 m, puis 200 m en 200,1 s.
_LONG = [30.2, 21.4, 23.3, 25.6, 25.8, 22.7, 25.4, 25.9] + [25.4, 25.6, 25.5, 25.6, 25.6, 25.7, 25.5, 25.6]
_SHORT = [26.0, 24.5, 24.8, 25.1, 25.0, 24.9, 25.0, 24.8]


def css_session() -> list[dict]:
    warmup = [27.0] * 8
    return _session([(warmup, 60), ([27.5] * 4, 100), (_LONG, 270), (_SHORT, 120), ([28.3] * 12, 0)])


@pytest.fixture
def tools(tmp_path) -> tuple[dict, MagicMock]:
    fake = MagicMock(spec=GarminClient)
    conn = get_connection(":memory:")
    init_db(conn)
    yield gt.build_garmin_tools(fake, conn), fake
    conn.close()


# -- référence CSS ---------------------------------------------------------------------------


def test_css_reference_structure() -> None:
    assert round(sum(_LONG), 1) == 404.8 and round(sum(_LONG[:8]), 1) == 200.3 and round(sum(_SHORT), 1) == 200.1

    result = swim.analyse_swim(css_session(), target_css_min_per_100m=100 / 60)
    css = result["css"]

    assert result["status"] == "ok"
    assert (css["t_long_mmss"], css["t_short_mmss"]) == ("6:45", "3:20")
    assert css["css_mmss"] == "1:42"
    assert css["sensitivity_short_minus_5s_mmss"] == "1:45"
    assert css["control"]["short_vs_first_half_s"] == -0.2
    assert css["control"]["short_not_faster"] is True
    assert css["gap_s_per_100m"] == pytest.approx(2.4, abs=0.1)
    assert css["gap_s_over_1900m"] == 45
    assert result["suspect_lengths"] == []


def test_css_reference_real_session() -> None:
    path = FIXTURES / f"{CSS_TEST_ID}.json.gz"
    if not path.exists():
        pytest.skip("fixture réelle absente (cf. scripts/capture_fixtures.py)")
    payload = json.load(gzip.open(path, "rt", encoding="utf-8"))
    client = GarminClient("a@b.com", "pw", "unused")
    client._garmin = MagicMock()
    client._logged_in = True
    client._garmin.get_activity_splits.return_value = payload["splits"]

    result = swim.analyse_swim(client.get_swim_splits(CSS_TEST_ID))
    css = result["css"]

    assert (css["t_long_mmss"], css["t_short_mmss"]) == ("6:45", "3:20")
    assert css["css_mmss"] == "1:42"
    assert css["sensitivity_short_minus_5s_mmss"] == "1:45"
    assert css["control"]["short_not_faster"] is True
    assert result["suspect_lengths"] == []
    assert result["overview"]["distance_m"] == 1600
    # Allure sur le temps nagé (1715 s), pas sur le temps écoulé.
    assert result["overview"]["pace_mmss"] == "1:47"


def test_paces_use_swim_time_not_elapsed() -> None:
    result = swim.analyse_swim(css_session())
    ov = result["overview"]

    assert ov["rest_s"] == pytest.approx(ov["elapsed_s"] - ov["swim_time_s"], abs=0.2)
    assert ov["pace_min_per_100m"] == pytest.approx(ov["swim_time_s"] / ov["distance_m"] * 100 / 60, abs=0.01)
    assert result["blocks"][-1]["rest_after_s"] is None


def test_block_halves_and_drift() -> None:
    long_block = swim.analyse_swim(css_session())["blocks"][2]

    assert long_block["first_half_pace_mmss"] == "1:40"
    assert long_block["second_half_pace_mmss"] == "1:42"
    assert long_block["drift_pct"] == pytest.approx((204.5 - 200.3) / 200.3 * 100, abs=0.1)
    assert long_block["hr_avg"] == 140 and long_block["hr_max"] == 140


def test_output_has_no_length_list() -> None:
    result = swim.analyse_swim(css_session())
    assert "splits" not in result
    assert len(json.dumps(result)) < 6000


# -- cas limites ------------------------------------------------------------------------------


def test_session_without_css_blocks() -> None:
    result = swim.analyse_swim(_session([([27.0] * 4, 20)] * 6))

    assert result["status"] == "ok"
    assert result["css"] is None
    assert result["css_null_reason"] == "aucun bloc de 400 m dans la séance"


def test_400_without_following_200() -> None:
    result = swim.analyse_swim(_session([([27.0] * 4, 60), (_LONG, 0)]))
    assert result["css"] is None
    assert result["css_null_reason"] == "aucun bloc de 200 m après le bloc de 400 m"


def test_css_blocks_imposed() -> None:
    session = _session([([26.0] * 12, 120), ([25.0] * 4, 0)])  # 300 m puis 100 m

    result = swim.analyse_swim(session, css_blocks=[1, 2])

    assert result["css"]["d_long_m"] == 300 and result["css"]["d_short_m"] == 100
    assert result["css"]["css_s_per_100m"] == pytest.approx((312 - 100) / 200 * 100, abs=0.1)


def test_single_length_block() -> None:
    result = swim.analyse_swim(_session([([27.0] * 8, 60), ([15.0], 60), ([27.0] * 8, 0)]))
    single = result["blocks"][1]

    assert single["nb_lengths"] == 1
    assert single["drift_pct"] is None
    assert single["first_half_pace_mmss"] is None
    assert result["suspect_lengths"] == []  # bloc de moins de 4 longueurs : non testé


def test_aberrant_length() -> None:
    durations = [27.0, 27.2, 26.8, 18.0, 36.0, 27.1, 26.9, 27.0]
    result = swim.analyse_swim(_session([(durations, 0)]))

    flagged = {s["length_idx"]: s["reason"] for s in result["suspect_lengths"]}
    assert flagged == {4: "too_fast", 5: "too_slow"}
    assert result["suspect_lengths"][0]["block_median_s"] == 27.0
    assert result["blocks"][0]["suspect_count"] == 2


def test_real_session_24748453127_suspects() -> None:
    path = FIXTURES / "24748453127.json.gz"
    if not path.exists():
        pytest.skip("fixture réelle absente (cf. scripts/capture_fixtures.py)")
    payload = json.load(gzip.open(path, "rt", encoding="utf-8"))
    client = GarminClient("a@b.com", "pw", "unused")
    client._garmin = MagicMock()
    client._logged_in = True
    client._garmin.get_activity_splits.return_value = payload["splits"]

    result = swim.analyse_swim(client.get_swim_splits("24748453127"))
    flagged = {s["length_idx"] for s in result["suspect_lengths"]}

    # 21 (18,0 s) et sa jumelle 22 (36,1 s) : virage mal détecté. La longueur 59 (20,3 s,
    # écart 8,45 s) reste sous le seuil de la règle demandée dans son bloc (9,12 s).
    assert {21, 22} <= flagged
    assert 59 not in flagged


def test_no_lengths_is_no_data() -> None:
    assert swim.analyse_swim([])["status"] == "no_data"


# -- outil MCP --------------------------------------------------------------------------------


async def test_tool_ok_and_cache(tools) -> None:
    tool_map, fake = tools
    fake.get_swim_splits.return_value = css_session()

    first = await tool_map["garmin_analyse_swim"]("24752973255", target_css_min_per_100m=1.6667)
    await tool_map["garmin_get_swim_splits"]("24752973255")

    assert first["status"] == "ok"
    assert first["activity_id"] == "24752973255"
    assert first["css"]["css_mmss"] == "1:42"
    fake.get_swim_splits.assert_called_once()


async def test_tool_no_data(tools) -> None:
    tool_map, fake = tools
    fake.get_swim_splits.return_value = []

    result = await tool_map["garmin_analyse_swim"]("1")

    assert result["status"] == "no_data"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [(GarminConnectAuthenticationError("Token expired"), "auth"), (GarminConnectConnectionError("timeout"), "network")],
)
async def test_tool_access_error(tools, exc, expected) -> None:
    from src.garmin_client import GarminAuthenticationError, GarminUnavailableError

    tool_map, fake = tools
    fake.get_swim_splits.side_effect = (
        GarminAuthenticationError(str(exc)) if expected == "auth" else GarminUnavailableError(str(exc))
    )

    result = await tool_map["garmin_analyse_swim"]("1")

    assert result["status"] == "error"
    assert result["error"]["type"] == expected
    assert str(exc) in result["error"]["message"]


@pytest.mark.parametrize("css_blocks", [[1], [1, "2"], [9, 10]])
async def test_tool_invalid_css_blocks(tools, css_blocks) -> None:
    tool_map, fake = tools
    fake.get_swim_splits.return_value = css_session()

    result = await tool_map["garmin_analyse_swim"]("1", css_blocks=css_blocks)

    assert result["status"] == "error"
    assert result["error"]["type"] == "invalid_input"


# -- N2 : garmin_get_swim_splits enrichi --------------------------------------------------------


async def test_swim_splits_blocks_enriched(tools) -> None:
    tool_map, fake = tools
    fake.get_swim_splits.return_value = css_session()

    result = await tool_map["garmin_get_swim_splits"]("1")
    long_block = result["blocks"][2]

    assert long_block["swim_time_s"] == 404.8
    assert long_block["rest_after_s"] == pytest.approx(270, abs=1)
    assert (long_block["first_half_pace_mmss"], long_block["second_half_pace_mmss"]) == ("1:40", "1:42")
    # Champs historiques conservés.
    assert {"suspect", "suspect_reason", "ecart_reference_pct"} <= set(result["splits"][0])
    assert {"duree_nage_s", "repos_apres_s", "allure_mmss"} <= set(long_block)
