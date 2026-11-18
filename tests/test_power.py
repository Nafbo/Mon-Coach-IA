"""Analyse vélo en puissance (`src/analysis/power.py` via `series.analyse`) sur séries
synthétiques : puissance constante, rampe, intervalles, FC absente, échantillonnage espacé,
et non-régression des sorties sans puissance."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from src.analysis import power as pwr
from src.analysis.series import analyse
from src.garmin_client import GarminClient


def ride(
    power: list[int | None],
    *,
    hr: list[int | None] | None = None,
    step_s: int = 1,
    cadence: int | None = 90,
    with_power: bool = True,
) -> dict[str, Any]:
    """Séries au format de `get_activity_details` : vitesse virtuelle constante (30 km/h),
    terrain plat, un point toutes les `step_s` secondes."""
    n = len(power)
    out: dict[str, Any] = {
        "time_s": [k * step_s for k in range(n)],
        "heart_rate_bpm": hr if hr is not None else [None] * n,
        "pace_min_per_km": [2.0] * n,
        "elevation_m": [100.0] * n,
        "distance_m": [k * step_s * 30 / 3.6 for k in range(n)],
    }
    if with_power:
        out["power_w"] = power
        if cadence:
            out["cadence_rpm"] = [cadence] * n
    return out


def run(details: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return analyse(details, 165, sport="velo", **kw)


def test_constant_power_np_equals_average_and_if_tss() -> None:
    r = run(ride([200] * 3600, hr=[140] * 3600), ftp_w=200)
    v = r["vue_ensemble"]
    assert v["puissance_moy_w"] == 200 and v["puissance_normalisee_w"] == 200 and v["vi"] == 1.0
    assert v["if"] == 1.0 and v["tss"] == pytest.approx(100.0, abs=0.5)
    assert v["cadence_moy_rpm"] == 90 and v["travail_kj"] == 720
    assert r["puissance_disponible"] is True
    assert r["meilleurs_efforts_w"] == {"1min": 200, "5min": 200, "10min": 200, "20min": 200}
    z4 = next(z for z in r["zones_puissance"] if z["zone"].startswith("Z4"))
    assert z4["pct"] == 100.0 and sum(z["pct"] for z in r["zones_puissance"]) == pytest.approx(100)
    assert r["blocs"] == [] and r["type_applique"]["base_detection"] == "puissance"


def test_without_ftp_nothing_relative_to_ftp_is_computed() -> None:
    r = run(ride([200] * 1200, hr=[140] * 1200))
    v = r["vue_ensemble"]
    assert v["if"] is None and v["tss"] is None and v["ftp_utilisee_w"] is None
    assert "jamais devinée" in v["message_ftp"]
    assert r["zones_puissance"] is None
    assert v["puissance_moy_w"] == 200  # les watts restent calculés


def test_ramp_zones_and_best_efforts() -> None:
    power = [100 + k // 6 for k in range(1800)]  # 100 -> 399 W en 30 min
    r = run(ride(power, hr=[120 + k // 30 for k in range(1800)]), ftp_w=200)
    best = r["meilleurs_efforts_w"]
    assert best["1min"] == 394 and best["20min"] is None or best["20min"] < best["10min"] < best["5min"] < best["1min"]
    pcts = {z["zone"][:2]: z["pct"] for z in r["zones_puissance"]}
    assert pcts["Z1"] > 0 and pcts["Z7"] > 0  # de 50 % à près de 200 % de la FTP
    assert sum(pcts.values()) == pytest.approx(100, abs=0.2)
    # Rampe trop courte pour un découplage (< 40 min) : null avec la raison.
    assert r["derive_cardiaque"] is None and "< 40 min" in r["derive_cardiaque_raison"]


def test_intervals_detected_on_power_not_virtual_speed() -> None:
    block = [250] * 300 + [120] * 180
    power = [120] * 600 + block * 4 + [120] * 600
    hr = [110] * 600 + ([150] * 300 + [120] * 180) * 4 + [110] * 600
    r = run(ride(power, hr=hr), ftp_w=250, puissance_cible_w=245)
    assert len(r["blocs"]) == 4  # vitesse virtuelle constante : seule la puissance distingue les blocs
    b = r["blocs"][0]
    assert 240 <= b["puissance_moy_w"] <= 250 and b["puissance_max_w"] == 250
    assert 96 <= b["pct_ftp"] <= 100 and b["fc_moy"] >= 140
    assert b["verdict_puissance"] == "conforme" and abs(b["ecart_cible_pct"]) <= 5


def test_missing_heart_rate_still_analysed_in_power() -> None:
    # Garmin renvoie 0 bpm quand aucun cardio n'est appairé : c'est une absence.
    r = run(ride([180] * 3000, hr=[0] * 3000), ftp_w=200)
    v = r["vue_ensemble"]
    assert v["fc_disponible"] is False and "FC absente" in v["message_fc"]
    assert v["fc_moy"] is None and r["zones_fc"] == []
    assert r["derive_cardiaque"] is None and "FC absente" in r["derive_cardiaque_raison"]
    assert v["puissance_normalisee_w"] == 180


def test_sparse_sampling_is_time_weighted() -> None:
    # Un point toutes les 2 s (cas Garmin « intelligent », Tour de Corse) : mêmes résultats.
    dense = run(ride([200] * 1200 + [100] * 1200, hr=[140] * 2400), ftp_w=200)
    sparse = run(ride([200] * 600 + [100] * 600, hr=[140] * 1200, step_s=2), ftp_w=200)
    for key in ("puissance_moy_w", "puissance_normalisee_w", "tss", "travail_kj"):
        assert sparse["vue_ensemble"][key] == pytest.approx(dense["vue_ensemble"][key], rel=0.01)
    assert sparse["meilleurs_efforts_w"]["20min"] == dense["meilleurs_efforts_w"]["20min"] == 200


def test_power_decoupling_on_long_steady_ride() -> None:
    n = 3600
    hr = [130 + (10 * k) // n for k in range(n)]  # FC qui dérive de 130 à 139 bpm
    r = run(ride([200] * n, hr=hr), ftp_w=250)
    d = r["derive_cardiaque"]
    assert d is not None and r["derive_cardiaque_raison"] is None
    assert d["methode"].startswith("puissance / FC")
    assert 2 < d["decouplage_pct"] < 5 and d["fiable"] is True and d["raisons_fiabilite"] == []
    assert d["moities"]["premiere"]["puissance_moy_w"] == 200
    assert d["moities"]["seconde"]["fc_moy"] > d["moities"]["premiere"]["fc_moy"]
    assert {"couverture_pct", "pause_merge_s", "pause_merge_mode", "fusion_elargie", "lecture"} <= set(d)


def test_power_decoupling_unreliable_when_intensity_changes() -> None:
    n = 3600
    power = [150] * (n // 2) + [220] * (n // 2)
    r = run(ride(power, hr=[130] * n), ftp_w=250, type="endurance")
    d = r["derive_cardiaque"]
    assert d["fiable"] is False and any("puissance moyenne" in x for x in d["raisons_fiabilite"])
    assert d["lecture"] == "non interprétable"


def test_without_power_output_is_unchanged() -> None:
    base = ride([200] * 1800, hr=[140] * 1800, with_power=False)
    zeros = {**base, "power_w": [0] * 1800}  # série de 0 : traitée comme absente
    reference = run(base)
    assert run(zeros) == reference
    assert "puissance_disponible" not in reference and "zones_puissance" not in reference
    assert "puissance_moy_w" not in reference["vue_ensemble"]
    assert "ftp_w" not in reference["parametres"]


def test_power_zone_bounds() -> None:
    zones = pwr.power_zones([55, 56, 75, 76, 150, 151], 100)
    pct = {z["zone"][:2]: z["duree_s"] for z in zones}
    assert pct == {"Z1": 1, "Z2": 2, "Z3": 1, "Z4": 0, "Z5": 0, "Z6": 1, "Z7": 1}
    assert zones[0]["pct_ftp"] == "<= 55 %" and zones[1]["pct_ftp"] == "56-75 %" and zones[-1]["pct_ftp"] == "> 150 %"


def test_details_expose_power_and_cadence_only_when_present(tmp_path) -> None:
    client = GarminClient("a@b.c", "pw", str(tmp_path / "tok"))
    client._garmin = MagicMock()
    client._logged_in = True
    descriptors = [
        {"key": "sumElapsedDuration", "metricsIndex": 0},
        {"key": "directHeartRate", "metricsIndex": 1},
        {"key": "directPower", "metricsIndex": 2},
        {"key": "directBikeCadence", "metricsIndex": 3},
    ]
    client._garmin.get_activity_details.return_value = {
        "metricDescriptors": descriptors,
        "activityDetailMetrics": [{"metrics": [0, 0.0, 150.0, 88.0]}, {"metrics": [1, 0.0, 160.0, 90.0]}],
    }
    details = client.get_activity_details(1)
    assert details["power_w"] == [150, 160] and details["cadence_rpm"] == [88, 90]

    client._garmin.get_activity_details.return_value = {
        "metricDescriptors": descriptors,
        "activityDetailMetrics": [{"metrics": [0, 120.0, 0.0, 0.0]}, {"metrics": [1, 121.0, None, None]}],
    }
    details = client.get_activity_details(1)
    assert "power_w" not in details and "cadence_rpm" not in details  # jamais une série de 0
