"""
Safety governors. Enforced server-side, in the worker, BEFORE any API call
to GoDaddy goes out. The UI is not the gate.

Every governor below either passes silently or raises GovernorRejection.
The trigger worker wraps every buy/bid attempt in a `check_all()` call and
treats any rejection as terminal — no API call fires.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.purchase import Purchase
from app.models.settings import SystemSettings

logger = logging.getLogger(__name__)


class GovernorRejection(Exception):
    """Raised when a safety check fails. Message is suitable for the audit log."""


@dataclass
class GovernorContext:
    """Inputs to a governor check, gathered once per attempted action."""

    action_type: str  # "BID" or "CLOSEOUT_BUY"
    total_cost_dollars: Decimal  # For closeouts: listing+renewal+ICANN+tax. For bids: bid amount.
    listing_id: int
    domain: str
    # Optional context for the sanity-check governor.
    reference_floor_dollars: Optional[Decimal] = None
    # Confirm-to-exceed (2026-09-29, the client): when the client deliberately sets a
    # max above the per-transaction cap and confirms the exact dollar amount
    # in the dashboard, that amount is stored here. The human confirmation IS
    # the safety check for this one action, so it lifts the per-transaction
    # cap, the daily cap, AND the sanity multiplier — but ONLY up to the
    # confirmed figure, and ONLY for this transaction. Kill switch and
    # closeout-only mode are NOT lifted (those are separate global controls).
    operator_override_dollars: Optional[Decimal] = None


async def check_all(session: AsyncSession, ctx: GovernorContext) -> None:
    """Run every governor. Raises GovernorRejection on any failure.

    Pattern: load the current SystemSettings row once, then evaluate each rule.
    Each rule is small and explicit — easier to audit than a single big function.
    """
    settings = await _load_settings(session)

    _check_kill_switch(settings)
    _check_closeout_only_mode(settings, ctx)
    # Sanity multiplier runs BEFORE the caps: when someone fat-fingers
    # $26,000 for $2,600, "Sanity check failed: 10.0x the reference floor"
    # is the diagnostic message we want surfaced — not a generic cap
    # rejection that hides the typo. All gates still run; order only
    # affects which rejection message wins.
    _check_sanity_multiplier(settings, ctx)
    _check_per_transaction_cap(settings, ctx)
    await _check_daily_spend_cap(session, settings, ctx)


async def _load_settings(session: AsyncSession) -> SystemSettings:
    result = await session.execute(select(SystemSettings).where(SystemSettings.id == 1))
    settings = result.scalar_one_or_none()
    if settings is None:
        # If no settings row exists, that's a misconfiguration — fail closed.
        raise GovernorRejection(
            "SystemSettings row not found (id=1). The system is unconfigured and "
            "will not act until settings are initialized."
        )
    return settings


def _check_kill_switch(settings: SystemSettings) -> None:
    if settings.kill_switch_active:
        raise GovernorRejection(
            f"Kill switch is active. Reason: {settings.kill_switch_reason or '(none)'}"
        )


def _check_closeout_only_mode(settings: SystemSettings, ctx: GovernorContext) -> None:
    if settings.closeout_only_mode and ctx.action_type == "BID":
        raise GovernorRejection(
            "Closeout-only mode is active. BID actions are blocked globally."
        )


def _override_covers(ctx: GovernorContext) -> bool:
    """True when the client confirmed an over-cap amount that covers this action."""
    return (
        ctx.operator_override_dollars is not None
        and ctx.total_cost_dollars <= ctx.operator_override_dollars
    )


def _check_per_transaction_cap(settings: SystemSettings, ctx: GovernorContext) -> None:
    if ctx.total_cost_dollars > settings.per_transaction_cap_dollars:
        if _override_covers(ctx):
            logger.info(
                "Per-transaction cap ($%.2f) exceeded by $%.2f on %s, but "
                "operator confirmed an override up to $%.2f — allowing.",
                settings.per_transaction_cap_dollars, ctx.total_cost_dollars,
                ctx.domain, ctx.operator_override_dollars,
            )
            return
        raise GovernorRejection(
            f"Per-transaction cap exceeded: ${ctx.total_cost_dollars:,.2f} > "
            f"${settings.per_transaction_cap_dollars:,.2f}"
        )


async def _check_daily_spend_cap(
    session: AsyncSession, settings: SystemSettings, ctx: GovernorContext
) -> None:
    """Sum the total cost of purchases in the last 24 hours; reject if adding
    this action would push over the daily cap."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    result = await session.execute(
        select(Purchase).where(Purchase.fired_at >= cutoff, Purchase.outcome != "outbid")
    )
    purchases = result.scalars().all()
    spent = sum((p.amount_dollars for p in purchases), Decimal("0.00"))
    projected = spent + ctx.total_cost_dollars
    if projected > settings.daily_spend_cap_dollars:
        if _override_covers(ctx):
            logger.info(
                "Daily spend cap ($%.2f) would be exceeded (projected $%.2f) on "
                "%s, but operator confirmed an override up to $%.2f — allowing.",
                settings.daily_spend_cap_dollars, projected, ctx.domain,
                ctx.operator_override_dollars,
            )
            return
        raise GovernorRejection(
            f"Daily spend cap would be exceeded: spent ${spent:,.2f} + this "
            f"${ctx.total_cost_dollars:,.2f} = ${projected:,.2f} > "
            f"${settings.daily_spend_cap_dollars:,.2f}"
        )


SANITY_CHECK_MIN_DOLLARS = Decimal("150.00")


def _check_sanity_multiplier(settings: SystemSettings, ctx: GovernorContext) -> None:
    """Catches the '$26,000 instead of $2,600' typo: if our target is wildly above
    the reference floor (e.g., the auction's current price), reject.

    2026-08-26 (civiar.com, audit item R10 finally closed): an $11 bid on a
    $1 auction is 11x — the client's completely NORMAL cheap-auction play — and
    the ratio check would have disarmed it at the bell. Ratios only mean
    something at scale: below $150 total there is no typo catastrophe to
    prevent, so small stakes skip the ratio test. Both halves of the
    motivating typo ($2,600 and $26,000) remain far above this floor.
    (The 'sanity_check_bypass' the old message promised never existed —
    the message now tells the truth.)"""
    if ctx.total_cost_dollars <= SANITY_CHECK_MIN_DOLLARS:
        return  # small stakes: ratio math is noise, not protection
    if _override_covers(ctx):
        # The client deliberately confirmed this exact over-cap amount; the human
        # eyeball replaces the ratio guard for this one action.
        return
    if ctx.reference_floor_dollars is None or ctx.reference_floor_dollars <= 0:
        return  # no floor to compare against; skip this check
    ratio = ctx.total_cost_dollars / ctx.reference_floor_dollars
    # >= not >: the motivating typo ($2,600 -> $26,000) is EXACTLY 10.0x.
    # With strict >, the guard's own canonical example sailed through.
    if ratio >= settings.sanity_check_multiplier:
        raise GovernorRejection(
            f"Sanity check failed: target ${ctx.total_cost_dollars:,.2f} is "
            f"{ratio:.1f}x the reference floor ${ctx.reference_floor_dollars:,.2f}. "
            f"This pattern produces the '$26,000 instead of $2,600' typo. "
            f"If this amount is intentional, raise the sanity multiplier in "
            f"settings, or wait for the current price to rise."
        )
