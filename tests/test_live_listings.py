"""Parser test for the Listings Availability client — pinned against the
sample response in GoDaddy's docs (2026-08)."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.godaddy.client import GoDaddyAuth, GoDaddyClient, GoDaddyClientConfig
from app.godaddy.live_listings import LiveListingsClient

SAMPLE = {
    "availabilities": [
        {
            "domainName": "oragoxbk.com",
            "status": "AVAILABLE",
            "listing": {
                "kind": "MIN", "currencyId": "USD",
                "listingId": 696987004, "listingType": "EXPIRY_AUCTIONS",
                "bidsCount": 3, "auctionEndAt": "2026-04-18T18:53:00Z",
                "priceCurrent": 12000000, "priceBuyItNow": 100000000,
                "priceRenewal": 21990000, "memberBiddingStatus": "NOT_BIDDING",
                "watching": True,
            },
        },
        {"domainName": "gone.com", "status": "UNAVAILABLE"},
    ]
}


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    for var in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy",
                "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.asyncio
async def test_live_check_parses_available_and_gone():
    def handler(request):
        assert request.url.path.endswith("/aftermarket/listings/available")
        assert "includes=listingMin" in str(request.url)
        return httpx.Response(200, json=SAMPLE)

    gd = GoDaddyClient(
        auth=GoDaddyAuth(key="k", secret="s"),
        config=GoDaddyClientConfig(rest_base_url="https://api.example.test", customer_id="c-1"),
    )
    gd._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    out = await LiveListingsClient(gd).check(["oragoxbk.com", "gone.com"])

    live = out["oragoxbk.com"]
    assert live.status == "AVAILABLE"
    assert live.listing_id == 696987004
    assert live.price_current_dollars == Decimal("12.00")
    assert live.price_renewal_dollars == Decimal("21.99")
    assert live.watching is True
    assert out["gone.com"].status == "UNAVAILABLE"
