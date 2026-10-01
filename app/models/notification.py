"""Email reminder preferences for a watchlist entry, plus per-threshold
sent-flags.

Why the sent-flags live in Postgres rather than in the scheduler's memory:
the Fly machine restarts on every deploy, and the notifier ticks every 60
seconds. An in-memory "already sent" set would forget everything on
restart and re-send every reminder whose window was still open — the same
3-hour warning three times over a deploy-heavy afternoon. One row per
watchlist entry, one nullable timestamp per threshold, and the timestamp is
committed BEFORE the email goes out (see worker/notifier.py) so a crash
mid-send can never produce a duplicate.

Timestamps rather than booleans: same double-send guarantee, but they also
answer "when did he actually get told?" when the client asks why he missed one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


@dataclass(frozen=True)
class ReminderThreshold:
    """One rung of the reminder ladder."""

    minutes: int
    column: str  # attribute on NotificationPref holding the sent timestamp
    label: str  # human phrasing used in the subject line


# Ordered LONGEST-first, which is the order they fire in as an auction runs
# down. Several places rely on this ordering — notably the catch-up
# suppression in worker/notifier.py, which walks the ladder to find the most
# urgent rung that's due.
REMINDER_THRESHOLDS: tuple[ReminderThreshold, ...] = (
    ReminderThreshold(minutes=24 * 60, column="sent_24h_at", label="24 hours"),
    ReminderThreshold(minutes=12 * 60, column="sent_12h_at", label="12 hours"),
    ReminderThreshold(minutes=3 * 60, column="sent_3h_at", label="3 hours"),
    ReminderThreshold(minutes=30, column="sent_30m_at", label="30 minutes"),
)


class NotificationPref(Base):
    """One row per watchlist entry that has ever had notifications toggled.

    The row is created lazily the first time the bell is switched on and is
    kept (with enabled=False) when it's switched off, so the sent-flags
    survive a toggle — flicking the bell off and on again shouldn't re-send
    the reminders the client already got.
    """

    __tablename__ = "notification_prefs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 1:1 with the watchlist entry. CASCADE so un-watching a domain takes
    # its reminder state with it — a removed entry must never send.
    watchlist_entry_id: Mapped[int] = mapped_column(
        ForeignKey("watchlist_entries.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )

    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Per-entry recipient override. Normally NULL — the notifier falls back
    # to NOTIFY_EMAIL_TO from the environment.
    email_to: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)

    # --- sent-flags, one per rung of REMINDER_THRESHOLDS -------------------
    # NULL = not sent yet (armed). Non-NULL = when we committed the claim.
    sent_24h_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_12h_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_3h_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_30m_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # The auction end time the sent-flags above were computed against. When
    # the auction's real end time moves past this (GoDaddy resets the clock
    # ~5-6 min on any late bid, and the daily refresh picks up longer
    # extensions), the notifier re-arms every rung that's now in the future
    # again. See worker/notifier.py:rearm_for_extension.
    armed_end_time_utc: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Last time we observed the end time move LATER. Diagnostics only.
    last_extended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # ----------------------------------------------------------------------

    def sent_at(self, threshold: ReminderThreshold) -> Optional[datetime]:
        return getattr(self, threshold.column)

    def mark_sent(self, threshold: ReminderThreshold, when: datetime) -> None:
        setattr(self, threshold.column, when)

    def clear_sent(self, threshold: ReminderThreshold) -> None:
        setattr(self, threshold.column, None)
