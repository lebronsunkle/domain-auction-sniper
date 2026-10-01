"""
Pydantic request/response schemas for the REST API.

Kept in a single file so the dashboard team can import them as a contract
without touching ORM models. ORM model -> API schema conversion is explicit.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


# ============================================================================
# Auctions
# ============================================================================


class AuctionResponse(BaseModel):
    id: int
    listing_id: int
    domain: str
    tld: str
    auction_type: str
    current_price: Optional[Decimal] = None
    estimated_value: Optional[Decimal] = None
    end_time_utc: Optional[datetime] = None
    bid_count: int
    has_bids: bool
    score: Optional[int] = None
    score_breakdown: Optional[dict[str, Any]] = None
    first_seen_at: datetime
    last_synced_at: datetime
    status: str

    @field_validator("score_breakdown", mode="before")
    @classmethod
    def parse_score_breakdown(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (ValueError, json.JSONDecodeError):
                return None
        return v

    model_config = {"from_attributes": True}


class AuctionListResponse(BaseModel):
    total: int
    items: list[AuctionResponse]


# ============================================================================
# Watchlist
# ============================================================================


class WatchlistCreate(BaseModel):
    listing_id: int
    domain: str
    closeout_price_ladder: Optional[list[Decimal]] = Field(
        default=None,
        description="Ordered price targets, e.g. [50, 25, 11] — buy at first hit.",
    )
    max_bid_dollars: Optional[Decimal] = None
    # Confirm-to-exceed (2026-09-29): set only when the client confirmed a max
    # above the global per-transaction cap. Lifts the caps for THIS entry up
    # to this amount. None = normal caps apply.
    cap_override_dollars: Optional[Decimal] = None
    note: Optional[str] = None
    is_armed: bool = True

    # Optional auction snapshot — used to bootstrap the auctions table if the
    # backend hasn't yet seen this listing via the daily sync. The dashboard
    # ships these from the static JSON since it's the source of truth pre-sync.
    tld: Optional[str] = None
    auction_type: Optional[str] = None  # "EXPIRY_AUCTION" or "CLOSEOUT"
    current_price: Optional[Decimal] = None
    estimated_value: Optional[Decimal] = None
    end_time_utc: Optional[datetime] = None
    bid_count: Optional[int] = 0
    has_bids: Optional[bool] = False
    score: Optional[int] = None


class WatchlistUpdate(BaseModel):
    closeout_price_ladder: Optional[list[Decimal]] = None
    max_bid_dollars: Optional[Decimal] = None
    # Confirm-to-exceed (2026-09-29). Send with a max_bid above the per-tx cap
    # after the client confirms the dashboard dialog. Send explicit null to clear.
    cap_override_dollars: Optional[Decimal] = None
    note: Optional[str] = None
    is_armed: Optional[bool] = None

    # Email reminders (24h / 12h / 3h / 30m before close). The prefs row is
    # created on first enable and kept when disabled, so the sent-flags
    # survive a bell toggle — flicking it off and back on must not re-send
    # reminders that already went out.
    notify_enabled: Optional[bool] = None
    # Per-entry recipient override; None leaves whatever is stored (the
    # notifier falls back to NOTIFY_EMAIL_TO).
    notify_email_to: Optional[str] = None

    # Re-arm an entry stranded in a terminal status (executed/error/lost/
    # expired/outbid) back to "pending" so the worker picks it up again.
    # 2026-09-07 dellport: a 504 mid-purchase left the entry tombstoned
    # with the domain still on sale. Explicit opt-in; audit-noted.
    reopen: bool = False


class WatchlistResponse(BaseModel):
    id: int
    auction_id: int
    listing_id: int
    domain: str
    closeout_price_ladder: Optional[list[Decimal]] = None
    max_bid_dollars: Optional[Decimal] = None
    cap_override_dollars: Optional[Decimal] = None
    is_armed: bool
    note: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    status: str

    # Reminder state. These come from the notification_prefs row rather than
    # the entry itself, so the API layer fills them in explicitly (see
    # app/api/watchlist.py:_to_response) — there's no ORM relationship to
    # lazy-load, which would blow up under async SQLAlchemy.
    notify_enabled: bool = False
    notify_email_to: Optional[str] = None

    @field_validator("closeout_price_ladder", mode="before")
    @classmethod
    def parse_ladder(cls, v):
        if isinstance(v, str):
            try:
                return [Decimal(str(x)) for x in json.loads(v)]
            except Exception:
                return None
        return v

    model_config = {"from_attributes": True}


# ============================================================================
# Settings
# ============================================================================


class SettingsResponse(BaseModel):
    per_transaction_cap_dollars: Decimal
    daily_spend_cap_dollars: Decimal
    closeout_only_mode: bool
    kill_switch_active: bool
    kill_switch_reason: str
    sanity_check_multiplier: Decimal
    updated_at: datetime

    model_config = {"from_attributes": True}


class SettingsUpdate(BaseModel):
    """All fields optional. Only provided fields are updated."""
    per_transaction_cap_dollars: Optional[Decimal] = None
    daily_spend_cap_dollars: Optional[Decimal] = None
    closeout_only_mode: Optional[bool] = None
    sanity_check_multiplier: Optional[Decimal] = None


# ============================================================================
# Kill switch
# ============================================================================


class KillSwitchToggle(BaseModel):
    active: bool
    reason: str = ""
