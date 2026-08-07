"""Connexion SQLite et création des tables au démarrage."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL,
  creneau TEXT NOT NULL,
  discipline TEXT NOT NULL,
  objectif TEXT NOT NULL,
  description TEXT NOT NULL,
  duree_minutes INTEGER,
  distance_km REAL,
  nutrition_avant TEXT,
  nutrition_apres TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(date);

CREATE TABLE IF NOT EXISTS activity_feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL,
  discipline TEXT NOT NULL,
  ressenti TEXT NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS garmin_cache (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL,
  type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);
"""


def get_connection(db_path: str) -> sqlite3.Connection:
    """Ouvre une connexion SQLite, en créant le dossier parent si besoin."""
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Crée les tables du schéma si elles n'existent pas."""
    conn.executescript(SCHEMA)
    conn.commit()
