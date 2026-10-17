"""Vélo route sans capteur de puissance : sport, vitesse km/h, distance officielle, fusion
des pauses pour le découplage, paramètre `type` explicite — et non-régression course/nage."""

from __future__ import annotations

import gzip
import json
import random
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.analysis import series as an
from src.db import get_connection, init_db
from src.garmin_client import GarminClient
from src.tools import garmin_tools as gt
from tests.synthetic import interval_run, to_garmin_details

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
BIKE_0920 = "24435612737"
BIKE_0913 = "24343455443"
RUN_ID = "24737274389"
SWIM_ID = "24752973255"


def _client(tmp_path) -> GarminClient:
    client = GarminClient("a@b.com", "pw", str(tmp_path / "tokens"))
    client._garmin = MagicMock()
    client._logged_in = True
    return client


def bike_ride(
    seed: int = 4,
    pauses: tuple[tuple[int, int], ...] = ((1500, 30), (2400, 90)),
    drift: int = 12,
    end: int = 4800,
    speed_fn=None,
    elev_fn=None,
) -> dict:
    """Sortie vélo synthétique (par défaut 80 min à ~25 km/h, plate), FC qui dérive de `drift`
    bpm, avec des pauses (début, durée) ; `speed_fn(t)` / `elev_fn(t)` remplacent la vitesse
    de base et l'altitude. Séries au format GarminClient.get_activity_details."""
    rng = random.Random(seed)
    time_s, hr, pace, elev, dist = [], [], [], [], []
    t, d = 0, 0.0
    while t <= end:
        pause = next((p for p in pauses if p[0] < t < p[0] + p[1]), None)
        if pause:
            t = pause[0] + pause[1]
            continue
        speed = (speed_fn(t) if speed_fn else 25) + rng.uniform(-1.5, 1.5)
        time_s.append(t)
        pace.append(round(60 / speed, 3))
        hr.append(135 + drift * t // end + rng.randint(-2, 2))
        elev.append(round(elev_fn(t), 1) if elev_fn else 50.0)
        d += speed / 3.6 * (1 if time_s[-1] - (time_s[-2] if len(time_s) > 1 else 0) <= 15 else 0)
        dist.append(round(d, 1))
        t += 1
    return {"time_s": time_s, "heart_rate_bpm": hr, "pace_min_per_km": pace, "elevation_m": elev, "distance_m": dist}


# -- A. détection du sport -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("type_key", "sport"),
    [
        ("cycling", "velo"),
        ("road_biking", "velo"),
        ("gravel_cycling", "velo"),
        ("mountain_biking", "velo"),
        ("virtual_ride", "velo"),
        ("indoor_cycling", "velo"),
        ("running", "course"),
        ("trail_running", "course"),
        ("lap_swimming", "nage"),
        ("hiking", "hiking"),
    ],
)
def test_sport_detection(tmp_path, type_key: str, sport: str) -> None:
    client = _client(tmp_path)
    client._garmin.get_activity.return_value = {
        "activityTypeDTO": {"typeKey": type_key},
        "summaryDTO": {"distance": 40000.0, "duration": 6197.2, "movingDuration": 5968.0, "elapsedDuration": 6740.5,
                       "averageSpeed": 6.454, "maxSpeed": 10.534},
    }

    summary = client.get_activity_summary("1")

    assert summary["sport"] == sport
    assert summary["type_garmin"] == type_key
    assert summary["distance_km"] == 40.0
    assert summary["duree_s"] == 6197.2
    assert summary["vitesse_moy_kmh"] == 23.2
    assert summary["vitesse_max_kmh"] == 37.9


def test_summary_missing_values_are_null(tmp_path) -> None:
    client = _client(tmp_path)
    client._garmin.get_activity.return_value = {"activityTypeDTO": {"typeKey": "cycling"}, "summaryDTO": {"distance": 0}}

    summary = client.get_activity_summary("1")

    assert summary["distance_km"] is None
    assert summary["vitesse_max_kmh"] is None
    assert summary["duree_s"] is None


# -- B. activités récentes : vitesse km/h ------------------------------------------------------


