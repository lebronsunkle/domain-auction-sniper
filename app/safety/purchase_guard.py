"""Duplicate-purchase protection + crash-safe purchase records.

Closes audit R3 and R5 (2026-07-14):

R3 — nothing stopped the same listing being bought twice: a double-tapped
Buy button, a manual Buy Now racing the trigger worker, or two concurrent
requests each passing the daily-cap check before either committed a
Purchase row (TOCTOU).

R5 — the Purchase row was written only AFTER the SOAP/REST call. A crash
between "money moved" and "commit" left real spend with no record, which
also silently under-counted the daily spend cap.

The pattern every money-moving call site now follows:

    await acquire_listing_lock(session, listing_id)   # serialize per listing
    dup = await find_recent_money_attempt(session, listing_id)
    if dup: bail out
    p = await begin_in_flight(session, ...)           # committed BEFORE the call
    result = <the actual GoDaddy call>
    complete_attempt(p, outcome=..., ...)             # update + commit

"in_flight" rows count toward the daily-spend governor (its query includes
every outcome except "outbid"), so concurrent attempts see each other's
reserved spend even before confirmation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.purchase import Purchase

logger = logging.getLogger(__name__)

# Outcomes that mean "money may already be moving/moved for this listing —
# do not fire again". "error"/"lost"/"outbid" deliberately excluded so a
# failed attempt can be retried.
BLOCKING_OUTCOMES = ("in_flight", "won", "dry_run")

# How far back a blocking attempt suppresses a new one. Long enough to
# cover any conceivable SOAP latency + operator confusion window; short
# enough that a genuine repurchase attempt next day isn't blocked.
DUPLICATE_WINDOW_MINUTES = 10


async def acquire_listing_lock(session: AsyncSession, listing_id: int) -> None:
    """Serialize money attempts per listing across ALL connections.

    Uses a Postgres transaction-scoped advisory lock (released automatically
    at commit/rollback). On other dialects (SQLite in tests) it's a no-op —
    the recent-attempt check still provides best-effort protection there.
    """
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        # Fold the 63-bit listing id into signed bigint space for pg.
        key = listing_id & 0x7FFF_FFFF_FFFF_FFFF
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": key}
        )


async def find_recent_money_attempt(
    session: AsyncSession,
    listing_id: int,
    *,
    window_minutes: int = DUPLICATE_WINDOW_MINUTES,
) -> Optional[Purchase]:
    """Most recent blocking attempt for this listing inside the window."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    result = await session.execute(
        select(Purchase)
        .where(
            Purchase.listing_id == listing_id,
            Purchase.fired_at >= cutoff,
            Purchase.outcome.in_(BLOCKING_OUTCOMES),
        )
        .order_by(Purchase.fired_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def begin_in_flight(
    session: AsyncSession,
    *,
    watchlist_entry_id: Optional[int],
    listing_id: int,
    domain: str,
    action_type: str,
    amount_dollars: Decimal,
) -> Purchase:
    """Create and COMMIT an in_flight Purchase row before the money call.

    If the process dies mid-call, this row is the evidence that money may
    have moved — and it keeps counting against the daily cap until an
    operator reconciles it against GoDaddy order history.
    """
    p = Purchase(
        watchlist_entry_id=watchlist_entry_id,
        listing_id=listing_id,
        domain=domain,
        action_type=action_type,
        amount_dollars=amount_dollars,
        outcome="in_flight",
        fired_at=datetime.now(timezone.utc),
        raw_response="in_flight — API call dispatched, awaiting response",
    )
    session.add(p)
    await session.commit()
    return p


def complete_attempt(
    p: Purchase,
    *,
    outcome: str,
    order_id: Optional[str] = None,
    bid_id: Optional[str] = None,
    raw_response: Optional[str] = None,
) -> None:
    """Fill in the result on an in_flight row (caller commits)."""
    p.outcome = outcome
    p.godaddy_order_id = order_id or p.godaddy_order_id
    p.godaddy_bid_id = bid_id or p.godaddy_bid_id
    p.confirmed_at = datetime.now(timezone.utc)
    if raw_response is not None:
        p.raw_response = raw_response
