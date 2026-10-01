"""GET/POST/PUT/DELETE /api/watchlist — the client's per-auction targeting entries."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import WatchlistCreate, WatchlistResponse, WatchlistUpdate
from app.db import get_db
from app.godaddy.listing_ids import is_real_listing_id
from app.models.auction import Auction
from app.models.notification import NotificationPref
from app.models.watchlist import WatchlistEntry

router = APIRouter()


def _to_response(
    entry: WatchlistEntry, pref: NotificationPref | None
) -> WatchlistResponse:
    """Merge the entry with its reminder prefs into one API object.

    Done by hand rather than via an ORM relationship: async SQLAlchemy
    can't lazy-load on attribute access, so an unloaded relationship would
    raise inside response serialization instead of returning null.
    """
    response = WatchlistResponse.model_validate(entry)
    response.notify_enabled = bool(pref and pref.enabled)
    response.notify_email_to = pref.email_to if pref else None
    return response


async def _get_pref(
    db: AsyncSession, entry_id: int
) -> NotificationPref | None:
    result = await db.execute(
        select(NotificationPref).where(
            NotificationPref.watchlist_entry_id == entry_id
        )
    )
    return result.scalar_one_or_none()


@router.get("", response_model=list[WatchlistResponse])
async def list_watchlist(db: AsyncSession = Depends(get_db)) -> list[WatchlistResponse]:
    """All watchlist entries (no pagination — this list should stay manageable)."""
    result = await db.execute(
        select(WatchlistEntry, NotificationPref)
        .outerjoin(
            NotificationPref,
            NotificationPref.watchlist_entry_id == WatchlistEntry.id,
        )
        .order_by(WatchlistEntry.created_at.desc())
    )
    return [_to_response(entry, pref) for entry, pref in result.all()]


@router.post("", response_model=WatchlistResponse, status_code=201)
async def add_watchlist(
    payload: WatchlistCreate,
    db: AsyncSession = Depends(get_db),
) -> WatchlistResponse:
    """Add an auction to the watchlist.

    If the underlying auction row doesn't already exist (e.g., the daily
    inventory sync hasn't run yet, or this listing came from a manual
    dashboard click against the static JSON feed), we bootstrap it from
    the optional snapshot fields on the payload. This keeps the FK
    invariant intact and gives the auto-buy worker the data it needs.
    """
    # Heal synthetic listing ids server-side (2026-07-14): the dashboard
    # shims Date.now() when it has no real id, but the inventory index
    # usually knows the REAL id for the domain. Swapping it here makes the
    # entry snipeable and keeps the dedupe/lookup keys consistent.
    if not is_real_listing_id(payload.listing_id):
        try:
            from app.godaddy.inventory_index import resolve_domain

            indexed = await resolve_domain(payload.domain)
            if indexed is not None:
                payload.listing_id = indexed.listing_id
                if not payload.auction_type:
                    payload.auction_type = indexed.auction_type
        except Exception:  # noqa: BLE001 — best-effort; shim still works for watch/buy-now
            pass

    # Look up the auction row.
    result = await db.execute(
        select(Auction).where(Auction.listing_id == payload.listing_id)
    )
    auction = result.scalar_one_or_none()

    if auction is None:
        # Bootstrap an auction row from the snapshot in the payload.
        # We need at least tld + auction_type to build a usable row.
        if not payload.tld or not payload.auction_type:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"No auction with listing_id={payload.listing_id} found "
                    "and the request didn't include tld + auction_type to "
                    "bootstrap one. Either run the sync first or include "
                    "the auction snapshot fields on the payload."
                ),
            )
        now = datetime.now(timezone.utc)
        auction = Auction(
            listing_id=payload.listing_id,
            domain=payload.domain,
            tld=payload.tld,
            auction_type=payload.auction_type,
            current_price=payload.current_price,
            estimated_value=payload.estimated_value,
            end_time_utc=payload.end_time_utc,
            bid_count=payload.bid_count or 0,
            has_bids=payload.has_bids or False,
            score=payload.score,
            last_synced_at=now,
            first_seen_at=now,
            status="active",
        )
        db.add(auction)
        await db.flush()  # populate auction.id before we link to it

    # Disallow duplicates.
    existing = await db.execute(
        select(WatchlistEntry).where(WatchlistEntry.listing_id == payload.listing_id)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Listing {payload.listing_id} is already on the watchlist.",
        )

    now = datetime.now(timezone.utc)
    entry = WatchlistEntry(
        auction_id=auction.id,
        listing_id=payload.listing_id,
        domain=payload.domain,
        closeout_price_ladder_json=(
            json.dumps([str(x) for x in payload.closeout_price_ladder])
            if payload.closeout_price_ladder is not None
            else None
        ),
        max_bid_dollars=payload.max_bid_dollars,
        cap_override_dollars=payload.cap_override_dollars,
        is_armed=payload.is_armed,
        note=payload.note,
        created_at=now,
        updated_at=now,
        status="pending",
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    # New entries start with the bell off — reminders are opt-in per domain.
    return _to_response(entry, None)


@router.put("/{entry_id}", response_model=WatchlistResponse)
async def update_watchlist(
    entry_id: int,
    payload: WatchlistUpdate,
    db: AsyncSession = Depends(get_db),
) -> WatchlistResponse:
    """Update price ladder, max bid, note, or armed-state on an existing entry."""
    result = await db.execute(
        select(WatchlistEntry).where(WatchlistEntry.id == entry_id)
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Watchlist entry {entry_id} not found")

    if payload.closeout_price_ladder is not None:
        entry.closeout_price_ladder_json = json.dumps(
            [str(x) for x in payload.closeout_price_ladder]
        )
    if payload.max_bid_dollars is not None:
        # Setting a max bid on an EXPIRY auction requires a real GoDaddy
        # auction id — synthetic ids (feed hashes, "+ Add domain" shims)
        # would be rejected by the bid endpoint in the final 60 seconds.
        # Fail HERE, at save time, so the user finds out immediately.
        auction_q = await db.execute(
            select(Auction).where(Auction.listing_id == entry.listing_id)
        )
        auction = auction_q.scalar_one_or_none()
        auction_type = (auction.auction_type if auction else "").upper()
        if auction_type == "EXPIRY_AUCTION" and not is_real_listing_id(
            entry.listing_id
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Can't arm a snipe for {entry.domain}: this entry has "
                    "no real GoDaddy auction id (it was added manually or "
                    "synced before the 2026-07-11 fix). Remove it and "
                    "re-add it from the main list after the next daily "
                    "sync. Buy Now on closeouts is unaffected."
                ),
            )
        entry.max_bid_dollars = payload.max_bid_dollars
        # Raising the max on an outbid entry re-arms it: the worker only
        # (cap override handled below, so it moves in lockstep with the max)
        # polls status="pending", so flip it back so the new ceiling gets
        # a chance to fire (demo feedback 2026-07-11).
        if entry.status == "outbid":
            entry.status = "pending"
            # Re-arm clears the outbid-alert flag so a fresh outbid on the
            # raised ceiling alerts him again (2026-09-29).
            entry.outbid_alert_sent_at = None
    # Confirm-to-exceed (2026-09-29): the dashboard sends cap_override_dollars
    # only when the client confirmed a max above the per-tx cap; it sends explicit
    # null to clear when he lowers the max back under the cap. Use
    # model_fields_set so "not sent" (leave as-is) is distinct from "sent null"
    # (clear the override).
    if "cap_override_dollars" in payload.model_fields_set:
        entry.cap_override_dollars = payload.cap_override_dollars
    if payload.note is not None:
        entry.note = payload.note
    if payload.is_armed is not None:
        entry.is_armed = payload.is_armed
    if payload.reopen:
        # 2026-09-07 (dellport): a purchase attempt that ERRORS (504 mid-
        # buy) leaves the entry locked "executed" forever — the pre-fire
        # lock doubling as a tombstone. The operator must be able to
        # re-arm after verifying no order went through on GoDaddy's side.
        # Explicit opt-in flag, audit-noted; the duplicate-purchase guard
        # still protects against re-firing inside its window.
        if entry.status in ("executed", "error", "lost", "expired", "outbid"):
            entry.note = (
                f"[re-armed by operator from status={entry.status}]\n\n{entry.note or ''}"
            )
            entry.status = "pending"
            entry.outbid_alert_sent_at = None  # re-arm re-enables outbid alerts
    now = datetime.now(timezone.utc)
    entry.updated_at = now

    # --- reminder prefs ----------------------------------------------------
    # The row is created on first enable and then kept forever (with
    # enabled=False when switched off) so its sent-flags survive toggling.
    pref = await _get_pref(db, entry.id)
    if payload.notify_enabled is not None or payload.notify_email_to is not None:
        if pref is None:
            pref = NotificationPref(
                watchlist_entry_id=entry.id,
                enabled=False,
                created_at=now,
                updated_at=now,
            )
            db.add(pref)
        if payload.notify_enabled is not None:
            pref.enabled = payload.notify_enabled
        if payload.notify_email_to is not None:
            # Empty string clears the override and falls back to the
            # NOTIFY_EMAIL_TO default.
            pref.email_to = payload.notify_email_to.strip() or None
        pref.updated_at = now

    await db.commit()
    await db.refresh(entry)
    if pref is not None:
        await db.refresh(pref)
    return _to_response(entry, pref)


@router.delete("/{entry_id}", status_code=204)
async def remove_watchlist(
    entry_id: int,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a watchlist entry. Doesn't touch the underlying auction row."""
    result = await db.execute(
        select(WatchlistEntry).where(WatchlistEntry.id == entry_id)
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Watchlist entry {entry_id} not found")
    # Drop the reminder prefs explicitly rather than relying on the FK's
    # ON DELETE CASCADE: SQLite (used by the test suite) doesn't enforce
    # foreign keys unless the pragma is on, and an orphaned prefs row is
    # exactly the thing that could email about a domain no longer watched.
    pref = await _get_pref(db, entry.id)
    if pref is not None:
        await db.delete(pref)
    await db.delete(entry)
    await db.commit()
