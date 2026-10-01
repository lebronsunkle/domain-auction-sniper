"""Auto-buy trigger worker.

Polls armed watchlist entries, calls the GoDaddy SOAP estimate to get the
current closeout price, and fires execute_closeout_purchase when the price
hits the user's max_bid (or the next rung of the closeout price ladder).

Safety:
  * Every potential purchase passes through `governors.check_all()` before
    any side-effectful API call. Kill switch, per-tx cap, daily cap, sanity
    multiplier — all enforced server-side.
  * The watchlist entry's `status` is moved to "executed" BEFORE the buy
    call goes out, so a crash mid-call can't cause a double-fire. The buy
    result (won / lost) is recorded after.
  * Dry-run mode (TRIGGER_DRY_RUN=true) makes the worker do everything
    EXCEPT the actual buy call. Used to validate decision logic without
    spending money. Default ON until execute_closeout_purchase is wired
    against a real captured SOAP request.

Integration:
  * Started from FastAPI app startup (see app/main.py). Runs as an asyncio
    background task using the same event loop as the API server.
  * Tick interval comes from Settings.trigger_loop_interval_seconds (default 1s).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal
from app.godaddy.client import GoDaddyAuth, GoDaddyClient, GoDaddyClientConfig
from app.godaddy.rest import BidRequest, GoDaddyBidError, RestClient
from app.godaddy.soap import CloseoutEstimate, SoapClient
from app.models.audit_log import AuditLogEntry
from app.models.auction import Auction
from app.models.purchase import Purchase
from app.models.settings import SystemSettings
from app.models.watchlist import WatchlistEntry
from app.safety.governors import GovernorContext, GovernorRejection, check_all
from app.safety.purchase_guard import (
    acquire_listing_lock,
    begin_in_flight,
    complete_attempt,
    find_recent_money_attempt,
)

logger = logging.getLogger(__name__)


# Feature flag — turn off dry-run only when execute_closeout_purchase has been
# verified against a real OTE capture. Default ON for safety. Set the env var
# TRIGGER_DRY_RUN=false to enable live buying.
DRY_RUN = os.getenv("TRIGGER_DRY_RUN", "true").lower() not in ("false", "0", "no")

# 2026-08-25 (myhomebills.com): the legacy SOAP purchase failed a live $5
# closeout buy with a phantom "added to your cart" message (cart was empty),
# then the official Instant Purchase API bought the SAME domain minutes
# later on the first try (order 4171518683). Same flag as the manual Buy
# Now path in app/api/closeout.py: when on, worker auto-buys go through the
# official API (preview -> purchase with server-side PRICE_MISMATCH guard).
USE_INSTANT_PURCHASE_API = os.getenv(
    "USE_INSTANT_PURCHASE_API", "false"
).lower() in ("true", "1", "yes")


# How many entries to process per tick (rate limit). We process the
# closest-to-ending entries first so we don't miss imminent triggers if the
# list is long.
MAX_ENTRIES_PER_TICK = 25

# Worker pulse, exposed via /health (2026-08-26 finacredit post-mortem: the
# loop's liveness was invisible; a dead worker looked identical to a calm
# one). Mutated only by run_trigger_loop.
WORKER_STATUS: dict = {
    "started_at": None,
    "last_tick_at": None,
    "ticks": 0,
    "entries_last_tick": 0,
    "dry_run": None,
    "last_error": None,
}

# Expiry-watch heartbeat throttle: entry_id -> monotonic ts of last line.
# The closeout path got its WATCHING heartbeat on 08-25; the expiry path
# stayed silent and that silence hid the finacredit failure. Never again.
_expiry_hb: dict = {}

# Snipe window — how close to auction close we wait before firing a bid on
# an expiry auction. Classic sniper strategy: wait until the last moment so
# competing bidders can't react with their own counter-bid. Override via env.
#
# 2026-07-14 (client call): tightened 60 -> 8 seconds.
# The client wants 1-2s eventually. The floor is set by our own latency chain:
# 1s worker tick + pre-bid SOAP verification + REST bid round-trip. Every
# live snipe now logs its fire-to-response latency ("SNIPE LATENCY" lines);
# once we've seen real numbers consistently under ~2s, this can drop to ~4.
# Going tighter than (tick + observed p95 latency + 1s margin) risks the
# bid landing after the auction closes — losing a domain the client wanted,
# which is worse than a bid landing 6 seconds "early". Note GoDaddy resets
# the clock +5-6 min on any late bid anyway, so precision buys secrecy,
# not victory — the proxy war decides the winner.
SNIPE_WINDOW_SECONDS = int(os.getenv("SNIPE_WINDOW_SECONDS", "8"))

# --- Precise fire lead (2026-09-23, client call) ---------------------
# The client wants the bid to land ~1.5s before close,
# not on the worker's 1s tick granularity. Inside the snipe window the
# evaluator sleeps until end_time - SNIPE_LEAD_SECONDS before initiating the
# bid; the fresh SOAP check runs BEFORE that sleep, so only DB bookkeeping +
# the bid POST (~0.3-0.8s observed) remain after it. GoDaddy still resets
# the clock to 5:00 when the bid changes the price — confirmed with the client
# on tape 2026-09-23 that this is DESIRED: stretching the auction drains
# rival patience, and GoDaddy's proxy defends the max through extensions.
SNIPE_LEAD_SECONDS = float(os.getenv("SNIPE_LEAD_SECONDS", "1.5"))

# Self-bidding collision guard (2026-09-24, the client): before firing, check the
# availability API's memberBiddingStatus and stand down if the client already has
# a bid on the listing. Fail-open (a lookup error proceeds with the snipe) so
# a flaky availability call never costs him a domain. Toggle if it ever
# misbehaves against a real observed enum value.
COLLISION_GUARD_ENABLED = os.getenv("COLLISION_GUARD_ENABLED", "true").lower() not in ("false", "0", "no")

# Hard ceiling on a single tick (2026-09-24: the loop froze mid-await for
# 19 hours — second freeze of this class after finacredit 2026-08-26 — and
# the watchdog hung awaiting the cancelled task's cleanup). Generous: a
# legitimate worst-case tick (25 entries x SOAP lookups + a precise-wait
# snipe + purchase) stays well under this.
TICK_TIMEOUT_SECONDS = int(os.getenv("TICK_TIMEOUT_SECONDS", "120"))

# --- the client's five-minute-bell strategy (2026-08-26) -------------------------
# GoDaddy extends any auction back to >=5:00 remaining when a bid CHANGES
# the price inside the final 5 minutes (rule since 2014-07; verified against
# thedomains.com / domaininvesting.com). Every extension also parks the
# domain back on the "ending soon" shelf with fresh bid activity — the most
# watched list on the platform. That spotlight is how the client's $1 finds
# turned into $200 wars. So:
#   * auction HAS bids   -> fire the max ONCE just BEFORE the bell: no
#     extension, no spotlight, auction ends on schedule with GoDaddy's
#     proxy defending up to the max
#   * auction has NO bids -> never bid at all; let it lapse into closeout
#     and the sweet-spot auto-buy takes it at $11/$5 (myhomebills pattern)
#   * a first bid lands INSIDE the bell while we held -> spotlight is
#     already on, so the classic final-seconds snipe fires as the fallback
BELL_SECONDS = 300
PRE_BELL_FIRE_SECONDS = int(os.getenv("PRE_BELL_FIRE_SECONDS", "312"))
# 2026-09-23: default flipped true -> false. The client watched the pre-bell
# fire live on a live auction and reversed course: on auctions WITH bids he now
# wants the last-gasp snipe at T-~1.5s (SNIPE_LEAD_SECONDS), accepting the
# 5:00 clock reset on purpose. The Aug 26 bell call, he clarified, was
# about the OTHER rule — never place the FIRST bid (that "attracts the
# sharks"); zero-bid auctions still lapse to the $50 closeout instant-buy.
# Bell mode is retained behind this flag in case he reverses again.
USE_BELL_SNIPE = os.getenv("USE_BELL_SNIPE", "false").lower() in ("true", "1", "yes")

# 2026-07-11 (THMY incident): GoDaddy RESETS the auction clock by ~5-6
# minutes whenever a bid lands late — so the end_time we stored at
# star/sync time goes stale the moment a bidding war starts. Within this
# horizon of the STORED end time, we re-verify the auction's real end time
# and current price via GetAuctionDetailsByDomainName before making any
# decision, and we NEVER mark an entry expired on the stored clock alone.
LOOKUP_HORIZON_SECONDS = int(os.getenv("LOOKUP_HORIZON_SECONDS", "900"))

# Throttle live lookups per entry (the loop ticks every 1s; GoDaddy doesn't
# need to hear from us that often). Inside the snipe window we bypass the
# throttle once for the final pre-bid check.
LOOKUP_TTL_SECONDS = 20.0

# In-memory cache: entry_id -> (monotonic_ts, parsed LookupResponse or None)
_fresh_state_cache: dict[int, tuple[float, object]] = {}

# --- Sweet-spot automation (2026-07-11, the client's #1 ask) -------------------
# When an expiry auction he's watching ends without our snipe firing (the
# $1-no-bids case), the domain converts to a $50 closeout shortly after —
# and the profit is in racing that conversion. We flip the entry into
# closeout-watch mode: the closeout evaluator polls for the conversion and
# buys the instant the price is at/under his max.
#
# --- Brooke-pattern priority polling (2026-08-25 call with GoDaddy) -----
# Brooke (GoDaddy, runs the same play for himself): poll a domain every
# ~1.5s ONLY when it's near its event; back everything else off so you
# never rate-limit. Closeout rungs drop ~every 24h (50 -> 30 -> 11 -> 5),
# and between rungs the listing VANISHES for up to a minute — whoever is
# polling fastest when it reappears wins the buy. Our old flat 15s backoff
# was slowest exactly then.
#
# HOT  (~1.5s): transition in progress (estimate just flipped listed ->
#               gone), or price sits ONE rung above the target (next drop
#               hits it), or the entry is brand new (fast feedback).
# WARM (~5s):  sweet-spot conversion watch in its first minutes.
# COLD (~60s): listed but target is 2+ rungs below current (next relevant
#              drop is ~a day away) — no reason to burn rate budget.
CLOSEOUT_HOT_SECONDS = float(os.getenv("CLOSEOUT_HOT_SECONDS", "1.5"))
CLOSEOUT_WARM_SECONDS = float(os.getenv("CLOSEOUT_WARM_SECONDS", "5"))
CLOSEOUT_COLD_SECONDS = float(os.getenv("CLOSEOUT_COLD_SECONDS", "60"))
# How long a transition stays HOT before we accept it might not reappear.
CLOSEOUT_TRANSITION_HOT_WINDOW = float(os.getenv("CLOSEOUT_TRANSITION_HOT_WINDOW", "300"))

# GoDaddy's closeout price ladder (per Brooke).
CLOSEOUT_RUNGS = (Decimal("50"), Decimal("30"), Decimal("11"), Decimal("5"))


def _closeout_poll_interval(
    *,
    target: Optional[Decimal],
    last_success: Optional[bool],
    last_price: Optional[Decimal],
    seconds_since_change: float,
    entry_age_seconds: float,
) -> float:
    """Seconds to wait between estimate calls for one closeout entry."""
    # Brand-new entries: fast feedback while the operator is watching.
    if entry_age_seconds < 600:
        return CLOSEOUT_HOT_SECONDS
    # Transition: it WAS listed, now the estimate fails — the reappearance
    # race. Hottest moment there is.
    if last_success is False and seconds_since_change < CLOSEOUT_TRANSITION_HOT_WINDOW:
        return CLOSEOUT_HOT_SECONDS
    if last_success is False:
        # Long gone (sweet-spot conversion watch / might be sold).
        return CLOSEOUT_WARM_SECONDS if seconds_since_change < 3600 else CLOSEOUT_COLD_SECONDS
    # Listed: how far is the target below the current rung?
    if last_price is not None and target is not None:
        if last_price <= target:
            return CLOSEOUT_HOT_SECONDS  # should be firing already
        lower_rungs = [r for r in CLOSEOUT_RUNGS if r < last_price]
        next_rung = lower_rungs[0] if lower_rungs else None
        if next_rung is not None and next_rung <= target:
            # ONE drop away from the buy — Brooke's every-1.5s case.
            return CLOSEOUT_HOT_SECONDS
    return CLOSEOUT_COLD_SECONDS


# entry_id -> {"success": bool|None, "price": Decimal|None, "changed": monotonic}
_closeout_state: dict[int, dict] = {}

# If the closeout conversion never comes (someone won the expiry auction,
# or the closeout window passed), give up this long after the recorded
# auction end time and mark the entry expired.
CLOSEOUT_GIVEUP_DAYS = 6




async def _transition_to_closeout_watch(
    session: AsyncSession,
    entry: WatchlistEntry,
    auction: Auction,
    now: datetime,
) -> None:
    """Expiry phase over, snipe never fired — switch this entry to closeout
    watch. The dispatcher routes by auction_type, so flipping it to CLOSEOUT
    sends the entry to _evaluate_closeout on the next tick."""
    auction.auction_type = "CLOSEOUT"
    auction.status = "ended"
    entry.note = (
        f"[sweet spot] Expiry auction ended without a snipe. Now watching "
        f"for the closeout conversion — will buy instantly when the "
        f"closeout price is at or under your max "
        f"${entry.max_bid_dollars}.\n\n{entry.note or ''}"
    )
    entry.updated_at = now
    await session.commit()
    logger.info(
        "Sweet spot: %s id=%s transitioned to closeout watch (max=$%s)",
        entry.domain,
        entry.id,
        entry.max_bid_dollars,
    )


async def run_trigger_loop() -> None:
    """Forever loop. Started as a background task at app boot."""
    cfg = get_settings()
    interval = max(1, int(cfg.trigger_loop_interval_seconds or 1))

    logger.info(
        "Trigger worker starting. interval=%ds dry_run=%s",
        interval,
        DRY_RUN,
    )
    if DRY_RUN:
        logger.warning(
            "TRIGGER_DRY_RUN is on. The worker will compute and log buy "
            "decisions but will NOT call execute_closeout_purchase. Set "
            "TRIGGER_DRY_RUN=false to enable live buying."
        )

    # We construct one long-lived GoDaddy client and reuse it for all SOAP
    # calls. The client manages its own httpx.AsyncClient internally and is
    # safe to share across the loop iterations.
    gd_auth = GoDaddyAuth(key=cfg.godaddy_api_key, secret=cfg.godaddy_api_secret)
    gd_config = GoDaddyClientConfig(
        rest_base_url=cfg.rest_base_url,
        customer_id=cfg.godaddy_customer_id,
    )
    gd_client = GoDaddyClient(auth=gd_auth, config=gd_config)
    soap = SoapClient(gd_client)
    rest = RestClient(gd_client)

    WORKER_STATUS["started_at"] = datetime.now(timezone.utc).isoformat()
    WORKER_STATUS["dry_run"] = DRY_RUN
    try:
        while True:
            handled = 0
            try:
                # Hard tick deadline (2026-09-24: 19h freeze, second of its
                # class after finacredit). Some await inside a tick can hang
                # forever despite per-call timeouts — wedged connection pools
                # don't always honor them. wait_for cancels the tick outright;
                # a cancel mid-money-op is safe: the in_flight purchase row
                # stays and the dup guard blocks a refire (dellport pattern).
                handled = await asyncio.wait_for(
                    _tick(soap, rest), timeout=TICK_TIMEOUT_SECONDS
                )
                WORKER_STATUS["last_error"] = None
            except asyncio.TimeoutError:
                logger.error(
                    "Trigger tick exceeded %ds and was cancelled (hung await "
                    "somewhere in the chain). Continuing with next tick.",
                    TICK_TIMEOUT_SECONDS,
                )
                WORKER_STATUS["last_error"] = (
                    f"tick timeout >{TICK_TIMEOUT_SECONDS}s (cancelled, loop alive)"
                )
            except Exception as exc:  # noqa: BLE001 — never let the loop die
                logger.exception("Trigger loop tick failed: %s", exc)
                WORKER_STATUS["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
            # Pulse (2026-08-26, finacredit post-mortem): the loop's health
            # was only inferable from log silence, and silence reads the
            # same whether the worker is calmly holding or dead. /health
            # now carries this dict; the dashboard shows the pulse.
            WORKER_STATUS["last_tick_at"] = datetime.now(timezone.utc).isoformat()
            WORKER_STATUS["ticks"] = WORKER_STATUS.get("ticks", 0) + 1
            WORKER_STATUS["entries_last_tick"] = handled
            # DB-egress diet (2026-08-20: the 1s SELECT loop + per-estimate
            # audit rows blew Neon's free-tier transfer quota and took the
            # whole app down). With nothing armed, poll every 10s; the 1s
            # cadence returns automatically once entries exist.
            await asyncio.sleep(interval if handled else max(interval, 10))
    finally:
        await gd_client.aclose()


# --- GoDaddy-watchlist sweep (2026-09-24, the client) ---------------------------
# The /live piggyback only imports stars on domains someone VIEWS in the
# dashboard. This sweep walks the near-term expiring feed (soonest-ending
# first — where a star is most urgent) asking the availability API in bulk,
# so a domain the client starred on auctions.godaddy.com shows up here even if
# nobody ever scrolled past it. Gentle by design: bounded calls per sweep,
# spaced out, long interval. Disable with GD_WATCH_SWEEP_ENABLED=false.
GD_WATCH_SWEEP_ENABLED = os.getenv("GD_WATCH_SWEEP_ENABLED", "true").lower() not in ("false", "0", "no")
GD_WATCH_SWEEP_MINUTES = int(os.getenv("GD_WATCH_SWEEP_MINUTES", "360"))
GD_WATCH_SWEEP_HORIZON_HOURS = float(os.getenv("GD_WATCH_SWEEP_HORIZON_HOURS", "48"))
GD_WATCH_SWEEP_MAX_CALLS = int(os.getenv("GD_WATCH_SWEEP_MAX_CALLS", "400"))


async def run_gd_watch_sweep_loop() -> None:
    """Forever loop. Started as a background task at app boot."""
    from app.godaddy.inventory_index import list_by_end_time
    from app.godaddy.live_listings import LiveListingsClient
    from app.godaddy.watch_import import import_gd_watched

    cfg = get_settings()
    gd_auth = GoDaddyAuth(key=cfg.godaddy_api_key, secret=cfg.godaddy_api_secret)
    gd_config = GoDaddyClientConfig(
        rest_base_url=cfg.rest_base_url,
        customer_id=cfg.godaddy_customer_id,
    )
    gd_client = GoDaddyClient(auth=gd_auth, config=gd_config)
    live = LiveListingsClient(gd_client)

    logger.info(
        "GD-watch sweep starting: every %dmin, horizon %.0fh, max %d calls/sweep",
        GD_WATCH_SWEEP_MINUTES, GD_WATCH_SWEEP_HORIZON_HOURS, GD_WATCH_SWEEP_MAX_CALLS,
    )
    await asyncio.sleep(300)  # let boot + first ticks settle first
    try:
        while True:
            imported_total = 0
            calls = 0
            try:
                offset = 0
                while calls < GD_WATCH_SWEEP_MAX_CALLS:
                    rows = await list_by_end_time(
                        limit=50, offset=offset,
                        to_hours=GD_WATCH_SWEEP_HORIZON_HOURS,
                    )
                    if not rows:
                        break
                    offset += len(rows)
                    calls += 1
                    results = await live.check([r.domain for r in rows])
                    watched = [L for L in results.values() if L.watching]
                    if watched:
                        async with SessionLocal() as session:
                            imported = await import_gd_watched(session, watched)
                            imported_total += len(imported)
                    await asyncio.sleep(0.6)  # be gentle; sniper calls come first
            except Exception as exc:  # noqa: BLE001 — never let the loop die
                logger.exception("GD-watch sweep pass failed: %s", exc)
            logger.info(
                "GD-watch sweep done: %d calls, %d imported. Next in %dmin.",
                calls, imported_total, GD_WATCH_SWEEP_MINUTES,
            )
            await asyncio.sleep(GD_WATCH_SWEEP_MINUTES * 60)
    finally:
        await gd_client.aclose()


async def _tick(soap: SoapClient, rest: RestClient) -> int:
    """One pass over the armed watchlist. Returns entries handled."""
    async with SessionLocal() as session:
        entries = await _load_armed_entries(session)
        if not entries:
            return 0
        for entry in entries:
            try:
                await _evaluate_entry(session, soap, rest, entry)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Failed to evaluate watchlist entry id=%s domain=%s: %s",
                    entry.id,
                    entry.domain,
                    exc,
                )
        return len(entries)


async def _load_armed_entries(session: AsyncSession) -> list[WatchlistEntry]:
    """Pull ACTIONABLE armed entries, imminent-first.

    2026-08-26 (finacredit post-mortem, the REAL killer): 78 armed entries,
    25-per-tick cap, and `end_time ASC` sorts LONG-DEAD auctions first —
    their end times are the smallest. Every future-ending auction (i.e.
    every auction that could still be won) starved behind the corpses, and
    a $51 snipe never ran once. Two fixes:

      * only load entries that can ACT — a max bid or a ladder. A star
        with no price is watch-only; the worker has no move to make.
      * sort future-ending auctions FIRST (soonest first), then unknown
        end times, then the already-ended (closeout-conversion watches).
    """
    from sqlalchemy import case, or_

    now = datetime.now(timezone.utc)
    priority = case(
        (Auction.end_time_utc >= now, 0),   # live auctions: the ones we can win
        (Auction.end_time_utc.is_(None), 1),  # unknown: don't bury them
        else_=2,                              # ended: closeout-conversion watches
    )
    result = await session.execute(
        select(WatchlistEntry)
        .join(Auction, Auction.listing_id == WatchlistEntry.listing_id, isouter=True)
        .where(WatchlistEntry.is_armed.is_(True))
        .where(WatchlistEntry.status == "pending")
        .where(or_(
            WatchlistEntry.max_bid_dollars.isnot(None),
            WatchlistEntry.closeout_price_ladder_json.isnot(None),
        ))
        .order_by(
            priority,
            Auction.end_time_utc.asc().nulls_last(),
            WatchlistEntry.created_at.asc(),
        )
        .limit(MAX_ENTRIES_PER_TICK)
    )
    return list(result.scalars().all())


async def _evaluate_entry(
    session: AsyncSession,
    soap: SoapClient,
    rest: RestClient,
    entry: WatchlistEntry,
) -> None:
    """Dispatcher — routes the entry to the closeout or expiry handler.

    We look up the associated Auction row to learn auction_type and end_time.
    If no auction row exists (entry was added before we tracked it), we
    default to CLOSEOUT, which is the more common case.
    """
    auction = await _load_auction(session, entry)
    auction_type = (
        (auction.auction_type if auction else None) or "CLOSEOUT"
    ).upper()

    if auction_type == "EXPIRY_AUCTION":
        await _evaluate_expiry(session, rest, entry, auction, soap=soap)
    elif auction_type in ("CLOSEOUT", "CLOSEOUTS"):
        await _evaluate_closeout(session, soap, entry, auction=auction)
    else:
        logger.warning(
            "Unknown auction_type=%s for %s; skipping",
            auction_type,
            entry.domain,
        )


async def _load_auction(
    session: AsyncSession, entry: WatchlistEntry
) -> Optional[Auction]:
    """Fetch the matching auctions row by listing_id. May be None if the entry
    was created without the inventory sync having seen this listing yet."""
    result = await session.execute(
        select(Auction).where(Auction.listing_id == entry.listing_id)
    )
    return result.scalar_one_or_none()


async def _evaluate_closeout(
    session: AsyncSession,
    soap: SoapClient,
    entry: WatchlistEntry,
    auction: Optional[Auction] = None,
) -> None:
    """Closeout path — check one watchlist entry. Fire purchase if conditions are met."""
    import time as _time

    # Skip if no buy strategy is set. We can't act on an entry with no
    # max_bid AND no price ladder — there's no condition to match.
    ladder = _parse_ladder(entry.closeout_price_ladder_json)
    if entry.max_bid_dollars is None and not ladder:
        return

    # Brooke-pattern cadence: poll hot near events, cold otherwise.
    st = _closeout_state.get(entry.id)
    now_mono = _time.monotonic()
    if st is not None:
        target = entry.max_bid_dollars
        interval = _closeout_poll_interval(
            target=target,
            last_success=st.get("success"),
            last_price=st.get("price"),
            seconds_since_change=now_mono - st.get("changed", now_mono),
            entry_age_seconds=(
                (datetime.now(timezone.utc) - entry.created_at).total_seconds()
                if entry.created_at else 1e9
            ),
        )
        if (now_mono - st.get("checked", 0)) < interval:
            return

    # 1. Hit SOAP to get the current closeout price.
    estimate = await soap.estimate_closeout_price(entry.domain)
    await _audit_estimate(session, entry, estimate)

    # Track state transitions for the cadence policy.
    prev = _closeout_state.get(entry.id) or {}
    new_price = estimate.listing_price_dollars if estimate.success else None
    changed = (
        prev.get("success") != estimate.success
        or (estimate.success and prev.get("price") != new_price)
    )
    _closeout_state[entry.id] = {
        "success": estimate.success,
        "price": new_price if estimate.success else prev.get("price"),
        "changed": _time.monotonic() if changed else prev.get("changed", _time.monotonic()),
        "checked": _time.monotonic(),
        "hb": prev.get("hb", 0.0),
    }
    # Heartbeat (2026-08-25, buildultra post-mortem): at INFO level a healthy
    # watch was silent, so a DEAD watch looked identical to a live one and we
    # only found out when the buy didn't fire. One line per minute per armed
    # entry — grep WATCHING and you know exactly what the worker is holding.
    # Fires immediately on the first poll so arming gives instant feedback.
    _st = _closeout_state[entry.id]
    if _time.monotonic() - _st["hb"] >= 60:
        _st["hb"] = _time.monotonic()
        logger.info(
            "WATCHING: %s id=%s listed=%s target=$%s",
            entry.domain,
            entry.id,
            f"${new_price}" if estimate.success else "(not in closeout)",
            entry.max_bid_dollars,
        )
    if changed and not estimate.success and prev.get("success"):
        logger.info(
            "TRANSITION: %s left closeout listing (was $%s) — polling HOT "
            "every %.1fs for the reappearance race",
            entry.domain, prev.get("price"), CLOSEOUT_HOT_SECONDS,
        )
    if changed and estimate.success and prev.get("success") is False:
        logger.info(
            "REAPPEARED: %s back in closeout at $%s (was gone %.0fs)",
            entry.domain, new_price,
            _time.monotonic() - prev.get("changed", _time.monotonic()),
        )

    if not estimate.success:
        # Not currently in closeout (or domain not found). This is normal —
        # the auction may not have moved to closeout phase yet, or might
        # have already been bought by someone else. Either way: wait; the
        # cadence policy above decides how aggressively we retry.

        # Give-up rule: if the conversion never comes (someone won the
        # expiry auction, or the 5-day closeout window has passed), stop
        # watching a while after the recorded auction end.
        if auction is not None and auction.end_time_utc is not None:
            from datetime import timedelta as _td

            now = datetime.now(timezone.utc)
            if now > auction.end_time_utc + _td(days=CLOSEOUT_GIVEUP_DAYS):
                logger.info(
                    "Closeout watch for %s id=%s gave up (%d days past "
                    "auction end with no conversion); marking expired",
                    entry.domain,
                    entry.id,
                    CLOSEOUT_GIVEUP_DAYS,
                )
                entry.status = "expired"
                entry.note = (
                    "[closeout watch ended] No closeout conversion appeared "
                    f"within {CLOSEOUT_GIVEUP_DAYS} days of the auction end — "
                    "the domain was likely won by another bidder or renewed."
                    f"\n\n{entry.note or ''}"
                )
                entry.updated_at = now
                await session.commit()
                _closeout_state.pop(entry.id, None)
                return

        logger.debug(
            "Estimate not available for %s (id=%s): %s",
            entry.domain,
            entry.id,
            estimate.failure_message,
        )
        return

    current_price = estimate.listing_price_dollars
    total_cost = estimate.total_dollars
    if current_price is None or total_cost is None:
        logger.warning(
            "Estimate for %s parsed but missing price fields. Skipping. "
            "Raw failure: %s",
            entry.domain,
            estimate.failure_message,
        )
        return

    # 2. Decide: does the current price hit any of our trigger conditions?
    decision = _should_fire(
        current_price=current_price,
        max_bid=entry.max_bid_dollars,
        ladder=ladder,
    )
    if not decision.fire:
        logger.debug(
            "Hold: %s current=$%s max=$%s ladder=%s (reason=%s)",
            entry.domain,
            current_price,
            entry.max_bid_dollars,
            ladder,
            decision.reason,
        )
        return

    logger.info(
        "FIRE: %s id=%s current=$%s total=$%s trigger=%s",
        entry.domain,
        entry.id,
        current_price,
        total_cost,
        decision.reason,
    )

    # 3. Safety governors. Any rejection here is terminal — no API call.
    ctx = GovernorContext(
        action_type="CLOSEOUT_BUY",
        total_cost_dollars=total_cost,
        listing_id=entry.listing_id,
        domain=entry.domain,
        reference_floor_dollars=current_price,
        operator_override_dollars=entry.cap_override_dollars,
    )
    try:
        await check_all(session, ctx)
    except GovernorRejection as rej:
        logger.warning(
            "Governor rejected buy for %s: %s. Disarming entry.",
            entry.domain,
            rej,
        )
        entry.is_armed = False
        entry.note = (
            f"[auto-disarmed by governor] {rej}\n\n{entry.note or ''}"
        )
        entry.updated_at = datetime.now(timezone.utc)
        await session.commit()
        return

    # 4. Lock the entry to "executed" BEFORE firing the buy. If the buy call
    # crashes mid-flight, the next tick will see status=executed and skip.
    # This is our protection against double-buying.
    entry.status = "executed"
    entry.updated_at = datetime.now(timezone.utc)
    await session.commit()

    # 5. Fire the buy (or, in dry-run, log it).
    if DRY_RUN:
        logger.warning(
            "DRY RUN: would have purchased %s for $%s (price_key=%s). "
            "Set TRIGGER_DRY_RUN=false to enable live buying.",
            entry.domain,
            total_cost,
            (estimate.price_key or "")[:20],
        )
        # Record a dry-run purchase row so the audit trail is complete.
        purchase = Purchase(
            watchlist_entry_id=entry.id,
            listing_id=entry.listing_id,
            domain=entry.domain,
            action_type="CLOSEOUT_BUY",
            amount_dollars=total_cost,
            outcome="dry_run",
            fired_at=datetime.now(timezone.utc),
            raw_response="DRY_RUN — no API call made",
        )
        session.add(purchase)
        await session.commit()
        return

    # Live path. Duplicate guard first (audit R3): a manual Buy Now may
    # have just fired for the same listing from the API.
    await acquire_listing_lock(session, entry.listing_id)
    dup = await find_recent_money_attempt(session, entry.listing_id)
    if dup is not None:
        logger.warning(
            "Skipping worker buy for %s: attempt already %s at %s",
            entry.domain,
            dup.outcome,
            dup.fired_at,
        )
        return

    # Official-API path: preview BEFORE the in_flight record. Preview is
    # read-only, so a declined preview (listing blinking out mid-rung, the
    # "purgatory" window) must NOT create a purchase row — that would arm
    # the duplicate guard's 10-min window and block the refire when the
    # listing reappears seconds later.
    instant = None
    preview_total_micros = None
    preview_total_dollars = None
    if USE_INSTANT_PURCHASE_API:
        from app.godaddy.instant import InstantPurchaseClient

        instant = InstantPurchaseClient(soap.client)
        try:
            previews = await instant.preview([entry.domain])
            pv = previews.get(entry.domain.lower())
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Instant preview raised for %s: %s — resuming watch",
                entry.domain, exc,
            )
            pv = None
        if pv is None or not pv.success or pv.total_price_micros is None:
            reason = pv.failure_reason if pv else "no preview result"
            logger.warning(
                "Instant preview declined %s (%s) — resuming watch "
                "(no money moved, no purchase row)",
                entry.domain, reason,
            )
            entry.status = "pending"
            entry.updated_at = datetime.now(timezone.utc)
            await session.commit()
            return
        if pv.total_dollars is not None and pv.total_dollars > total_cost + Decimal("1.00"):
            logger.warning(
                "Preview total $%s moved above approved $%s for %s — "
                "resuming watch; next tick re-decides at the real price",
                pv.total_dollars, total_cost, entry.domain,
            )
            entry.status = "pending"
            entry.updated_at = datetime.now(timezone.utc)
            await session.commit()
            return
        preview_total_micros = pv.total_price_micros
        preview_total_dollars = pv.total_dollars

    # Commit the in_flight record BEFORE the money call (audit R5).
    purchase = await begin_in_flight(
        session,
        watchlist_entry_id=entry.id,
        listing_id=entry.listing_id,
        domain=entry.domain,
        action_type="CLOSEOUT_BUY",
        amount_dollars=preview_total_dollars or total_cost,
    )

    try:
        if instant is not None:
            res = await instant.purchase(
                domain_name=entry.domain,
                total_price_micros=preview_total_micros,
            )
            success = res.success
            order_id = res.order_id
            failure_reason = res.failure_reason
            raw = (
                "OK [official api]" if success
                else f"{failure_reason or 'FAILED'} [official api]"
            )
        else:
            result = await soap.execute_closeout_purchase(
                domain_name=entry.domain,
                price_key=estimate.price_key,
            )
            success = result.success
            order_id = result.order_id
            failure_reason = None
            raw = result.failure_message or "OK"
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "purchase call raised for %s: %s",
            entry.domain,
            exc,
        )
        # Mark the attempt errored; operator reconciles against GoDaddy
        # order history (the order MAY have gone through).
        complete_attempt(
            purchase,
            outcome="error",
            raw_response=f"EXCEPTION: {type(exc).__name__}: {exc}",
        )
        await session.commit()
        return

    # 6. Persist the result on the in_flight row.
    complete_attempt(
        purchase,
        outcome="won" if success else "lost",
        order_id=order_id,
        raw_response=raw,
    )
    if success:
        entry.status = "won"
    elif failure_reason == "PRICE_MISMATCH":
        # Price moved between preview and purchase (rung drop mid-race).
        # GoDaddy's server-side guard means nothing was charged — resume
        # watching; the dup-guard window applies before any refire.
        logger.info(
            "PRICE_MISMATCH for %s — nothing charged, resuming watch",
            entry.domain,
        )
        entry.status = "pending"
    else:
        entry.status = "lost"
    entry.updated_at = datetime.now(timezone.utc)
    await session.commit()

    logger.info(
        "Purchase outcome: %s id=%s result=%s order_id=%s via=%s",
        entry.domain,
        entry.id,
        "WON" if success else "LOST",
        order_id,
        "official-api" if instant is not None else "soap",
    )


async def _get_fresh_state(
    soap: SoapClient,
    entry: WatchlistEntry,
    *,
    ignore_ttl: bool = False,
):
    """Live auction state via GetAuctionDetailsByDomainName, throttled.

    Returns a LookupResponse (found True/False), or None when the SOAP call
    itself failed (network / HTTP error) — callers must treat None as
    "unknown", NOT as "auction gone".
    """
    import time as _time

    from app.api.lookup import _parse_details

    cached = _fresh_state_cache.get(entry.id)
    if cached and not ignore_ttl and (_time.monotonic() - cached[0]) < LOOKUP_TTL_SECONDS:
        return cached[1]

    inner_xml = await soap.get_auction_details_by_domain_name(entry.domain)
    state = _parse_details(inner_xml) if inner_xml else None
    _fresh_state_cache[entry.id] = (_time.monotonic(), state)
    return state


def _expiry_fire_decision(
    seconds_remaining: float,
    has_bids: bool,
    use_bell: bool = True,
) -> str:
    """Pure timing policy for expiry snipes: 'fire' | 'hold' | 'lapse'.

    The client's two rules (clarified on the 2026-09-23 call — they were always
    two separate rules, not one):

    Rule 1 — never throw the first punch. A first bid "attracts the sharks",
    so with ZERO bids we never fire, in ANY mode, all the way to the end.
    Lapse -> $50 closeout instant-buy is the play (myhomebills pattern).

    Rule 2 — when others are already fighting, snipe at the last gasp.
    Default mode (use_bell=False): hold until the final SNIPE_WINDOW_SECONDS,
    then fire; the evaluator's precise-wait then lands the bid at
    T-SNIPE_LEAD_SECONDS (~1.5s). The resulting 5:00 clock reset is desired.

    Bell mode (use_bell=True, retired 2026-09-23 but kept switchable): fire
    once just BEFORE the 5-minute extension window instead, so the bid causes
    no extension and no ending-soon spotlight.
    """
    if seconds_remaining <= 0:
        return "lapse"
    if not has_bids:
        # Rule 1: never volunteer the first bid — closeout is the play.
        return "hold"
    if not use_bell:
        return "fire" if seconds_remaining <= SNIPE_WINDOW_SECONDS else "hold"
    if seconds_remaining > PRE_BELL_FIRE_SECONDS:
        return "hold"
    if seconds_remaining > BELL_SECONDS:
        # The pre-bell slot: the ONLY moment bell mode volunteers a bid.
        return "fire"
    # Inside the bell with bids (pre-bell fire failed or a rival jumped in
    # late): classic snipe in the final seconds.
    return "fire" if seconds_remaining <= SNIPE_WINDOW_SECONDS else "hold"


async def _evaluate_expiry(
    session: AsyncSession,
    rest: RestClient,
    entry: WatchlistEntry,
    auction: Optional[Auction],
    soap: Optional[SoapClient] = None,
) -> None:
    """Expiry-auction path — snipe a bid in the last `SNIPE_WINDOW_SECONDS`.

    Strategy: classic auction sniping. We wait until the very end (default
    last 60 seconds) and then fire a single bid for the user's max_bid.
    GoDaddy's bid system auto-bids us up only as needed to stay top, so
    placing max_bid as a single bid is equivalent to placing the lowest
    winning bid we'd be willing to accept.

    We don't try to react to outbids — that turns the workflow into a war
    of attrition that overpays. One shot, near the end, at the max.
    """
    if entry.max_bid_dollars is None:
        logger.debug(
            "Expiry entry %s has no max_bid_dollars set; skipping",
            entry.domain,
        )
        return

    # A synthetic listing_id (feed-hash or dashboard Date.now() shim) can
    # never be bid on — GoDaddy would reject it in the final 60 seconds,
    # silently losing the auction. Disarm NOW with a visible note instead.
    from app.godaddy.listing_ids import is_real_listing_id

    if not is_real_listing_id(entry.listing_id):
        logger.warning(
            "Expiry entry %s (id=%s) has synthetic listing_id=%s — cannot "
            "snipe. Disarming with note.",
            entry.domain,
            entry.id,
            entry.listing_id,
        )
        entry.is_armed = False
        entry.note = (
            "[auto-disarmed] This entry has no real GoDaddy auction id, so "
            "the snipe bid would be rejected. Remove and re-add the domain "
            "from the main list (post 2026-07-11 sync), then set the max "
            f"bid again.\n\n{entry.note or ''}"
        )
        entry.updated_at = datetime.now(timezone.utc)
        await session.commit()
        return

    # We need the auction end_time to know when to snipe. If the entry was
    # added without an auction snapshot (or before sync ran), we can't snipe.
    if auction is None or auction.end_time_utc is None:
        logger.warning(
            "No end_time available for %s (id=%s); cannot determine snipe "
            "timing. Add the auction snapshot to the watchlist entry or run "
            "the inventory sync first.",
            entry.domain,
            entry.id,
        )
        return

    now = datetime.now(timezone.utc)
    seconds_remaining = (auction.end_time_utc - now).total_seconds()

    # --- Live re-verification near close (THMY fix, 2026-07-11) ----------
    # GoDaddy resets the clock ~5-6 min on every late bid, so the stored
    # end_time understates reality the moment a bidding war starts. Within
    # the lookup horizon we refresh end time + current price from GoDaddy
    # before deciding anything, and we never expire an entry on the stored
    # clock alone.
    if soap is not None and seconds_remaining <= LOOKUP_HORIZON_SECONDS:
        state = await _get_fresh_state(
            soap,
            entry,
            # Final pre-bid checks must be current, not 20s-old cache: the
            # last-seconds snipe, AND the pre-bell decision slot (a stale
            # has_bids there could make us hold when we should fire).
            ignore_ttl=(
                seconds_remaining <= SNIPE_WINDOW_SECONDS
                or (USE_BELL_SNIPE and BELL_SECONDS - 5 < seconds_remaining <= PRE_BELL_FIRE_SECONDS + 20)
            ),
        )
        if state is None:
            if seconds_remaining <= 0:
                # Stored clock says it's over but we couldn't verify.
                # Do nothing — next tick retries. Never expire unverified.
                logger.warning(
                    "Stored end time passed for %s but live lookup failed; "
                    "holding (will retry).",
                    entry.domain,
                )
                return
        elif not state.found:
            # GoDaddy confirms the expiry auction is gone. Sweet spot: if it
            # ended with no winning bids it converts to a $50 closeout soon —
            # switch to closeout watch instead of giving up.
            logger.info(
                "Live lookup confirms expiry over for %s id=%s; switching "
                "to closeout watch",
                entry.domain,
                entry.id,
            )
            await _transition_to_closeout_watch(session, entry, auction, now)
            return
        elif state.auction_type == "CLOSEOUT":
            # Stale routing fix (2026-08-25, the sorrybutno lesson): the
            # auction row said EXPIRY but GoDaddy says this domain is in
            # CLOSEOUT now. Flip the row so the next tick routes to the
            # closeout evaluator and the auto-buy can actually fire.
            logger.info(
                "%s is in CLOSEOUT per live lookup; flipping stale EXPIRY "
                "routing", entry.domain,
            )
            auction.auction_type = "CLOSEOUT"
            await session.commit()
            return
        else:
            # Refresh our snapshot with live values.
            if state.end_time_utc is not None:
                auction.end_time_utc = state.end_time_utc
                seconds_remaining = (state.end_time_utc - now).total_seconds()
            if state.bid_count is not None:
                # The bell decision runs on has_bids — keep it live.
                auction.bid_count = state.bid_count
                auction.has_bids = state.bid_count > 0
            if state.current_price_dollars is not None:
                auction.current_price = state.current_price_dollars
                auction.last_synced_at = now
                if state.current_price_dollars >= entry.max_bid_dollars:
                    # Someone already bid at/above the client's ceiling. Keep
                    # the entry VISIBLE with an explicit outbid status —
                    # never silently drop it (demo feedback 2026-07-11).
                    logger.info(
                        "Outbid before snipe: %s current=$%s >= max=$%s",
                        entry.domain,
                        state.current_price_dollars,
                        entry.max_bid_dollars,
                    )
                    entry.status = "outbid"
                    entry.note = (
                        f"[outbid] Current bid ${state.current_price_dollars} "
                        f"reached your max ${entry.max_bid_dollars} before the "
                        f"snipe window. Raise the max to re-arm.\n\n{entry.note or ''}"
                    )
                    entry.updated_at = now
                    await session.commit()
                    return
            await session.commit()  # persist refreshed end time / price

    has_bids = bool(auction.has_bids) or (auction.bid_count or 0) > 0
    if seconds_remaining > 0:
        fire_decision = _expiry_fire_decision(seconds_remaining, has_bids, USE_BELL_SNIPE)
        if fire_decision == "hold":
            # Heartbeat: one INFO line per minute per armed expiry entry.
            # (2026-08-26 finacredit: the expiry path was silent, so a dead
            # worker and a patient one looked identical in the logs.)
            import time as _t

            if _t.monotonic() - _expiry_hb.get(entry.id, 0.0) >= 60:
                _expiry_hb[entry.id] = _t.monotonic()
                logger.info(
                    "SNIPE WATCH: %s id=%s ends_in=%.0fs max_bid=$%s bids=%s bell=%s",
                    entry.domain,
                    entry.id,
                    seconds_remaining,
                    entry.max_bid_dollars,
                    auction.bid_count,
                    USE_BELL_SNIPE,
                )
            logger.debug(
                "Hold: %s ends in %.0fs has_bids=%s (bell=%s window=%ds)",
                entry.domain,
                seconds_remaining,
                has_bids,
                USE_BELL_SNIPE,
                SNIPE_WINDOW_SECONDS,
            )
            return

    if seconds_remaining <= 0:
        # Reachable only after live verification (or with no SOAP client,
        # e.g. in unit tests).
        if soap is not None:
            # Verified over — hand off to closeout watch (sweet spot).
            logger.info(
                "Expiry ended without firing for %s id=%s; switching to "
                "closeout watch",
                entry.domain,
                entry.id,
            )
            await _transition_to_closeout_watch(session, entry, auction, now)
            return
        # Legacy/no-SOAP path: mark expired so we stop checking.
        logger.info(
            "Auction ended without firing for %s id=%s; marking expired",
            entry.domain,
            entry.id,
        )
        entry.status = "expired"
        entry.updated_at = now
        await session.commit()
        return

    logger.info(
        "SNIPE (%s): %s id=%s ends_in=%.0fs max_bid=$%s bids=%s",
        "pre-bell" if seconds_remaining > BELL_SECONDS else "final-window",
        entry.domain,
        entry.id,
        seconds_remaining,
        entry.max_bid_dollars,
        auction.bid_count,
    )

    # Self-bidding collision guard (2026-09-24, the client's #1 fear). Right
    # before firing, ask the availability API whether the client ALREADY has a
    # bid on this listing (manual bid on auctions.godaddy.com). If so, stand
    # down — firing his max would pile onto his own bid and drive his own
    # price up. Hold (don't disarm): if the availability lookup fails we
    # proceed with the snipe rather than miss the domain (fail-open on an
    # UNKNOWN, since the whole point is not to lose auctions).
    if COLLISION_GUARD_ENABLED:
        try:
            from app.godaddy.collision import is_already_bidding
            from app.godaddy.live_listings import LiveListingsClient

            live = await LiveListingsClient(rest.client).check([entry.domain])
            ll = live.get(entry.domain)
            mbs = ll.member_bidding_status if ll else None
            if mbs:
                logger.info(
                    "Collision check: %s memberBiddingStatus=%s", entry.domain, mbs
                )
            if ll is not None and is_already_bidding(mbs):
                logger.warning(
                    "COLLISION GUARD: %s — the client already has a bid "
                    "(memberBiddingStatus=%s). Standing down to avoid bidding "
                    "against himself.",
                    entry.domain, mbs,
                )
                entry.note = (
                    f"[collision guard] You already have a bid on this domain "
                    f"on GoDaddy (status: {mbs}). The sniper stood down so it "
                    f"wouldn't bid against you and raise your own price. Raise "
                    f"or clear your GoDaddy bid, or remove this to let the "
                    f"sniper take over.\n\n{entry.note or ''}"
                )
                entry.updated_at = now
                await session.commit()
                return
        except Exception as exc:  # noqa: BLE001 — never lose a snipe to this
            logger.warning(
                "Collision check failed for %s (%s); proceeding with snipe.",
                entry.domain, exc,
            )

    # Safety governors. Reference floor for the sanity check is the auction's
    # current_price — bidding 10x current is the classic typo this protects.
    ctx = GovernorContext(
        action_type="BID",
        total_cost_dollars=entry.max_bid_dollars,
        listing_id=entry.listing_id,
        domain=entry.domain,
        reference_floor_dollars=auction.current_price,
        operator_override_dollars=entry.cap_override_dollars,
    )
    try:
        await check_all(session, ctx)
    except GovernorRejection as rej:
        logger.warning(
            "Governor rejected bid for %s: %s. Disarming entry.",
            entry.domain,
            rej,
        )
        entry.is_armed = False
        entry.note = f"[auto-disarmed by governor] {rej}\n\n{entry.note or ''}"
        entry.updated_at = now
        await session.commit()
        return

    # Lock the entry BEFORE firing so a crash can't double-bid.
    entry.status = "executed"
    entry.updated_at = now
    await session.commit()

    if DRY_RUN:
        logger.warning(
            "DRY RUN: would have placed bid $%s on %s. Set TRIGGER_DRY_RUN=false to enable.",
            entry.max_bid_dollars,
            entry.domain,
        )
        purchase = Purchase(
            watchlist_entry_id=entry.id,
            listing_id=entry.listing_id,
            domain=entry.domain,
            action_type="BID",
            amount_dollars=entry.max_bid_dollars,
            outcome="dry_run",
            fired_at=now,
            raw_response="DRY_RUN — no API call made",
        )
        session.add(purchase)
        await session.commit()
        return

    # Precise-fire wait (2026-09-23, client call): land the bid at
    # T-~SNIPE_LEAD_SECONDS instead of on 1s tick granularity. The fresh
    # SOAP verification already ran above, so after this sleep only the DB
    # bookkeeping below + the bid POST remain (~0.3-0.8s total observed) —
    # initiating at T-lead puts the bid on GoDaddy's books just before the
    # close. Guarded to the final window so a bell-mode pre-bell fire
    # (t≈305s) can never sleep here.
    if (
        seconds_remaining <= SNIPE_WINDOW_SECONDS
        and auction.end_time_utc is not None
    ):
        _wait = (
            (auction.end_time_utc - datetime.now(timezone.utc)).total_seconds()
            - SNIPE_LEAD_SECONDS
        )
        if _wait > 0:
            await asyncio.sleep(min(_wait, float(SNIPE_WINDOW_SECONDS)))

    # Live path. Duplicate guard (audit R3) + in_flight record (R5), then
    # pull the per-transaction cap from settings as a final guard; the
    # governor already checked it, but RestClient enforces it again.
    await acquire_listing_lock(session, entry.listing_id)
    dup = await find_recent_money_attempt(session, entry.listing_id)
    if dup is not None:
        logger.warning(
            "Skipping snipe for %s: attempt already %s at %s",
            entry.domain,
            dup.outcome,
            dup.fired_at,
        )
        return

    settings = (
        await session.execute(select(SystemSettings).where(SystemSettings.id == 1))
    ).scalar_one()

    purchase = await begin_in_flight(
        session,
        watchlist_entry_id=entry.id,
        listing_id=entry.listing_id,
        domain=entry.domain,
        action_type="BID",
        amount_dollars=entry.max_bid_dollars,
    )

    import time as _time

    # Recompute post-sleep so SNIPE LATENCY reports the true fire moment.
    _remaining_at_fire = (
        (auction.end_time_utc - datetime.now(timezone.utc)).total_seconds()
        if auction.end_time_utc is not None
        else seconds_remaining
    )
    _fire_started = _time.monotonic()
    try:
        responses = await rest.place_bids(
            [
                BidRequest(
                    listing_id=entry.listing_id,
                    amount_dollars=entry.max_bid_dollars,
                )
            ],
            per_tx_cap_dollars=settings.per_transaction_cap_dollars,
        )
        logger.info(
            "SNIPE LATENCY: %s fire->response %.0fms (window=%ds, lead=%.1fs, "
            "seconds_remaining_at_fire=%.1f)",
            entry.domain,
            (_time.monotonic() - _fire_started) * 1000,
            SNIPE_WINDOW_SECONDS,
            SNIPE_LEAD_SECONDS,
            _remaining_at_fire,
        )
    except GoDaddyBidError as exc:
        logger.error(
            "Bid rejected for %s: status=%s body=%s",
            entry.domain,
            exc.status_code,
            exc.error_body,
        )
        complete_attempt(
            purchase,
            outcome="error",
            raw_response=f"HTTP {exc.status_code}: {exc.error_body}",
        )
        await session.commit()
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error placing bid for %s: %s", entry.domain, exc)
        complete_attempt(
            purchase,
            outcome="error",
            raw_response=f"EXCEPTION: {type(exc).__name__}: {exc}",
        )
        await session.commit()
        return

    # Record the response. For a single bid, we use the first (only) item.
    bid_resp = responses[0]
    outcome = (
        "won"
        if bid_resp.status == "SUCCESS" and bid_resp.is_highest_bidder
        else (
            "outbid"
            if bid_resp.status == "SUCCESS"
            else "error"
        )
    )
    complete_attempt(
        purchase,
        outcome=outcome,
        bid_id=bid_resp.bid_id,
        raw_response=(
            f"status={bid_resp.status} highest={bid_resp.is_highest_bidder} "
            f"bid_id={bid_resp.bid_id} fail={bid_resp.failure_reason}"
        ),
    )

    # Update entry status. "won" means we placed the highest bid AT THIS MOMENT
    # — the auction may still tick down and we could get outbid before close.
    # The final outcome (won/lost) is determined by the auction-end webhook,
    # which we don't have yet; for now we mark optimistically.
    if outcome == "won":
        entry.status = "executed"  # bid placed, awaiting result
    elif outcome == "outbid":
        entry.status = "lost"
    else:
        entry.status = "executed"  # bid attempted but failed; needs human review
    entry.updated_at = now
    await session.commit()

    logger.info(
        "BID outcome: %s id=$%s result=%s bid_id=%s",
        entry.domain,
        entry.max_bid_dollars,
        outcome,
        bid_resp.bid_id,
    )


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------


class _Decision:
    """Tiny dataclass-shaped object so the caller can read .fire and .reason."""

    __slots__ = ("fire", "reason")

    def __init__(self, fire: bool, reason: str):
        self.fire = fire
        self.reason = reason


def _should_fire(
    *,
    current_price: Decimal,
    max_bid: Optional[Decimal],
    ladder: list[Decimal],
) -> _Decision:
    """Determine whether the current closeout price triggers a buy.

    Two trigger modes:
      * max_bid: fire if current_price <= max_bid (the user said "I'll pay
        up to this much; buy whenever it's at or below.")
      * ladder: fire if current_price matches any rung of the ladder (the
        user gave a specific schedule — typically GoDaddy's natural
        closeout step-down: [50, 25, 11, 5].)

    If both are set, ladder wins on exact match; otherwise max_bid applies.
    """
    # Ladder: exact match on any rung (within a 1¢ tolerance for Decimal
    # comparison safety). A ladder rung NEVER overrides max_bid: if both are
    # set, max_bid is the user's stated ceiling and a rung above it must not
    # fire (audit R4, fixed 2026-07-11).
    for rung in ladder:
        if abs(current_price - rung) <= Decimal("0.01"):
            if max_bid is not None and current_price > max_bid:
                return _Decision(
                    False,
                    f"ladder rung ${rung} exceeds max_bid ${max_bid}; not firing",
                )
            return _Decision(True, f"ladder hit @${rung}")

    # max_bid: at-or-below.
    if max_bid is not None and current_price <= max_bid:
        return _Decision(
            True,
            f"max_bid threshold met (current=${current_price} <= max=${max_bid})",
        )

    return _Decision(False, "no trigger condition met")


def _parse_ladder(ladder_json: Optional[str]) -> list[Decimal]:
    """Parse the JSON-encoded price ladder. Empty list on any failure."""
    if not ladder_json:
        return []
    try:
        raw = json.loads(ladder_json)
        return [Decimal(str(x)) for x in raw]
    except Exception:
        logger.warning("Failed to parse price ladder JSON: %r", ladder_json)
        return []


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------


# entry_id -> (success, price_repr, monotonic_ts) of the last written audit.
_last_audit: dict[int, tuple] = {}
_AUDIT_REPEAT_SECONDS = 600.0


async def _audit_estimate(
    session: AsyncSession,
    entry: WatchlistEntry,
    estimate: CloseoutEstimate,
) -> None:
    """Persist estimate-call audits — SAMPLED (2026-08-20 Neon-quota diet):
    identical consecutive results for an entry are written at most once per
    _AUDIT_REPEAT_SECONDS. State CHANGES (price moved, success flipped)
    always write immediately, so postmortems keep every transition."""
    import time as _time

    key = (estimate.success, str(estimate.listing_price_dollars))
    prev = _last_audit.get(entry.id)
    now_mono = _time.monotonic()
    if prev is not None and prev[0] == key and (now_mono - prev[1]) < _AUDIT_REPEAT_SECONDS:
        return
    _last_audit[entry.id] = (key, now_mono)

    cfg = get_settings()
    audit = AuditLogEntry(
        timestamp=datetime.now(timezone.utc),
        method="POST",
        # We don't have the literal URL on hand here; use a stable identifier
        # so audit queries can filter by call type.
        url=f"soap://{cfg.godaddy_env}/EstimateCloseoutDomainPrice",
        status_code=200 if estimate.success else 0,
        request_body=f"domain={entry.domain}",
        response_body=(
            "success price=${} total=${} price_key={}".format(
                estimate.listing_price_dollars,
                estimate.total_dollars,
                (estimate.price_key or "")[:20],
            )
            if estimate.success
            else f"failure: {estimate.failure_message or '(no message)'}"
        ),
        listing_id=entry.listing_id,
        watchlist_entry_id=entry.id,
    )
    session.add(audit)
    await session.commit()
