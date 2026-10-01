"""
Base GoDaddy API client.

Provides shared sso-key authentication, retry handling that respects
retryAfterSec, and an audit-log hook that every request goes through.

Both the REST client (rest.py) and the SOAP client (soap.py) inherit from
GoDaddyClient. There's exactly one place auth headers are constructed and
exactly one place requests are dispatched.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class GoDaddyAuth:
    """API credentials. Loaded from env via app.config."""

    key: str
    secret: str

    @property
    def header_value(self) -> str:
        return f"sso-key {self.key}:{self.secret}"


@dataclass
class GoDaddyClientConfig:
    """Per-environment settings (OTE vs production)."""

    # REST base URL. https://api.ote-godaddy.com for OTE, https://api.godaddy.com for prod.
    rest_base_url: str
    # SOAP endpoint URL. Same for OTE and prod (auctions.godaddy.com); GoDaddy may
    # provide a different one for OTE on request.
    soap_endpoint_url: str = "https://auctions.godaddy.com/gdAuctionsWSAPI/gdAuctionsBiddingWS_v2.asmx"
    # The customer UUID (from the auth_idp cookie JWT cid field).
    customer_id: str = ""
    # Default request timeout.
    timeout_seconds: float = 30.0
    # How many times to retry on 429 / 5xx.
    max_retries: int = 3
    # Maximum total wait time when retrying. Beyond this we give up.
    max_total_retry_wait_seconds: float = 60.0


# Audit hook signature: (method, url, status_code, request_body, response_body) -> None
AuditHook = Callable[[str, str, int, Optional[str], Optional[str]], Awaitable[None]]


@dataclass
class GoDaddyClient:
    """Base client. Use RestClient or SoapClient for actual operations."""

    auth: GoDaddyAuth
    config: GoDaddyClientConfig
    audit_hook: Optional[AuditHook] = None
    _http: httpx.AsyncClient = field(init=False)

    def __post_init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=self.config.timeout_seconds)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "GoDaddyClient":
        return self

    async def __aexit__(self, *a: Any) -> None:
        await self.aclose()

    # --- internal request dispatch ----------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        content: Optional[str | bytes] = None,
        json: Optional[Any] = None,
        idempotent: bool = True,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
    ) -> httpx.Response:
        """Single request with retry. Every request goes through this method.

        idempotent=False MUST be used for any call that moves money
        (InstantPurchaseCloseoutDomain, place_bids). For those calls we only
        retry when we can PROVE the server never processed the request:
          * httpx.ConnectError — the connection was never established
          * HTTP 429 — GoDaddy explicitly rejected without processing
        Everything else (read timeouts, 5xx) might mean the purchase already
        went through on GoDaddy's side; retrying could buy the domain twice.
        See docs/audit-2026-07-08-fable.md R2.
        """
        all_headers = {"Authorization": self.auth.header_value}
        if headers:
            all_headers.update(headers)

        # Fail-fast overrides (2026-09-25 outage: GoDaddy ConnectTimeouts made
        # every dashboard poll hold a request slot for 2+ minutes of retries,
        # starving Fly's health check until the proxy stopped routing to us).
        # Interactive read paths pass short timeout / low max_retries so a
        # sick GoDaddy degrades those responses instead of the whole app.
        _max_retries = self.config.max_retries if max_retries is None else max_retries

        total_wait = 0.0
        last_response: Optional[httpx.Response] = None
        attempt = 0

        while attempt <= _max_retries:
            attempt += 1
            try:
                response = await self._http.request(
                    method, url, headers=all_headers, content=content, json=json,
                    timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
                )
            except httpx.ConnectError as e:
                # Connection never established — safe to retry even for
                # money-moving calls; the request cannot have been processed.
                logger.warning("GoDaddy connect error (attempt %d): %s", attempt, e)
                if attempt > _max_retries:
                    raise
                await asyncio.sleep(min(2 ** attempt, 10))
                continue
            except httpx.RequestError as e:
                logger.warning("GoDaddy request error (attempt %d): %s", attempt, e)
                if not idempotent:
                    # Timeout/broken pipe AFTER the request may have been
                    # sent. The server might have processed it. Never retry
                    # a money-moving call here — surface the error and let
                    # the operator reconcile against GoDaddy order history.
                    logger.error(
                        "Non-idempotent request to %s failed with %s after "
                        "possibly reaching the server. NOT retrying — check "
                        "GoDaddy order history before re-attempting.",
                        url,
                        type(e).__name__,
                    )
                    raise
                if attempt > _max_retries:
                    raise
                await asyncio.sleep(min(2 ** attempt, 10))
                continue

            await self._log_audit(method, url, response, content, json)
            last_response = response

            # Success or client error: return immediately. Don't retry 400/401/403.
            if response.status_code < 500 and response.status_code != 429:
                return response

            # 429 Too Many Requests: respect retryAfterSec from the error body.
            if response.status_code == 429:
                retry_after = self._parse_retry_after(response)
                if total_wait + retry_after > self.config.max_total_retry_wait_seconds:
                    logger.error(
                        "GoDaddy 429 retry-after %ss exceeds total wait budget %ss; giving up",
                        retry_after,
                        self.config.max_total_retry_wait_seconds,
                    )
                    return response
                logger.warning(
                    "GoDaddy 429 received, sleeping %ss before retry %d",
                    retry_after,
                    attempt,
                )
                await asyncio.sleep(retry_after)
                total_wait += retry_after
                continue

            # 5xx: the server may have processed the request before failing.
            # Only idempotent calls may retry; money-moving calls return the
            # error response as-is so the caller records it for reconciliation.
            if not idempotent:
                logger.error(
                    "Non-idempotent request to %s got HTTP %d. NOT retrying — "
                    "the server may have processed it. Check GoDaddy order "
                    "history before re-attempting.",
                    url,
                    response.status_code,
                )
                return response

            if attempt <= _max_retries:
                backoff = min(2 ** attempt, 10)
                logger.warning(
                    "GoDaddy %d, retry %d after %ss", response.status_code, attempt, backoff
                )
                await asyncio.sleep(backoff)
                continue

        # Exhausted retries on 5xx.
        assert last_response is not None
        return last_response

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float:
        """Pull retryAfterSec from GoDaddy's standard ErrorLimit response shape.
        Falls back to the Retry-After header, then to a conservative default."""
        try:
            data = response.json()
            if isinstance(data, dict) and "retryAfterSec" in data:
                return float(data["retryAfterSec"])
        except Exception:
            pass
        header = response.headers.get("Retry-After")
        if header:
            try:
                return float(header)
            except ValueError:
                pass
        return 5.0  # conservative default

    async def _log_audit(
        self,
        method: str,
        url: str,
        response: httpx.Response,
        request_content: Optional[str | bytes],
        request_json: Optional[Any],
    ) -> None:
        if self.audit_hook is None:
            return
        body_str: Optional[str] = None
        if request_json is not None:
            import json as _json
            body_str = _json.dumps(request_json)
        elif request_content is not None:
            body_str = (
                request_content.decode("utf-8")
                if isinstance(request_content, bytes)
                else request_content
            )
        try:
            await self.audit_hook(method, url, response.status_code, body_str, response.text)
        except Exception as e:
            logger.error("Audit hook raised: %s", e)
