"""Tests de src/analysis/series.py et du tool garmin_analyse_activity (hors ligne)."""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock

import pytest

from src.analysis import series as an
from src.db import get_connection, init_db
from src.garmin_client import GarminAuthenticationError, GarminClient
from src.tools import garmin_tools as gt
from tests.synthetic import interval_run

FC_SEUIL = 177


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = get_connection(":memory:")
    init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def fake_client() -> MagicMock:
    return MagicMock(spec=GarminClient)


@pytest.fixture
def tools(fake_client: MagicMock, conn: sqlite3.Connection) -> dict:
    return gt.build_garmin_tools(fake_client, conn)


# -- P1 : analyse côté serveur ---------------------------------------------------


def test_p1_analyse_detects_interval_blocks() -> None:
    result = an.analyse(interval_run(), FC_SEUIL, allure_cible=4.25, type="fractionne")

    assert result["status"] == "ok"
    assert len(result["blocs"]) == 5
    for bloc in result["blocs"]:
        assert 170 <= bloc["duree_s"] <= 185
        assert bloc["allure_mmss"] in {"4:14", "4:15", "4:16"}
        assert bloc["verdict"] == "conforme"
    assert len(result["recuperations"]) == 4
    assert result["vue_ensemble"]["nb_segments"] == 3
    assert sum(z["pct"] for z in result["zones_fc"]) == pytest.approx(100, abs=1.5)


def test_p1_analyse_output_is_compact() -> None:
    details = interval_run()
    result = an.analyse(details, FC_SEUIL)
    # ~4 caractères par token : la réponse doit rester sous ~2 000 tokens, quand les
    # séries brutes en pèsent plusieurs dizaines de milliers.
    assert len(json.dumps(result)) < 8000
    assert len(json.dumps(details)) > 10 * len(json.dumps(result))


def test_p1_analyse_without_pace_is_no_data() -> None:
    result = an.analyse({"time_s": [0, 1, 2], "heart_rate_bpm": [100, 101, 102]}, FC_SEUIL)
    assert result["status"] == "no_data"


def test_p1_downsample_reduces_points_and_keeps_alignment() -> None:
    details = interval_run()
    small = an.downsample(details, 5)

    assert small["resolution_s"] == 5
    assert len(small["time_s"]) < len(details["time_s"]) / 4
    assert {len(small[k]) for k in ("time_s", "heart_rate_bpm", "pace_min_per_km", "elevation_m")} == {
        len(small["time_s"])
    }
    # Les pauses restent visibles dans le temps sous-échantillonné.
    assert 2560 not in small["time_s"]


async def test_p1_tool_uses_details_cache(tools: dict, fake_client: MagicMock) -> None:
    fake_client.get_activity_details.return_value = interval_run()

    first = await tools["garmin_analyse_activity"]("24725112038", fc_seuil=FC_SEUIL)
    await tools["garmin_get_activity_details"]("24725112038")
    await tools["garmin_analyse_activity"]("24725112038", fc_seuil=FC_SEUIL, type="seuil")

    assert first["status"] == "ok"
    assert first["activity_id"] == "24725112038"
    fake_client.get_activity_details.assert_called_once_with("24725112038")


async def test_p1_tool_rejects_invalid_type(tools: dict, fake_client: MagicMock) -> None:
    result = await tools["garmin_analyse_activity"]("1", fc_seuil=FC_SEUIL, type="tempo")
    assert result["error"] is True
    fake_client.get_activity_details.assert_not_called()


async def test_p1_tool_maps_garmin_errors(tools: dict, fake_client: MagicMock) -> None:
    fake_client.get_activity_details.side_effect = GarminAuthenticationError("bad creds")
    result = await tools["garmin_analyse_activity"]("1", fc_seuil=FC_SEUIL)
    assert result["error"] is True
    assert "Identifiants Garmin invalides" in result["message"]


async def test_p1_details_resolution_param(tools: dict, fake_client: MagicMock) -> None:
    fake_client.get_activity_details.return_value = interval_run()

    full = await tools["garmin_get_activity_details"]("1")
    small = await tools["garmin_get_activity_details"]("1", resolution_s=5)
    bad = await tools["garmin_get_activity_details"]("1", resolution_s=0)

    assert "resolution_s" not in full
    assert len(small["time_s"]) < len(full["time_s"])
    assert bad["error"] is True
