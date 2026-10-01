"""
Daily inventory sync worker.

Pulls one or both inventory feeds from GoDaddy, scores each listing using
the engine calibrated to the client's portfolio, and either:
  - writes to Postgres if a DB connection is configured, OR
  - writes a local JSON file of scored results (useful for early Phase 1
    when the DB schema isn't migrated yet)

Feed sources (per 2026-05-21 scope expansion):
  - "closeouts" -> closeout_listings.json.zip (5-day fixed-price window)
  - "expiring"  -> all_expiring_auctions.json.zip (10-day expiry-auction window)
  - "both"      -> pull and score both, dedup by listing_id

Designed so the same module can be invoked from scripts/run_sync.py (CLI)
or scheduled by APScheduler (worker process). The scoring logic is
deterministic; running it twice on the same feed produces identical scores.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from app.godaddy.inventory import InventoryFetcher, InventoryListing
from app.scoring.engine import ScoringEngine
from app.scoring.tokenizer import Tokenizer, load_default_wordlist

logger = logging.getLogger(__name__)


@dataclass
class ScoredListing:
    """Memory-lean per-listing record. Carries only what every downstream
    consumer needs across the full 935k-row feed. For the top N listings
    being written to JSON, we recompute the full ScoreBreakdown lazily
    (cheap -- the scorer is ~0.04ms per call)."""

    listing_id: int
    domain: str
    tld: str
    auction_type: str
    current_price: Optional[str]  # serialized Decimal as str for JSON compat
    end_time_utc: Optional[str]
    has_bids: bool
    score: int
    # Cheap surrogate for the full score_breakdown dict. Pre-extracted at
    # scoring time so the per-theme bucketing pass can avoid string-parsing
    # the notes list 935k times. Single list per listing instead of a dict
    # tree saves ~2 GB across the feed.
    themes: list[str]
    estimated_value: Optional[str]
    # TLD spread enrichment (filled in by sync_feed when --check-tld-spread is on).
    # None means "not checked yet" — distinguished from 0 ("checked, none taken").
    tld_spread_taken: Optional[int] = None
    tld_spread_checked: Optional[int] = None
    tld_spread_ratio: Optional[float] = None


def _extract_themes(notes: list[str]) -> list[str]:
    """Parse 'themes: [...]' from a ScoreBreakdown.notes list once,
    returning the parsed theme names. Used by _to_scored so the per-theme
    bucketing pass can read pre-parsed lists instead of re-parsing strings."""
    for n in notes:
        if isinstance(n, str) and n.startswith("themes: ["):
            inner = n[len("themes: ["):-1]
            return [t.strip().strip("'\"") for t in inner.split(",") if t.strip()]
    return []


def _to_scored(listing: InventoryListing, engine: ScoringEngine) -> ScoredListing:
    bd = engine.score(
        listing.domain,
        gd_estimated_value_dollars=listing.estimated_value_dollars,
        current_price_dollars=listing.current_price_dollars,
    )
    return ScoredListing(
        listing_id=listing.listing_id,
        domain=listing.domain,
        tld=listing.tld,
        auction_type=listing.auction_type,
        current_price=str(listing.current_price_dollars) if listing.current_price_dollars else None,
        end_time_utc=listing.end_time_utc.isoformat() if listing.end_time_utc else None,
        has_bids=listing.has_bids,
        score=bd.total,
        themes=_extract_themes(bd.notes),
        estimated_value=(
            str(listing.estimated_value_dollars) if listing.estimated_value_dollars else None
        ),
    )


def _to_top_n_dict(s: ScoredListing, engine: ScoringEngine) -> dict:
    """Build the full JSON-serializable dict for ONE top-N listing.

    Calls engine.score() a second time to get the full breakdown -- cheap
    relative to the rest of the run (5000 * ~40us = ~200ms), and lets us
    avoid hoarding the breakdown for all 935k listings during scoring.
    """
    from decimal import Decimal as _Decimal
    est_dec = _Decimal(s.estimated_value) if s.estimated_value else None
    price_dec = _Decimal(s.current_price) if s.current_price else None
    bd = engine.score(
        s.domain,
        gd_estimated_value_dollars=est_dec,
        current_price_dollars=price_dec,
    )
    return {
        "listing_id": s.listing_id,
        "domain": s.domain,
        "tld": s.tld,
        "auction_type": s.auction_type,
        "current_price": s.current_price,
        "end_time_utc": s.end_time_utc,
        "has_bids": s.has_bids,
        "score": s.score,
        "themes": s.themes,
        "score_breakdown": json.loads(bd.to_json()),
        "estimated_value": s.estimated_value,
        "tld_spread_taken": s.tld_spread_taken,
        "tld_spread_checked": s.tld_spread_checked,
        "tld_spread_ratio": s.tld_spread_ratio,
    }


async def _enrich_with_tld_spread(
    top_listings: list[ScoredListing],
    audit_log_path: Optional[Path] = None,
) -> None:
    """Run a TLD-spread check against the top N listings, in-place.

    Wires up the GoDaddyClient + AvailabilityClient using the existing
    config (so credentials come from .env). When audit_log_path is provided,
    every GoDaddy API call goes through the JSONL audit writer for
    after-the-fact troubleshooting. Quietly logs and continues on any
    failure -- TLD spread is a nice-to-have signal, not a blocker for the
    rest of the sync output.
    """
    # Local imports so the worker can be imported in environments without
    # the GoDaddy auth modules wired up (e.g. CI smoke).
    from app.config import get_settings
    from app.godaddy.availability import AvailabilityClient
    from app.godaddy.client import GoDaddyAuth, GoDaddyClient, GoDaddyClientConfig
    from app.logging_setup import make_audit_jsonl_writer

    settings = get_settings()
    if not (settings.godaddy_api_key and settings.godaddy_api_secret):
        logger.warning(
            "TLD spread enrichment requested but no GoDaddy API key in env; "
            "skipping. Populate .env and re-run with --check-tld-spread."
        )
        return

    # SLDs to check. Dedup so two listings sharing an SLD (.com + .net of
    # the same name) only hit the API once.
    slds_to_check: list[str] = []
    seen_slds: set[str] = set()
    listings_by_sld: dict[str, list[ScoredListing]] = {}
    for s in top_listings:
        sld = s.domain.split(".", 1)[0].lower() if "." in s.domain else s.domain.lower()
        if not sld:
            continue
        listings_by_sld.setdefault(sld, []).append(s)
        if sld not in seen_slds:
            seen_slds.add(sld)
            slds_to_check.append(sld)

    auth = GoDaddyAuth(key=settings.godaddy_api_key, secret=settings.godaddy_api_secret)
    config = GoDaddyClientConfig(
        rest_base_url=settings.rest_base_url,
        customer_id=settings.godaddy_customer_id,
    )

    # Wire the API audit hook. Every request to /v1/domains/available gets
    # one line in the audit JSONL alongside its response.
    audit_hook = make_audit_jsonl_writer(audit_log_path) if audit_log_path else None

    print(f"\n--- TLD spread enrichment: checking {len(slds_to_check)} SLDs ---")
    logger.info("tld spread enrichment starting for %d SLDs", len(slds_to_check))
    started = datetime.now(timezone.utc)
    async with GoDaddyClient(auth=auth, config=config, audit_hook=audit_hook) as client:
        ac = AvailabilityClient(client)
        spreads = await ac.check_tld_spread_many(
            slds_to_check,
            concurrency=4,
            delay_between_calls_sec=0.2,
        )
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    print(f"  done in {elapsed:.1f}s -- got spread for {len(spreads)} SLDs")
    logger.info(
        "tld spread enrichment complete: %d/%d SLDs in %.1fs",
        len(spreads), len(slds_to_check), elapsed,
    )

    # Populate the new fields on the scored listings.
    for sld, spread in spreads.items():
        for s in listings_by_sld.get(sld, []):
            s.tld_spread_taken = spread.taken_count
            s.tld_spread_checked = spread.total_checked
            s.tld_spread_ratio = spread.spread_ratio


async def sync_closeouts(
    output_path: Optional[Path] = None,
    top_n_preview: int = 25,
    persist_to_db: bool = False,
) -> list[ScoredListing]:
    """Pull and score the closeouts feed. Thin wrapper around sync_feed."""
    return await sync_feed(
        target="closeouts",
        output_path=output_path,
        top_n_preview=top_n_preview,
        persist_to_db=persist_to_db,
    )


async def sync_expiring(
    output_path: Optional[Path] = None,
    top_n_preview: int = 25,
    persist_to_db: bool = False,
) -> list[ScoredListing]:
    """Pull and score the 10-day expiring-auctions feed.

    Per the 2026-05-21 call: this surfaces the list the client manually scans
    every day. Visibility only -- we do NOT auto-purchase expiring auctions
    in v1.
    """
    return await sync_feed(
        target="expiring",
        output_path=output_path,
        top_n_preview=top_n_preview,
        persist_to_db=persist_to_db,
    )


async def sync_feed(
    target: str = "closeouts",
    output_path: Optional[Path] = None,
    top_n_preview: int = 25,
    persist_to_db: bool = False,
    check_tld_spread_for_top_n: int = 0,
    output_limit: int = 0,
    audit_log_path: Optional[Path] = None,
) -> list[ScoredListing]:
    """Pull a feed (or both), score, optionally write to file and/or DB.

    target: one of "closeouts", "expiring", or "both".
       "both" pulls both feeds and dedupes by listing_id, preferring
       the CLOSEOUT row when a domain appears in both (closeouts are
       actionable in v1; expiring is visibility-only).

    output_path:    if provided, writes the full scored list to this JSON file.
    top_n_preview:  how many top-scored to log to console.
    persist_to_db:  if True, upsert into the auctions table (requires Postgres + migrations).
    check_tld_spread_for_top_n: if > 0, enrich the top N highest-scoring listings
       with GoDaddy bulk-availability TLD spread data. Each enriched listing
       takes ~1 bulk API call (26 TLDs in one POST). Heuristic: keep this
       <= 500 per run to stay well under GoDaddy's per-account throttle.
    output_limit: if > 0, only the top N listings are written to output_path.
       The full scored list is still returned from the function and the
       per-theme breakdown still uses the full set. Useful in CI where the
       full 935k-row JSON (~850 MB) is too big to upload as an artifact.
       Default 0 = no limit, write everything.

    Returns the scored list.
    """
    if target not in ("closeouts", "expiring", "both"):
        raise ValueError(f"unknown target {target!r}; expected closeouts|expiring|both")

    started = datetime.now(timezone.utc)
    logger.info("Inventory sync started at %s (target=%s)", started.isoformat(), target)

    # Load wordlist once; build the engine.
    wordlist = load_default_wordlist()
    if not wordlist:
        logger.warning(
            "No system wordlist found. Install one with: "
            "`brew install words` on Mac, or use a bundled fallback. "
            "Scoring will skip dictionary tokens until this is fixed."
        )
    engine = ScoringEngine(Tokenizer(wordlist))
    fetcher = InventoryFetcher()

    # Pull and parse the requested feed(s).
    closeouts: list = []
    expiring: list = []
    if target in ("closeouts", "both"):
        closeouts = await fetcher.fetch_closeouts()
        logger.info("Got %d closeout listings", len(closeouts))
    if target in ("expiring", "both"):
        expiring = await fetcher.fetch_expiring()
        logger.info("Got %d expiring-auction listings", len(expiring))

    # Merge with closeouts taking precedence (a domain showing on both is
    # actionable as a closeout right now; the expiring row is stale state).
    by_id: dict[int, "InventoryListing"] = {}
    for L in expiring:
        by_id[L.listing_id] = L
    for L in closeouts:
        by_id[L.listing_id] = L
    listings = list(by_id.values())

    # Score everything.
    scored = [_to_scored(L, engine) for L in listings]
    scored.sort(key=lambda s: s.score, reverse=True)
    logger.info(
        "Scored and sorted. n=%d highest=%d lowest=%d",
        len(scored),
        scored[0].score if scored else 0,
        scored[-1].score if scored else 0,
    )

    # Optional: enrich the top-N with TLD spread (GoDaddy bulk availability).
    # Per the 2026-05-22 call, TLD spread is one of the client's core signals --
    # how many other TLDs of the same SLD are already registered. This is
    # a free path that uses GoDaddy's standard domain-availability API.
    if check_tld_spread_for_top_n > 0 and scored:
        await _enrich_with_tld_spread(
            scored[:check_tld_spread_for_top_n],
            audit_log_path=audit_log_path,
        )

    # Console preview of the top N. Show auction_type so the user can tell
    # closeouts (purchasable) from expiring (watchlist-only) at a glance.
    # When TLD spread enrichment is on, show the taken/checked ratio too.
    label = {"closeouts": "closeouts", "expiring": "expiring auctions", "both": "listings"}[target]
    has_spread = any(s.tld_spread_checked is not None for s in scored[:top_n_preview])
    print(f"\n--- Top {top_n_preview} {label} by score ---")
    if has_spread:
        print(f"{'Score':>5}  {'Type':<8}  {'Price':>7}  {'Bids':>4}  {'TLDs':>7}  Domain")
        print("-" * 86)
    else:
        print(f"{'Score':>5}  {'Type':<8}  {'Price':>7}  {'Bids':>4}  Domain")
        print("-" * 78)
    for s in scored[:top_n_preview]:
        price_disp = f"${s.current_price}" if s.current_price else "n/a"
        short_type = "CLOSE" if s.auction_type == "CLOSEOUT" else "EXPIRY"
        if has_spread:
            spread_disp = (
                f"{s.tld_spread_taken}/{s.tld_spread_checked}"
                if s.tld_spread_checked is not None
                else "n/a"
            )
            print(
                f"{s.score:>5}  {short_type:<8}  {price_disp:>7}  "
                f"{'Y' if s.has_bids else 'N':>4}  {spread_disp:>7}  {s.domain}"
            )
        else:
            print(
                f"{s.score:>5}  {short_type:<8}  {price_disp:>7}  "
                f"{'Y' if s.has_bids else 'N':>4}  {s.domain}"
            )

    # Industry / theme breakdown. Per the 2026-05-22 call, the client wants to scan
    # the top-scoring domains by industry, not just one global ranked list. We
    # bucket each scored listing by every theme it matched. ScoredListing
    # already carries a pre-extracted `themes: list[str]` from _to_scored, so
    # this pass is just dict appends -- no string parsing per listing.
    from app.scoring.themes import THEME_BUCKETS  # local import -- avoid worker startup cost
    if scored:
        per_industry_top_n = max(15, top_n_preview // 5)

        # Bucket all scored listings by every theme they matched.
        by_theme: dict[str, list["ScoredListing"]] = {b: [] for b in THEME_BUCKETS}
        for s in scored:
            for t in s.themes:
                if t in by_theme:
                    by_theme[t].append(s)

        print(f"\n--- Top {per_industry_top_n} per industry / theme bucket ---")
        for theme, items in by_theme.items():
            if not items:
                continue
            print(f"\n  [{theme}]  ({len(items)} listings total in this bucket)")
            print(f"    {'Score':>5}  {'Type':<8}  {'Price':>7}  Domain")
            for s in items[:per_industry_top_n]:
                price_disp = f"${s.current_price}" if s.current_price else "n/a"
                short_type = "CLOSE" if s.auction_type == "CLOSEOUT" else "EXPIRY"
                print(
                    f"    {s.score:>5}  {short_type:<8}  {price_disp:>7}  {s.domain}"
                )

    # Distribution summary, broken out by type so we can see whether the
    # expiry feed is producing a different score profile.
    if scored:
        def _bucket(score: int) -> str:
            if score < 30:   return "0-30"
            if score < 60:   return "30-60"
            if score < 90:   return "60-90"
            if score < 120:  return "90-120"
            return "120+"
        bucket_order = ["0-30", "30-60", "60-90", "90-120", "120+"]
        by_type: dict[str, dict[str, int]] = {}
        for s in scored:
            by_type.setdefault(s.auction_type, {b: 0 for b in bucket_order})
            by_type[s.auction_type][_bucket(s.score)] += 1
        print(f"\n--- Score distribution by auction type (n={len(scored)}) ---")
        for atype, buckets in by_type.items():
            print(f"  [{atype}]  total={sum(buckets.values())}")
            for b in bucket_order:
                count = buckets[b]
                bar = "#" * min(50, count // 20)
                print(f"     {b:>6}: {count:>5} {bar}")

    # Optional file output.
    if output_path:
        # Cap the list written to disk if output_limit is set. The full set is
        # still in `scored` for downstream code (e.g. per-theme breakdowns,
        # DB persistence) -- we just don't serialize all 935k rows when
        # we don't have to. Keeps GH Actions artifacts manageable.
        # Reserve slots for closeouts so "Buy Now only" is never empty
        # (2026-08-18 — see _select_output_listings docstring).
        from app.godaddy.inventory import _select_output_listings

        listings_to_write = _select_output_listings(scored, output_limit)
        # For each top-N listing, recompute the full score breakdown so the
        # JSON has the rich data the dashboard wants. Cheap relative to the
        # rest of the run (5000 * ~40us = ~200ms).
        out = {
            "generated_at": started.isoformat(),
            "target": target,
            "total_listings": len(scored),
            "listings_written": len(listings_to_write),
            "listings": [_to_top_n_dict(s, engine) for s in listings_to_write],
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Compact JSON (no indent) -- the file's for machines, not humans.
        # Saves ~50% disk + upload time at this scale.
        output_path.write_text(json.dumps(out, separators=(",", ":")))
        logger.info(
            "Wrote %d of %d scored listings to %s",
            len(listings_to_write), len(scored), output_path,
        )
        print(f"\nScored dataset written to: {output_path} ({len(listings_to_write)} listings)")

    # Optional Postgres persistence.
    if persist_to_db:
        # Import inside the branch so missing DB config doesn't break the
        # console-only path used in early Phase 1 work.
        from app.db import SessionLocal
        from worker.persist import upsert_scored_listings

        async with SessionLocal() as session:
            inserted, updated = await upsert_scored_listings(session, scored)
        print(f"\nDB persistence: {inserted} inserted, {updated} updated")

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    print(f"\nInventory sync complete in {elapsed:.1f}s (target={target})")
    return scored
