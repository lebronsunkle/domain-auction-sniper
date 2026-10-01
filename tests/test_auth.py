"""Tests for the bearer-token auth middleware (app/auth.py).

Money-path relevance: this middleware is the only thing standing between the
open internet and the kill switch / caps / buy endpoints. See audit R1.

We build a minimal FastAPI app per test rather than importing app.main, so
these tests need no database and no GoDaddy env vars.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import BearerAuthMiddleware

TOKEN = "test-token-abc123"


def _make_app(*, token: str, godaddy_env: str) -> TestClient:
    app = FastAPI()
    app.add_middleware(BearerAuthMiddleware, token=token, godaddy_env=godaddy_env)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/echo")
    async def echo():
        return {"echo": True}

    @app.post("/api/echo")
    async def echo_post():
        return {"echo": "post"}

    return TestClient(app)


# --- enforce mode (token configured) ----------------------------------------


def test_health_is_open_without_token():
    client = _make_app(token=TOKEN, godaddy_env="production")
    r = client.get("/health")
    assert r.status_code == 200


def test_api_rejected_without_header():
    client = _make_app(token=TOKEN, godaddy_env="production")
    r = client.get("/api/echo")
    assert r.status_code == 401
    assert r.headers.get("WWW-Authenticate") == "Bearer"


def test_api_rejected_with_wrong_token():
    client = _make_app(token=TOKEN, godaddy_env="production")
    r = client.get("/api/echo", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_api_rejected_with_wrong_scheme():
    client = _make_app(token=TOKEN, godaddy_env="production")
    r = client.get("/api/echo", headers={"Authorization": f"Basic {TOKEN}"})
    assert r.status_code == 401


def test_api_accepted_with_correct_token():
    client = _make_app(token=TOKEN, godaddy_env="production")
    r = client.get("/api/echo", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json() == {"echo": True}


def test_post_requires_token_too():
    client = _make_app(token=TOKEN, godaddy_env="production")
    assert client.post("/api/echo").status_code == 401
    assert (
        client.post(
            "/api/echo", headers={"Authorization": f"Bearer {TOKEN}"}
        ).status_code
        == 200
    )


def test_token_with_extra_whitespace_is_accepted():
    client = _make_app(token=TOKEN, godaddy_env="production")
    r = client.get("/api/echo", headers={"Authorization": f"Bearer  {TOKEN} "})
    assert r.status_code == 200


def test_options_preflight_is_not_blocked():
    """CORS preflight carries no Authorization header; it must never 401."""
    client = _make_app(token=TOKEN, godaddy_env="production")
    r = client.options("/api/echo")
    assert r.status_code != 401


# --- fail-closed: production with no token ----------------------------------


def test_production_without_token_fails_closed_503():
    client = _make_app(token="", godaddy_env="production")
    r = client.get("/api/echo")
    assert r.status_code == 503


def test_production_without_token_even_valid_looking_header_gets_503():
    client = _make_app(token="", godaddy_env="production")
    r = client.get("/api/echo", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 503


def test_production_without_token_health_still_open():
    """Fly health checks must keep passing so the machine stays routable
    while the operator fixes the missing secret."""
    client = _make_app(token="", godaddy_env="production")
    assert client.get("/health").status_code == 200


# --- disabled: OTE/dev with no token ------------------------------------------


def test_ote_without_token_auth_disabled():
    client = _make_app(token="", godaddy_env="ote")
    assert client.get("/api/echo").status_code == 200


def test_whitespace_only_token_counts_as_unset():
    client = _make_app(token="   ", godaddy_env="production")
    r = client.get("/api/echo")
    assert r.status_code == 503
