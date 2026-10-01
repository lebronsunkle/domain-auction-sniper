"""Email reminder worker.

Two APScheduler jobs, both running inside the FastAPI process on Fly (same
place the trigger worker lives — see the note in fly.toml about keeping this
to exactly one machine, which is also what makes a single un-elected
notifier safe):

  1. `notification_tick` — every 60s. Walks every watchlist entry with the
     bell switched on, works out which rungs of the reminder ladder are due,
     and sends at most one email per entry per tick.

  2. `refresh_watchlist_end_times` — daily, just after the inventory sync.
     Re-checks the real end time of every notifying auction against GoDaddy
     and writes it back to the auctions row.

The re-arm-on-extension logic deliberately lives in the tick, not in the
daily refresh: that way ANY source that moves an end time — the daily
refresh, or the trigger worker's live lookup inside its 15-minute snipe
horizon (worker/trigger.py:LOOKUP_HORIZON_SECONDS) — re-arms the shorter
reminders on the next tick. The notifier doesn't need to know who moved it.

Double-send safety: the sent-flag is committed BEFORE the email goes out.
A crash between the two loses a reminder; committing after would duplicate
one. Losing one is the better failure for a deploy-heavy afternoon, and it
is the explicit design requirement (restarts never double-send). A clean
send failure — Resend returning 4xx, network down — releases the claim so
the next tick retries.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal
from app.models.auction import Auction
from app.models.notification import (
    REMINDER_THRESHOLDS,
    NotificationPref,
    ReminderThreshold,
)
from app.models.watchlist import WatchlistEntry
from app.notify.email import EmailSendError, is_email_configured, send_email
from app.notify.reminder import ReminderContext, render_reminder

logger = logging.getLogger(__name__)


# Watchlist statuses that mean "this auction is over for us". Note that
# "executed" and "outbid" are NOT here: a fired snipe can still be beaten,
# and an outbid entry is exactly the one the client wants nagging him.
TERMINAL_STATUSES = frozenset({"won", "lost", "expired", "cancelled"})

# End times from different sources (feed, SOAP, trigger worker) disagree by
# a few seconds routinely. Only a move larger than this counts as a real
# extension worth re-arming for.
EXTENSION_TOLERANCE_SECONDS = 60

# Per-entry backoff after a failed send, so a misconfigured from-domain
# doesn't retry every 60 seconds for the life of the auction.
# entry_id -> (consecutive_failures, monotonic timestamp of next attempt)
_send_backoff: dict[int, tuple[int, float]] = {}
_BACKOFF_BASE_SECONDS = 120
_BACKOFF_MAX_SECONDS = 1800


# ---------------------------------------------------------------------------
# Pure logic (unit-tested in tests/test_notifier.py)
# ---------------------------------------------------------------------------


def due_thresholds(
    pref: NotificationPref,
    seconds_remaining: float,
    thresholds: Iterable[ReminderThreshold] = REMINDER_THRESHOLDS,
) -> list[ReminderThreshold]:
    """Rungs that have been crossed and not yet sent, longest-first.

    An auction already over returns nothing — a reminder for a closed
    auction is just noise.
    """
    if seconds_remaining <= 0:
        return []
    return [
        t
        for t in thresholds
        if seconds_remaining <= t.minutes * 60 and pref.sent_at(t) is None
    ]


def rearm_for_extension(
    pref: NotificationPref,
    new_end_utc: datetime,
    now: datetime,
    thresholds: Iterable[ReminderThreshold] = REMINDER_THRESHOLDS,
) -> list[ReminderThreshold]:
    """Re-arm rungs that the new end time puts back in the future.

    GoDaddy pushes an auction's clock out by ~5-6 minutes on any late bid,
    and a feed correction can move it by hours. If a domain was 20 minutes
    out (30m reminder already sent) and gets extended to 6 hours out, the
    30m and 3h reminders should fire again on the way back down; the 12h
    and 24h ones should not, because 6 hours is still inside them.

    Returns the list of rungs actually cleared. Mutates `pref` — the caller
    commits.
    """
    cleared: list[ReminderThreshold] = []
    previous = pref.armed_end_time_utc

    if previous is None:
        # First observation for this entry: just take the baseline. Nothing
        # has been sent against a different clock, so nothing to re-arm.
        pref.armed_end_time_utc = new_end_utc
        return cleared

    delta = (new_end_utc - previous).total_seconds()
    if abs(delta) <= EXTENSION_TOLERANCE_SECONDS:
        return cleared  # jitter between sources, not a real move

    if delta > 0:
        seconds_remaining = (new_end_utc - now).total_seconds()
        for t in thresholds:
            if seconds_remaining > t.minutes * 60 and pref.sent_at(t) is not None:
                pref.clear_sent(t)
                cleared.append(t)
        pref.last_extended_at = now

    # Whether it moved later or earlier, re-baseline so we compare against
    # the clock we're actually arming on now.
    pref.armed_end_time_utc = new_end_utc
    return cleared


def _backoff_blocks(entry_id: int) -> bool:
    state = _send_backoff.get(entry_id)
    return state is not None and time.monotonic() < state[1]


def _record_send_failure(entry_id: int) -> None:
    failures = _send_backoff.get(entry_id, (0, 0.0))[0] + 1
    delay = min(_BACKOFF_BASE_SECONDS * (2 ** (failures - 1)), _BACKOFF_MAX_SECONDS)
    _send_backoff[entry_id] = (failures, time.monotonic() + delay)
    logger.warning(
        "Reminder send failed for entry %s (%d in a row); backing off %ds",
        entry_id,
        failures,
        delay,
    )


def _record_send_success(entry_id: int) -> None:
    _send_backoff.pop(entry_id, None)


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------


async def _notifying_entries(
    session: AsyncSession,
) -> list[tuple[WatchlistEntry, NotificationPref, Optional[Auction]]]:
    """Every bell-on watchlist entry with its prefs and auction snapshot."""
    rows = await session.execute(
        select(WatchlistEntry, NotificationPref, Auction)
        .join(NotificationPref, NotificationPref.watchlist_entry_id == WatchlistEntry.id)
        .outerjoin(Auction, Auction.listing_id == WatchlistEntry.listing_id)
        .where(NotificationPref.enabled.is_(True))
        .order_by(Auction.end_time_utc.asc().nulls_last())
    )
    return list(rows.all())


async def outbid_alert_tick(session: Optional[AsyncSession] = None) -> int:
    """Send ONE alert per newly-outbid entry (2026-09-29, the client).

    The client wants an email (and text, once Twilio is provisioned) the moment
    he's outbid on something he's bidding on — and nothing else, so he isn't
    flooded. The dedup is `outbid_alert_sent_at` on the entry: stamped BEFORE
    the send (same double-send discipline as the reminder ladder), cleared on
    re-arm so a fresh outbid on a re-entered auction alerts again.

    Independent of the reminder bell: outbid alerts fire whether or not the
    entry has notifications toggled on. Email and SMS are attempted
    separately — a failed text never suppresses the email, and vice versa.
    """
    if session is None:
        async with SessionLocal() as own_session:
            return await outbid_alert_tick(own_session)

    from app.notify.outbid import OutbidContext, render_outbid_alert
    from app.notify.sms import SmsSendError, is_sms_configured, send_sms

    cfg = get_settings()
    email_on = bool(cfg.resend_api_key and cfg.outbid_alert_email_to)
    sms_on = is_sms_configured()
    if not (email_on or sms_on):
        return 0  # nothing configured — claim nothing, so enabling later works

    now = datetime.now(timezone.utc)
    rows = await session.execute(
        select(WatchlistEntry, Auction)
        .outerjoin(Auction, Auction.listing_id == WatchlistEntry.listing_id)
        .where(
            WatchlistEntry.status == "outbid",
            WatchlistEntry.outbid_alert_sent_at.is_(None),
        )
    )
    sent = 0
    for entry, auction in rows.all():
        if _backoff_blocks(entry.id):
            continue

        # Claim BEFORE sending (crash loses one alert; never duplicates).
        entry.outbid_alert_sent_at = now
        entry.updated_at = now
        await session.commit()

        subject, html, text, sms_body = render_outbid_alert(
            OutbidContext(
                domain=entry.domain,
                current_price=auction.current_price if auction else None,
                max_bid_dollars=entry.max_bid_dollars,
            )
        )

        ok = False
        if email_on:
            try:
                await send_email(cfg.outbid_alert_email_to, subject, html, text)
                ok = True
            except EmailSendError:
                logger.exception("Outbid email failed for %s", entry.domain)
        if sms_on:
            try:
                await send_sms(cfg.outbid_alert_sms_to, sms_body)
                ok = True
            except SmsSendError:
                logger.exception("Outbid SMS failed for %s", entry.domain)

        if ok:
            _record_send_success(entry.id)
            sent += 1
            logger.info("Outbid alert sent for %s", entry.domain)
        else:
            # Both legs failed — release the claim so the next tick retries.
            entry.outbid_alert_sent_at = None
            entry.updated_at = datetime.now(timezone.utc)
            await session.commit()
            _record_send_failure(entry.id)

    return sent


async def notification_tick(session: Optional[AsyncSession] = None) -> int:
    """One pass over the reminder ladder. Returns the number of emails sent."""
    if session is None:
        async with SessionLocal() as own_session:
            return await notification_tick(own_session)

    # No key or no recipient: evaluate nothing and claim nothing, so that
    # configuring Resend later still delivers the pending reminders.
    if not is_email_configured():
        return 0

    cfg = get_settings()
    now = datetime.now(timezone.utc)
    sent_count = 0

    for entry, pref, auction in await _notifying_entries(session):
        if entry.status in TERMINAL_STATUSES:
            continue
        if auction is None or auction.end_time_utc is None:
            continue

        end_utc = auction.end_time_utc
        if end_utc.tzinfo is None:
            end_utc = end_utc.replace(tzinfo=timezone.utc)

        cleared = rearm_for_extension(pref, end_utc, now)
        if cleared:
            logger.info(
                "Auction extended for %s (now ends %s); re-armed %s reminders",
                entry.domain,
                end_utc.isoformat(),
                ", ".join(t.label for t in cleared),
            )

        seconds_remaining = (end_utc - now).total_seconds()
        due = due_thresholds(pref, seconds_remaining)
        if not due:
            pref.updated_at = now
            continue

        if _backoff_blocks(entry.id):
            continue

        # Ordered longest-first, so the LAST due rung is the most urgent one
        # and the only one worth sending. The rest are rungs whose moment
        # already passed — typically because the bell was switched on late,
        # or the machine was down through their window. Mark them sent so
        # switching notifications on 20 minutes before close sends one
        # "30 minutes" email instead of a burst of four.
        to_send = due[-1]
        stale = due[:-1]

        # --- claim BEFORE sending (see module docstring) -------------------
        for threshold in due:
            pref.mark_sent(threshold, now)
        pref.updated_at = now
        await session.commit()

        if stale:
            logger.info(
                "Suppressed stale reminders for %s: %s (sending %s instead)",
                entry.domain,
                ", ".join(t.label for t in stale),
                to_send.label,
            )

        subject, html, text = render_reminder(
            ReminderContext(
                domain=entry.domain,
                listing_id=entry.listing_id,
                threshold_label=to_send.label,
                end_time_utc=end_utc,
                seconds_remaining=seconds_remaining,
                auction_type=auction.auction_type,
                current_price=auction.current_price,
                estimated_value=auction.estimated_value,
                max_bid_dollars=entry.max_bid_dollars,
                is_armed=entry.is_armed,
            )
        )

        recipient = pref.email_to or cfg.notify_email_to
        try:
            await send_email(recipient, subject, html, text)
        except EmailSendError:
            # Release only the rung we tried to send; the stale ones stay
            # suppressed because their window has passed either way.
            pref.clear_sent(to_send)
            pref.updated_at = datetime.now(timezone.utc)
            await session.commit()
            _record_send_failure(entry.id)
            logger.exception("Reminder email failed for %s", entry.domain)
            continue

        _record_send_success(entry.id)
        sent_count += 1
        logger.info(
            "Reminder sent: %s (%s out, ends %s)",
            entry.domain,
            to_send.label,
            end_utc.isoformat(),
        )

    await session.commit()
    return sent_count


# ---------------------------------------------------------------------------
# Daily end-time refresh
# ---------------------------------------------------------------------------


async def refresh_watchlist_end_times(session: Optional[AsyncSession] = None) -> int:
    """Re-check the end time of every notifying auction. Returns rows changed.

    The GitHub Actions inventory sync doesn't touch Postgres (it runs
    without --persist and commits scored JSON to the repo), so nothing else
    keeps `auctions.end_time_utc` honest for a watchlisted domain — an entry
    starred a week ago still carries the end time the dashboard sent at
    star time. This job closes that gap.

    Source of truth is GoDaddy's live SOAP lookup, which is what the trigger
    worker trusts near the snipe window. The locally-built inventory index
    is the fallback when SOAP is unavailable. Volume is a handful of
    domains, so a sequential pass with a small delay is plenty.
    """
    if session is None:
        async with SessionLocal() as own_session:
            return await refresh_watchlist_end_times(own_session)

    rows = await _notifying_entries(session)
    if not rows:
        return 0

    from app.api.lookup import _parse_details
    from app.godaddy.client import GoDaddyAuth, GoDaddyClient, GoDaddyClientConfig
    from app.godaddy.soap import SoapClient

    cfg = get_settings()
    now = datetime.now(timezone.utc)
    changed = 0

    auth = GoDaddyAuth(key=cfg.godaddy_api_key, secret=cfg.godaddy_api_secret)
    client_cfg = GoDaddyClientConfig(
        rest_base_url=cfg.rest_base_url,
        customer_id=cfg.godaddy_customer_id,
    )

    async with GoDaddyClient(auth=auth, config=client_cfg) as client:
        soap = SoapClient(client)
        for entry, _pref, auction in rows:
            if auction is None or entry.status in TERMINAL_STATUSES:
                continue

            fresh_end: Optional[datetime] = None
            fresh_price = None
            try:
                inner_xml = await soap.get_auction_details_by_domain_name(entry.domain)
                state = _parse_details(inner_xml) if inner_xml else None
                if state is not None and state.found:
                    fresh_end = state.end_time_utc
                    fresh_price = state.current_price_dollars
            except Exception:  # noqa: BLE001 — one bad domain mustn't stop the pass
                logger.exception("End-time refresh: SOAP lookup failed for %s", entry.domain)

            if fresh_end is None:
                fresh_end = await _end_time_from_index(entry.domain)

            if fresh_end is None:
                logger.info("End-time refresh: no fresh end time for %s", entry.domain)
                continue

            if fresh_end.tzinfo is None:
                fresh_end = fresh_end.replace(tzinfo=timezone.utc)

            previous = auction.end_time_utc
            if previous is not None and previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)

            if previous is None or abs((fresh_end - previous).total_seconds()) > EXTENSION_TOLERANCE_SECONDS:
                logger.info(
                    "End-time refresh: %s %s -> %s",
                    entry.domain,
                    previous.isoformat() if previous else "unknown",
                    fresh_end.isoformat(),
                )
                auction.end_time_utc = fresh_end
                changed += 1

            if fresh_price is not None:
                auction.current_price = fresh_price
            auction.last_synced_at = now

            # Be polite to GoDaddy; this pass is never time-critical.
            await asyncio.sleep(0.2)

    await session.commit()
    logger.info("End-time refresh complete: %d auction(s) updated", changed)
    return changed


async def _end_time_from_index(domain: str) -> Optional[datetime]:
    """Fallback end time from the locally-built full-inventory index."""
    try:
        from app.godaddy.inventory_index import resolve_domain

        indexed = await resolve_domain(domain)
        if indexed is None or not indexed.end_time_utc:
            return None
        parsed = datetime.fromisoformat(indexed.end_time_utc.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001 — index is a soft dependency
        logger.exception("End-time refresh: index fallback failed for %s", domain)
        return None


# ---------------------------------------------------------------------------
# Scheduler wiring
# ---------------------------------------------------------------------------


def create_notification_scheduler():
    """Build (but don't start) the APScheduler instance for both jobs."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    cfg = get_settings()
    scheduler = AsyncIOScheduler(timezone=timezone.utc)

    scheduler.add_job(
        _tick_job,
        trigger=IntervalTrigger(seconds=cfg.notification_tick_seconds),
        id="notification_tick",
        name="auction reminder tick",
        coalesce=True,       # a paused machine catches up with ONE tick, not a queue
        max_instances=1,     # never two passes claiming the same rung
        misfire_grace_time=cfg.notification_tick_seconds,
        replace_existing=True,
    )

    hour, _, minute = cfg.notification_end_time_refresh_utc.partition(":")
    scheduler.add_job(
        _refresh_job,
        trigger=CronTrigger(hour=int(hour), minute=int(minute or 0), timezone=timezone.utc),
        id="watchlist_end_time_refresh",
        name="daily watchlist end-time refresh",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,  # fire even if the deploy made us an hour late
        replace_existing=True,
    )
    return scheduler


async def _tick_job() -> None:
    """APScheduler entry point. Must never raise — a job that throws gets
    its next run scheduled anyway, but the traceback is easier to read from
    here than from APScheduler's executor."""
    try:
        await notification_tick()
    except Exception:  # noqa: BLE001
        logger.exception("Notification tick failed; will retry next interval")
    # Outbid alerts ride the same 60s cadence but are independent — a failure
    # in one must not skip the other.
    try:
        await outbid_alert_tick()
    except Exception:  # noqa: BLE001
        logger.exception("Outbid alert tick failed; will retry next interval")


async def _refresh_job() -> None:
    try:
        await refresh_watchlist_end_times()
    except Exception:  # noqa: BLE001
        logger.exception("Watchlist end-time refresh failed; will retry tomorrow")


def notifier_disabled() -> bool:
    return os.getenv("DISABLE_NOTIFIER", "").lower() in ("true", "1", "yes")
