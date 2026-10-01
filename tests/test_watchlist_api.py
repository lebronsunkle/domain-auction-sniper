"""Reopen flow (2026-09-07 dellport): a 504 mid-purchase tombstoned the
entry as 'executed' while the domain stayed on sale. PUT {reopen: true}
re-arms it; plain updates must NOT."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.schemas import WatchlistUpdate
from app.api.watchlist import update_watchlist
from app.models.auction import Auction
from app.models.base import Base
from app.models.watchlist import WatchlistEntry


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _seed(session, status="executed"):
    now = datetime.now(timezone.utc)
    auction = Auction(
        listing_id=723396031, domain="dellport.com", tld="com",
        auction_type="CLOSEOUT", current_price=Decimal("50.00"),
        end_time_utc=now + timedelta(days=1), bid_count=0, has_bids=False,
        last_synced_at=now, first_seen_at=now, status="active",
    )
    session.add(auction)
    await session.flush()
    entry = WatchlistEntry(
        auction_id=auction.id, listing_id=auction.listing_id,
        domain=auction.domain, max_bid_dollars=Decimal("55.00"),
        is_armed=True, created_at=now, updated_at=now, status=status,
    )
    session.add(entry)
    await session.commit()
    return entry


@pytest.mark.asyncio
async def test_reopen_rearms_executed_entry(session):
    entry = await _seed(session, status="executed")
    out = await update_watchlist(
        entry.id, WatchlistUpdate(reopen=True, max_bid_dollars=Decimal("11.00")), session
    )
    assert out.status == "pending"
    assert out.max_bid_dollars == Decimal("11.00")
    assert "re-armed by operator" in (out.note or "")


@pytest.mark.asyncio
async def test_plain_update_does_not_rearm(session):
    entry = await _seed(session, status="executed")
    out = await update_watchlist(
        entry.id, WatchlistUpdate(max_bid_dollars=Decimal("11.00")), session
    )
    assert out.status == "executed"  # untouched without the explicit flag


@pytest.mark.asyncio
async def test_reopen_does_not_touch_pending(session):
    entry = await _seed(session, status="pending")
    out = await update_watchlist(entry.id, WatchlistUpdate(reopen=True), session)
    assert out.status == "pending"
    assert "re-armed" not in (out.note or "")
