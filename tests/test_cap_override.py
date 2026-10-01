"""Confirm-to-exceed cap override (2026-09-29, the client). A confirmed over-cap
amount lifts the per-transaction cap, daily cap, and sanity check for THAT
transaction only — kill switch and closeout-only stay absolute, and an
UNconfirmed over-cap bid is still rejected."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.base import Base
from app.models.settings import SystemSettings
from app.safety.governors import GovernorContext, GovernorRejection, check_all


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(SystemSettings(
            id=1,
            per_transaction_cap_dollars=Decimal("5000.00"),
            daily_spend_cap_dollars=Decimal("7500.00"),
            closeout_only_mode=False,
            kill_switch_active=False,
            kill_switch_reason="",
            sanity_check_multiplier=Decimal("10.00"),
            updated_at=datetime.now(timezone.utc),
        ))
        await s.commit()
        yield s
    await engine.dispose()


def _ctx(total, override=None, floor=Decimal("400.00")):
    return GovernorContext(
        action_type="BID", total_cost_dollars=Decimal(total), listing_id=1,
        domain="big.com", reference_floor_dollars=floor,
        operator_override_dollars=(Decimal(override) if override is not None else None),
    )


@pytest.mark.asyncio
async def test_over_cap_without_override_rejected(session):
    with pytest.raises(GovernorRejection):
        await check_all(session, _ctx("8000"))


@pytest.mark.asyncio
async def test_over_cap_with_matching_override_allowed(session):
    # Confirmed $8,000 override covers an $8,000 bid — per-tx, daily, sanity
    # all lifted. Should not raise.
    await check_all(session, _ctx("8000", override="8000"))


@pytest.mark.asyncio
async def test_override_below_amount_does_not_cover(session):
    # He confirmed $6,000 but the bid is $8,000 — override does NOT cover it.
    with pytest.raises(GovernorRejection):
        await check_all(session, _ctx("8000", override="6000"))


@pytest.mark.asyncio
async def test_override_still_blocked_by_kill_switch(session):
    settings = (await session.get(SystemSettings, 1))
    settings.kill_switch_active = True
    settings.kill_switch_reason = "maintenance"
    await session.commit()
    with pytest.raises(GovernorRejection, match="Kill switch"):
        await check_all(session, _ctx("8000", override="8000"))


@pytest.mark.asyncio
async def test_under_cap_unaffected(session):
    # A normal bid under the cap needs no override and passes.
    await check_all(session, _ctx("300", floor=Decimal("100.00")))
