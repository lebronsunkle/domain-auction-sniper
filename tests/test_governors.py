"""
Tests for the safety governors module.

This is the most safety-critical code in the system. Each test pins a
specific gate to ensure it rejects the right inputs. If these tests start
failing, do not deploy.

Uses an in-memory SQLite database for speed and isolation. Each test gets
a fresh DB with no carryover.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.base import Base
from app.models.purchase import Purchase
from app.models.settings import SystemSettings
from app.safety.governors import (
    GovernorContext,
    GovernorRejection,
    check_all,
)


# ---------------------------------------------------------------------------
# Per-test DB setup
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    """Fresh in-memory SQLite for each test. All tables created, no rows yet."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _seed_settings(
    session: AsyncSession,
    *,
    per_tx_cap: Decimal = Decimal("25.00"),
    daily_cap: Decimal = Decimal("50.00"),
    closeout_only: bool = True,
    kill_switch: bool = False,
    sanity_mult: Decimal = Decimal("10.00"),
) -> SystemSettings:
    settings = SystemSettings(
        id=1,
        per_transaction_cap_dollars=per_tx_cap,
        daily_spend_cap_dollars=daily_cap,
        closeout_only_mode=closeout_only,
        kill_switch_active=kill_switch,
        kill_switch_reason="",
        sanity_check_multiplier=sanity_mult,
        updated_at=datetime.now(timezone.utc),
    )
    session.add(settings)
    await session.commit()
    return settings


def _ctx(
    *,
    action_type: str = "CLOSEOUT_BUY",
    total: Decimal = Decimal("10.00"),
    listing_id: int = 12345,
    domain: str = "test.com",
    reference_floor: Decimal | None = None,
) -> GovernorContext:
    return GovernorContext(
        action_type=action_type,
        total_cost_dollars=total,
        listing_id=listing_id,
        domain=domain,
        reference_floor_dollars=reference_floor,
    )


# ---------------------------------------------------------------------------
# Happy path: all gates pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_under_all_caps(session: AsyncSession):
    await _seed_settings(session)
    # $10 closeout, well under both per-tx ($25) and daily ($50) caps.
    await check_all(session, _ctx(total=Decimal("10.00")))


# ---------------------------------------------------------------------------
# Missing settings row -> closed-fail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_settings_row_rejects(session: AsyncSession):
    # No settings row at all. The governor must refuse to act rather than
    # default to permissive behavior.
    with pytest.raises(GovernorRejection, match="SystemSettings row not found"):
        await check_all(session, _ctx())


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_switch_blocks_everything(session: AsyncSession):
    await _seed_settings(session, kill_switch=True)
    with pytest.raises(GovernorRejection, match="Kill switch is active"):
        await check_all(session, _ctx())


@pytest.mark.asyncio
async def test_kill_switch_reason_included_in_rejection(session: AsyncSession):
    settings = await _seed_settings(session, kill_switch=True)
    settings.kill_switch_reason = "manual halt during anomaly"
    await session.commit()
    with pytest.raises(GovernorRejection, match="manual halt during anomaly"):
        await check_all(session, _ctx())


# ---------------------------------------------------------------------------
# Closeout-only mode (v1 default = on)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_closeout_only_blocks_bids(session: AsyncSession):
    await _seed_settings(session, closeout_only=True)
    with pytest.raises(GovernorRejection, match="Closeout-only mode is active"):
        await check_all(session, _ctx(action_type="BID"))


@pytest.mark.asyncio
async def test_closeout_only_allows_closeouts(session: AsyncSession):
    await _seed_settings(session, closeout_only=True)
    # Should pass; same gate is the one we just tested rejecting bids.
    await check_all(session, _ctx(action_type="CLOSEOUT_BUY"))


@pytest.mark.asyncio
async def test_closeout_only_disabled_allows_bids(session: AsyncSession):
    await _seed_settings(session, closeout_only=False)
    await check_all(session, _ctx(action_type="BID"))


# ---------------------------------------------------------------------------
# Per-transaction cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_tx_cap_under_limit(session: AsyncSession):
    await _seed_settings(session, per_tx_cap=Decimal("25.00"))
    await check_all(session, _ctx(total=Decimal("24.99")))


@pytest.mark.asyncio
async def test_per_tx_cap_at_limit_allowed(session: AsyncSession):
    """At the cap exactly is allowed; just over rejects."""
    await _seed_settings(session, per_tx_cap=Decimal("25.00"))
    await check_all(session, _ctx(total=Decimal("25.00")))


@pytest.mark.asyncio
async def test_per_tx_cap_exceeded(session: AsyncSession):
    await _seed_settings(session, per_tx_cap=Decimal("25.00"))
    with pytest.raises(GovernorRejection, match="Per-transaction cap exceeded"):
        await check_all(session, _ctx(total=Decimal("25.01")))


# ---------------------------------------------------------------------------
# Daily spend cap
# ---------------------------------------------------------------------------


async def _add_purchase(
    session: AsyncSession,
    *,
    amount: Decimal,
    when: datetime,
    outcome: str = "won",
) -> None:
    p = Purchase(
        listing_id=999,
        domain="prior.com",
        action_type="CLOSEOUT_BUY",
        amount_dollars=amount,
        outcome=outcome,
        fired_at=when,
    )
    session.add(p)
    await session.commit()


