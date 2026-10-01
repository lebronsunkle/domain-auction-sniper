"""Official GoDaddy Instant Purchase API client (launched 2026-08).

Replaces the reverse-engineered SOAP closeout flow (soap.py) with the
supported REST flow from developer.godaddy.com:

    GET  /v1/customers/{id}/paymentProfiles
    POST /v1/customers/{id}/auctions/purchases/preview   {domains: [...]}
    POST /v1/customers/{id}/auctions/purchases           {currencyId,
          paymentProfileId?, domains: [{domainName, totalPrice, acceptTos}]}

Why this is better than the SOAP path it replaces:
  * Documented + supported (the SOAP endpoints were undocumented; schema
    drift was our biggest structural risk — audit 2026-07-08).
  * Bulk: preview/purchase up to many domains per call.
  * Built-in price guard: purchase REQUIRES the totalPrice from preview;
    if pricing moved, GoDaddy fails that domain with PRICE_MISMATCH
    instead of charging — the "refuse rather than guess" rule, enforced
    server-side.
  * Real auctionIds come back from preview.
  * Same auth (sso-key), same micro-units, same 429 semantics as the bid
    endpoint we already use — client.py handles all of it.

The worker/API keep every existing governor, duplicate guard, and
in-flight record; this module only swaps the transport underneath.
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
class PaymentProfile:
    payment_profile_id: int
    currency_id: str
    label: str
    category: str
    sub_category: Optional[str] = None
    exp_month: Optional[int] = None
    exp_year: Optional[int] = None


@dataclass
class PurchasePreview:
    """Per-domain result from the preview endpoint."""

    domain_name: str
    success: bool
    auction_id: Optional[int] = None
    auction_price_micros: Optional[int] = None
    total_price_micros: Optional[int] = None
    failure_reason: Optional[str] = None  # AUCTION_NOT_FOUND | PRICING_UNAVAILABLE

    @property
    def total_dollars(self) -> Optional[Decimal]:
        return (
            micros_to_dollars(self.total_price_micros)
            if self.total_price_micros is not None
            else None
        )

    @property
    def auction_price_dollars(self) -> Optional[Decimal]:
        return (
            micros_to_dollars(self.auction_price_micros)
            if self.auction_price_micros is not None
            else None
        )


@dataclass
class PurchaseResult:
    """Per-domain result from the purchase endpoint."""

    domain_name: str
    success: bool
    order_id: Optional[str] = None
    auction_id: Optional[int] = None
    total_price_micros: Optional[int] = None
    failure_reason: Optional[str] = None
    # AUCTION_NOT_FOUND | PRICE_MISMATCH | PRICING_UNAVAILABLE | TOS_NOT_ACCEPTED


class InstantPurchaseError(Exception):
    """Non-2xx from the Instant Purchase API."""

    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Instant Purchase API error {status_code}: {body}")


class InstantPurchaseClient:
    """Typed wrapper over the three Instant Purchase endpoints."""

    def __init__(self, client: GoDaddyClient, customer_id: Optional[str] = None):
        self.client = client
        # The API supports the MY alias for the authenticated customer —
        # use it when no explicit id is configured.
        self.customer_id = customer_id or client.config.customer_id or "MY"

    def _url(self, path: str) -> str:
        return f"{self.client.config.rest_base_url}/v1/customers/{self.customer_id}{path}"

    async def payment_profiles(self) -> list[PaymentProfile]:
        response = await self.client._request("GET", self._url("/paymentProfiles"))
        if response.status_code != 200:
            raise InstantPurchaseError(response.status_code, _safe_body(response))
        out = []
        for p in (response.json().get("paymentProfiles") or []):
            out.append(PaymentProfile(
                payment_profile_id=int(p["paymentProfileId"]),
                currency_id=p.get("currencyId", "USD"),
                label=p.get("label", ""),
                category=p.get("category", ""),
                sub_category=p.get("subCategory"),
                exp_month=p.get("expMonth"),
                exp_year=p.get("expYear"),
            ))
        return out

    async def preview(self, domains: list[str]) -> dict[str, PurchasePreview]:
        """All-in pricing for closeout domains. Read-only; safe to retry."""
        response = await self.client._request(
            "POST",
            self._url("/auctions/purchases/preview"),
            headers={"Content-Type": "application/json"},
            json={"domains": domains},
        )
        if response.status_code not in (200, 207):
            raise InstantPurchaseError(response.status_code, _safe_body(response))
        body = response.json()
        out: dict[str, PurchasePreview] = {}
        for a in body.get("auctions") or []:
            d = (a.get("domainName") or "").lower()
            out[d] = PurchasePreview(
                domain_name=d,
                success=a.get("status") == "SUCCESS",
                auction_id=a.get("auctionId"),
                auction_price_micros=a.get("auctionPrice"),
                total_price_micros=a.get("totalPrice"),
                failure_reason=a.get("failureReason"),
            )
        return out

    async def purchase(
        self,
        *,
        domain_name: str,
        total_price_micros: int,
        currency_id: str = "USD",
        payment_profile_id: Optional[int] = None,
    ) -> PurchaseResult:
        """Buy ONE closeout domain at the previewed price.

        THIS CALL SPENDS MONEY. It is sent with idempotent=False (no
        auto-retry after the request may have reached GoDaddy — audit R2),
        and totalPrice must equal the preview or GoDaddy fails the domain
        with PRICE_MISMATCH instead of charging.

        Single-domain on purpose: our governors, duplicate guards, and
        in-flight records are per-listing. Bulk buying can come later as
        an explicit feature with its own safety design.
        """
        payload = {
            "currencyId": currency_id,
            "domains": [
                {
                    "domainName": domain_name,
                    "totalPrice": int(total_price_micros),
                    "acceptTos": True,
                }
            ],
        }
        if payment_profile_id is not None:
            payload["paymentProfileId"] = int(payment_profile_id)

        response = await self.client._request(
            "POST",
            self._url("/auctions/purchases"),
            headers={"Content-Type": "application/json"},
            json=payload,
            idempotent=False,  # money moves here
        )
        if response.status_code not in (200, 207):
            raise InstantPurchaseError(response.status_code, _safe_body(response))

        body = response.json()
        order_id = (body.get("orderDetails") or {}).get("orderId")
        for a in body.get("auctions") or []:
            if (a.get("domainName") or "").lower() == domain_name.lower():
                return PurchaseResult(
                    domain_name=domain_name,
                    success=a.get("status") == "SUCCESS",
                    order_id=order_id,
                    auction_id=a.get("auctionId"),
                    total_price_micros=a.get("totalPrice"),
                    failure_reason=a.get("failureReason"),
                )
        # Response didn't mention our domain — treat as failure, keep body.
        return PurchaseResult(
            domain_name=domain_name,
            success=False,
            order_id=order_id,
            failure_reason=f"DOMAIN_MISSING_FROM_RESPONSE: {str(body)[:300]}",
        )


def _safe_body(response):
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        return response.text[:300]
