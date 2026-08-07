"""Vérification du segment secret dans l'URL (pas d'OAuth, cf. spec section 5)."""

from __future__ import annotations

import hmac
import logging

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

MCP_PATH_PREFIX = "/mcp"


def mcp_mount_path(secret: str) -> str:
    """Construit le chemin de montage `/mcp/{secret}` à partir du secret configuré."""
    if not secret:
        raise ValueError("MCP_SECRET_PATH ne doit pas être vide")
    return f"{MCP_PATH_PREFIX}/{secret}"


def redact_path(path: str, secret: str) -> str:
    """Masque le secret dans un chemin, pour un affichage/log sûr."""
    if secret and secret in path:
        return path.replace(secret, "***")
    return path


class SecretPathMiddleware:
    """Laisse passer uniquement les requêtes dont le chemin correspond exactement au segment
    secret configuré ; toute autre requête reçoit un 404 brut (jamais 401/403), pour ne pas
    révéler l'existence du endpoint MCP à un client qui devine des chemins. La comparaison est
    faite en temps constant pour limiter le risque d'attaque par timing sur le secret.
    """

    def __init__(self, app: ASGIApp, expected_path: str) -> None:
        self._app = app
        self._expected_path = expected_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not hmac.compare_digest(path, self._expected_path):
            response = PlainTextResponse("Not Found", status_code=404)
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)


class AccessLogMiddleware:
    """Logge method + status code pour chaque requête HTTP en INFO, sans jamais logger le
    chemin de la requête (qui contient le segment secret) — cf. spec sections 5 et 9.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        method = scope.get("method", "?")
        status_holder: dict[str, int] = {}

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        await self._app(scope, receive, send_wrapper)
        logger.info("http_request method=%s status=%s", method, status_holder.get("status", "?"))
