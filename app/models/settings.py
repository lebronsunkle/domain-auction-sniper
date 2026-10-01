"""System settings stored in the database. Read by the safety governors at
runtime so the client can adjust caps from the dashboard without code changes."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class SystemSettings(Base):
    __tablename__ = "system_settings"

    # Singleton row. We always read the row with id=1.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    # Hard cap per single bid/buy.
    per_transaction_cap_dollars: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("5000.00"), nullable=False
    )

    # Rolling 24h spend ceiling.
    daily_spend_cap_dollars: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("500.00"), nullable=False
    )

    # When True, blocks every BID action regardless of other settings.
    closeout_only_mode: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Master kill switch. When True, no purchases or bids fire.
    kill_switch_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Reason for last kill-switch activation, if any.
    kill_switch_reason: Mapped[str] = mapped_column(String(255), default="", nullable=False)

    # Sanity-check multiplier — if a target is > N times the floor, ask for
    # confirmation in the UI.
    sanity_check_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), default=Decimal("10.00"), nullable=False
    )

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
