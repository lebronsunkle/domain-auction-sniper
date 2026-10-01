"""notification_prefs — per-watchlist-entry email reminders with sent-flags

Revision ID: 002_notification_prefs
Revises: 001_initial
Create Date: 2026-08-05 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "002_notification_prefs"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "notification_prefs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("watchlist_entry_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("email_to", sa.String(length=320), nullable=True),
        # One sent-flag per rung of the reminder ladder. NULL = armed.
        sa.Column("sent_24h_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_12h_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_3h_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_30m_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("armed_end_time_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_extended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["watchlist_entry_id"],
            ["watchlist_entries.id"],
            ondelete="CASCADE",
        ),
    )
    # Unique, not just indexed: one prefs row per watchlist entry. The
    # constraint is the thing that stops a race between two concurrent
    # bell-toggles creating two rows (and therefore two emails per rung).
    op.create_index(
        "ix_notification_prefs_entry",
        "notification_prefs",
        ["watchlist_entry_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_notification_prefs_entry", table_name="notification_prefs")
    op.drop_table("notification_prefs")