def test_recent_activities_bike_speed(tmp_path) -> None:
    client = _client(tmp_path)
    client._garmin.get_activities.return_value = [
        {"activityId": 1, "startTimeLocal": "2026-09-20 17:33:00", "activityType": {"typeKey": "road_biking"},
         "distance": 39999.48, "duration": 6197.218, "maxSpeed": 10.534},
        {"activityId": 2, "startTimeLocal": "2026-09-13 10:05:00", "activityType": {"typeKey": "cycling"},
         "distance": 40780.23, "duration": 5298.452},
        {"activityId": 3, "startTimeLocal": "2026-10-15 19:41:00", "activityType": {"typeKey": "running"},
         "distance": 6045.3, "duration": 2361.9, "averageSpeed": 2.56},
    ]
    from datetime import date

    acts = {a["activity_id"]: a for a in client.get_recent_activities(date(2026, 9, 1), fetch_limit=20)}

    assert acts[1]["vitesse_moyenne_kmh"] == 23.2
    assert acts[1]["vitesse_max_kmh"] == 37.9
    assert acts[2]["vitesse_moyenne_kmh"] == 27.7
    assert acts[2]["vitesse_max_kmh"] is None  # absente du résumé : null, jamais 0
    assert acts[1]["allure_moyenne_min_km"] is None  # champs existants conservés
    assert "vitesse_moyenne_kmh" not in acts[3]  # course strictement inchangée


# -- fusion des pauses et découplage vélo ---------------------------------------------------------


def test_merged_segments() -> None:
    t = list(range(0, 100)) + list(range(130, 200)) + list(range(400, 500))  # pauses 30 s et 200 s
    segs = an.continuous_segments(t)

    merged, absorbed = an.merged_segments(t, segs, 120)

    assert len(segs) == 3
    assert [(t[i], t[j]) for i, j in merged] == [(0, 199), (400, 499)]
    assert absorbed == [[{"debut_s": 99, "duree_s": 31}], []]
    assert len(an.merged_segments(t, segs, 300)[0]) == 1
    assert len(an.merged_segments(t, segs, 0)[0]) == 3


def test_bike_decoupling_through_short_pauses() -> None:
    result = an.analyse(bike_ride(), 160, sport="velo")
    derive = result["derive_cardiaque"]

    assert derive is not None
    assert derive["pause_merge_s"] == 120
    assert [p["duree_s"] for p in derive["pauses_fusionnees"]] == [30, 90]
    assert derive["debut_s"] == 0 and derive["fin_s"] == 4800
    assert derive["decouplage_pct"] > 2  # FC qui dérive à vitesse constante
    assert "sans correction de pente" in derive["methode"]


def test_bike_decoupling_null_without_merge() -> None:
    assert an.analyse(bike_ride(), 160, sport="velo", pause_merge_s=0)["derive_cardiaque"] is None


def test_bike_negative_decoupling_is_not_stable() -> None:
    # -7,9 % : fiable mais négatif marqué (pas « stable »).
    derive = an.analyse(bike_ride(drift=-20), 160, sport="velo")["derive_cardiaque"]
    assert derive["fiable"] is True
    assert derive["lecture"].startswith("negatif marque")


def test_course_default_has_no_merge() -> None:
    """Course : comportement historique (aucune fusion) tant que pause_merge_s n'est pas donné."""
    result = an.analyse(interval_run(), 177, sport="course")
    assert "pause_merge_s" not in result["parametres"]
    assert result["derive_cardiaque"] is None


# -- distance officielle, km/h, type, fc_seuil -----------------------------------------------------


def test_bike_overview_in_kmh_with_official_distance() -> None:
    summary = {"sport": "velo", "distance_km": 33.5, "duree_s": 4710.0, "vitesse_max_kmh": 41.2}
    vue = an.analyse(bike_ride(), 160, sport="velo", summary=summary)["vue_ensemble"]

    assert vue["distance_km"] == 33.5
    assert vue["distance_officielle_km"] == 33.5
    assert vue["distance_integree_km"] != 33.5
    assert vue["source_distance"] == "garmin_officielle"
    assert vue["vitesse_moy_kmh"] == round(33.5 / 4710 * 3600, 1)
    assert vue["vitesse_max_kmh"] == 41.2
    assert not [k for k in vue if "mmss" in k or "allure" in k or "pace" in k or k.startswith("bascule_effort_recup") and not k.endswith("_kmh")]


