"""GET /api/lookup — find any domain in GoDaddy's current auction inventory.

Uses the SOAP `GetAuctionDetailsByDomainName` operation. This is the unlock
for "the client can paste any domain and our app finds it" — no listing_id
needed, no manual data entry. If the domain is currently in an auction
(closeout or expiry), we return the parsed details.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.godaddy.client import GoDaddyAuth, GoDaddyClient, GoDaddyClientConfig
from app.godaddy.soap import SoapClient

logger = logging.getLogger(__name__)

router = APIRouter()


class LookupResponse(BaseModel):
    """Best-effort parsed auction details. Fields are optional because the
    underlying SOAP response shape varies by auction type and whether the
    domain is actually in auction. Callers should treat any subset as valid."""

    found: bool
    domain: str
    listing_id: Optional[int] = None
    auction_type: Optional[str] = None  # "CLOSEOUT" / "EXPIRY_AUCTION"
    current_price_dollars: Optional[Decimal] = None
    estimated_value_dollars: Optional[Decimal] = None
    end_time_utc: Optional[datetime] = None
    bid_count: Optional[int] = None
    raw_inner_xml: Optional[str] = None  # for debugging — what GoDaddy returned
    failure_message: Optional[str] = None


@router.get("", response_model=LookupResponse)
async def lookup(domain: str) -> LookupResponse:
    """Look up any domain in GoDaddy's auctions inventory."""
    domain = (domain or "").strip().lower()
    if not domain or "." not in domain:
        raise HTTPException(
            status_code=400,
            detail="Provide a valid domain name (e.g., crystalpro.com)",
        )

    cfg = get_settings()
    auth = GoDaddyAuth(key=cfg.godaddy_api_key, secret=cfg.godaddy_api_secret)
    client_cfg = GoDaddyClientConfig(
        rest_base_url=cfg.rest_base_url,
        customer_id=cfg.godaddy_customer_id,
    )

    async with GoDaddyClient(auth=auth, config=client_cfg) as client:
        soap = SoapClient(client)
        inner_xml = await soap.get_auction_details_by_domain_name(domain)

    if inner_xml is None:
        return LookupResponse(
            found=False,
            domain=domain,
            failure_message="GoDaddy SOAP call failed or returned no result.",
        )

    # GoDaddy's response shape isn't formally documented; we parse defensively.
    # Expected attribute-style XML similar to the estimate response.
    # Example we anticipate (will need refinement on first real call):
    #   <GetAuctionDetailsByDomainName Result="Success" AuctionID="123"
    #       Domain="x.com" Type="Closeout" Price="$5.00" Value="$1341"
    #       EndTime="2026-06-03T18:00:00Z" BidCount="0" />
    parsed = _parse_details(inner_xml)
    if parsed is None:
        return LookupResponse(
            found=False,
            domain=domain,
            failure_message="Domain not currently in any GoDaddy auction.",
            raw_inner_xml=inner_xml[:1000],  # for debugging shape mismatches
        )

    # The SOAP response carries no listing id (confirmed 2026-06-03) — but
    # the full-inventory index has the REAL id for every feed listing
    # (extracted from the `link` URL). Filling it here is what makes
    # "+ Add domain" entries snipeable (2026-07-14).
    if parsed.found and parsed.listing_id is None:
        try:
            from app.godaddy.inventory_index import resolve_domain

            indexed = await resolve_domain(domain)
            if indexed is not None:
                parsed.listing_id = indexed.listing_id
                if parsed.auction_type is None:
                    parsed.auction_type = indexed.auction_type
        except Exception:  # noqa: BLE001 — index is best-effort
            logger.exception("Inventory-index resolution failed for %s", domain)

    return parsed


