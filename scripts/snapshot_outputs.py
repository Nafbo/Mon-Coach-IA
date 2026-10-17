"""Fige les sorties actuelles des outils sur les fixtures réelles (course et nage), pour les
tests de non-régression (tests/test_bike.py). Les snapshots contiennent des données de santé
réelles : ils sont écrits dans tests/fixtures/ (ignoré par git), comme les fixtures.

Usage (racine du repo) : python scripts/snapshot_outputs.py
"""

from __future__ import annotations

import asyncio
import gzip
import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.db import get_connection, init_db  # noqa: E402
from src.garmin_client import GarminClient  # noqa: E402
from src.tools import garmin_tools as gt  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
RUN_ID = "24737274389"
SWIM_ID = "24752973255"


def client_for(*activity_ids: str) -> GarminClient:
    payloads = {a: json.load(gzip.open(FIXTURES / f"{a}.json.gz", "rt", encoding="utf-8")) for a in activity_ids}
    client = GarminClient("a@b.com", "pw", "unused")
    client._garmin = g = MagicMock()
    client._logged_in = True
    g.get_activity_details.side_effect = lambda a, *args, **kw: payloads[a]["details"]
    g.get_activity_splits.side_effect = lambda a: payloads[a]["splits"]
    g.get_activity.side_effect = lambda a: payloads[a]["activity"]
    return client


async def capture() -> dict:
    gt.today_paris = lambda: date(2026, 10, 17)
    conn = get_connection(":memory:")
    init_db(conn)
    tools = gt.build_garmin_tools(client_for(RUN_ID, SWIM_ID), conn)
    return {
        "analyse_course": await tools["garmin_analyse_activity"](RUN_ID, fc_seuil=174),
        "analyse_course_endurance": await tools["garmin_analyse_activity"](RUN_ID, fc_seuil=174, type="endurance"),
        "details_course": await tools["garmin_get_activity_details"](RUN_ID, resolution_s=5),
        "swim_splits": await tools["garmin_get_swim_splits"](SWIM_ID),
        "analyse_swim": await tools["garmin_analyse_swim"](SWIM_ID, target_css_min_per_100m=1.6667),
    }


if __name__ == "__main__":
    out = FIXTURES / "snapshots_course_nage.json"
    out.write_text(json.dumps(asyncio.run(capture()), ensure_ascii=False), encoding="utf-8")
    print(out)