def test_course_keeps_fields_and_adds_official_distance() -> None:
    before = an.analyse(interval_run(), 177)
    after = an.analyse(interval_run(), 177, sport="course", summary={"distance_km": 10.18})

    assert after["vue_ensemble"]["distance_officielle_km"] == 10.18
    assert after["vue_ensemble"]["distance_km"] == before["vue_ensemble"]["distance_km"]
    for key, value in before["vue_ensemble"].items():
        assert after["vue_ensemble"][key] == value


def test_type_is_explicit() -> None:
    auto = an.analyse(bike_ride(), 160, sport="velo")["type_applique"]
    endurance = an.analyse(bike_ride(), 160, sport="velo", type="endurance")["type_applique"]
    forced = an.analyse(interval_run(), 177, type="fractionne")["type_applique"]
    run_auto = an.analyse(interval_run(), 177)["type_applique"]

    assert auto["detection_blocs"] == "automatique" and auto["allure_bimodale"] is False
    assert "identique" in auto["effet"]
    assert endurance["detection_blocs"] == "desactivee"
    assert forced["detection_blocs"] == "forcee"
    assert run_auto["allure_bimodale"] is True and run_auto["effet"] == "5 bloc(s) d'effort retenu(s)"


def test_bike_blocks_in_kmh_with_target() -> None:
    ride = bike_ride(pauses=())
    # 4 efforts de 3 min à ~35 km/h.
    for start in (600, 1200, 1800, 2400):
        for i, t in enumerate(ride["time_s"]):
            if start <= t < start + 180:
                ride["pace_min_per_km"][i] = round(60 / 35, 3)
                ride["heart_rate_bpm"][i] = 165
    ride["distance_m"] = [None] * len(ride["time_s"])  # distance recalculée depuis la vitesse modifiée

    result = an.analyse(ride, 160, sport="velo", vitesse_cible_kmh=35)

    assert len(result["blocs"]) == 4
    bloc = result["blocs"][0]
    assert 34 <= bloc["vitesse_kmh"] <= 36
    assert bloc["verdict"] == "conforme"
    assert not [k for k in bloc if "allure" in k]
    assert result["fc_seuil_utilisee"] == 160


# -- outils MCP ------------------------------------------------------------------------------------


@pytest.fixture
def bike_tools(tmp_path):
    client = _client(tmp_path)
    client._garmin.get_activity_details.return_value = to_garmin_details(bike_ride())
    client._garmin.get_activity.return_value = {
        "activityTypeDTO": {"typeKey": "road_biking"},
        "summaryDTO": {"distance": 33500.0, "duration": 4710.0, "maxSpeed": 11.4},
    }
    conn = get_connection(":memory:")
    init_db(conn)
    yield gt.build_garmin_tools(client, conn), client
    conn.close()


async def test_details_tool_bike_speed_series(bike_tools) -> None:
    tools, client = bike_tools

    full = await tools["garmin_get_activity_details"]("1")
    small = await tools["garmin_get_activity_details"]("1", resolution_s=5)

    for result in (full, small):
        assert result["sport"] == "velo"
        assert len(result["speed_kmh"]) == len(result["time_s"])
        assert 20 <= result["speed_kmh"][10] <= 30
        assert "pace_min_per_km" in result  # conservée pour compatibilité
    client._garmin.get_activity.assert_called_once()  # résumé mis en cache


async def test_analyse_tool_bike(bike_tools) -> None:
    tools, _ = bike_tools

    result = await tools["garmin_analyse_activity"]("1", fc_seuil=160, vitesse_cible_kmh=25)

    assert result["sport"] == "velo"
    assert result["vue_ensemble"]["vitesse_moy_kmh"] == round(33.5 / 4710 * 3600, 1)
    assert result["parametres"]["pause_merge_s"] == 120
    assert "time_s" not in result and len(json.dumps(result)) < 8000


