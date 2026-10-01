"""Outbid alerts (2026-09-29, the client): one email/text per outbid event,
never a flood; re-arm re-enables the alert."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.notify.email as email_mod
import app.notify.sms as sms_mod
import worker.notifier as notifier
from app.config import get_settings
from app.models.auction import Auction
from app.models.base import Base
from app.models.watchlist import WatchlistEntry
from app.notify.outbid import OutbidContext, render_outbid_alert


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
def _reset_backoff():
    notifier._send_backoff.clear()
    yield
    notifier._send_backoff.clear()


async def _seed_outbid(session, *, status="outbid", sent_at=None):
    now = datetime.now(timezone.utc)
    auction = Auction(
        listing_id=713111001, domain="bigname.com", tld="com",
        auction_type="EXPIRY_AUCTION", current_price=Decimal("600.00"),
        end_time_utc=now + timedelta(hours=2), bid_count=5, has_bids=True,
        last_synced_at=now, first_seen_at=now, status="active",
    )
    session.add(auction)
    await session.flush()
    entry = WatchlistEntry(
        auction_id=auction.id, listing_id=auction.listing_id, domain=auction.domain,
        max_bid_dollars=Decimal("500.00"), is_armed=True, created_at=now,
        updated_at=now, status=status, outbid_alert_sent_at=sent_at,
    )
    session.add(entry)
    await session.commit()
    return entry


def test_render_outbid_has_domain_and_price():
    subject, html, text, sms = render_outbid_alert(
        OutbidContext(domain="bigname.com", current_price=Decimal("600.00"),
                      max_bid_dollars=Decimal("500.00"))
    )
    assert "bigname.com" in subject
    assert "$600.00" in html and "$500.00" in html
    assert "bigname.com" in sms and "pages.dev" in sms


@pytest.mark.asyncio
async def test_outbid_alert_sends_once_and_stamps(session, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("OUTBID_ALERT_EMAIL_TO", "owner@example.com")
    calls = []

    async def _fake_send_email(to, subject, html, text=None, **kw):
        calls.append(to)
        return "msg_1"
    monkeypatch.setattr(notifier, "send_email", _fake_send_email)

    entry = await _seed_outbid(session)
    sent = await notifier.outbid_alert_tick(session)
    assert sent == 1
    assert calls == ["owner@example.com"]

    refreshed = (await session.execute(
        select(WatchlistEntry).where(WatchlistEntry.id == entry.id)
    )).scalar_one()
    assert refreshed.outbid_alert_sent_at is not None

    # Second pass: already stamped -> no flood.
    calls.clear()
    assert await notifier.outbid_alert_tick(session) == 0
    assert calls == []


@pytest.mark.asyncio
async def test_no_alert_when_nothing_configured(session, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "")
    monkeypatch.setenv("OUTBID_ALERT_EMAIL_TO", "")
    monkeypatch.setattr(sms_mod, "is_sms_configured", lambda: False)
    await _seed_outbid(session)
    # Claims nothing so enabling Resend later still alerts.
    assert await notifier.outbid_alert_tick(session) == 0
    entry = (await session.execute(select(WatchlistEntry))).scalar_one()
    assert entry.outbid_alert_sent_at is None


@pytest.mark.asyncio
async def test_send_failure_releases_claim(session, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("OUTBID_ALERT_EMAIL_TO", "owner@example.com")
    monkeypatch.setattr(sms_mod, "is_sms_configured", lambda: False)

    async def _boom(*a, **k):
        raise email_mod.EmailSendError("nope")
    monkeypatch.setattr(notifier, "send_email", _boom)

    entry = await _seed_outbid(session)
    assert await notifier.outbid_alert_tick(session) == 0
    refreshed = (await session.execute(
        select(WatchlistEntry).where(WatchlistEntry.id == entry.id)
    )).scalar_one()
    assert refreshed.outbid_alert_sent_at is None  # released for retry


@pytest.mark.asyncio
async def test_non_outbid_entries_ignored(session, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("OUTBID_ALERT_EMAIL_TO", "owner@example.com")

    async def _fake(*a, **k):
        return "x"
    monkeypatch.setattr(notifier, "send_email", _fake)
    await _seed_outbid(session, status="pending")
    assert await notifier.outbid_alert_tick(session) == 0
