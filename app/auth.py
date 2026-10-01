"""Bearer-token auth for the public Fly.io API.

Why this exists: the dashboard sits behind Cloudflare Access, but the Fly
backend URL is directly reachable by anyone on the internet. Before this
middleware, every governor-protected endpoint (kill switch, caps, watchlist
arm, buy-now) was anonymous-writable. See docs/audit-2026-07-08-fable.md R1.

Design:
  * Single shared token in the API_AUTH_TOKEN env var (Fly secret).
    Clients send `Authorization: Bearer <token>` on every /api request.
  * /health is exempt (Fly health checks), as are CORS preflight OPTIONS.
  * Fail-closed in production: if GODADDY_ENV=production and no token is
    configured, every /api request is rejected with 503 rather than
    silently running open.
  * In OTE/dev with no token configured, auth is disabled with a loud
    warning so local development isn't blocked.
  * Comparison is constant-time (secrets.compare_digest).

The trigger worker runs in-process and never crosses HTTP, so it is
unaffected.
"""

from __future__ import annotations

import logging
import secrets

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# Paths that never require auth. Keep this list tiny and boring.
EXEMPT_PATHS = frozenset({"/health"})


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Rejects unauthenticated requests to /api/* with 401 (or 503 if the
    server itself is misconfigured in production)."""

    def __init__(self, app, *, token: str, godaddy_env: str):
        super().__init__(app)
        self._token = (token or "").strip()
        self._production = godaddy_env == "production"

        if self._token:
            self._mode = "enforce"
        elif self._production:
            # Production with no token: fail closed. Better a dead API than
            # an open one.
            self._mode = "misconfigured"
            logger.error(
                "API_AUTH_TOKEN is not set and GODADDY_ENV=production. "
                "All /api requests will be rejected with 503 until the "
                "secret is set (fly secrets set API_AUTH_TOKEN=...)."
            )
        else:
            self._mode = "disabled"
            logger.warning(
                "API_AUTH_TOKEN is not set (non-production env). API auth "
                "is DISABLED. Set the env var to enable it."
            )

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path

        # Only guard the API surface. /health stays open for Fly checks.
        if not path.startswith("/api") or path in EXEMPT_PATHS:
            return await call_next(request)

        # CORS preflight carries no Authorization header by design.
        if request.method == "OPTIONS":
            return await call_next(request)

        if self._mode == "disabled":
            return await call_next(request)

        if self._mode == "misconfigured":
            return JSONResponse(
                status_code=503,
                content={
                    "detail": (
                        "API auth is not configured on the server "
                        "(API_AUTH_TOKEN missing in production). Refusing "
                        "all API requests until it is set."
                    )
                },
            )

        header = request.headers.get("Authorization", "")
        scheme, _, candidate = header.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            candidate.strip(), self._token
        ):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid API token."},
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await call_next(request)