async def test_analyse_tool_summary_unavailable_falls_back(tmp_path) -> None:
    from garminconnect import GarminConnectConnectionError

    client = _client(tmp_path)
    client._garmin.get_activity_details.return_value = to_garmin_details(interval_run())
    client._garmin.get_activity.side_effect = GarminConnectConnectionError("timeout")
    conn = get_connection(":memory:")
    init_db(conn)
    tools = gt.build_garmin_tools(client, conn)

    result = await tools["garmin_analyse_activity"]("1", fc_seuil=177)

    assert result["status"] == "ok"
    assert result["sport"] is None
    assert "allure_moy_mmss" in result["vue_ensemble"]


@pytest.mark.parametrize("kwargs", [{"pause_merge_s": -1}, {"vitesse_cible_kmh": 0}])
async def test_analyse_tool_invalid_params(bike_tools, kwargs) -> None:
    tools, _ = bike_tools
    result = await tools["garmin_analyse_activity"]("1", fc_seuil=160, **kwargs)
    assert result["error_type"] == "invalid_input"


# -- critères d'acceptation sur les sorties réelles (fixtures locales) --------------------------------


def _real_tools(*ids: str) -> dict:
    missing = [a for a in ids if not (FIXTURES / f"{a}.json.gz").exists()]
    if missing:
        pytest.skip(f"fixtures réelles absentes : {missing} (cf. scripts/capture_fixtures.py)")
    sys.path.insert(0, str(ROOT / "scripts"))
    from snapshot_outputs import client_for

    conn = get_connection(":memory:")
    init_db(conn)
    return gt.build_garmin_tools(client_for(*ids), conn)


async def test_acceptance_bike_0920() -> None:
    tools = _real_tools(BIKE_0920)

    result = await tools["garmin_analyse_activity"](BIKE_0920, fc_seuil=160)
    vue, derive = result["vue_ensemble"], result["derive_cardiaque"]

    assert result["sport"] == "velo"
    assert vue["vitesse_moy_kmh"] == pytest.approx(23.2, abs=0.1)
    assert vue["distance_km"] == 40.0
    assert derive is not None
    assert derive["duree_s"] == pytest.approx(72 * 60, abs=60)
    assert [p["duree_s"] for p in derive["pauses_fusionnees"]] == [23, 50]
    assert not [k for k in vue if "mmss" in k or "allure" in k or "pace" in k]


async def test_acceptance_bike_0913() -> None:
    tools = _real_tools(BIKE_0913)

    default = await tools["garmin_analyse_activity"](BIKE_0913, fc_seuil=160)
    explicit = await tools["garmin_analyse_activity"](BIKE_0913, fc_seuil=160, pause_merge_s=120)

    assert default["vue_ensemble"]["vitesse_moy_kmh"] == pytest.approx(27.7, abs=0.1)
    # Pauses de 408 s et 175 s. Mode auto (défaut) : la fenêtre à 120 s ne couvre que 51 % de
    # la sortie, la fusion est élargie jusqu'à couvrir toute la sortie. Avec 120 s explicite,
    # la fenêtre reste 0-2721 s, signalée non fiable (couverture < 2/3).
    assert default["derive_cardiaque"]["duree_mouvement_s"] > 2721
    assert (explicit["derive_cardiaque"]["debut_s"], explicit["derive_cardiaque"]["fin_s"]) == (0, 2721)
    assert explicit["derive_cardiaque"]["fiable"] is False


# -- non-régression course / nage (snapshots figés avant la modification) ---------------------------


def _contains(old, new, path="") -> None:
    if isinstance(old, dict):
        assert isinstance(new, dict), path
        for key, value in old.items():
            assert key in new, f"clé retirée : {path}.{key}"
            _contains(value, new[key], f"{path}.{key}")
    elif isinstance(old, list):
        assert isinstance(new, list) and len(new) == len(old), path
        for i, (a, b) in enumerate(zip(old, new)):
            _contains(a, b, f"{path}[{i}]")
    else:
        assert old == new, f"{path} : {old!r} -> {new!r}"


