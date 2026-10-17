"""garmin_get_activity_feedback : sensations et effort perçu saisis sur la montre."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.db import get_connection, init_db
from src.garmin_client import GarminClient
from src.tools import garmin_tools as gt


@pytest.fixture
def client(tmp_path) -> GarminClient:
    gc = GarminClient("a@b.com", "pw", str(tmp_path / "tokenstore"))
    gc._garmin = MagicMock()
    gc._logged_in = True
    return gc


@pytest.fixture
def tools(client: GarminClient) -> dict:
    conn = get_connection(":memory:")
    init_db(conn)
    yield gt.build_garmin_tools(client, conn)
    conn.close()


# Valeurs relevées sur le vrai compte le 17/10/2026.
@pytest.mark.parametrize(
    ("feel", "rpe", "sensations", "effort", "libelle"),
    [
        (75, 70, "Fort", 7, "Difficile"),  # nage du 17/10 (24752973255)
        (50, 50, "Normal", 5, "Modéré"),  # nage du 16/10 (24748453127)
        (50, 30, "Normal", 3, "Léger"),  # footing du 15/10 (24737274389)
        (0, 100, "Très faible", 10, "Maximal"),
        (100, 10, "Très fort", 1, "Très léger"),
    ],
)
def test_feedback_parsing(client: GarminClient, feel, rpe, sensations, effort, libelle) -> None:
    client._garmin.get_activity.return_value = {"summaryDTO": {"directWorkoutFeel": feel, "directWorkoutRpe": rpe}}

    result = client.get_activity_feedback("24752973255")

    assert result == {
        "status": "ok",
        "sensations": sensations,
        "sensations_brut": feel,
        "effort_percu_sur_10": effort,
        "effort_percu_libelle": libelle,
    }


def test_feedback_partial(client: GarminClient) -> None:
    client._garmin.get_activity.return_value = {"summaryDTO": {"directWorkoutRpe": 60}}

    result = client.get_activity_feedback("1")

    assert result["status"] == "ok"
    assert result["sensations"] is None
    assert result["effort_percu_sur_10"] == 6


def test_feedback_not_entered_is_no_data(client: GarminClient) -> None:
    client._garmin.get_activity.return_value = {"summaryDTO": {"duration": 1800}}

    result = client.get_activity_feedback("1")

    assert result["status"] == "no_data"
    assert result["sensations"] is None and result["effort_percu_sur_10"] is None


async def test_feedback_tool_no_data_not_cached(tools: dict, client: GarminClient) -> None:
    """Une saisie faite plus tard dans l'app doit apparaître dès l'appel suivant."""
    client._garmin.get_activity.side_effect = [
        {"summaryDTO": {}},
        {"summaryDTO": {"directWorkoutFeel": 75, "directWorkoutRpe": 70}},
    ]

    first = await tools["garmin_get_activity_feedback"]("24752973255")
    second = await tools["garmin_get_activity_feedback"]("24752973255")
    third = await tools["garmin_get_activity_feedback"]("24752973255")

    assert first["status"] == "no_data"
    assert second["sensations"] == "Fort"
    assert third == second
    assert client._garmin.get_activity.call_count == 2


async def test_feedback_tool_error(tools: dict, client: GarminClient) -> None:
    from garminconnect import GarminConnectConnectionError

    client._garmin.get_activity.side_effect = GarminConnectConnectionError("HTTP 500")

    result = await tools["garmin_get_activity_feedback"]("1")

    assert result["status"] == "error"
    assert "HTTP 500" in result["message"]
