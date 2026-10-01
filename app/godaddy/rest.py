"""
REST client for the GoDaddy Auctions API.

The public Auctions REST API exposes exactly one endpoint:
    POST /v1/customers/{customerId}/aftermarket/listings/bids

That's confirmed from the official Swagger spec at
developer.godaddy.com/swagger/swagger_auctions.json. All other auction
operations (closeout purchase, watchlist, price queries) go through the
SOAP API in soap.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .client import GoDaddyClient
from .listing_ids import is_real_listing_id
from .micro_units import assert_within_transaction_cap, dollars_to_micros

logger = logging.getLogger(__name__)


# Failure reasons returned by GoDaddy on a partial-success bid response.
# Lifted from the Swagger spec.
class BidFailureReason:
    BID_MIN_NOT_MET = "BID_MIN_NOT_MET"
    BIDDER_IS_SELLER = "BIDDER_IS_SELLER"
    BIDDER_VERIFICATION_AMOUNT = "BIDDER_VERIFICATION_SHOPPER_MAX_APPROVED_AMOUNT"
    BIDDER_VERIFICATION_COUNT = "BIDDER_VERIFICATION_SHOPPER_MAX_APPROVED_COUNT"
    LISTING_NOT_FOUND = "LISTING_NOT_FOUND"
    LISTING_NOT_OPEN = "LISTING_NOT_OPEN"
    UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE"
    USER_TOS = "USER_TOS"


@dataclass
class BidRequest:
    """One bid to be placed. Up to 20 may be sent in a single API call."""

    listing_id: int
    amount_dollars: Decimal
    tos_accepted: bool = True

    def to_payload(self, per_tx_cap: Decimal) -> dict:
        """Convert to the JSON body GoDaddy expects. Performs all safety conversions."""
        # Synthetic ids (blake2b hashes from the feed parser, Date.now()
        # shims from the dashboard) are NOT real auction ids; GoDaddy would
        # reject them with LISTING_NOT_FOUND — at snipe time, when it's too
        # late to fix. Fail loudly here instead. See app/godaddy/listing_ids.py.
        if not is_real_listing_id(self.listing_id):
            raise ValueError(
                f"listing_id {self.listing_id} is synthetic (feed hash or "
                "dashboard shim), not a real GoDaddy auction id. Bids "
                "require the real id — re-add this domain after the "
                "2026-07-11 feed-parser fix, or wait for the +Add resolver."
            )
        # Two layers of defense: the micro_units helper has its own ceiling,
        # and we check against the per-transaction cap before that.
        assert_within_transaction_cap(self.amount_dollars, per_tx_cap)
        return {
            "listingId": int(self.listing_id),
            "bidAmountUsd": dollars_to_micros(self.amount_dollars),
            "tosAccepted": bool(self.tos_accepted),
        }


@dataclass
class BidResponse:
    """Result of a single bid attempt within a batch."""

    listing_id: int
    status: str  # "SUCCESS" or "FAILED"
    is_highest_bidder: Optional[bool] = None
    bid_id: Optional[str] = None
    bid_amount_usd_micros: Optional[int] = None
    failure_reason: Optional[str] = None


class RestClient:
    """Thin typed wrapper around the bid endpoint."""

    def __init__(self, client: GoDaddyClient):
        self.client = client

    async def place_bids(
        self, bids: list[BidRequest], *, per_tx_cap_dollars: Decimal
    ) -> list[BidResponse]:
        """Place one or more bids. Up to 20 per call. Per the official spec.

        per_tx_cap_dollars is the per-transaction safety cap; any bid above this
        is rejected locally without an API call.
        """
        if not bids:
            raise ValueError("place_bids requires at least one bid")
        if len(bids) > 20:
            raise ValueError(f"place_bids supports at most 20 bids per request, got {len(bids)}")

        customer_id = self.client.config.customer_id
        if not customer_id:
            raise ValueError(
                "Customer ID is not configured. Set GODADDY_CUSTOMER_ID in env "
                "(extract via the auth_idp cookie + jwt.io method described in README)."
            )

        url = f"{self.client.config.rest_base_url}/v1/customers/{customer_id}/aftermarket/listings/bids"
        payload = [b.to_payload(per_tx_cap_dollars) for b in bids]

        response = await self.client._request(
            "POST",
            url,
            headers={"Content-Type": "application/json"},
            json=payload,
            # Bids move money. Never auto-retry after a timeout/5xx — the
            # bid may already have been placed (audit R2).
            idempotent=False,
        )

        if response.status_code in (200, 207):
            return [self._parse_bid_response(item) for item in response.json()]

        # Error path. Raise with the GoDaddy error body for context.
        try:
            error = response.json()
        except Exception:
            error = {"message": response.text}
        raise GoDaddyBidError(response.status_code, error)

    @staticmethod
    def _parse_bid_response(item: dict) -> BidResponse:
        return BidResponse(
            listing_id=int(item["listingId"]),
            status=item.get("status", "UNKNOWN"),
            is_highest_bidder=item.get("isHighestBidder"),
            bid_id=item.get("bidId"),
            bid_amount_usd_micros=item.get("bidAmountUsd"),
            failure_reason=item.get("bidFailureReason") or item.get("failureReason"),
        )


class GoDaddyBidError(Exception):
    """Raised when the bid endpoint returns a non-success status."""

    def __init__(self, status_code: int, error_body: dict):
        self.status_code = status_code
        self.error_body = error_body
        super().__init__(f"GoDaddy bid error {status_code}: {error_body}")
