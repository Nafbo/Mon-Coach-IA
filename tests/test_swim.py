"""P6 : blocs de natation et longueurs aberrantes (séance calquée sur 24748453127)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.analysis import swim
from src.db import get_connection, init_db
from src.garmin_client import GarminClient
from src.tools import garmin_tools as gt
from tests.synthetic import SWIM_SUSPECTS_REF, swim_session


def test_p6_blocks_follow_rests() -> None:
    blocks = swim.annotate(swim_session())["blocks"]

    assert [b["nb_longueurs"] for b in blocks] == [8] + [4] * 8 + [16, 16]
    assert sum(b["distance_m"] for b in blocks) == 72 * 25
    assert blocks[0]["repos_apres_s"] == pytest.approx(30, abs=1)
    assert blocks[1]["repos_apres_s"] == pytest.approx(20, abs=1)
    assert blocks[-1]["repos_apres_s"] is None
    assert (blocks[1]["premiere_longueur"], blocks[1]["derniere_longueur"]) == (9, 12)


def test_p6_block_metrics() -> None:
    b = swim.annotate(swim_session())["blocks"][9]  # 400 m à ~28 s / 25 m

    assert b["allure_mmss"] == "1:52"
    assert 130 <= b["fc_moy"] <= 145
    assert 43 <= b["swolf_moy"] <= 45
    assert b["nages"] == {"FREESTYLE": 16}


def test_p6_reference_suspect_lengths_flagged_not_removed() -> None:
    result = swim.annotate(swim_session())
    splits = result["splits"]

    assert len(splits) == 72
    flagged = {s["length_idx"]: s["suspect_reason"] for s in splits if s["suspect"]}
    assert flagged == {21: "too_fast", 59: "too_fast"}
    for idx, duration in SWIM_SUSPECTS_REF.items():
        assert splits[idx - 1]["duration_s"] == duration
    assert result["blocks"][4]["nb_suspectes"] == 1  # 5e bloc = longueurs 21-24


def test_p6_slow_length_flagged() -> None:
    splits = swim_session()
    splits[40]["duration_s"] = 45.0  # virage manqué : deux longueurs comptées en une

    flagged = {s["length_idx"]: s["suspect_reason"] for s in swim.annotate(splits)["splits"] if s["suspect"]}

    assert flagged[41] == "too_slow"


def test_p6_short_block_uses_session_reference() -> None:
    splits = swim_session()[:3]
    splits[1]["start_s"] = 200  # isole la longueur 2 dans son propre bloc
    splits[2]["start_s"] = 400
    splits[1]["duration_s"] = 15.0

    out = swim.annotate(splits)["splits"]

    assert out[1]["block"] == 2
    assert out[1]["suspect"] is True


def test_p6_empty() -> None:
    result = swim.annotate([])
    assert result["splits"] == [] and result["blocks"] == []
    assert result["totaux"]["temps_nage_s"] == 0


async def test_p6_tool_returns_blocks() -> None:
    conn = get_connection(":memory:")
    init_db(conn)
    fake = MagicMock(spec=GarminClient)
    fake.get_swim_splits.return_value = swim_session()
    tools = gt.build_garmin_tools(fake, conn)

    first = await tools["garmin_get_swim_splits"]("24748453127")
    second = await tools["garmin_get_swim_splits"]("24748453127")

    assert first["status"] == "ok"
    assert len(first["blocks"]) == 11
    assert sum(s["suspect"] for s in first["splits"]) == 2
    assert second == first
    fake.get_swim_splits.assert_called_once()
    conn.close()


def test_p6_raw_cache_unchanged() -> None:
    """Les longueurs mises en cache restent brutes (annotation recalculée à chaque appel)."""
    splits = swim_session()
    swim.annotate(splits)
    assert "suspect" not in splits[0]

