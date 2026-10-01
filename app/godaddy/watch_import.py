"""Import GoDaddy-watchlist stars into the sniper (2026-09-24, the client).

Every Listings Availability response carries `watching: true/false` — the
account's OWN GoDaddy-watchlist flag for that listing. The client stars things
on auctions.godaddy.com during his morning ritual; this module notices
those stars whenever they pass through ANY availability check and creates
the matching sniper watchlist entry automatically (unarmed — no bid, no
auto-buy — just visible, exactly like a hand-star in the dashboard).

One-way by design: GoDaddy -> sniper. There is no public API for WRITING
to the GoDaddy watchlist (confirmed against the Aug-2026 Expiry API launch
and the current developer docs), so sniper stars do not flow back. If an
imported entry is deleted here while still watched on GoDaddy, the next
sighting re-imports it — GoDaddy is treated as the source of truth for
its own stars.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auction import Auction
from app.models.watchlist import WatchlistEntry

from .live_listings import LiveListing

logger = logging.getLogger(__name__)


def _parse_end(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _auction_type_for(live: LiveListing) -> str:
    # House rule (cookent/dellport lessons): price evidence beats GoDaddy's
    # label. A buy-now price means closeout regardless of listingType.
    if live.price_buy_it_now_micros is not None:
        return "CLOSEOUT"
    return "EXPIRY_AUCTION"


async def import_gd_watched(
    db: AsyncSession,
    results: Iterable[LiveListing],
) -> list[str]:
    """Create sniper watchlist entries for GoDaddy-watched listings.

    Safe to call on every /live pass: does nothing (and hits the DB at
    most once per WATCHED domain) unless a listing has watching=True,
    is AVAILABLE, carries a real listing_id, and isn't already tracked.
    Returns the domains imported. Never raises past itself — callers on
    hot paths wrap it best-effort anyway.
    """
    imported: list[str] = []
    now = datetime.now(timezone.utc)

    for live in results:
        if not live.watching or live.status != "AVAILABLE" or not live.listing_id:
            continue

        existing = await db.execute(
            select(WatchlistEntry.id).where(
                (WatchlistEntry.listing_id == live.listing_id)
                | (WatchlistEntry.domain == live.domain)
            ).limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            continue

        auction = (
            await db.execute(
                select(Auction).where(Auction.listing_id == live.listing_id)
            )
        ).scalar_one_or_none()
        if auction is None:
            auction = Auction(
                listing_id=live.listing_id,
                domain=live.domain,
                tld=live.domain.rsplit(".", 1)[-1],
                auction_type=_auction_type_for(live),
                current_price=live.price_current_dollars,
                end_time_utc=_parse_end(live.auction_end_at),
                bid_count=live.bids_count or 0,
                has_bids=(live.bids_count or 0) > 0,
                last_synced_at=now,
                first_seen_at=now,
                status="active",
            )
            db.add(auction)
            await db.flush()

        entry = WatchlistEntry(
            auction_id=auction.id,
            listing_id=live.listing_id,
            domain=live.domain,
            max_bid_dollars=None,
            is_armed=False,
            note=f"[imported from GoDaddy watchlist {now.date().isoformat()}]",
            created_at=now,
            updated_at=now,
            status="pending",
        )
        db.add(entry)
        imported.append(live.domain)

    if imported:
        await db.commit()
        logger.info("GD-watch import: %d new entries: %s", len(imported), imported)
    return imported