async def test_course_and_swim_outputs_unchanged() -> None:
    snapshot_path = FIXTURES / "snapshots_course_nage.json"
    if not snapshot_path.exists():
        pytest.skip("snapshots absents (cf. scripts/snapshot_outputs.py)")
    sys.path.insert(0, str(ROOT / "scripts"))
    from snapshot_outputs import capture

    before = json.loads(snapshot_path.read_text(encoding="utf-8"))
    after = await capture()

    for name, old in before.items():
        _contains(old, after[name], name)
    # Swim : strictement identique (aucune clé ajoutée).
    assert after["analyse_swim"] == before["analyse_swim"]
    assert after["swim_splits"] == before["swim_splits"]
    assert after["analyse_course"]["sport"] == "course"


# -- fiabilité du découplage vélo -------------------------------------------------------------------


def test_half_metrics() -> None:
    # 1re moitié : montée régulière de 150 m à 22 km/h ; 2e moitié : plat à 28 km/h.
    ride = bike_ride(
        pauses=(),
        speed_fn=lambda t: 22 if t < 2400 else 28,
        elev_fn=lambda t: 50 + 150 * min(t, 2400) / 2400,
    )

    derive = an.analyse(ride, 160, sport="velo")["derive_cardiaque"]
    first, second = derive["moities"]["premiere"], derive["moities"]["seconde"]

    assert first["vitesse_moy_kmh"] == pytest.approx(22, abs=0.3)
    assert second["vitesse_moy_kmh"] == pytest.approx(28, abs=0.3)
    assert first["dplus_m"] == pytest.approx(150, abs=2)
    assert second["dplus_m"] == 0
    assert first["altitude_moy_m"] == pytest.approx(125, abs=2)
    assert second["altitude_moy_m"] == 200
    assert first["duree_mouvement_s"] == pytest.approx(second["duree_mouvement_s"], abs=1)


def test_reliable_ride() -> None:
    derive = an.analyse(bike_ride(), 160, sport="velo")["derive_cardiaque"]

    assert derive["fiable"] is True
    assert derive["raisons_fiabilite"] == []
    assert derive["lecture"] == "stable"
    assert derive["couverture_pct"] == 100.0
    assert "sans correction de pente" in derive["methode"]


def test_rule_a_large_decoupling() -> None:
    derive = an.analyse(bike_ride(drift=-30), 160, sport="velo")["derive_cardiaque"]

    assert derive["decouplage_pct"] < -10
    assert derive["fiable"] is False
    assert derive["lecture"] == "non interprétable"
    assert any("plage plausible" in r for r in derive["raisons_fiabilite"])


def test_rule_b_speed_gap() -> None:
    ride = bike_ride(pauses=(), drift=0, speed_fn=lambda t: 22 if t < 2400 else 27)

    derive = an.analyse(ride, 160, sport="velo")["derive_cardiaque"]

    assert derive["fiable"] is False
    assert any("vitesse moyenne" in r for r in derive["raisons_fiabilite"])
    assert derive["decouplage_pct"] is not None  # conservé, même non interprétable


def test_rule_c_asymmetric_profile() -> None:
    climb = bike_ride(pauses=(), elev_fn=lambda t: 50 + 120 * min(t, 2400) / 2400)
    # Une bosse de 10 m puis une de 25 m : rapport > 2 mais écart < 50 m (terrain plat).
    bumps = bike_ride(pauses=(), elev_fn=lambda t: 50 + (10 if 600 < t < 900 else 25 if 3000 < t < 3600 else 0))

    asym = an.analyse(climb, 160, sport="velo")["derive_cardiaque"]
    flat = an.analyse(bumps, 160, sport="velo")["derive_cardiaque"]

    assert any("asymétrique" in r for r in asym["raisons_fiabilite"])
    assert not any("asymétrique" in r for r in flat["raisons_fiabilite"])


def test_rule_d_low_coverage_with_explicit_merge() -> None:
    # 100 min, pauses de 200 s à 30 min et 75 min : plus long segment 45 min = 45 %.
    ride = bike_ride(pauses=((1800, 200), (4700, 200)), end=6400)

    derive = an.analyse(ride, 160, sport="velo", pause_merge_s=120)["derive_cardiaque"]

    assert derive["pause_merge_mode"] == "explicite"
    assert derive["fusion_elargie"] is False
    assert derive["couverture_pct"] < 50
    assert derive["fiable"] is False
    assert any("couvrant" in r for r in derive["raisons_fiabilite"])


