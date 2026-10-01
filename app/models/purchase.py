"""Purchase log. One row per successful buy or bid."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Purchase(Base):
    __tablename__ = "purchases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    watchlist_entry_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("watchlist_entries.id"), index=True, nullable=True
    )
    listing_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)

    # "BID" or "CLOSEOUT_BUY"
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)

    # Amount in dollars (what we actually committed). For closeouts this is the
    # TOTAL cost (listing + renewal + ICANN + tax), not just the listing price.
    amount_dollars: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    # GoDaddy's identifier for the action.
    godaddy_bid_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    godaddy_order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Was this a successful buy/winning bid, or just a placed bid that didn't win?
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)  # "won", "outbid", "pending"

    fired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    raw_response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
