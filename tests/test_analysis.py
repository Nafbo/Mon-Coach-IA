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
from tests.synthetic import END_REF, PAUSES_REF, endurance_run, interval_run, to_garmin_details

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


# -- P2 : bugs portés depuis analyse_series.py -------------------------------------


def test_p2_pauses_excluded_from_duration() -> None:
    vue = an.analyse(interval_run(), FC_SEUIL)["vue_ensemble"]

    assert vue["duree_ecoulee_s"] == END_REF
    assert vue["duree_mouvement_s"] == END_REF - 134 - 89 == 3083  # 51:23, cohérent avec Garmin (51 min)
    assert vue["duree_s"] == vue["duree_mouvement_s"]
    assert [(p["debut_s"], p["fin_s"]) for p in vue["pauses"]] == PAUSES_REF


def test_p2_pauses_excluded_from_distance() -> None:
    details = interval_run()
    t, pa = details["time_s"], details["pace_min_per_km"]
    # Ancien calcul (bug) : chaque pas, pause comprise, compté à l'allure du point.
    buggy = sum(((t[k + 1] - t[k]) / 60.0) / pa[k] for k in range(len(t) - 1))
    expected = sum(((t[k + 1] - t[k]) / 60.0) / pa[k] for k in range(len(t) - 1) if t[k + 1] - t[k] <= 15)

    vue = an.analyse(details, FC_SEUIL)["vue_ensemble"]

    assert vue["distance_km"] == pytest.approx(expected, abs=0.01)
    assert buggy - vue["distance_km"] > 0.6  # 223 s de pause ~ 0,65 km fantômes
    assert vue["source_distance"] == "integration_allure"


def test_p2_garmin_distance_preferred_when_available(tmp_path) -> None:
    client = GarminClient("a@b.com", "pw", str(tmp_path / "tokens"))
    client._garmin = MagicMock()
    client._logged_in = True
    client._garmin.get_activity_details.return_value = to_garmin_details(interval_run())

    details = client.get_activity_details("24725112038")
    vue = an.analyse(details, FC_SEUIL)["vue_ensemble"]

    assert vue["source_distance"] == "garmin"
    assert vue["distance_km"] == pytest.approx(details["distance_m"][-1] / 1000, abs=0.01)


def test_p2_recovery_sign_when_hr_rises() -> None:
    details = interval_run()
    # FC qui remonte pendant les récupérations (ex. côte pendant le trot).
    details["heart_rate_bpm"] = [
        185 if 900 <= t < 2400 and (t - 900) % 300 >= 180 else h
        for t, h in zip(details["time_s"], details["heart_rate_bpm"])
    ]

    recup = an.analyse(details, FC_SEUIL)["recuperations"][0]

    assert recup["baisse_60s_bpm"] < 0
    assert recup["fc_remonte"] is True
    assert recup["fc_60s"] > recup["fc_fin_bloc"]


def test_p2_recovery_flags_pause() -> None:
    details = interval_run()
    # Pause de 40 s au milieu de la 1re récupération (1080 -> 1200 s).
    keep = [i for i, t in enumerate(details["time_s"]) if not 1120 < t < 1160]
    details = {k: [v[i] for i in keep] for k, v in details.items()}

    recups = an.analyse(details, FC_SEUIL)["recuperations"]

    assert recups[0]["inclut_pause"] is True
    assert recups[0]["duree_s"] < 120 - 30
    assert recups[1]["inclut_pause"] is False


def test_p2_no_false_blocks_on_continuous_endurance() -> None:
    result = an.analyse(endurance_run(), FC_SEUIL)

    assert result["blocs"] == []
    assert result["recuperations"] == []
    assert result["vue_ensemble"]["seance_continue"] is True
    assert result["derive_cardiaque"]["duree_s"] == 3000


@pytest.mark.parametrize("seed", range(5))
def test_p2_no_false_blocks_across_seeds(seed: int) -> None:
    assert an.analyse(endurance_run(seed=seed), FC_SEUIL)["blocs"] == []


def test_p2_type_overrides_detection() -> None:
    assert an.analyse(interval_run(), FC_SEUIL, type="endurance")["blocs"] == []
    assert len(an.analyse(interval_run(), FC_SEUIL, type="fractionne")["blocs"]) == 5
    # Pas de découplage calculé sur un segment mêlant efforts et récups.
    assert an.analyse(interval_run(), FC_SEUIL)["derive_cardiaque"] is None
