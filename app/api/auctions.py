"""GET /api/auctions — scored, sorted, filterable auction listings."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import AuctionListResponse, AuctionResponse
from app.db import get_db
from app.models.auction import Auction

router = APIRouter()


# Time horizon windows as documented in the plan.
TimeHorizon = Literal["imminent", "soon", "planning", "all"]


@router.get("", response_model=AuctionListResponse)
async def list_auctions(
    horizon: TimeHorizon = Query(
        "all",
        description="imminent=0-24h, soon=24-48h, planning=48h-10d, all=no filter",
    ),
    min_score: Optional[int] = Query(None, ge=0, le=150),
    max_score: Optional[int] = Query(None, ge=0, le=150),
    tld: Optional[str] = Query(None, description="Filter to a specific TLD, e.g. com"),
    has_bids: Optional[bool] = Query(None, description="Filter by bid presence"),
    auction_type: Optional[str] = Query(None, description="EXPIRY_AUCTION or CLOSEOUT"),
    status: Optional[str] = Query("active", description="Filter by listing status"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> AuctionListResponse:
    """List scored auctions sorted by score descending. Supports time-horizon
    bucketing aligned with the client's "imminent/soon/planning" mental model."""

    query = select(Auction)

    now = datetime.now(timezone.utc)
    if horizon == "imminent":
        query = query.where(
            Auction.end_time_utc.is_not(None),
            Auction.end_time_utc >= now,
            Auction.end_time_utc < now + timedelta(hours=24),
        )
    elif horizon == "soon":
        query = query.where(
            Auction.end_time_utc.is_not(None),
            Auction.end_time_utc >= now + timedelta(hours=24),
            Auction.end_time_utc < now + timedelta(hours=48),
        )
    elif horizon == "planning":
        query = query.where(
            Auction.end_time_utc.is_not(None),
            Auction.end_time_utc >= now + timedelta(hours=48),
            Auction.end_time_utc < now + timedelta(days=10),
        )

    if min_score is not None:
        query = query.where(Auction.score >= min_score)
    if max_score is not None:
        query = query.where(Auction.score <= max_score)
    if tld:
        query = query.where(Auction.tld == tld.lower().lstrip("."))
    if has_bids is not None:
        query = query.where(Auction.has_bids == has_bids)
    if auction_type:
        query = query.where(Auction.auction_type == auction_type)
    if status:
        query = query.where(Auction.status == status)

    # Get total count before pagination.
    from sqlalchemy import func
    count_result = await db.execute(
        select(func.count()).select_from(query.subquery())
    )
    total = count_result.scalar() or 0

    # Paginated query: score desc, then end_time asc as a stable tie-breaker.
    query = query.order_by(Auction.score.desc(), Auction.end_time_utc.asc()).limit(limit).offset(offset)
    result = await db.execute(query)
    auctions = result.scalars().all()

    return AuctionListResponse(
        total=total,
        items=[AuctionResponse.model_validate(a) for a in auctions],
    )


@router.get("/{auction_id}", response_model=AuctionResponse)
async def get_auction(
    auction_id: int,
    db: AsyncSession = Depends(get_db),
) -> AuctionResponse:
    result = await db.execute(select(Auction).where(Auction.id == auction_id))
    auction = result.scalar_one_or_none()
    if auction is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Auction {auction_id} not found")
    return AuctionResponse.model_validate(auction)
