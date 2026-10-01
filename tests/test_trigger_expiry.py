"""Tests for the extension-aware expiry evaluation (the 2026-07-11 THMY fix).

The bug: GoDaddy resets the auction clock ~5-6 minutes on every late bid.
The worker stored the original end_time, so when it passed, entries were
marked expired mid-auction and the snipe never fired. These tests pin the
new behavior:

  * NEVER expire an entry on the stored clock alone — only after a live
    lookup confirms the auction is really gone.
  * A live lookup that shows a LATER end time pushes the entry's clock out.
  * A live current price at/above the max marks the entry "outbid" with a
    visible note — instead of silently disappearing.
  * Lookup failure (network) = unknown = do nothing this tick.

Uses in-memory SQLite + a fake SOAP client. No network, no real money.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.auction import Auction
from app.models.base import Base
from app.models.watchlist import WatchlistEntry
from worker import trigger
from worker.trigger import _evaluate_expiry


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
def _clear_lookup_cache():
    trigger._fresh_state_cache.clear()
    yield
    trigger._fresh_state_cache.clear()


async def _seed(session, *, end_delta_seconds: float, max_bid="399.00", current="333.00"):
    now = datetime.now(timezone.utc)
    auction = Auction(
        listing_id=713000001,  # real-shaped id so the synthetic-id guard passes
        domain="thmy.com",
        tld="com",
        auction_type="EXPIRY_AUCTION",
        current_price=Decimal(current),
        end_time_utc=now + timedelta(seconds=end_delta_seconds),
        bid_count=3,
        has_bids=True,
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


class FakeSoap:
    """Returns canned inner XML for GetAuctionDetailsByDomainName."""

    def __init__(self, inner_xml):
        self.inner_xml = inner_xml
        self.calls = 0

    async def get_auction_details_by_domain_name(self, domain):
        self.calls += 1
        return self.inner_xml


def _details_xml(*, end_utc: datetime, price: str) -> str:
    end_str = end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f'<GetAuctionDetailsByDomainName IsValid="True" DomainName="thmy.com" '
        f'AuctionEndTime="{end_str}" BidCount="7" Price="{price}" '
        f'ValuationPrice="$5,063" AuctionModel="Bid" />'
    )


NOT_FOUND_XML = '<GetAuctionDetailsByDomainName IsValid="False" />'


@pytest.mark.asyncio
async def test_stored_clock_passed_but_lookup_fails_does_not_expire(session):
    """Network hiccup at the worst moment must NOT kill the entry."""
    auction, entry = await _seed(session, end_delta_seconds=-30)
    soap = FakeSoap(None)  # SOAP call fails -> parsed state None
    await _evaluate_expiry(session, rest=None, entry=entry, auction=auction, soap=soap)
    assert entry.status == "pending", "expired on an UNVERIFIED stale clock"


@pytest.mark.asyncio
async def test_extension_pushes_end_time_out_instead_of_expiring(session):
    """THMY scenario: stored end passed, but live lookup shows the clock was
    reset 5 minutes out. Entry must stay pending with the new end time."""
    auction, entry = await _seed(session, end_delta_seconds=-30)
    new_end = datetime.now(timezone.utc) + timedelta(minutes=5)
    soap = FakeSoap(_details_xml(end_utc=new_end, price="$380"))
    await _evaluate_expiry(session, rest=None, entry=entry, auction=auction, soap=soap)
    assert entry.status == "pending"
    assert auction.end_time_utc is not None
    assert (auction.end_time_utc - new_end).total_seconds() == pytest.approx(0, abs=2)
    assert auction.current_price == Decimal("380")


@pytest.mark.asyncio
async def test_confirmed_gone_transitions_to_closeout_watch(session):
    """CHANGED 2026-07-11 (sweet spot): a confirmed-over expiry auction no
    longer expires the entry — it flips to closeout watch so the $50
    conversion can be raced. See tests/test_sweet_spot.py for the rest."""
    auction, entry = await _seed(session, end_delta_seconds=-30)
    soap = FakeSoap(NOT_FOUND_XML)
    await _evaluate_expiry(session, rest=None, entry=entry, auction=auction, soap=soap)
    assert entry.status == "pending"
    assert auction.auction_type == "CLOSEOUT"
    assert "sweet spot" in (entry.note or "")


@pytest.mark.asyncio
async def test_outbid_before_snipe_sets_visible_status(session):
    """Someone bid $480 against a $399 max: entry becomes status=outbid with
    an explanatory note — it must not fire and must not vanish silently."""
    auction, entry = await _seed(session, end_delta_seconds=240, max_bid="399.00")
    new_end = datetime.now(timezone.utc) + timedelta(minutes=4)
    soap = FakeSoap(_details_xml(end_utc=new_end, price="$480"))
    await _evaluate_expiry(session, rest=None, entry=entry, auction=auction, soap=soap)
    assert entry.status == "outbid"
    assert "480" in (entry.note or "")
    assert entry.is_armed is True  # visible + explanation, not silently disarmed


@pytest.mark.asyncio
async def test_below_max_inside_horizon_keeps_waiting(session):
    """Current price under the max, still minutes out: hold, refreshed price."""
    auction, entry = await _seed(session, end_delta_seconds=240, max_bid="399.00")
    new_end = datetime.now(timezone.utc) + timedelta(minutes=4)
    soap = FakeSoap(_details_xml(end_utc=new_end, price="$350"))
    await _evaluate_expiry(session, rest=None, entry=entry, auction=auction, soap=soap)
    assert entry.status == "pending"
    assert auction.current_price == Decimal("350")


@pytest.mark.asyncio
async def test_lookup_throttled_between_ticks(session):
    """Two ticks in quick succession = one SOAP call (TTL cache)."""
    auction, entry = await _seed(session, end_delta_seconds=240)
    new_end = datetime.now(timezone.utc) + timedelta(minutes=4)
    soap = FakeSoap(_details_xml(end_utc=new_end, price="$350"))
    await _evaluate_expiry(session, rest=None, entry=entry, auction=auction, soap=soap)
    await _evaluate_expiry(session, rest=None, entry=entry, auction=auction, soap=soap)
    assert soap.calls == 1


@pytest.mark.asyncio
async def test_no_soap_client_keeps_legacy_expire_behavior(session):
    """Back-compat: without a SOAP client the old stored-clock expiry runs
    (unit-test path only; production always passes soap)."""
    auction, entry = await _seed(session, end_delta_seconds=-30)
    await _evaluate_expiry(session, rest=None, entry=entry, auction=auction, soap=None)
    assert entry.status == "expired"


# --- the client's five-minute-bell strategy (2026-08-26) --------------------------
# Fire just BEFORE GoDaddy's 5-minute extension window when bids exist (no
# clock reset, no ending-soon spotlight); never volunteer the first bid on a
# zero-bid auction (closeout is cheaper); classic final-seconds snipe only as
# the fallback when a rival bids inside the bell.


async def _seed_settings_for_fire(session):
    from datetime import datetime, timezone
    from app.models.settings import SystemSettings
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


def test_last_gasp_decision_matrix():
    """Default mode since 2026-09-23 (client call): last-gasp snipe."""
    from worker.trigger import _expiry_fire_decision as d

    # Far out: always hold, bids or not.
    assert d(3600, True, use_bell=False) == "hold"
    assert d(3600, False, use_bell=False) == "hold"

    # Rule 1 — zero bids NEVER fire, all the way down (closeout is the play).
    # This changed 2026-09-23: the old non-bell fallback fired regardless.
    assert d(310, False, use_bell=False) == "hold"
    assert d(8, False, use_bell=False) == "hold"
    assert d(2, False, use_bell=False) == "hold"

    # Rule 2 — with bids, hold through the old pre-bell slot and the bell;
    # fire only inside the final window (precise wait then lands T-1.5s).
    assert d(310, True, use_bell=False) == "hold"
    assert d(200, True, use_bell=False) == "hold"
    assert d(8, True, use_bell=False) == "fire"
    assert d(3, True, use_bell=False) == "fire"

    # Over = lapse (sweet-spot closeout handoff decides from here).
    assert d(0, True, use_bell=False) == "lapse"
    assert d(-10, False, use_bell=False) == "lapse"


def test_bell_decision_matrix():
    """Bell mode (retired 2026-09-23, kept switchable via USE_BELL_SNIPE)."""
    from worker.trigger import _expiry_fire_decision as d

    # Far out: always hold, bids or not.
    assert d(3600, True, use_bell=True) == "hold"
    assert d(3600, False, use_bell=True) == "hold"

    # The pre-bell slot (300 < remaining <= 312): fire IF bids exist.
    assert d(310, True, use_bell=True) == "fire"
    assert d(305, True, use_bell=True) == "fire"
    assert d(310, False, use_bell=True) == "hold"   # zero bids: stay invisible

    # Inside the bell, zero bids: never fire, all the way down.
    assert d(200, False, use_bell=True) == "hold"
    assert d(8, False, use_bell=True) == "hold"
    assert d(2, False, use_bell=True) == "hold"

    # Inside the bell WITH bids (rival jumped in while we held): classic
    # final-seconds snipe, not an instant panic bid.
    assert d(200, True, use_bell=True) == "hold"
    assert d(8, True, use_bell=True) == "fire"
    assert d(3, True, use_bell=True) == "fire"

    # Over = lapse (sweet-spot closeout handoff decides from here).
    assert d(0, True, use_bell=True) == "lapse"
    assert d(-10, False, use_bell=True) == "lapse"


@pytest.mark.asyncio
async def test_pre_bell_fire_with_bids(session, monkeypatch):
    """Bell mode: bids present at T-305 -> the snipe fires pre-bell."""
    monkeypatch.setattr(trigger, "USE_BELL_SNIPE", True)
    await _seed_settings_for_fire(session)
    auction, entry = await _seed(session, end_delta_seconds=305)
    auction.bid_count = 1
    auction.has_bids = True
    await session.commit()
    assert trigger.DRY_RUN
    await _evaluate_expiry(session, rest=None, entry=entry, auction=auction, soap=None)
    assert entry.status == "executed"


@pytest.mark.asyncio
async def test_last_gasp_holds_at_pre_bell_with_bids(session):
    """Default mode: bids at T-305 -> HOLD (no pre-bell fire since 2026-09-23)."""
    await _seed_settings_for_fire(session)
    auction, entry = await _seed(session, end_delta_seconds=305)
    auction.bid_count = 1
    auction.has_bids = True
    await session.commit()
    await _evaluate_expiry(session, rest=None, entry=entry, auction=auction, soap=None)
    assert entry.status == "pending"


@pytest.mark.asyncio
async def test_pre_bell_holds_with_zero_bids(session):
    """Zero bids at T-305 -> no bid, entry stays pending (closeout play)."""
    auction, entry = await _seed(session, end_delta_seconds=305)
    auction.bid_count = 0
    auction.has_bids = False
    await session.commit()
    await _evaluate_expiry(session, rest=None, entry=entry, auction=auction, soap=None)
    assert entry.status == "pending"


@pytest.mark.asyncio
async def test_final_window_holds_with_zero_bids(session):
    """Zero bids even at T-5 -> still no bid; lapse to closeout is the play."""
    auction, entry = await _seed(session, end_delta_seconds=5)
    auction.bid_count = 0
    auction.has_bids = False
    await session.commit()
    await _evaluate_expiry(session, rest=None, entry=entry, auction=auction, soap=None)
    assert entry.status == "pending"


@pytest.mark.asyncio
async def test_final_window_fallback_fires_with_late_bids(session):
    """A rival bid inside the bell -> fallback snipe fires at T-5."""
    await _seed_settings_for_fire(session)
    auction, entry = await _seed(session, end_delta_seconds=5)
    auction.bid_count = 2
    auction.has_bids = True
    await session.commit()
    assert trigger.DRY_RUN
    await _evaluate_expiry(session, rest=None, entry=entry, auction=auction, soap=None)
    assert entry.status == "executed"


class _FakeRest:
    """Minimal rest stub carrying a .client for the collision lookup."""
    def __init__(self):
        self.client = object()


@pytest.mark.asyncio
async def test_collision_guard_stands_down_when_already_bidding(session, monkeypatch):
    """The client already has a bid (memberBiddingStatus != NOT_BIDDING) -> the
    sniper holds, does NOT execute, and leaves a visible collision note."""
    import app.godaddy.live_listings as ll_mod

    await _seed_settings_for_fire(session)
    auction, entry = await _seed(session, end_delta_seconds=5)
    auction.bid_count = 2
    auction.has_bids = True
    await session.commit()

    class _FakeLive:
        def __init__(self, client):
            pass
        async def check(self, domains):
            return {domains[0]: ll_mod.LiveListing(
                domain=domains[0], status="AVAILABLE", listing_id=713000001,
                member_bidding_status="HIGH_BIDDER",
            )}

    monkeypatch.setattr(ll_mod, "LiveListingsClient", _FakeLive)
    await _evaluate_expiry(session, rest=_FakeRest(), entry=entry, auction=auction, soap=None)
    assert entry.status == "pending"          # did NOT fire
    assert "collision guard" in (entry.note or "")


@pytest.mark.asyncio
async def test_collision_guard_fires_when_not_bidding(session, monkeypatch):
    """memberBiddingStatus=NOT_BIDDING -> guard stays out of the way, snipe fires."""
    import app.godaddy.live_listings as ll_mod

    await _seed_settings_for_fire(session)
    auction, entry = await _seed(session, end_delta_seconds=5)
    auction.bid_count = 2
    auction.has_bids = True
    await session.commit()

    class _FakeLive:
        def __init__(self, client):
            pass
        async def check(self, domains):
            return {domains[0]: ll_mod.LiveListing(
                domain=domains[0], status="AVAILABLE", listing_id=713000001,
                member_bidding_status="NOT_BIDDING",
            )}

    monkeypatch.setattr(ll_mod, "LiveListingsClient", _FakeLive)
    assert trigger.DRY_RUN
    await _evaluate_expiry(session, rest=_FakeRest(), entry=entry, auction=auction, soap=None)
    assert entry.status == "executed"         # fired normally