def _parse_details(inner_xml: str) -> Optional[LookupResponse]:
    """Parse GetAuctionDetailsByDomainName response.

    Real response shape (confirmed 2026-06-03 against crystalpro.com):
        <GetAuctionDetailsByDomainName
            IsValid="True"
            DomainName="crystalpro.com"
            AuctionEndTime="06/09/2026 02:06 PM (PDT)"
            BidCount="10"
            Price="$41"
            ValuationPrice="$6,079"
            CreateDate="05/09/1998"
            Traffic="0"
            BidIncrementAmount="$5"
            AuctionModel="Bid"
            AuditDateTime="6/3/2026 11:02:30 AM" />

    Success is signalled by `IsValid="True"`. When the domain isn't in an
    auction, `IsValid="False"` (and most fields are missing).
    """
    fail = re.search(r'IsValid\s*=\s*"False"', inner_xml)
    if fail:
        return LookupResponse(
            found=False,
            domain="",
            failure_message="Domain not currently in any GoDaddy auction.",
            raw_inner_xml=inner_xml[:1000],
        )

    success_tag = re.search(
        r"<\w+\b([^<]*?IsValid\s*=\s*\"True\"[^<]*?)/?>",
        inner_xml,
        re.DOTALL,
    )
    if not success_tag:
        return None
    attrs_blob = success_tag.group(1)

    def attr(*names: str) -> Optional[str]:
        for n in names:
            m = re.search(rf'\b{re.escape(n)}\s*=\s*"([^"]*)"', attrs_blob)
            if m:
                return m.group(1)
        return None

    def attr_dollars(*names: str) -> Optional[Decimal]:
        raw = attr(*names)
        if raw is None or raw == "" or raw == "N/A":
            return None
        cleaned = raw.replace("$", "").replace(",", "").strip()
        try:
            return Decimal(cleaned)
        except Exception:
            return None

    def attr_int(*names: str) -> Optional[int]:
        raw = attr(*names)
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def attr_dt(*names: str) -> Optional[datetime]:
        raw = attr(*names)
        if not raw:
            return None
        # GoDaddy uses formats like "06/09/2026 02:06 PM (PDT)". Strip the
        # parenthesized timezone tag (Python's strptime doesn't handle
        # arbitrary tz abbreviations) and apply a fixed PT offset since
        # GoDaddy's auction times are always in Pacific.
        clean = re.sub(r"\s*\([A-Z]{2,5}\)\s*$", "", raw).strip()
        for fmt in (
            "%m/%d/%Y %I:%M %p",      # "06/09/2026 02:06 PM"
            "%m/%d/%Y %I:%M:%S %p",   # "6/3/2026 11:02:30 AM"
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%S",
        ):
            try:
                dt = datetime.strptime(clean, fmt)
                if dt.tzinfo is None:
                    if fmt.endswith("Z"):
                        # A trailing Z is explicit UTC — do NOT apply the
                        # Pacific assumption (was a 7-hour error).
                        dt = dt.replace(tzinfo=timezone.utc)
                    else:
                        # GoDaddy's US-format auction times are Pacific.
                        # PDT = UTC-7; approximating year-round is fine for
                        # snipe windows (off 1h in PST winter).
                        from datetime import timedelta
                        dt = dt.replace(tzinfo=timezone(timedelta(hours=-7)))
                # Normalize to UTC for our DB schema.
                return dt.astimezone(timezone.utc)
            except ValueError:
                continue
        return None

    auction_model = (attr("AuctionModel") or "").upper()
    # GoDaddy returns:
    #   AuctionModel="Bid"      → standard expiry auction
    #   AuctionModel="Closeout" → buy-now closeout
    if "CLOSEOUT" in auction_model:
        auction_type = "CLOSEOUT"
    elif "BID" in auction_model or "AUCTION" in auction_model:
        auction_type = "EXPIRY_AUCTION"
    else:
        auction_type = auction_model or None

    return LookupResponse(
        found=True,
        domain=attr("DomainName", "Domain") or "",
        listing_id=attr_int("AuctionID", "ListingID", "ListingId", "Id"),
        auction_type=auction_type,
        current_price_dollars=attr_dollars("Price", "CurrentPrice", "CurrentBid"),
        estimated_value_dollars=attr_dollars(
            "ValuationPrice", "Value", "EstimatedValue", "GDValue"
        ),
        end_time_utc=attr_dt("AuctionEndTime", "EndTime", "EndDate", "EndsAt"),
        bid_count=attr_int("BidCount", "Bids"),
        raw_inner_xml=inner_xml[:1000],
    )
