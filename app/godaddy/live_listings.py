"""Live listing state via the official Listings Availability API.

POST /v1/customers/{id}/aftermarket/listings/available  (2026-08 API)
Body: {"domains": [1-50 FQDNs]}, ?includes=listingMin

This is the fix for the staleness the client diagnosed on the 2026-08-23 demo
("you harvest once a day; nothing feeds it continuously"): the daily feed
DISCOVERS listings; this endpoint is the live TRUTH for whatever's on
screen — current price, real end time, bid count, sold/gone status, the
customer's own GoDaddy-watchlist flag, and the domain's actual renewal
price. Read-only, bulk (50/call), safe to poll.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .client import GoDaddyClient
from .micro_units import micros_to_dollars

logger = logging.getLogger(__name__)


@dataclass
class LiveListing:
    domain: str
    status: str  # AVAILABLE | UNAVAILABLE
    listing_id: Optional[int] = None
    listing_type: Optional[str] = None      # e.g. EXPIRY_AUCTIONS
    bids_count: Optional[int] = None
    auction_end_at: Optional[str] = None    # ISO UTC
    price_current_micros: Optional[int] = None
    price_buy_it_now_micros: Optional[int] = None
    price_renewal_micros: Optional[int] = None
    member_bidding_status: Optional[str] = None
    watching: Optional[bool] = None         # on the customer's GoDaddy watchlist!

    def _d(self, micros: Optional[int]) -> Optional[Decimal]:
        return micros_to_dollars(micros) if micros is not None else None

    @property
    def price_current_dollars(self) -> Optional[Decimal]:
        return self._d(self.price_current_micros)

    @property
    def price_buy_it_now_dollars(self) -> Optional[Decimal]:
        return self._d(self.price_buy_it_now_micros)

    @property
    def price_renewal_dollars(self) -> Optional[Decimal]:
        return self._d(self.price_renewal_micros)


class LiveListingsClient:
    def __init__(self, client: GoDaddyClient, customer_id: Optional[str] = None):
        self.client = client
        self.customer_id = customer_id or client.config.customer_id or "MY"

    async def check(self, domains: list[str]) -> dict[str, LiveListing]:
        """Live state for up to 50 domains. Read-only — default retries OK."""
        domains = [d.lower().strip() for d in domains if d and "." in d][:50]
        if not domains:
            return {}
        url = (
            f"{self.client.config.rest_base_url}/v1/customers/"
            f"{self.customer_id}/aftermarket/listings/available"
            f"?includes=listingMin"
        )
        response = await self.client._request(
            "POST",
            url,
            headers={"Content-Type": "application/json"},
            json={"domains": domains},
            # Fail fast (2026-09-25 outage): this is the dashboard's hot
            # poll. When GoDaddy is sick, a missing overlay for one cycle
            # beats a 2-minute retry pyramid that suffocates the app.
            timeout=8.0,
            max_retries=1,
        )
        if response.status_code != 200:
            logger.warning(
                "listings/available returned %d: %s",
                response.status_code,
                response.text[:200],
            )
            return {}
        out: dict[str, LiveListing] = {}
        for a in response.json().get("availabilities") or []:
            dom = (a.get("domainName") or "").lower()
            if not dom:
                continue
            listing = a.get("listing") or {}
            out[dom] = LiveListing(
                domain=dom,
                status=a.get("status") or "UNAVAILABLE",
                listing_id=listing.get("listingId"),
                listing_type=listing.get("listingType"),
                bids_count=listing.get("bidsCount"),
                auction_end_at=listing.get("auctionEndAt"),
                price_current_micros=listing.get("priceCurrent"),
                price_buy_it_now_micros=listing.get("priceBuyItNow"),
                price_renewal_micros=listing.get("priceRenewal"),
                member_bidding_status=listing.get("memberBiddingStatus"),
                watching=listing.get("watching"),
            )
        return out
