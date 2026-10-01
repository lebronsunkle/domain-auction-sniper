"""GoDaddy-watchlist import (2026-09-24): stars made on auctions.godaddy.com
flow into the sniper watchlist via the `watching` flag on availability
responses. One-way, unarmed, dedupe-safe."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.godaddy.live_listings import LiveListing
from app.godaddy.watch_import import import_gd_watched
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


def _live(domain="dnaark.com", watching=True, status="AVAILABLE",
          listing_id=723400001, buy_now=None, bids=2):
    return LiveListing(
        domain=domain, status=status, listing_id=listing_id,
        listing_type="EXPIRY_AUCTIONS", bids_count=bids,
        auction_end_at="2026-09-26T18:00:00Z",
        price_current_micros=11_000_000,
        price_buy_it_now_micros=buy_now,
        watching=watching,
    )


@pytest.mark.asyncio
async def test_watched_listing_creates_unarmed_entry(session):
    imported = await import_gd_watched(session, [_live()])
    assert imported == ["dnaark.com"]
    entry = (
        await session.execute(select(WatchlistEntry).where(WatchlistEntry.domain == "dnaark.com"))
    ).scalar_one()
    assert entry.is_armed is False
    assert entry.max_bid_dollars is None
    assert "imported from GoDaddy watchlist" in (entry.note or "")
    auction = (
        await session.execute(select(Auction).where(Auction.listing_id == 723400001))
    ).scalar_one()
    assert auction.auction_type == "EXPIRY_AUCTION"  # no buy-now price
    assert auction.has_bids is True


@pytest.mark.asyncio
async def test_buy_now_price_beats_label(session):
    await import_gd_watched(session, [_live(buy_now=50_000_000)])
    auction = (
        await session.execute(select(Auction).where(Auction.listing_id == 723400001))
    ).scalar_one()
    assert auction.auction_type == "CLOSEOUT"


@pytest.mark.asyncio
async def test_not_watching_and_unavailable_skipped(session):
    imported = await import_gd_watched(session, [
        _live(watching=False),
        _live(domain="gone.com", listing_id=723400002, status="UNAVAILABLE"),
        _live(domain="noid.com", listing_id=None),
    ])
    assert imported == []


@pytest.mark.asyncio
async def test_existing_entry_not_duplicated(session):
    assert await import_gd_watched(session, [_live()]) == ["dnaark.com"]
    # Second sighting (same listing) and a relist (same domain, new id):
    assert await import_gd_watched(session, [_live()]) == []
    assert await import_gd_watched(session, [_live(listing_id=723400099)]) == []
    n = (
        await session.execute(select(WatchlistEntry).where(WatchlistEntry.domain == "dnaark.com"))
    ).scalars().all()
    assert len(n) == 1
