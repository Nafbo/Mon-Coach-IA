"""Vérification en direct, sur le vrai compte Garmin, des corrections P1-P6 sur les
activités de référence (lecture seule, aucun appel d'écriture).

Usage (racine du repo, venv activé, token Garmin valide dans data/garmin_tokens/) :
    python scripts/verify_reference.py [fc_seuil]

Affiche, pour la course 24725112038 et la nage 24748453127, les valeurs à comparer à
Garmin Connect : temps en mouvement vs écoulé, pauses, distance, blocs, récupérations,
longueurs suspectes, ainsi que l'état du connecteur et le snapshot du jour.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis import series as an  # noqa: E402
from src.analysis import swim  # noqa: E402
from src.garmin_client import GarminClient  # noqa: E402

RUN_ID = "24725112038"
SWIM_ID = "24748453127"


def show(title: str, data: object) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main() -> None:
    load_dotenv(ROOT / ".env")
    fc_seuil = float(sys.argv[1]) if len(sys.argv) > 1 else 177
    db_path = os.environ.get("DB_PATH", "./data/coach.db")
    client = GarminClient(
        os.environ["GARMIN_EMAIL"],
        os.environ["GARMIN_PASSWORD"],
        str((ROOT / db_path).resolve().parent / "garmin_tokens"),
    )

    show("P3 garmin_health_check", client.get_health(date.today()))

    details = client.get_activity_details(RUN_ID)
    result = an.analyse(details, fc_seuil)
    show(f"P1/P2 analyse {RUN_ID} (attendu : ~51 min en mouvement, 10,18 km, pauses 2558-2692 et 2832-2921)", {
        "vue_ensemble": result["vue_ensemble"],
        "nb_blocs": len(result["blocs"]),
        "recuperations": result["recuperations"],
        "derive_cardiaque": result["derive_cardiaque"],
        "taille_json_caracteres": len(json.dumps(result)),
    })
    show(f"P4 météo {RUN_ID}", client.get_activity_weather(RUN_ID))

    annotated = swim.annotate(client.get_swim_splits(SWIM_ID))
    show(f"P6 nage {SWIM_ID} (attendu : 72 longueurs, 21 et 59 suspectes)", {
        "nb_longueurs": len(annotated["splits"]),
        "suspectes": [
            {k: s[k] for k in ("length_idx", "duration_s", "pace_min_sec_per_100m", "suspect_reason")}
            for s in annotated["splits"]
            if s["suspect"]
        ],
        "blocs": annotated["blocks"],
    })

    show("P5 snapshot du jour", client.get_daily_snapshot(date.today()))


if __name__ == "__main__":
    main()
