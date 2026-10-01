"""Tests for the official Instant Purchase API client (2026-08 GoDaddy API).

Shapes pinned against developer.godaddy.com/docs/references/rest/auctions/
instant-purchase. The purchase call is money-moving: these tests also pin
that it ships with idempotent=False (exactly one send on timeout — audit R2
must survive the SOAP->REST migration).
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.godaddy.client import GoDaddyAuth, GoDaddyClient, GoDaddyClientConfig
from app.godaddy.instant import InstantPurchaseClient, InstantPurchaseError


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    for var in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy",
                "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)


def _client(handler) -> InstantPurchaseClient:
    gd = GoDaddyClient(
        auth=GoDaddyAuth(key="k", secret="s"),
        config=GoDaddyClientConfig(
            rest_base_url="https://api.example.test",
            customer_id="cust-1",
            max_retries=2,
        ),
    )
    gd._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    )
    return InstantPurchaseClient(gd)


@pytest.mark.asyncio
async def test_payment_profiles_parse():
    def handler(request):
        assert request.url.path == "/v1/customers/cust-1/paymentProfiles"
        return httpx.Response(200, json={"paymentProfiles": [{
            "paymentProfileId": 42, "currencyId": "USD",
            "label": "Visa **** 1234", "category": "CREDIT_CARD",
            "subCategory": "Visa", "expMonth": 12, "expYear": 2028,
        }]})

    profiles = await _client(handler).payment_profiles()
    assert len(profiles) == 1
    assert profiles[0].payment_profile_id == 42
    assert profiles[0].label == "Visa **** 1234"


@pytest.mark.asyncio
async def test_preview_parses_success_and_failure_rows():
    def handler(request):
        assert request.url.path == "/v1/customers/cust-1/auctions/purchases/preview"
        return httpx.Response(207, json={
            "currencyId": "USD",
            "auctions": [
                {"domainName": "cheap.com", "status": "SUCCESS",
                 "auctionId": 710000123, "auctionPrice": 5_000_000,
                 "totalPrice": 16_190_000},
                {"domainName": "gone.com", "status": "FAILED",
                 "failureReason": "AUCTION_NOT_FOUND"},
            ],
        })

    previews = await _client(handler).preview(["cheap.com", "gone.com"])
    ok = previews["cheap.com"]
    assert ok.success and ok.auction_id == 710000123
    assert ok.total_dollars == Decimal("16.19")
    assert ok.auction_price_dollars == Decimal("5.00")
    bad = previews["gone.com"]
    assert not bad.success and bad.failure_reason == "AUCTION_NOT_FOUND"


@pytest.mark.asyncio
async def test_purchase_success_parse():
    def handler(request):
        assert request.url.path == "/v1/customers/cust-1/auctions/purchases"
        import json
        body = json.loads(request.content)
        assert body["domains"][0]["totalPrice"] == 16_190_000
        assert body["domains"][0]["acceptTos"] is True
        return httpx.Response(200, json={
            "currencyId": "USD",
            "orderDetails": {"orderId": "4104999999"},
            "auctions": [{"domainName": "cheap.com", "status": "SUCCESS",
                          "auctionId": 710000123, "totalPrice": 16_190_000}],
        })

    result = await _client(handler).purchase(
        domain_name="cheap.com", total_price_micros=16_190_000
    )
    assert result.success
    assert result.order_id == "4104999999"


@pytest.mark.asyncio
async def test_purchase_price_mismatch_fails_safe():
    """Price moved between preview and purchase: GoDaddy refuses, no charge."""
    def handler(request):
        return httpx.Response(200, json={
            "currencyId": "USD",
            "orderDetails": {"orderId": "4104999998"},
            "auctions": [{"domainName": "cheap.com", "status": "FAILED",
                          "failureReason": "PRICE_MISMATCH"}],
        })

    result = await _client(handler).purchase(
        domain_name="cheap.com", total_price_micros=16_190_000
    )
    assert not result.success
    assert result.failure_reason == "PRICE_MISMATCH"


@pytest.mark.asyncio
async def test_purchase_is_non_idempotent_one_send_on_timeout():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(httpx.ReadTimeout):
        await _client(handler).purchase(
            domain_name="cheap.com", total_price_micros=16_190_000
        )
    assert len(calls) == 1, "money-moving purchase must never auto-retry"


@pytest.mark.asyncio
async def test_preview_error_status_raises():
    def handler(request):
        return httpx.Response(403, json={"code": "ACCESS_DENIED"})

    with pytest.raises(InstantPurchaseError) as exc:
        await _client(handler).preview(["x.com"])
    assert exc.value.status_code == 403
