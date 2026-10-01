"""watchlist_entries.outbid_alert_sent_at — dedup for outbid alerts

Revision ID: 003_outbid_alert
Revises: 002_notification_prefs
Create Date: 2026-09-29 12:00:00.000000

The client (2026-09-29): email + text when he's been outbid on something he's
bidding on, and nothing else, so he isn't flooded. This nullable timestamp
is the sent-flag: stamped before the alert goes out (double-send safety),
cleared on re-arm so a fresh outbid on a re-entered auction alerts again.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "003_outbid_alert"
down_revision: Union[str, None] = "002_notification_prefs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "watchlist_entries",
        sa.Column("outbid_alert_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Confirm-to-exceed override (2026-09-29): the amount the client confirmed when
    # setting a max above the global per-transaction cap.
    op.add_column(
        "watchlist_entries",
        sa.Column("cap_override_dollars", sa.Numeric(12, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("watchlist_entries", "cap_override_dollars")
    op.drop_column("watchlist_entries", "outbid_alert_sent_at")
