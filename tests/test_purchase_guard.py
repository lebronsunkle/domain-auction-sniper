"""Tests for duplicate-purchase protection and in-flight records (R3/R5),
plus the worker's snipe-priority ordering (R10)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.auction import Auction
from app.models.base import Base
from app.models.purchase import Purchase
from app.models.watchlist import WatchlistEntry
from app.safety.purchase_guard import (
    begin_in_flight,
    complete_attempt,
    find_recent_money_attempt,
)
from worker.trigger import _load_armed_entries


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _add_purchase(session, *, listing_id=1, outcome="won", minutes_ago=1):
    p = Purchase(
        listing_id=listing_id,
        domain="x.com",
        action_type="CLOSEOUT_BUY",
        amount_dollars=Decimal("20.00"),
        outcome=outcome,
        fired_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )
    session.add(p)
    await session.commit()
    return p


@pytest.mark.asyncio
async def test_recent_won_blocks(session):
    await _add_purchase(session, outcome="won", minutes_ago=2)
    assert await find_recent_money_attempt(session, 1) is not None


@pytest.mark.asyncio
async def test_recent_in_flight_blocks(session):
    await _add_purchase(session, outcome="in_flight", minutes_ago=1)
    assert await find_recent_money_attempt(session, 1) is not None


@pytest.mark.asyncio
async def test_error_and_outbid_do_not_block_retry(session):
    await _add_purchase(session, outcome="error", minutes_ago=1)
    await _add_purchase(session, outcome="outbid", minutes_ago=1)
    assert await find_recent_money_attempt(session, 1) is None


@pytest.mark.asyncio
async def test_old_attempt_outside_window_does_not_block(session):
    await _add_purchase(session, outcome="won", minutes_ago=30)
    assert await find_recent_money_attempt(session, 1) is None


@pytest.mark.asyncio
async def test_other_listing_does_not_block(session):
    await _add_purchase(session, listing_id=2, outcome="won", minutes_ago=1)
    assert await find_recent_money_attempt(session, 1) is None


@pytest.mark.asyncio
async def test_in_flight_row_committed_before_and_completed_after(session):
    """R5: the record exists (committed) before any money call would run,
    and completion fills the result in-place."""
    p = await begin_in_flight(
        session,
        watchlist_entry_id=None,
        listing_id=99,
        domain="y.com",
        action_type="CLOSEOUT_BUY",
        amount_dollars=Decimal("66.37"),
    )
    # Simulate what a concurrent request would see mid-call.
    concurrent_view = await find_recent_money_attempt(session, 99)
    assert concurrent_view is not None and concurrent_view.outcome == "in_flight"

    complete_attempt(p, outcome="won", order_id="4104099999", raw_response="OK")
    await session.commit()
    rows = (await session.execute(select(Purchase))).scalars().all()
    assert len(rows) == 1
    assert rows[0].outcome == "won"
    assert rows[0].godaddy_order_id == "4104099999"
    assert rows[0].confirmed_at is not None


# --- R10: worker processes closest-to-ending entries first ---------------------


@pytest.mark.asyncio
async def test_armed_entries_ordered_by_end_time(session):
    now = datetime.now(timezone.utc)
    specs = [
        ("late.com", 701, now + timedelta(hours=8)),
        ("soon.com", 702, now + timedelta(minutes=2)),
        ("noend.com", 703, None),
        ("mid.com", 704, now + timedelta(hours=1)),
    ]
    for domain, lid, end in specs:
        a = Auction(
            listing_id=lid, domain=domain, tld="com",
            auction_type="EXPIRY_AUCTION", end_time_utc=end,
            bid_count=0, has_bids=False,
            last_synced_at=now, first_seen_at=now, status="active",
        )
        session.add(a)
        await session.flush()
        session.add(WatchlistEntry(
            auction_id=a.id, listing_id=lid, domain=domain,
            max_bid_dollars=Decimal("10.00"), is_armed=True,
            created_at=now, updated_at=now, status="pending",
        ))
    await session.commit()

    entries = await _load_armed_entries(session)
    domains = [e.domain for e in entries]
    assert domains == ["soon.com", "mid.com", "late.com", "noend.com"], (
        "imminent snipes must come first; unknown end times last"
    )
