"""Retry-safety tests for GoDaddyClient._request (audit R2).

The invariant under test: a money-moving call (idempotent=False) must NEVER
be sent twice unless we can prove the server never processed the first
attempt. A read timeout or a 5xx after the request was sent does NOT prove
that — GoDaddy may have already bought the domain.

Uses httpx.MockTransport so no real network is touched.
"""

from __future__ import annotations

import httpx
import pytest

from app.godaddy.client import GoDaddyAuth, GoDaddyClient, GoDaddyClientConfig


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    """GoDaddyClient's constructor builds a real httpx.AsyncClient, which
    reads proxy env vars. Clear them so tests are hermetic on any machine/CI."""
    for var in (
        "ALL_PROXY", "all_proxy",
        "HTTP_PROXY", "http_proxy",
        "HTTPS_PROXY", "https_proxy",
    ):
        monkeypatch.delenv(var, raising=False)


def _make_client(handler) -> GoDaddyClient:
    client = GoDaddyClient(
        auth=GoDaddyAuth(key="k", secret="s"),
        config=GoDaddyClientConfig(
            rest_base_url="https://api.example.test",
            max_retries=3,
            # Keep retry sleeps fast: 429 path sleeps retryAfterSec from the
            # response; 5xx path sleeps 2**attempt. We only exercise the 5xx
            # path for idempotent calls, so cap total test time by max_retries.
        ),
    )
    # trust_env=False: ignore proxy env vars so tests are hermetic.
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    )
    return client


URL = "https://api.example.test/v1/thing"


# --- non-idempotent (money-moving) calls --------------------------------------


@pytest.mark.asyncio
async def test_non_idempotent_does_not_retry_on_5xx():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, text="boom")

    client = _make_client(handler)
    response = await client._request("POST", URL, content="x", idempotent=False)
    assert response.status_code == 500
    assert len(calls) == 1, "money-moving call was sent more than once on 5xx"


@pytest.mark.asyncio
async def test_non_idempotent_does_not_retry_on_read_timeout():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("timed out waiting for response")

    client = _make_client(handler)
    with pytest.raises(httpx.ReadTimeout):
        await client._request("POST", URL, content="x", idempotent=False)
    assert len(calls) == 1, "money-moving call was re-sent after a timeout"


@pytest.mark.asyncio
async def test_non_idempotent_retries_on_connect_error():
    """ConnectError means the connection was never established — the server
    provably never saw the request, so retrying is safe and desirable."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 2:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, text="ok")

    client = _make_client(handler)
    response = await client._request("POST", URL, content="x", idempotent=False)
    assert response.status_code == 200
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_non_idempotent_retries_on_429():
    """429 is an explicit 'rejected, not processed' from GoDaddy — safe to
    retry even for money-moving calls, honoring retryAfterSec."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 2:
            return httpx.Response(429, json={"retryAfterSec": 0})
        return httpx.Response(200, text="ok")

    client = _make_client(handler)
    response = await client._request("POST", URL, content="x", idempotent=False)
    assert response.status_code == 200
    assert len(calls) == 2


# --- idempotent calls keep the old behavior -----------------------------------


@pytest.mark.asyncio
async def test_idempotent_still_retries_on_5xx():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, text="ok")

    client = _make_client(handler)
    response = await client._request("GET", URL)
    assert response.status_code == 200
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_idempotent_still_retries_on_read_timeout():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 2:
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200, text="ok")

    client = _make_client(handler)
    response = await client._request("GET", URL)
    assert response.status_code == 200
    assert len(calls) == 2


# --- wiring: the actual money paths pass idempotent=False ---------------------


@pytest.mark.asyncio
async def test_execute_closeout_purchase_is_wired_non_idempotent():
    """If the purchase SOAP call times out, it must surface the error after
    exactly one send — proving idempotent=False is actually passed through."""
    from app.godaddy.soap import SoapClient

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("timed out")

    client = _make_client(handler)
    soap = SoapClient(client)
    with pytest.raises(httpx.ReadTimeout):
        await soap.execute_closeout_purchase(
            domain_name="example.com", price_key="key123"
        )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_place_bids_is_wired_non_idempotent():
    from decimal import Decimal

    from app.godaddy.rest import BidRequest, RestClient

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("timed out")

    client = _make_client(handler)
    client.config.customer_id = "cust-1"
    rest = RestClient(client)
    with pytest.raises(httpx.ReadTimeout):
        await rest.place_bids(
            [BidRequest(listing_id=1, amount_dollars=Decimal("10.00"))],
            per_tx_cap_dollars=Decimal("25.00"),
        )
    assert len(calls) == 1