@pytest.mark.asyncio
async def test_daily_cap_with_no_history(session: AsyncSession):
    # per_tx_cap raised above the amounts in play so this test exercises
    # ONLY the daily-cap gate (the fixture default of $25 would fire first).
    await _seed_settings(
        session, daily_cap=Decimal("50.00"), per_tx_cap=Decimal("100.00")
    )
    # No prior purchases — should pass.
    await check_all(session, _ctx(total=Decimal("40.00")))


@pytest.mark.asyncio
async def test_daily_cap_sums_prior_purchases(session: AsyncSession):
    await _seed_settings(session, daily_cap=Decimal("50.00"))
    # $30 already spent today. $25 more would push to $55 — must reject.
    await _add_purchase(
        session,
        amount=Decimal("30.00"),
        when=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    with pytest.raises(GovernorRejection, match="Daily spend cap would be exceeded"):
        await check_all(session, _ctx(total=Decimal("25.00")))


@pytest.mark.asyncio
async def test_daily_cap_ignores_old_purchases(session: AsyncSession):
    await _seed_settings(
        session, daily_cap=Decimal("50.00"), per_tx_cap=Decimal("100.00")
    )
    # $30 spent 25 hours ago — outside the rolling 24h window, doesn't count.
    await _add_purchase(
        session,
        amount=Decimal("30.00"),
        when=datetime.now(timezone.utc) - timedelta(hours=25),
    )
    await check_all(session, _ctx(total=Decimal("40.00")))


@pytest.mark.asyncio
async def test_daily_cap_ignores_outbid_attempts(session: AsyncSession):
    """If a prior bid was outbid (we didn't actually win), it shouldn't count
    against the daily spend cap."""
    await _seed_settings(
        session, daily_cap=Decimal("50.00"), per_tx_cap=Decimal("100.00")
    )
    await _add_purchase(
        session,
        amount=Decimal("100.00"),
        when=datetime.now(timezone.utc) - timedelta(minutes=10),
        outcome="outbid",
    )
    await check_all(session, _ctx(total=Decimal("40.00")))


# ---------------------------------------------------------------------------
# Sanity multiplier (the "$26,000 instead of $2,600" typo guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sanity_check_skipped_without_floor(session: AsyncSession):
    """No reference floor provided -> sanity check doesn't fire."""
    await _seed_settings(session, sanity_mult=Decimal("10.00"))
    # No reference_floor in context — should pass even though amount is high.
    await check_all(session, _ctx(total=Decimal("20.00"), reference_floor=None))


@pytest.mark.asyncio
async def test_sanity_check_passes_within_multiplier(session: AsyncSession):
    await _seed_settings(session, sanity_mult=Decimal("10.00"), per_tx_cap=Decimal("100.00"))
    # Target $50 vs floor $10 = 5x, under the 10x ceiling.
    await check_all(
        session,
        _ctx(total=Decimal("50.00"), reference_floor=Decimal("10.00")),
    )


@pytest.mark.asyncio
async def test_sanity_check_blocks_extreme_ratio(session: AsyncSession):
    await _seed_settings(session, sanity_mult=Decimal("10.00"), per_tx_cap=Decimal("100000.00"))
    # The classic $2,600-typed-as-$26,000 scenario.
    with pytest.raises(GovernorRejection, match="Sanity check failed"):
        await check_all(
            session,
            _ctx(total=Decimal("26000.00"), reference_floor=Decimal("2600.00")),
        )


# ---------------------------------------------------------------------------
# Daily cap considers total cost including renewal/fees, not list price
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_cap_uses_total_cost(session: AsyncSession):
    """Closeouts have renewal + ICANN + tax added on top of listing price.
    The governor cap operates on `total_cost_dollars`, which the caller is
    responsible for computing as (list + renewal + ICANN + tax)."""
    await _seed_settings(session, daily_cap=Decimal("50.00"))
    # 3 closeouts of $20 total each = $60, over the cap.
    for i in range(2):
        await _add_purchase(
            session,
            amount=Decimal("20.00"),
            when=datetime.now(timezone.utc) - timedelta(minutes=10 * (i + 1)),
        )
    # Third one at $20 would push to $60. Must reject.
    with pytest.raises(GovernorRejection):
        await check_all(session, _ctx(total=Decimal("20.00")))


def test_sanity_skips_small_stakes():
    """2026-08-26 civiar.com: $11 vs $1 floor is 11x but far below any typo
    catastrophe — small stakes skip the ratio test entirely."""
    from decimal import Decimal
    from app.safety.governors import GovernorContext, _check_sanity_multiplier

    class S:
        sanity_check_multiplier = Decimal("10.00")

    ctx = GovernorContext(
        action_type="BID",
        total_cost_dollars=Decimal("11.00"),
        listing_id=1,
        domain="civiar.com",
        reference_floor_dollars=Decimal("1.00"),
    )
    _check_sanity_multiplier(S(), ctx)  # must NOT raise


def test_sanity_still_catches_the_big_typo():
    from decimal import Decimal
    import pytest
    from app.safety.governors import GovernorContext, GovernorRejection, _check_sanity_multiplier

    class S:
        sanity_check_multiplier = Decimal("10.00")

    ctx = GovernorContext(
        action_type="BID",
        total_cost_dollars=Decimal("26000.00"),
        listing_id=1,
        domain="typo.com",
        reference_floor_dollars=Decimal("2600.00"),
    )
    with pytest.raises(GovernorRejection):
        _check_sanity_multiplier(S(), ctx)
