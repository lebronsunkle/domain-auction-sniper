"""
Persistence layer for inventory sync.

Upserts scored listings into the auctions table. Idempotent: re-running the
sync the same day finds existing listing_ids and updates them in place
rather than duplicating. first_seen_at is preserved on existing rows so we
can tell how long an auction has been in our system.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auction import Auction
from worker.inventory_sync import ScoredListing

logger = logging.getLogger(__name__)


async def upsert_scored_listings(
    session: AsyncSession,
    scored: Iterable[ScoredListing],
) -> tuple[int, int]:
    """Insert new auctions and update existing ones.

    Returns (inserted_count, updated_count).
    """
    now = datetime.now(timezone.utc)
    inserted = 0
    updated = 0

    # Pull existing rows in bulk to avoid one-query-per-listing.
    listing_ids = [s.listing_id for s in scored if s.listing_id > 0]
    if not listing_ids:
        return (0, 0)

    result = await session.execute(
        select(Auction).where(Auction.listing_id.in_(listing_ids))
    )
    existing_by_id: dict[int, Auction] = {a.listing_id: a for a in result.scalars().all()}

    for s in scored:
        if s.listing_id <= 0:
            continue  # skip malformed feed rows
        existing = existing_by_id.get(s.listing_id)
        if existing is None:
            # New listing — full insert.
            session.add(_build_new(s, now))
            inserted += 1
        else:
            # Existing listing — refresh state, preserve first_seen_at.
            _apply_update(existing, s, now)
            updated += 1

    await session.commit()
    logger.info("Persisted scored listings: %d inserted, %d updated", inserted, updated)
    return (inserted, updated)


def _build_new(s: ScoredListing, now: datetime) -> Auction:
    return Auction(
        listing_id=s.listing_id,
        domain=s.domain,
        tld=s.tld,
        auction_type=s.auction_type,
        current_price=_to_decimal(s.current_price),
        estimated_value=_to_decimal(s.estimated_value),
        end_time_utc=_parse_iso(s.end_time_utc),
        bid_count=0,
        has_bids=s.has_bids,
        score=s.score,
        score_breakdown=_breakdown_json(s),
        first_seen_at=now,
        last_synced_at=now,
        status="active",
    )


def _apply_update(existing: Auction, s: ScoredListing, now: datetime) -> None:
    # Update everything except first_seen_at (we want to preserve when we
    # FIRST observed this listing).
    existing.domain = s.domain
    existing.tld = s.tld
    existing.auction_type = s.auction_type
    existing.current_price = _to_decimal(s.current_price)
    existing.estimated_value = _to_decimal(s.estimated_value)
    existing.end_time_utc = _parse_iso(s.end_time_utc)
    existing.has_bids = s.has_bids
    existing.score = s.score
    existing.score_breakdown = _breakdown_json(s)
    existing.last_synced_at = now
    existing.status = "active"


def _breakdown_json(s: ScoredListing) -> str:
    """Persist just the cheap themes list as the score_breakdown text. The
    full breakdown (component scores, notes, valuation placeholders) can be
    recomputed on demand by the API layer calling engine.score(s.domain).
    Stripping this here is what lets us scale to 935k listings on the GH
    runner without OOM."""
    import json
    return json.dumps({"themes": s.themes})


def _to_decimal(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _to_json_text(d) -> str:
    import json
    return json.dumps(d)
