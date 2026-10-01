"""Audit log: every GoDaddy API call we make."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AuditLogEntry(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    request_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    response_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Optional cross-references when the call was tied to a specific listing/watchlist entry.
    listing_id: Mapped[Optional[int]] = mapped_column(Integer, index=True, nullable=True)
    watchlist_entry_id: Mapped[Optional[int]] = mapped_column(
        Integer, index=True, nullable=True
    )
