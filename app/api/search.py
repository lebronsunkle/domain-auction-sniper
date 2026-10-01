"""GET /api/search — substring search across the FULL GoDaddy inventory.

The dashboard's client-side search only sees the day's top-5,000 scored
listings. This endpoint searches all ~935k listings in the local inventory
index (see app/godaddy/inventory_index.py) — the client's "why can't I find
poly?" fix from the 2026-07-11 demo call.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db import get_db
from app.godaddy.collision import is_already_bidding
from pydantic import BaseModel

from app.godaddy.inventory_index import (
    estibot_values_for,
    get_or_fetch_estibot,
    index_built_at,
    search_domains,
    top_by_valuation,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class SearchResult(BaseModel):
    domain: str
    indexed_pages: Optional[int] = None      # pages indexed (GD List column)
    listing_id: int
    auction_type: str
    price: Optional[str] = None
    valuation: Optional[str] = None          # GoDaddy's estimate
    estibot_value: Optional[str] = None      # Estibot appraisal (enriched)
    end_time_utc: Optional[str] = None
    bid_count: Optional[int] = None


class SearchResponse(BaseModel):
    query: str
    index_built_at: Optional[str] = None  # None => index not built yet
    results: list[SearchResult]


@router.get("", response_model=SearchResponse)
async def search(
    q: str = Query(..., min_length=2, max_length=64),
    limit: int = Query(50, ge=1, le=200),
) -> SearchResponse:
    built = await index_built_at()
    if built is None:
        # First build still running (boot + ~2-4 min) or refresh failing.
        raise HTTPException(
            status_code=503,
            detail=(
                "Full-inventory search index isn't ready yet — it builds a "
                "few minutes after the backend starts. Try again shortly."
            ),
        )
    if "." in q:
        # Exact-domain query (2026-08-18 speed fix): a dotted query means
        # "find THIS domain". Primary-key lookup — milliseconds — instead
        # of a 1.19M-row substring scan. If it's not in the index, the
        # dashboard falls back to the live GoDaddy lookup.
        from app.godaddy.inventory_index import resolve_domain

        hit = await resolve_domain(q)
        return SearchResponse(
            query=q,
            index_built_at=built,
            results=[_to_result(hit)] if hit else [],
        )

    listings = await search_domains(q, limit=limit)
    return SearchResponse(
        query=q,
        index_built_at=built,
        results=[_to_result(L) for L in listings],
    )


def _to_result(L) -> SearchResult:
    return SearchResult(
        domain=L.domain,
        listing_id=L.listing_id,
        auction_type=L.auction_type,
        price=L.price,
        valuation=L.valuation,
        end_time_utc=L.end_time_utc,
        bid_count=L.bid_count,
    )


class EstibotBatchResponse(BaseModel):
    values: dict[str, str]  # domain -> appraised value (USD, string)


@router.get("/estibot", response_model=EstibotBatchResponse)
async def estibot_batch(
    d: str = Query(..., min_length=4, max_length=8000),
) -> EstibotBatchResponse:
    """Estibot values for a comma-separated list of domains (max 200).

    Serves the EB chip on every dashboard card: cached values return
    instantly; unknown domains are appraised on demand in one cache-mode
    Estibot batch. Domains Estibot has no appraisal for are omitted."""
    domains = [x for x in (p.strip() for p in d.split(",")) if x][:200]
    values = await _estibot_with_deadline(domains)
    return EstibotBatchResponse(values=values)


# Circuit breaker (2026-08-26 perf audit: with Estibot down, EVERY signals
# request still burned the full deadline, and requests queued behind each
# other — 8-13s per call for a service returning nothing). Three straight
# failures open the circuit for 10 minutes: cached values only, instantly.
_EB_BREAKER = {"fails": 0, "open_until": 0.0}


async def _estibot_with_deadline(domains: list[str], seconds: float = 6.0) -> dict[str, str]:
    """Estibot lookups with a hard deadline + circuit breaker. Signals must
    never wait on a sick nice-to-have: deadline misses serve cache-only,
    and repeated misses stop us asking at all for a while."""
    import asyncio
    import time as _time

    if _time.monotonic() < _EB_BREAKER["open_until"]:
        # Circuit open: cached values only — instant, no network.
        try:
            return await estibot_values_for(domains)
        except Exception:  # noqa: BLE001
            return {}

    try:
        out = await asyncio.wait_for(get_or_fetch_estibot(domains), timeout=seconds)
        _EB_BREAKER["fails"] = 0
        return out
    except asyncio.TimeoutError:
        _EB_BREAKER["fails"] += 1
        logger.warning(
            "estibot deadline (%.0fs) hit for %d domains (strike %d) — serving signals without EB",
            seconds, len(domains), _EB_BREAKER["fails"],
        )
    except Exception as exc:  # noqa: BLE001 — estibot must never break signals
        _EB_BREAKER["fails"] += 1
        logger.warning("estibot lookup failed: %s (strike %d)", exc, _EB_BREAKER["fails"])
    if _EB_BREAKER["fails"] >= 3:
        _EB_BREAKER["open_until"] = _time.monotonic() + 600
        logger.warning("estibot circuit OPEN for 10 min — cache-only until then")
    try:
        return await estibot_values_for(domains)
    except Exception:  # noqa: BLE001
        return {}


class DomainSignals(BaseModel):
    estibot: Optional[str] = None          # Estibot appraisal (USD)
    sp: Optional[str] = None               # "typo→intended" when misspelled (SP flag)
    gd_est: Optional[str] = None           # GoDaddy valuation from the full index
                                           # (2026-09-07 vlr.co: backend-only
                                           # watchlist rows had no feed value)
    indexed_pages: Optional[int] = None    # Semrush indexed page count
    exact_match_tlds: Optional[int] = None # TLDs of this SLD registered
    developed_tlds: Optional[int] = None   # ...of which developed
    renewal_est: Optional[float] = None    # est. annual renewal (USD/yr)
    search_volume: Optional[int] = None    # Semrush monthly Google searches
    cpc: Optional[float] = None            # Semrush cost-per-click (USD)


class SignalsResponse(BaseModel):
    signals: dict[str, DomainSignals]


@router.get("/signals", response_model=SignalsResponse)
async def domain_signals(
    d: str = Query(..., min_length=4, max_length=8000),
) -> SignalsResponse:
    """Everything the cards/popup show per domain, one batched call
    (2026-08-19, the client's asks): Estibot value, pages indexed on the
    internet, TLD spread, and estimated renewal price."""
    from app.godaddy.inventory_index import renewal_estimate_for, resolve_many
    from app.scoring.spellcheck import check_spelling

    domains = [x for x in (p.strip().lower() for p in d.split(",")) if x][:200]
    eb = await _estibot_with_deadline(domains)
    indexed = await resolve_many(domains)
    out: dict[str, DomainSignals] = {}
    for dom in domains:
        hit = indexed.get(dom)
        out[dom] = DomainSignals(
            estibot=eb.get(dom),
            sp=check_spelling(dom),
            gd_est=hit.valuation if hit else None,
            indexed_pages=hit.indexed_pages if hit else None,
            exact_match_tlds=hit.exact_match_tlds if hit else None,
            developed_tlds=hit.developed_tlds if hit else None,
            renewal_est=renewal_estimate_for(dom),
            search_volume=hit.search_volume if hit else None,
            cpc=hit.cpc if hit else None,
        )
    return SignalsResponse(signals=out)


class LiveState(BaseModel):
    status: str
    listing_id: Optional[int] = None
    listing_type: Optional[str] = None
    price: Optional[str] = None            # current bid / price (USD)
    buy_now: Optional[str] = None
    renewal: Optional[str] = None          # GoDaddy's ACTUAL renewal price
    end_time_utc: Optional[str] = None
    bid_count: Optional[int] = None
    watching: Optional[bool] = None        # on the GoDaddy-app watchlist
    member_bidding_status: Optional[str] = None  # raw GoDaddy enum
    already_bidding: Optional[bool] = None  # collision guard: the client has a bid
    source: Optional[str] = None           # "availability" | "estimate"


class LiveResponse(BaseModel):
    live: dict[str, LiveState]


@router.get("/live", response_model=LiveResponse)
async def live_listings(
    d: str = Query(..., min_length=4, max_length=4000),
    verify: Optional[str] = Query(None, max_length=2000),
    db=Depends(get_db),
) -> LiveResponse:
    """LIVE listing state for up to 50 domains via the official Listings
    Availability API — the staleness fix from the client's 2026-08-23 demo.
    The dashboard polls this for whatever's on screen; sold/ended domains
    come back UNAVAILABLE instead of haunting the list at yesterday's
    price.

    `verify` (2026-08-25, myhomebills lie-detector): comma list of domains
    — pass the WATCHLISTED subset here. GoDaddy's availability endpoint
    reported myhomebills.com UNAVAILABLE while godaddy.com sold it at $5;
    the closeout estimate API told the truth the whole time. For verify
    domains that availability calls UNAVAILABLE (or omits), we cross-check
    with the estimate; if it prices, the domain is LIVE at that price with
    source="estimate". Capped at 10 estimates per call — keep the verify
    list to starred domains, not the whole page.
    """
    from app.config import get_settings
    from app.godaddy.client import GoDaddyAuth, GoDaddyClient, GoDaddyClientConfig
    from app.godaddy.live_listings import LiveListingsClient
    from app.godaddy.soap import SoapClient

    domains = [x for x in (p.strip().lower() for p in d.split(",")) if x][:50]
    verify_set = {
        x for x in ((p.strip().lower() for p in (verify or "").split(",")) if verify else [])
        if x and x in set(domains)
    }
    cfg = get_settings()
    auth = GoDaddyAuth(key=cfg.godaddy_api_key, secret=cfg.godaddy_api_secret)
    client_cfg = GoDaddyClientConfig(
        rest_base_url=cfg.rest_base_url,
        customer_id=cfg.godaddy_customer_id,
    )
    async with GoDaddyClient(auth=auth, config=client_cfg) as client:
        results = await LiveListingsClient(client).check(domains)

        out: dict[str, LiveState] = {
            dom: LiveState(
                status=L.status,
                listing_id=L.listing_id,
                listing_type=L.listing_type,
                price=str(L.price_current_dollars) if L.price_current_dollars is not None else None,
                buy_now=str(L.price_buy_it_now_dollars) if L.price_buy_it_now_dollars is not None else None,
                renewal=str(L.price_renewal_dollars) if L.price_renewal_dollars is not None else None,
                end_time_utc=L.auction_end_at,
                bid_count=L.bids_count,
                watching=L.watching,
                member_bidding_status=L.member_bidding_status,
                already_bidding=is_already_bidding(L.member_bidding_status),
                source="availability",
            )
            for dom, L in results.items()
        }

        # Capture observed memberBiddingStatus values (2026-09-24 collision
        # guard): the positive enum is undocumented, so log every non-null
        # value we see. Once a real "you're bidding" value shows up here we
        # can refine the guard (e.g. resume firing on a confirmed OUTBID).
        for _dom, _L in results.items():
            if _L.member_bidding_status:
                logger.info(
                    "memberBiddingStatus observed: %s=%s (already_bidding=%s)",
                    _dom, _L.member_bidding_status,
                    is_already_bidding(_L.member_bidding_status),
                )

        # GoDaddy-watchlist import (2026-09-24, the client): any listing that
        # passes through here with watching=True gets auto-starred into the
        # sniper. Best-effort — must never break the live poll.
        if any(L.watching for L in results.values()):
            try:
                from app.godaddy.watch_import import import_gd_watched

                await import_gd_watched(db, results.values())
            except Exception:  # noqa: BLE001
                logger.exception("GD-watch import failed (live poll unaffected)")

        # Lie-detector pass: for watchlisted domains availability wrote off,
        # ask the estimate API before believing "gone".
        suspects = [
            dom for dom in verify_set
            if dom not in out or out[dom].status != "AVAILABLE"
        ][:10]
        if suspects:
            # Parallel (2026-08-26 perf audit): sequential estimates made
            # /live take up to 10s per request. Gather brings the whole
            # verify pass down to roughly one estimate's latency.
            import asyncio as _asyncio

            soap = SoapClient(client)

            async def _verify(dom: str):
                try:
                    return dom, await soap.estimate_closeout_price(dom)
                except Exception:  # noqa: BLE001 — verify must never break /live
                    return dom, None

            for dom, est in await _asyncio.gather(*(_verify(d) for d in suspects)):
                if est is not None and est.success and est.listing_price_dollars is not None:
                    out[dom] = LiveState(
                        status="AVAILABLE",
                        price=str(est.listing_price_dollars),
                        buy_now=str(est.listing_price_dollars),
                        source="estimate",
                    )

    return LiveResponse(live=out)


class TopValueResponse(BaseModel):
    index_built_at: Optional[str] = None
    results: list[SearchResult]


@router.get("/top-value", response_model=TopValueResponse)
async def top_value(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=5000),
    auction_type: Optional[str] = Query(None, pattern="^(EXPIRY_AUCTION|CLOSEOUT)$"),
    tld: Optional[str] = Query(None, max_length=24),
    within_hours: Optional[float] = Query(None, gt=0, le=24 * 30),
) -> TopValueResponse:
    """Highest GoDaddy-valued listings across all ~935k — the client's daily
    top-1,500-by-valuation scan, without leaving the dashboard. Estibot
    valuations join this response once their API access is unblocked."""
    built = await index_built_at()
    if built is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Inventory index isn't ready yet — it builds a few minutes "
                "after the backend starts. Try again shortly."
            ),
        )
    from app.godaddy.inventory_index import (
        DEFAULT_MIN_GD_EST,
        DEFAULT_TLD_ALLOWLIST,
    )

    listings = await top_by_valuation(
        limit=limit,
        offset=offset,
        auction_type=auction_type,
        tld=tld,
        within_hours=within_hours,
        # The client's curation rules (2026-08-19): no junk TLDs, nothing under
        # $1,400 GD est. When he asks for a specific tld=, that wins.
        min_valuation=DEFAULT_MIN_GD_EST,
        allowed_tlds=None if tld else DEFAULT_TLD_ALLOWLIST,
    )
    # Attach Estibot appraisals where the nightly enrichment has them.
    eb = await estibot_values_for([L.domain for L in listings])
    results = []
    for L in listings:
        r = _to_result(L)
        r.estibot_value = eb.get(L.domain)
        results.append(r)
    return TopValueResponse(index_built_at=built, results=results)


@router.get("/gd-list", response_model=TopValueResponse)
async def gd_list(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=100000),
    auction_type: Optional[str] = Query(None, pattern="^(EXPIRY_AUCTION|CLOSEOUT)$"),
    from_hours: float = Query(0, ge=0, le=24 * 30),
    to_hours: Optional[float] = Query(None, gt=0, le=24 * 30),
) -> TopValueResponse:
    """GoDaddy's raw expiring list, mirrored exactly (The client, 2026-09-19).

    Every listing, GoDaddy's end-time order, NO scoring, NO $1,400 floor,
    NO TLD allowlist — the list he's combed an hour a day for 28 years,
    with his trusted columns (GD est, Estibot, pages indexed) alongside.
    from_hours/to_hours = the day-range isolation GoDaddy can't do.
    """
    built = await index_built_at()
    if built is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Inventory index isn't ready yet — it builds a few minutes "
                "after the backend starts. Try again shortly."
            ),
        )
    from app.godaddy.inventory_index import list_by_end_time

    listings = await list_by_end_time(
        limit=limit,
        offset=offset,
        auction_type=auction_type,
        from_hours=from_hours,
        to_hours=to_hours,
    )
    eb = await estibot_values_for([L.domain for L in listings])
    results = []
    for L in listings:
        r = _to_result(L)
        r.indexed_pages = L.indexed_pages
        r.estibot_value = eb.get(L.domain)
        results.append(r)
    return TopValueResponse(index_built_at=built, results=results)
