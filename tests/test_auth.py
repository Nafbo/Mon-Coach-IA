"""Tests de src/auth.py : vérification du segment secret dans l'URL."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.auth import SecretPathMiddleware, build_transport_security, mcp_mount_path, redact_path

SECRET = "abc123def456"
EXPECTED_PATH = f"/mcp/{SECRET}"


async def _ok_endpoint(request):
    return JSONResponse({"ok": True})


@pytest.fixture
def client() -> TestClient:
    inner_app = Starlette(routes=[Route(EXPECTED_PATH, _ok_endpoint)])
    gated_app = SecretPathMiddleware(inner_app, expected_path=EXPECTED_PATH)
    return TestClient(gated_app)


def test_wrong_secret_returns_404(client: TestClient) -> None:
    response = client.get("/mcp/wrong-secret")
    assert response.status_code == 404


def test_missing_secret_segment_returns_404(client: TestClient) -> None:
    response = client.get("/mcp")
    assert response.status_code == 404


def test_unrelated_path_returns_404(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 404


def test_correct_secret_reaches_inner_app(client: TestClient) -> None:
    response = client.get(EXPECTED_PATH)
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_mcp_mount_path_builds_expected_path() -> None:
    assert mcp_mount_path(SECRET) == EXPECTED_PATH


def test_mcp_mount_path_rejects_empty_secret() -> None:
    with pytest.raises(ValueError):
        mcp_mount_path("")


def test_redact_path_masks_secret() -> None:
    assert redact_path(EXPECTED_PATH, SECRET) == "/mcp/***"


def test_redact_path_leaves_unrelated_path_untouched() -> None:
    assert redact_path("/health", SECRET) == "/health"


# ---------------------------------------------------------------------------
# Tests de build_transport_security : protection anti DNS-rebinding du SDK MCP,
# doit autoriser le domaine public déployé derrière Caddy en plus de localhost.
# ---------------------------------------------------------------------------


def test_transport_security_without_domain_allows_only_localhost() -> None:
    settings = build_transport_security(None)

    assert settings.enable_dns_rebinding_protection is True
    assert "127.0.0.1" in settings.allowed_hosts
    assert "localhost:*" in settings.allowed_hosts
    assert not any("coach-ia" in h for h in settings.allowed_hosts)


def test_transport_security_with_domain_includes_public_domain() -> None:
    settings = build_transport_security("coach-ia.duckdns.org")

    assert "coach-ia.duckdns.org" in settings.allowed_hosts
    assert "coach-ia.duckdns.org:*" in settings.allowed_hosts
    assert "https://coach-ia.duckdns.org" in settings.allowed_origins
    # localhost reste autorisé en parallèle, pour ne pas casser les tests locaux.
    assert "127.0.0.1" in settings.allowed_hosts
    assert "localhost:*" in settings.allowed_hosts
