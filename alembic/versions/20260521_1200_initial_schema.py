"""initial schema — auctions, watchlist, purchases, audit_log, system_settings

Revision ID: 001_initial
Revises:
Create Date: 2026-05-21 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- auctions -------------------------------------------------------
    op.create_table(
        "auctions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("listing_id", sa.BigInteger(), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("tld", sa.String(length=50), nullable=False),
        sa.Column("auction_type", sa.String(length=32), nullable=False),
        sa.Column("current_price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("estimated_value", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("end_time_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bid_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("has_bids", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("score_breakdown", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
    )
    op.create_index("ix_auctions_listing_id", "auctions", ["listing_id"])
    op.create_index("ix_auctions_listing_unique", "auctions", ["listing_id"], unique=True)
    op.create_index("ix_auctions_domain", "auctions", ["domain"])
    op.create_index("ix_auctions_tld", "auctions", ["tld"])
    op.create_index("ix_auctions_auction_type", "auctions", ["auction_type"])
    op.create_index("ix_auctions_end_time_utc", "auctions", ["end_time_utc"])
    op.create_index("ix_auctions_score", "auctions", ["score"])
    op.create_index("ix_auctions_score_endtime", "auctions", ["score", "end_time_utc"])

    # --- watchlist_entries ----------------------------------------------
    op.create_table(
        "watchlist_entries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("auction_id", sa.Integer(), sa.ForeignKey("auctions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("listing_id", sa.BigInteger(), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("closeout_price_ladder_json", sa.Text(), nullable=True),
        sa.Column("max_bid_dollars", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("is_armed", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
    )
    op.create_index("ix_watchlist_auction_id", "watchlist_entries", ["auction_id"])
    op.create_index("ix_watchlist_listing_id", "watchlist_entries", ["listing_id"])
    op.create_index("ix_watchlist_status_armed", "watchlist_entries", ["status", "is_armed"])

    # --- purchases ------------------------------------------------------
    op.create_table(
        "purchases",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("watchlist_entry_id", sa.Integer(), sa.ForeignKey("watchlist_entries.id"), nullable=True),
        sa.Column("listing_id", sa.BigInteger(), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("action_type", sa.String(length=32), nullable=False),
        sa.Column("amount_dollars", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("godaddy_bid_id", sa.String(length=64), nullable=True),
        sa.Column("godaddy_order_id", sa.String(length=64), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_response", sa.Text(), nullable=True),
    )
    op.create_index("ix_purchases_watchlist_entry_id", "purchases", ["watchlist_entry_id"])
    op.create_index("ix_purchases_listing_id", "purchases", ["listing_id"])
    op.create_index("ix_purchases_fired_at", "purchases", ["fired_at"])

    # --- audit_log ------------------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("url", sa.String(length=512), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("request_body", sa.Text(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("listing_id", sa.Integer(), nullable=True),
        sa.Column("watchlist_entry_id", sa.Integer(), nullable=True),
    )
    op.create_index("ix_audit_log_timestamp", "audit_log", ["timestamp"])
    op.create_index("ix_audit_log_listing_id", "audit_log", ["listing_id"])
    op.create_index("ix_audit_log_watchlist_entry_id", "audit_log", ["watchlist_entry_id"])

    # --- system_settings ------------------------------------------------
    op.create_table(
        "system_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "per_transaction_cap_dollars",
            sa.Numeric(precision=12, scale=2),
            nullable=False,
            server_default="25.00",
        ),
        sa.Column(
            "daily_spend_cap_dollars",
            sa.Numeric(precision=12, scale=2),
            nullable=False,
            server_default="50.00",
        ),
        sa.Column(
            "closeout_only_mode", sa.Boolean(), nullable=False, server_default=sa.true()
        ),  # v1 is closeouts only — default true
        sa.Column(
            "kill_switch_active", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "kill_switch_reason", sa.String(length=255), nullable=False, server_default=""
        ),
        sa.Column(
            "sanity_check_multiplier",
            sa.Numeric(precision=6, scale=2),
            nullable=False,
            server_default="10.00",
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("system_settings")
    op.drop_index("ix_audit_log_watchlist_entry_id", table_name="audit_log")
    op.drop_index("ix_audit_log_listing_id", table_name="audit_log")
    op.drop_index("ix_audit_log_timestamp", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_purchases_fired_at", table_name="purchases")
    op.drop_index("ix_purchases_listing_id", table_name="purchases")
    op.drop_index("ix_purchases_watchlist_entry_id", table_name="purchases")
    op.drop_table("purchases")
    op.drop_index("ix_watchlist_status_armed", table_name="watchlist_entries")
    op.drop_index("ix_watchlist_listing_id", table_name="watchlist_entries")
    op.drop_index("ix_watchlist_auction_id", table_name="watchlist_entries")
    op.drop_table("watchlist_entries")
    op.drop_index("ix_auctions_score_endtime", table_name="auctions")
    op.drop_index("ix_auctions_score", table_name="auctions")
    op.drop_index("ix_auctions_end_time_utc", table_name="auctions")
    op.drop_index("ix_auctions_auction_type", table_name="auctions")
    op.drop_index("ix_auctions_tld", table_name="auctions")
    op.drop_index("ix_auctions_domain", table_name="auctions")
    op.drop_index("ix_auctions_listing_unique", table_name="auctions")
    op.drop_index("ix_auctions_listing_id", table_name="auctions")
    op.drop_table("auctions")
