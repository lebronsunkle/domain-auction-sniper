"""GET /api/purchases — every money action the system has taken.

2026-08-24 (Adam): "once we purchase something, we should see that we
purchased it in the sniper." Before this, purchase outcomes lived only in
the DB and ephemeral logs — nobody could tell whether the sorrybutno.com
auto-buy fired without grepping Fly. Now the dashboard shows the full
ledger: wins, losses, errors, dry-runs, and in-flight attempts.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.purchase import Purchase

router = APIRouter()


class PurchaseOut(BaseModel):
    id: int
    domain: str
    listing_id: int
    action_type: str                  # BID | CLOSEOUT_BUY
    amount_dollars: Decimal
    outcome: str                      # won | lost | outbid | error | dry_run | in_flight
    fired_at: datetime
    confirmed_at: Optional[datetime] = None
    godaddy_order_id: Optional[str] = None
    godaddy_bid_id: Optional[str] = None
    detail: Optional[str] = None      # trimmed raw_response for humans

    model_config = {"from_attributes": True}


@router.get("", response_model=list[PurchaseOut])
async def list_purchases(
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[PurchaseOut]:
    result = await db.execute(
        select(Purchase).order_by(Purchase.fired_at.desc()).limit(limit)
    )
    out = []
    for p in result.scalars().all():
        row = PurchaseOut.model_validate(p)
        row.detail = (p.raw_response or "")[:300] or None
        out.append(row)
    return out
