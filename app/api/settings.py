"""GET/PUT /api/settings + POST /api/kill_switch — runtime safety controls."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import KillSwitchToggle, SettingsResponse, SettingsUpdate
from app.db import get_db
from app.models.settings import SystemSettings

router = APIRouter()


async def _load_settings(db: AsyncSession) -> SystemSettings:
    result = await db.execute(select(SystemSettings).where(SystemSettings.id == 1))
    settings = result.scalar_one_or_none()
    if settings is None:
        raise HTTPException(
            status_code=500,
            detail=(
                "system_settings row not found. Run `python scripts/init_db.py` "
                "to seed defaults."
            ),
        )
    return settings


@router.get("/settings", response_model=SettingsResponse)
async def get_settings_endpoint(db: AsyncSession = Depends(get_db)) -> SettingsResponse:
    settings = await _load_settings(db)
    return SettingsResponse.model_validate(settings)


@router.put("/settings", response_model=SettingsResponse)
async def update_settings(
    payload: SettingsUpdate,
    db: AsyncSession = Depends(get_db),
) -> SettingsResponse:
    """Update one or more safety governor parameters.

    Note: `kill_switch_active` is intentionally NOT settable here; toggle it
    via POST /api/kill_switch so the action gets its own audit trail and the
    "reason" field is required.
    """
    settings = await _load_settings(db)

    if payload.per_transaction_cap_dollars is not None:
        if payload.per_transaction_cap_dollars <= 0:
            raise HTTPException(status_code=400, detail="per_transaction_cap_dollars must be > 0")
        settings.per_transaction_cap_dollars = payload.per_transaction_cap_dollars

    if payload.daily_spend_cap_dollars is not None:
        if payload.daily_spend_cap_dollars <= 0:
            raise HTTPException(status_code=400, detail="daily_spend_cap_dollars must be > 0")
        settings.daily_spend_cap_dollars = payload.daily_spend_cap_dollars

    if payload.closeout_only_mode is not None:
        settings.closeout_only_mode = payload.closeout_only_mode

    if payload.sanity_check_multiplier is not None:
        if payload.sanity_check_multiplier <= 1:
            raise HTTPException(
                status_code=400,
                detail="sanity_check_multiplier must be > 1 (or it would never trigger)",
            )
        settings.sanity_check_multiplier = payload.sanity_check_multiplier

    settings.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(settings)
    return SettingsResponse.model_validate(settings)


@router.post("/kill_switch", response_model=SettingsResponse)
async def toggle_kill_switch(
    payload: KillSwitchToggle,
    db: AsyncSession = Depends(get_db),
) -> SettingsResponse:
    """Flip the kill switch on or off. When on, every purchase attempt is
    rejected by the safety governor before the API call goes out."""
    settings = await _load_settings(db)
    settings.kill_switch_active = payload.active
    settings.kill_switch_reason = payload.reason
    settings.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(settings)
    return SettingsResponse.model_validate(settings)
