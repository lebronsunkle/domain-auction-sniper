"""Tests for the sweet-spot automation (expiry -> closeout handoff).

The client's core play: find an expiry auction sitting at $1 with no bids. If
nobody bids, it converts to a $50 closeout — race the conversion and buy
instantly at/under his max. If someone DOES bid, the existing snipe logic
covers the bidding war. These tests pin the handoff machinery:

  * expiry confirmed over -> entry flips to closeout watch (not expired)
  * closeout watch fires a (dry-run) buy the moment the price appears at
    or under the max
  * failed estimates back off instead of polling at 1 req/s
  * watch gives up N days after auction end if the conversion never comes
  * a ladder rung above max_bid never fires (audit R4)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.godaddy.soap import CloseoutEstimate
from app.models.auction import Auction
from app.models.base import Base
from app.models.purchase import Purchase
from app.models.settings import SystemSettings
from app.models.watchlist import WatchlistEntry
from worker import trigger
from worker.trigger import _evaluate_closeout, _evaluate_expiry, _should_fire


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.fixture(autouse=True)
def _clear_worker_caches():
    trigger._fresh_state_cache.clear()
    trigger._closeout_state.clear()
    yield
    trigger._fresh_state_cache.clear()
    trigger._closeout_state.clear()


async def _seed_settings(session):
    session.add(SystemSettings(
        id=1,
        per_transaction_cap_dollars=Decimal("500.00"),
        daily_spend_cap_dollars=Decimal("1500.00"),
        closeout_only_mode=False,
        kill_switch_active=False,
        kill_switch_reason="",
        sanity_check_multiplier=Decimal("10.00"),
        updated_at=datetime.now(timezone.utc),
    ))
    await session.commit()


async def _seed_entry(session, *, auction_type="EXPIRY_AUCTION",
                      end_delta_seconds=-30.0, max_bid="280.00"):
    now = datetime.now(timezone.utc)
    auction = Auction(
        listing_id=713000002,
        domain="sweetspot.com",
        tld="com",
        auction_type=auction_type,
        current_price=Decimal("1.00"),
        end_time_utc=now + timedelta(seconds=end_delta_seconds),
        bid_count=0,
        has_bids=False,
        last_synced_at=now,
        first_seen_at=now,
        status="active",
    )
    session.add(auction)
    await session.flush()
    entry = WatchlistEntry(
        auction_id=auction.id,
        listing_id=auction.listing_id,
        domain=auction.domain,
        max_bid_dollars=Decimal(max_bid),
        is_armed=True,
        created_at=now,
        updated_at=now,
        status="pending",
    )
    session.add(entry)
    await session.commit()
    return auction, entry


class FakeSoapGone:
    """Expiry lookup: auction confirmed gone."""

    async def get_auction_details_by_domain_name(self, domain):
        return '<GetAuctionDetailsByDomainName IsValid="False" />'


class FakeSoapEstimate:
    """Closeout estimate with configurable outcome per call."""

    def __init__(self, estimates):
        self.estimates = list(estimates)
        self.calls = 0

    async def estimate_closeout_price(self, domain):
        self.calls += 1
        est = self.estimates.pop(0) if len(self.estimates) > 1 else self.estimates[0]
        return est


def _success_estimate(listing="50.00", total="66.37"):
    return CloseoutEstimate(
        domain_name="sweetspot.com",
        success=True,
        listing_price_micros=int(Decimal(listing) * 1_000_000),
        total_micros=int(Decimal(total) * 1_000_000),
        price_key="KEY123",
    )


def _failed_estimate():
    return CloseoutEstimate(
        domain_name="sweetspot.com",
        success=False,
        failure_message="not in closeout",
    )


# --- the handoff --------------------------------------------------------------


@pytest.mark.asyncio
async def test_expiry_over_transitions_to_closeout_watch(session):
    """Expiry confirmed gone -> auction flips to CLOSEOUT, entry stays
    pending with a sweet-spot note. NOT marked expired."""
    auction, entry = await _seed_entry(session)
    await _evaluate_expiry(session, rest=None, entry=entry, auction=auction,
                           soap=FakeSoapGone())
    assert entry.status == "pending"
    assert auction.auction_type == "CLOSEOUT"
    assert "sweet spot" in (entry.note or "")


@pytest.mark.asyncio
async def test_closeout_watch_buys_when_price_at_or_under_max(session):
    """Post-transition: closeout appears at $50 with $280 max -> fires.
    (DRY_RUN in tests, so we assert the dry-run purchase record.)"""
    await _seed_settings(session)
    auction, entry = await _seed_entry(session, auction_type="CLOSEOUT")
    soap = FakeSoapEstimate([_success_estimate(listing="50.00", total="66.37")])
    assert trigger.DRY_RUN, "tests must never run with TRIGGER_DRY_RUN=false"
    await _evaluate_closeout(session, soap, entry, auction=auction)
    assert entry.status == "executed"
    purchases = (await session.execute(select(Purchase))).scalars().all()
    assert len(purchases) == 1
    assert purchases[0].outcome == "dry_run"
    assert purchases[0].amount_dollars == Decimal("66.37")


@pytest.mark.asyncio
async def test_closeout_watch_holds_above_max(session):
    await _seed_settings(session)
    auction, entry = await _seed_entry(session, auction_type="CLOSEOUT", max_bid="30.00")
    soap = FakeSoapEstimate([_success_estimate(listing="50.00", total="66.37")])
    await _evaluate_closeout(session, soap, entry, auction=auction)
    assert entry.status == "pending"  # $50 > $30 max: wait for the next rung


@pytest.mark.asyncio
async def test_failed_estimates_back_off(session):
    """Two ticks in quick succession while waiting for conversion = one
    SOAP call, thanks to the cadence policy (HOT interval still > tick)."""
    await _seed_settings(session)
    auction, entry = await _seed_entry(session, auction_type="CLOSEOUT")
    soap = FakeSoapEstimate([_failed_estimate()])
    await _evaluate_closeout(session, soap, entry, auction=auction)
    await _evaluate_closeout(session, soap, entry, auction=auction)
    assert soap.calls == 1
    assert entry.status == "pending"


@pytest.mark.asyncio
async def test_watch_gives_up_days_after_auction_end(session):
    """No conversion N days past auction end (domain sold/renewed) ->
    entry marked expired with an explanatory note."""
    await _seed_settings(session)
    days = trigger.CLOSEOUT_GIVEUP_DAYS + 1
    auction, entry = await _seed_entry(
        session, auction_type="CLOSEOUT",
        end_delta_seconds=-days * 24 * 3600,
    )
    soap = FakeSoapEstimate([_failed_estimate()])
    await _evaluate_closeout(session, soap, entry, auction=auction)
    assert entry.status == "expired"
    assert "closeout watch ended" in (entry.note or "")


# --- official Instant Purchase path (2026-08-25 myhomebills migration) --------


class FakeInstant:
    """Stands in for InstantPurchaseClient in the worker fire path."""

    def __init__(self, preview_result, purchase_result=None):
        self._preview = preview_result
        self._purchase = purchase_result
        self.purchase_calls = 0

    async def preview(self, domains):
        from app.godaddy.instant import PurchasePreview
        if self._preview is None:
            return {}
        return {domains[0].lower(): self._preview}

    async def purchase(self, *, domain_name, total_price_micros):
        self.purchase_calls += 1
        return self._purchase


def _preview_ok(total="16.19", listing="5.00"):
    from app.godaddy.instant import PurchasePreview
    return PurchasePreview(
        domain_name="sweetspot.com",
        success=True,
        auction_id=123,
        auction_price_micros=int(Decimal(listing) * 1_000_000),
        total_price_micros=int(Decimal(total) * 1_000_000),
    )


@pytest.mark.asyncio
async def test_official_api_preview_decline_resumes_watch(session, monkeypatch):
    """Flag on + preview declined (purgatory blink) -> NO purchase row, entry
    back to pending. A declined preview must not poison the dup-guard."""
    await _seed_settings(session)
    auction, entry = await _seed_entry(session, auction_type="CLOSEOUT", max_bid="280.00")
    soap = FakeSoapEstimate([_success_estimate(listing="50.00", total="66.37")])
    monkeypatch.setattr(trigger, "USE_INSTANT_PURCHASE_API", True)
    monkeypatch.setattr(trigger, "DRY_RUN", False)
    fake = FakeInstant(preview_result=None)
    import app.godaddy.instant as instant_mod
    monkeypatch.setattr(instant_mod, "InstantPurchaseClient", lambda client: fake)
    soap.client = None  # attribute exists on real SoapClient
    await _evaluate_closeout(session, soap, entry, auction=auction)
    assert entry.status == "pending"
    purchases = (await session.execute(select(Purchase))).scalars().all()
    assert purchases == []
    assert fake.purchase_calls == 0


@pytest.mark.asyncio
async def test_official_api_purchase_success(session, monkeypatch):
    """Flag on + preview + purchase succeed -> won row with order id."""
    from app.godaddy.instant import PurchaseResult

    await _seed_settings(session)
    auction, entry = await _seed_entry(session, auction_type="CLOSEOUT", max_bid="280.00")
    soap = FakeSoapEstimate([_success_estimate(listing="50.00", total="66.37")])
    monkeypatch.setattr(trigger, "USE_INSTANT_PURCHASE_API", True)
    monkeypatch.setattr(trigger, "DRY_RUN", False)
    fake = FakeInstant(
        preview_result=_preview_ok(total="66.37", listing="50.00"),
        purchase_result=PurchaseResult(
            domain_name="sweetspot.com", success=True, order_id="4171518683",
        ),
    )
    import app.godaddy.instant as instant_mod
    monkeypatch.setattr(instant_mod, "InstantPurchaseClient", lambda client: fake)
    soap.client = None
    await _evaluate_closeout(session, soap, entry, auction=auction)
    assert entry.status == "won"
    purchases = (await session.execute(select(Purchase))).scalars().all()
    assert len(purchases) == 1
    assert purchases[0].outcome == "won"
    assert purchases[0].godaddy_order_id == "4171518683"
    assert fake.purchase_calls == 1


@pytest.mark.asyncio
async def test_official_api_price_mismatch_resumes_watch(session, monkeypatch):
    """PRICE_MISMATCH = GoDaddy refused to charge a moved price. Attempt
    recorded lost, but the entry resumes watching for the new price."""
    from app.godaddy.instant import PurchaseResult

    await _seed_settings(session)
    auction, entry = await _seed_entry(session, auction_type="CLOSEOUT", max_bid="280.00")
    soap = FakeSoapEstimate([_success_estimate(listing="50.00", total="66.37")])
    monkeypatch.setattr(trigger, "USE_INSTANT_PURCHASE_API", True)
    monkeypatch.setattr(trigger, "DRY_RUN", False)
    fake = FakeInstant(
        preview_result=_preview_ok(total="66.37", listing="50.00"),
        purchase_result=PurchaseResult(
            domain_name="sweetspot.com", success=False,
            failure_reason="PRICE_MISMATCH",
        ),
    )
    import app.godaddy.instant as instant_mod
    monkeypatch.setattr(instant_mod, "InstantPurchaseClient", lambda client: fake)
    soap.client = None
    await _evaluate_closeout(session, soap, entry, auction=auction)
    assert entry.status == "pending"
    purchases = (await session.execute(select(Purchase))).scalars().all()
    assert len(purchases) == 1
    assert purchases[0].outcome == "lost"


# --- audit R4: ladder must not override max_bid ---------------------------------


def test_ladder_rung_above_max_bid_does_not_fire():
    d = _should_fire(
        current_price=Decimal("50.00"),
        max_bid=Decimal("10.00"),
        ladder=[Decimal("50.00"), Decimal("25.00")],
    )
    assert d.fire is False
    assert "exceeds max_bid" in d.reason


def test_ladder_rung_under_max_bid_still_fires():
    d = _should_fire(
        current_price=Decimal("25.00"),
        max_bid=Decimal("100.00"),
        ladder=[Decimal("25.00")],
    )
    assert d.fire is True


def test_closeout_poll_interval_brooke_pattern():
    """2026-08-25 Brooke call: hot near events, cold otherwise."""
    from decimal import Decimal as D

    from worker.trigger import (
        CLOSEOUT_COLD_SECONDS,
        CLOSEOUT_HOT_SECONDS,
        CLOSEOUT_WARM_SECONDS,
        _closeout_poll_interval,
    )

    # Transition just happened (listed -> gone): HOT reappearance race.
    assert _closeout_poll_interval(
        target=D("5"), last_success=False, last_price=D("11"),
        seconds_since_change=10, entry_age_seconds=1e9,
    ) == CLOSEOUT_HOT_SECONDS

    # One rung above target ($11 listed, want $5): next drop hits it — HOT.
    assert _closeout_poll_interval(
        target=D("5"), last_success=True, last_price=D("11"),
        seconds_since_change=5000, entry_age_seconds=1e9,
    ) == CLOSEOUT_HOT_SECONDS

    # Target 3 rungs below ($50 listed, want $5): next drop is ~a day out — COLD.
    assert _closeout_poll_interval(
        target=D("5"), last_success=True, last_price=D("50"),
        seconds_since_change=5000, entry_age_seconds=1e9,
    ) == CLOSEOUT_COLD_SECONDS

    # Gone for 20 min (conversion watch): WARM.
    assert _closeout_poll_interval(
        target=D("50"), last_success=False, last_price=None,
        seconds_since_change=1200, entry_age_seconds=1e9,
    ) == CLOSEOUT_WARM_SECONDS

    # Gone for 2 hours: COLD.
    assert _closeout_poll_interval(
        target=D("50"), last_success=False, last_price=None,
        seconds_since_change=7200, entry_age_seconds=1e9,
    ) == CLOSEOUT_COLD_SECONDS

    # Freshly starred entry: HOT for operator feedback.
    assert _closeout_poll_interval(
        target=D("5"), last_success=None, last_price=None,
        seconds_since_change=0, entry_age_seconds=30,
    ) == CLOSEOUT_HOT_SECONDS
