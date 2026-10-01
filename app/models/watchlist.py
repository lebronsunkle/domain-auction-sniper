"""Watchlist entry. The client's per-auction targeting instructions.

This is the authoritative watchlist (we don't sync with GoDaddy's UI-side
watchlist in v1 — see plan section 8)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class WatchlistEntry(Base):
    __tablename__ = "watchlist_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    auction_id: Mapped[int] = mapped_column(
        ForeignKey("auctions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    listing_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)

    # For closeouts: a price ladder (JSON list of target prices, e.g. [50, 25, 11]).
    # The trigger worker buys at the first price the ladder hits.
    closeout_price_ladder_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # For expiry auctions: max bid amount in dollars (we never exceed this).
    max_bid_dollars: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)

    # Confirm-to-exceed (2026-09-29, the client): when he sets a max above the
    # global per-transaction cap and confirms in the dashboard, the confirmed
    # amount is stored here. The governor honors it for THIS entry (lifting
    # per-tx, daily, and sanity up to this figure) so a deliberate big bid
    # fires instead of being silently rejected. NULL = normal caps apply.
    cap_override_dollars: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)

    # Whether this entry is currently "armed" (the worker will act on it).
    # User can pause individual entries without removing them.
    is_armed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Free-form note from the client (e.g., "Park City cluster, only if under $5k").
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Resolved state:
    #   "pending"     — armed, waiting for trigger condition
    #   "executed"    — buy/bid was fired
    #   "won"         — confirmed purchased
    #   "lost"        — auction ended, we didn't get it
    #   "expired"     — auction ended without us firing
    #   "cancelled"   — the client removed
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)

    # Outbid-alert dedup (2026-09-29, the client): stamped when we email/text him
    # that he's been outbid on this entry, so the notifier sends exactly ONE
    # alert per outbid event (he explicitly does NOT want to be flooded).
    # Cleared whenever the entry is re-armed (raise max / reopen), so a fresh
    # outbid on a re-entered auction alerts again.
    outbid_alert_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (Index("ix_watchlist_status_armed", "status", "is_armed"),)