def test_auto_merge_widens_by_steps() -> None:
    ride = bike_ride(pauses=((1800, 200), (4700, 200)), end=6400)

    derive = an.analyse(ride, 160, sport="velo")["derive_cardiaque"]

    assert derive["pause_merge_mode"] == "auto"
    assert derive["pause_merge_s"] == 300  # premier palier suffisant
    assert derive["fusion_elargie"] is True
    assert [p["duree_s"] for p in derive["pauses_fusionnees"]] == [200, 200]
    assert derive["couverture_pct"] == 100.0


def test_auto_merge_ceiling() -> None:
    # Pauses de 15 min : aucun palier (300 s, 600 s) ne les absorbe ; plafond 600 s.
    ride = bike_ride(pauses=((1800, 900), (5400, 900)), end=8100)

    derive = an.analyse(ride, 160, sport="velo")["derive_cardiaque"]

    assert derive["pause_merge_s"] == 600
    assert derive["fusion_elargie"] is True
    assert derive["pauses_fusionnees"] == []
    assert derive["couverture_pct"] < 50
    assert derive["fiable"] is False


def test_auto_merge_not_widened_for_short_ride() -> None:
    # 55 min en mouvement (< 60 min) : pas d'élargissement, même avec une couverture faible.
    ride = bike_ride(pauses=((1500, 300),), end=3600, drift=0)

    result = an.analyse(ride, 160, sport="velo")

    assert result["derive_cardiaque"] is None  # plus long segment < 40 min
    assert result["parametres"]["pause_merge_s"] == 120


def test_explicit_merge_kept_as_is() -> None:
    ride = bike_ride(pauses=((1800, 200), (4700, 200)), end=6400)

    result = an.analyse(ride, 160, sport="velo", pause_merge_s=250)
    derive = result["derive_cardiaque"]

    assert derive["pause_merge_s"] == 250
    assert derive["pause_merge_mode"] == "explicite"
    assert derive["fusion_elargie"] is False
    assert result["parametres"]["pause_merge_mode"] == "explicite"


def test_course_decoupling_has_no_new_keys() -> None:
    from tests.synthetic import endurance_run

    derive = an.analyse(endurance_run(), 177, sport="course")["derive_cardiaque"]

    assert set(derive) == {"debut_s", "duree_s", "decouplage_pct", "lecture"}


async def test_acceptance_reliability_0920() -> None:
    tools = _real_tools(BIKE_0920)

    derive = (await tools["garmin_analyse_activity"](BIKE_0920, fc_seuil=160))["derive_cardiaque"]

    assert derive["fiable"] is False
    assert derive["raisons_fiabilite"]
    assert derive["lecture"] == "non interprétable"
    assert derive["decouplage_pct"] == pytest.approx(-19.6, abs=0.1)
    assert derive["couverture_pct"] == pytest.approx(68.5, abs=0.5)


async def test_acceptance_reliability_0913() -> None:
    tools = _real_tools(BIKE_0913)

    derive = (await tools["garmin_analyse_activity"](BIKE_0913, fc_seuil=160))["derive_cardiaque"]

    # Fenêtre par défaut 0-2721 s = 51,3 % du temps en mouvement (< 2/3) : le mode auto élargit.
    # 300 s absorbe la pause de 175 s sans dépasser le 1er morceau ; 600 s absorbe aussi celle
    # de 408 s et couvre toute la sortie.
    assert derive["fusion_elargie"] is True
    assert derive["pause_merge_s"] == 600
    assert [p["duree_s"] for p in derive["pauses_fusionnees"]] == [408, 175]
    assert (derive["debut_s"], derive["fin_s"]) == (0, 5882)
    assert derive["couverture_pct"] == 100.0
    assert derive["decouplage_pct"] == pytest.approx(-4.5, abs=0.1)
    assert derive["fiable"] is True
