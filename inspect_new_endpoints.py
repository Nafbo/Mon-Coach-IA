"""Script ponctuel (à supprimer une fois utilisé) : inspecte le format réel renvoyé par
garmin_get_activity_weather / garmin_get_activity_details pour finaliser leur spec sur des
données réelles plutôt que des hypothèses.

Lecture seule : aucun appel d'écriture, aucun risque pour ton compte Garmin ou ta montre.

Usage (depuis la racine du repo mcp-garmin-coach, venv activé) :
    python inspect_new_endpoints.py [activity_id]

Sans argument : prend automatiquement ta dernière activité loggée.
Réutilise le tokenstore existant (data/garmin_tokens/) donc pas de nouveau login MFA si un
token valide est déjà en cache.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from garminconnect import Garmin  # noqa: E402

email = os.environ["GARMIN_EMAIL"]
password = os.environ["GARMIN_PASSWORD"]
db_path = os.environ.get("DB_PATH", "./data/coach.db")
tokenstore = str(Path(db_path).resolve().parent / "garmin_tokens")

api = Garmin(email=email, password=password)
api.login(tokenstore=tokenstore)

if len(sys.argv) > 1:
    activity_id = sys.argv[1]
else:
    activities = api.get_activities(0, 1)
    if not activities:
        print("Aucune activité trouvée dans l'historique Garmin.")
        sys.exit(1)
    activity_id = activities[0]["activityId"]
    print(
        f"Activité utilisée : {activity_id} "
        f"({activities[0].get('activityName')}, {activities[0].get('startTimeLocal')})"
    )

print("\n=== get_activity_weather ===")
weather = api.get_activity_weather(activity_id)
print(json.dumps(weather, indent=2, ensure_ascii=False))

print("\n=== get_activity_details (aperçu) ===")
details = api.get_activity_details(activity_id)
print("Clés de premier niveau :", list(details.keys()))

if isinstance(details, dict) and "metricDescriptors" in details:
    print("\nmetricDescriptors (indique quelle colonne = quelle grandeur) :")
    print(json.dumps(details["metricDescriptors"], indent=2, ensure_ascii=False))

if isinstance(details, dict) and "activityDetailMetrics" in details:
    metrics = details["activityDetailMetrics"]
    print(f"\nactivityDetailMetrics : {len(metrics)} points, aperçu des 3 premiers :")
    print(json.dumps(metrics[:3], indent=2, ensure_ascii=False))

out_path = Path("garmin_details_sample.json")
out_path.write_text(
    json.dumps({"weather": weather, "details": details}, indent=2, ensure_ascii=False),
    encoding="utf-8",
)
print(f"\nPayload complet sauvegardé dans {out_path.resolve()}")
print("Colle-moi la sortie ci-dessus (ou ce fichier) pour qu'on finalise le parsing.")
