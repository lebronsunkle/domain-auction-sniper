"""Auction listing model. One row per (listing_id, sync_date) — kept versioned
so we can see how state changed across daily feed pulls."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Auction(Base):
    __tablename__ = "auctions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # GoDaddy's unique listing identifier (also called auctionId in some feeds).
    listing_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)

    domain: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    tld: Mapped[str] = mapped_column(String(50), index=True, nullable=False)

    # "EXPIRY_AUCTION" or "CLOSEOUT".
    auction_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)

    # Pricing and timing — current state at last feed sync.
    current_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    estimated_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    end_time_utc: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )

    bid_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    has_bids: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Computed by the scoring engine at sync time.
    score: Mapped[Optional[int]] = mapped_column(Integer, index=True, nullable=True)
    score_breakdown: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON

    # When this row was last refreshed from the feed.
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # When the auction was first observed (across all syncs).
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Lifecycle state in our system, not GoDaddy's:
    #   "active" — visible in feed
    #   "ended"  — past end time
    #   "bought" — we bought it (cross-ref via purchases table)
    #   "lost"   — auction ended without us winning
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)

    __table_args__ = (
        Index("ix_auctions_score_endtime", "score", "end_time_utc"),
        Index("ix_auctions_listing_unique", "listing_id", unique=True),
    )
