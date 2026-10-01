"""POST /api/closeout/buy — manual one-click closeout purchase.

The trigger worker handles auto-buy when a watchlist max_bid is set. This
endpoint is for the OTHER case: the client is looking at a closeout right now
and wants to buy it immediately at the current price, without setting a
max bid and waiting for the worker.

Flow:
  1. Look up (or bootstrap) the auction row from the listing_id.
  2. Call SOAP `estimate_closeout_price` to get the current price + key.
  3. Verify total_cost <= max_total_dollars guard from the request.
  4. Run safety governors (kill switch, caps, sanity multiplier).
  5. Call SOAP `execute_closeout_purchase` — actually buys it.
  6. Record a Purchase row + audit log entry.

In DRY_RUN mode the execute step is skipped and we return a "would have
bought" response with the estimate details. That gives the UI a believable
demo path before we have the live execute SOAP wired up.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.godaddy.client import GoDaddyAuth, GoDaddyClient, GoDaddyClientConfig
from app.godaddy.soap import SoapClient
from app.models.auction import Auction
from app.models.purchase import Purchase
from app.models.watchlist import WatchlistEntry
from app.safety.governors import GovernorContext, GovernorRejection, check_all
from app.safety.purchase_guard import (
    acquire_listing_lock,
    begin_in_flight,
    complete_attempt,
    find_recent_money_attempt,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Same DRY_RUN flag as the trigger worker so they stay in sync.
DRY_RUN = os.getenv("TRIGGER_DRY_RUN", "true").lower() not in ("false", "0", "no")

# 2026-08-18: GoDaddy shipped an OFFICIAL Instant Purchase API (preview +
# purchase with server-side PRICE_MISMATCH protection). This flag switches
# the manual Buy Now from the reverse-engineered SOAP flow to the supported
# API. Default OFF until validated with one cheap live buy; the SOAP path
# remains as fallback. Set USE_INSTANT_PURCHASE_API=true (Fly secret) to
# enable. The worker's auto-buy migrates after this path is proven.
USE_INSTANT_PURCHASE_API = os.getenv(
    "USE_INSTANT_PURCHASE_API", "false"
).lower() in ("true", "1", "yes")


class BuyNowRequest(BaseModel):
    listing_id: int
    domain: str
    max_total_dollars: Decimal
    # Optional auction-snapshot fields. The dashboard ships these from its
    # static JSON so we can bootstrap an auctions row even if the daily
    # sync hasn't seen this listing yet.
    tld: Optional[str] = None
    current_price: Optional[Decimal] = None
    estimated_value: Optional[Decimal] = None


class BuyNowResponse(BaseModel):
    success: bool
    dry_run: bool
    domain: str
    current_price_dollars: Optional[Decimal] = None
    total_cost_dollars: Optional[Decimal] = None
    order_id: Optional[str] = None
    message: str


@router.post("/buy", response_model=BuyNowResponse)
async def buy_closeout_now(
    payload: BuyNowRequest,
    db: AsyncSession = Depends(get_db),
) -> BuyNowResponse:
    cfg = get_settings()

    # 1. Get or bootstrap an auction row. The worker uses this to know
    # auction_type later. For a manual Buy Now this is mostly bookkeeping.
    auction_q = await db.execute(
        select(Auction).where(Auction.listing_id == payload.listing_id)
    )
    auction = auction_q.scalar_one_or_none()
    if auction is None:
        now = datetime.now(timezone.utc)
        auction = Auction(
            listing_id=payload.listing_id,
            domain=payload.domain,
            tld=payload.tld or payload.domain.rsplit(".", 1)[-1].lower(),
            auction_type="CLOSEOUT",
            current_price=payload.current_price,
            estimated_value=payload.estimated_value,
            end_time_utc=None,  # closeouts don't have a tick-down end time
            bid_count=0,
            has_bids=False,
            last_synced_at=now,
            first_seen_at=now,
            status="active",
        )
        db.add(auction)
        await db.flush()

    # 2. Build a SOAP client and call estimate.
    auth = GoDaddyAuth(key=cfg.godaddy_api_key, secret=cfg.godaddy_api_secret)
    client_cfg = GoDaddyClientConfig(
        rest_base_url=cfg.rest_base_url,
        customer_id=cfg.godaddy_customer_id,
    )
    async with GoDaddyClient(auth=auth, config=client_cfg) as client:
        soap = SoapClient(client)
        instant = None
        instant_preview = None

        if USE_INSTANT_PURCHASE_API:
            # Official API path: preview gives the exact all-in total that
            # the purchase call will require (PRICE_MISMATCH otherwise).
            from app.godaddy.instant import InstantPurchaseClient

            instant = InstantPurchaseClient(client)
            previews = await instant.preview([payload.domain])
            instant_preview = previews.get(payload.domain.lower())
            if instant_preview is None or not instant_preview.success:
                reason = (
                    instant_preview.failure_reason
                    if instant_preview
                    else "no result returned"
                )
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"GoDaddy declined the preview for {payload.domain}: "
                        f"{reason}. The domain may no longer be in closeout."
                    ),
                )
            total = instant_preview.total_dollars
            listing_price = instant_preview.auction_price_dollars
        else:
            estimate = await soap.estimate_closeout_price(payload.domain)

            if not estimate.success:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"GoDaddy declined the estimate for {payload.domain}: "
                        f"{estimate.failure_message or '(no message)'}. The "
                        "domain may no longer be in closeout."
                    ),
                )

            total = estimate.total_dollars
            listing_price = estimate.listing_price_dollars

        if total is None:
            raise HTTPException(
                status_code=502,
                detail="GoDaddy returned pricing but no total_dollars field.",
            )

        # 3. Sanity gate: total must be <= the user's posted ceiling.
        if total > payload.max_total_dollars:
            return BuyNowResponse(
                success=False,
                dry_run=DRY_RUN,
                domain=payload.domain,
                current_price_dollars=listing_price,
                total_cost_dollars=total,
                message=(
                    f"Current total cost ${total} exceeds your max "
                    f"${payload.max_total_dollars}. Refresh to retry "
                    f"at the new price, or raise your ceiling."
                ),
            )

        # 4a. Duplicate-purchase guard (audit R3). Serialize per listing,
        # then refuse if any attempt for this listing is in flight or
        # completed within the window — covers double-taps, two devices,
        # and racing the trigger worker.
        await acquire_listing_lock(db, payload.listing_id)
        dup = await find_recent_money_attempt(db, payload.listing_id)
        if dup is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"A purchase attempt for {payload.domain} is already "
                    f"{dup.outcome} (started {dup.fired_at:%H:%M:%S} UTC). "
                    "Not firing again — check the watchlist/purchase history."
                ),
            )

        # 4b. Safety governors.
        ctx = GovernorContext(
            action_type="CLOSEOUT_BUY",
            total_cost_dollars=total,
            listing_id=payload.listing_id,
            domain=payload.domain,
            reference_floor_dollars=listing_price,
        )
        try:
            await check_all(db, ctx)
        except GovernorRejection as rej:
            raise HTTPException(status_code=403, detail=str(rej))

        # 5. Execute (or dry-run).
        now = datetime.now(timezone.utc)

        # Look up an associated watchlist entry, if any — we link purchases
        # back to the watchlist row when one exists for audit purposes.
        wl_q = await db.execute(
            select(WatchlistEntry).where(
                WatchlistEntry.listing_id == payload.listing_id
            )
        )
        watchlist_entry = wl_q.scalar_one_or_none()

        if DRY_RUN:
            logger.warning(
                "DRY RUN: would have closeout-bought %s for $%s",
                payload.domain,
                total,
            )
            purchase = Purchase(
                watchlist_entry_id=watchlist_entry.id if watchlist_entry else None,
                listing_id=payload.listing_id,
                domain=payload.domain,
                action_type="CLOSEOUT_BUY",
                amount_dollars=total,
                outcome="dry_run",
                fired_at=now,
                raw_response="DRY_RUN — buy now click; no API call made",
            )
            db.add(purchase)
            if watchlist_entry:
                watchlist_entry.status = "executed"
                watchlist_entry.updated_at = now
            await db.commit()
            return BuyNowResponse(
                success=True,
                dry_run=True,
                domain=payload.domain,
                current_price_dollars=listing_price,
                total_cost_dollars=total,
                message=(
                    f"DRY RUN: would have purchased {payload.domain} for "
                    f"${total} (listing ${listing_price})."
                ),
            )

        # Live path (audit R5): commit an in_flight Purchase row BEFORE the
        # SOAP call so a crash mid-call can never leave untracked spend —
        # and so concurrent attempts see this one in the daily cap.
        purchase = await begin_in_flight(
            db,
            watchlist_entry_id=watchlist_entry.id if watchlist_entry else None,
            listing_id=payload.listing_id,
            domain=payload.domain,
            action_type="CLOSEOUT_BUY",
            amount_dollars=total,
        )

        try:
            if USE_INSTANT_PURCHASE_API:
                api_result = await instant.purchase(
                    domain_name=payload.domain,
                    total_price_micros=instant_preview.total_price_micros,
                )
                # Adapt to the shape the rest of this handler expects.
                class _R:  # noqa: N801 — tiny adapter
                    success = api_result.success
                    order_id = api_result.order_id
                    failure_message = api_result.failure_reason
                result = _R()
            else:
                result = await soap.execute_closeout_purchase(
                    domain_name=payload.domain,
                    price_key=estimate.price_key,
                )
        except Exception as exc:
            complete_attempt(
                purchase,
                outcome="error",
                raw_response=f"EXCEPTION: {type(exc).__name__}: {exc}",
            )
            await db.commit()
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Purchase call failed for {payload.domain}: "
                    f"{type(exc).__name__}. IMPORTANT: verify against GoDaddy "
                    "order history before retrying — the order may have gone "
                    "through despite the error."
                ),
            )

        # Record the result on the in_flight row.
        complete_attempt(
            purchase,
            outcome="won" if result.success else "lost",
            order_id=result.order_id,
            raw_response=result.failure_message or "OK",
        )
        if watchlist_entry:
            watchlist_entry.status = "won" if result.success else "lost"
            watchlist_entry.updated_at = now
        await db.commit()

        return BuyNowResponse(
            success=result.success,
            dry_run=False,
            domain=payload.domain,
            current_price_dollars=listing_price,
            total_cost_dollars=total,
            order_id=result.order_id,
            message=result.failure_message or "Purchase completed.",
        )
