"""Point d'entrée : création de l'app MCP, montage des tools, exposition ASGI."""

from __future__ import annotations

import functools
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from starlette.types import ASGIApp

from src.auth import AccessLogMiddleware, SecretPathMiddleware, build_transport_security, mcp_mount_path
from src.db import get_connection, init_db
from src.garmin_client import GarminClient
from src.mywhoosh_client import MyWhooshClient, load_device_id
from src.tools.garmin_tools import build_garmin_tools
from src.tools.mywhoosh_tools import build_mywhoosh_tools
from src.tools.operational_tools import build_operational_tools

logger = logging.getLogger(__name__)


def configure_logging(level_name: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level_name.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _register_tool(mcp: MCPServer, name: str, func: Callable[..., Any]) -> None:
    """Enregistre un tool en journalisant nom, succès/échec et durée (spec section 9)."""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.monotonic()
        try:
            result = await func(*args, **kwargs)
        except Exception as exc:
            duration_ms = (time.monotonic() - start) * 1000
            logger.info("tool=%s status=error duration_ms=%.1f", name, duration_ms)
            logger.exception("Exception non gérée dans le tool %s", name)
            return {"error": True, "message": f"Erreur interne inattendue : {exc}"}

        duration_ms = (time.monotonic() - start) * 1000
        is_error = isinstance(result, dict) and (result.get("error") is True or result.get("status") == "error")
        logger.info("tool=%s status=%s duration_ms=%.1f", name, "error" if is_error else "ok", duration_ms)
        return result

    mcp.tool(name=name)(wrapper)


def build_app(
    *,
    conn: sqlite3.Connection,
    garmin_client: GarminClient,
    secret: str,
    public_domain: str | None = None,
    mywhoosh_client: MyWhooshClient | None = None,
) -> ASGIApp:
    """Construit l'app ASGI complète (MCP + auth par segment secret) à partir de dépendances déjà créées.

    Séparé de `create_app_from_env` pour rester testable sans variables d'environnement ni
    compte Garmin réel (cf. tests).
    """
    mcp = MCPServer("coach-triathlon-garmin")

    all_tools = {
        **build_garmin_tools(garmin_client, conn),
        **build_operational_tools(conn),
        **build_mywhoosh_tools(garmin_client, mywhoosh_client),
    }
    for name, func in all_tools.items():
        _register_tool(mcp, name, func)

    mount_path = mcp_mount_path(secret)
    transport_security = build_transport_security(public_domain)
    inner_app = mcp.streamable_http_app(streamable_http_path=mount_path, transport_security=transport_security)
    gated_app = SecretPathMiddleware(inner_app, expected_path=mount_path)
    return AccessLogMiddleware(gated_app)


def create_app_from_env() -> ASGIApp:
    load_dotenv()

    email = os.environ["GARMIN_EMAIL"]
    password = os.environ["GARMIN_PASSWORD"]
    secret = os.environ["MCP_SECRET_PATH"]
    db_path = os.environ.get("DB_PATH", "./data/coach.db")
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    public_domain = os.environ.get("PUBLIC_DOMAIN") or None

    configure_logging(log_level)

    conn = get_connection(db_path)
    init_db(conn)

    tokenstore_path = str(Path(db_path).resolve().parent / "garmin_tokens")
    garmin_client = GarminClient(email, password, tokenstore_path)

    # Optionnel : sans identifiants MyWhoosh, `mywhoosh_sync` répond « non configuré ».
    mywhoosh_email = os.environ.get("MYWHOOSH_EMAIL")
    mywhoosh_password = os.environ.get("MYWHOOSH_PASSWORD")
    mywhoosh_client = (
        MyWhooshClient(
            mywhoosh_email,
            mywhoosh_password,
            device_id=load_device_id(Path(db_path).resolve().parent / "mywhoosh_device_id"),
        )
        if mywhoosh_email and mywhoosh_password
        else None
    )

    return build_app(
        conn=conn,
        garmin_client=garmin_client,
        secret=secret,
        public_domain=public_domain,
        mywhoosh_client=mywhoosh_client,
    )


app = create_app_from_env()
