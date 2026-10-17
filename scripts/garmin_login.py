"""Renouvelle le token Garmin local quand il est refusé (erreur 401 "Failed to retrieve
social profile") : met l'ancien token de côté, se reconnecte avec les identifiants du
.env (code MFA demandé dans le terminal si besoin) et enregistre le nouveau token dans
data/garmin_tokens/.

Usage (racine du repo, venv activé) :
    python scripts/garmin_login.py
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    load_dotenv(ROOT / ".env")
    from garminconnect import Garmin

    db_path = os.environ.get("DB_PATH", "./data/coach.db")
    tokenstore = (ROOT / db_path).resolve().parent / "garmin_tokens"
    if tokenstore.exists():
        backup = tokenstore.with_name(f"garmin_tokens.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.move(str(tokenstore), str(backup))
        print(f"Ancien token déplacé vers {backup}")
    tokenstore.mkdir(parents=True, exist_ok=True)

    api = Garmin(
        email=os.environ["GARMIN_EMAIL"],
        password=os.environ["GARMIN_PASSWORD"],
        prompt_mfa=lambda: input("Code MFA Garmin : ").strip(),
    )
    api.login(tokenstore=str(tokenstore))
    print(f"Connecté ({api.get_full_name()}). Token enregistré dans {tokenstore}")


if __name__ == "__main__":
    main()
