"""Modèles pydantic pour les payloads des tools opérationnels, et utilitaires de fuseau horaire partagés."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

PARIS_TZ = ZoneInfo("Europe/Paris")


def today_paris() -> date:
    """Date du jour en Europe/Paris, indépendamment du fuseau du serveur (cf. spec section 5bis)."""
    return datetime.now(PARIS_TZ).date()


def monday_of_week(d: date) -> date:
    """Retourne le lundi de la semaine ISO contenant la date donnée."""
    return d - timedelta(days=d.weekday())


class SessionInput(BaseModel):
    """Une séance telle que fournie en entrée de set_sessions / set_week_plan / add_session."""

    creneau: Literal["matin", "soir"]
    discipline: str
    objectif: str
    description: str
    duree_minutes: int | None = None
    distance_km: float | None = None
    nutrition_avant: str | None = None
    nutrition_apres: str | None = None


class DayPlanInput(BaseModel):
    """Un jour du plan de semaine tel que fourni en entrée de set_week_plan."""

    date: str
    sessions: list[SessionInput] = Field(default_factory=list)
