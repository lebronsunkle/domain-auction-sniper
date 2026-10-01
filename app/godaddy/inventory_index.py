"""Full-inventory index: every listing in GoDaddy's daily feeds, queryable.

Why this exists (2026-07-14, from the client's demo feedback):
  * Dashboard search only covered the day's top-5,000 scored listings —
    searching "poly" across all ~935k came up empty.
  * "+ Add domain" entries had no real listing_id (the SOAP lookup doesn't
    return one), so they could never be sniped. The real id IS in the
    feed's `link` URL for every listing — we just weren't keeping it.

Design constraints: the Fly machine has 256MB RAM. The decompressed
expiring feed is ~400MB of JSON, so we stream-parse (ijson) from the
downloaded zip straight into a local SQLite file (~100MB on ephemeral
disk) and never hold more than a batch of rows in memory. Queries are
sync sqlite3 wrapped in asyncio.to_thread.

The index rebuilds in the background at boot and every REFRESH_HOURS.
A half-built index is never visible: we build to a temp file and
atomically rename over the live one.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.godaddy.inventory import FeedFile, INVENTORY_BASE_URL, _normalize

logger = logging.getLogger(__name__)

# v2 (2026-07-14): schema adds an indexed numeric valuation column so the
# Top Value query is an index scan instead of a 935k-row CAST+sort (which
# took seconds per request — "the client doesn't have patience"). New filename
# so a stale v1 file is simply ignored and rebuilt.
# v5 (2026-08-19): + Semrush search volume + CPC — the client's #1 gauge,
# free in the feed. ("160k+ monthly searches = high-confidence buy.")
INDEX_PATH = os.getenv("INVENTORY_INDEX_PATH", "/tmp/inventory_index_v5.db")
REFRESH_HOURS = float(os.getenv("INVENTORY_INDEX_REFRESH_HOURS", "12"))

# Feeds to index. Expiring is the superset that matters for sniping;
# closeouts adds the buy-now window listings.
_FEEDS = (
    (FeedFile.EXPIRING_JSON, "EXPIRY_AUCTION"),
    (FeedFile.CLOSEOUT_JSON, "CLOSEOUT"),
)

_SCHEMA = """
CREATE TABLE listings (
    domain TEXT PRIMARY KEY,
    listing_id INTEGER NOT NULL,
    auction_type TEXT NOT NULL,
    price TEXT,
    valuation TEXT,
    end_time_utc TEXT,
    bid_count INTEGER,
    valuation_num REAL,
    indexed_pages INTEGER,
    exact_match_tlds INTEGER,
    developed_tlds INTEGER,
    search_volume INTEGER,
    cpc REAL
);
CREATE INDEX idx_listings_valuation ON listings(valuation_num DESC);
CREATE INDEX idx_listings_end ON listings(end_time_utc);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Explicit column list for reads — valuation_num is query-plumbing only.
_COLS = ("domain, listing_id, auction_type, price, valuation, end_time_utc, "
         "bid_count, indexed_pages, exact_match_tlds, developed_tlds, "
         "search_volume, cpc")


@dataclass
class IndexedListing:
    domain: str
    listing_id: int
    auction_type: str
    price: Optional[str]
    valuation: Optional[str]
    end_time_utc: Optional[str]
    bid_count: Optional[int]
    indexed_pages: Optional[int] = None
    exact_match_tlds: Optional[int] = None
    developed_tlds: Optional[int] = None
    search_volume: Optional[int] = None
    cpc: Optional[float] = None


def _row_to_listing(row) -> IndexedListing:
    return IndexedListing(
        domain=row[0], listing_id=row[1], auction_type=row[2],
        price=row[3], valuation=row[4], end_time_utc=row[5], bid_count=row[6],
        indexed_pages=row[7], exact_match_tlds=row[8], developed_tlds=row[9],
        search_volume=row[10], cpc=row[11],
    )


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------


def _ingest_zip_file(conn: sqlite3.Connection, zip_path: str, default_type: str) -> int:
    """Stream one feed zip into the listings table. Returns rows ingested."""
    import ijson  # local import: only the build thread needs it

    count = 0
    with zipfile.ZipFile(zip_path) as zf:
        inner = zf.namelist()[0]
        with zf.open(inner) as jf:
            batch = []
            # Feed shape: {"data": [ {...}, ... ]}
            for row in ijson.items(jf, "data.item"):
                L = _normalize(row, default_type)
                if not L.domain or not L.listing_id:
                    continue
                batch.append((
                    L.domain,
                    L.listing_id,
                    L.auction_type,
                    str(L.current_price_dollars) if L.current_price_dollars is not None else None,
                    str(L.estimated_value_dollars) if L.estimated_value_dollars is not None else None,
                    L.end_time_utc.isoformat() if L.end_time_utc else None,
                    L.bid_count,
                    float(L.estimated_value_dollars) if L.estimated_value_dollars is not None else None,
                    L.semrush_indexed_pages,
                    L.exact_match_tlds,
                    L.developed_tlds,
                    L.semrush_search_volume,
                    L.semrush_cpc,
                ))
                if len(batch) >= 5000:
                    conn.executemany(
                        "INSERT OR REPLACE INTO listings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        batch,
                    )
                    count += len(batch)
                    batch.clear()
            if batch:
                conn.executemany(
                    "INSERT OR REPLACE INTO listings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    batch,
                )
                count += len(batch)
    return count


def _build_sync(index_path: str) -> int:
    """Download both feeds and build a fresh index file. Returns row count.

    Runs in a worker thread (blocking I/O + CPU). Streams the JSON so peak
    memory stays in the tens of MB regardless of feed size.
    """
    total = 0
    tmp_db = index_path + ".building"
    if os.path.exists(tmp_db):
        os.remove(tmp_db)
    conn = sqlite3.connect(tmp_db)
    try:
        conn.executescript(_SCHEMA)

        for feed_file, default_type in _FEEDS:
            url = f"{INVENTORY_BASE_URL}/{feed_file}"
            logger.info("Inventory index: downloading %s", url)
            with tempfile.NamedTemporaryFile(suffix=".zip") as zf_file:
                with httpx.stream("GET", url, timeout=300.0, follow_redirects=True) as r:
                    r.raise_for_status()
                    for chunk in r.iter_bytes(1 << 20):
                        zf_file.write(chunk)
                zf_file.flush()
                total += _ingest_zip_file(conn, zf_file.name, default_type)
            conn.commit()
            logger.info("Inventory index: %s ingested (running total %d rows)", feed_file, total)

        conn.execute(
            "INSERT OR REPLACE INTO meta VALUES ('built_at', ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
    finally:
        conn.close()

    os.replace(tmp_db, index_path)  # atomic swap — readers never see a partial index
    return total


async def refresh_index(index_path: str = INDEX_PATH) -> int:
    """Rebuild the index off the event loop. Returns row count (0 on failure)."""
    try:
        count = await asyncio.to_thread(_build_sync, index_path)
        logger.info("Inventory index refreshed: %d listings at %s", count, index_path)
        return count
    except Exception:  # noqa: BLE001 — index is a soft dependency; never crash the app
        logger.exception("Inventory index refresh failed; keeping previous index if any")
        return 0


async def run_index_refresh_loop() -> None:
    """Background task: build at boot, then refresh every REFRESH_HOURS.
    After each successful refresh, run the Estibot enrichment pass (no-op
    when ESTIBOT_API_KEY isn't configured)."""
    while True:
        count = await refresh_index()
        if count > 0:
            try:
                enriched = await enrich_top_value_with_estibot()
                if enriched:
                    logger.info("Estibot enrichment: %d domains appraised", enriched)
            except Exception:  # noqa: BLE001 — enrichment is best-effort
                logger.exception("Estibot enrichment failed; continuing")
        await asyncio.sleep(REFRESH_HOURS * 3600)


# --------------------------------------------------------------------------
# Estibot enrichment (unblocked 2026-07-14 — see docs/estibot-integration.md)
# --------------------------------------------------------------------------
# Persistent cache in its OWN SQLite file so it survives the inventory
# index's atomic-rebuild swaps. Values refresh after ESTIBOT_TTL_DAYS.

ESTIBOT_CACHE_PATH = os.getenv("ESTIBOT_CACHE_PATH", "/tmp/estibot_cache.db")
ESTIBOT_TTL_DAYS = 7
# How many of the top-by-GD-valuation domains to appraise per refresh.
# Batches of 200 in cache mode => ~5 API calls per run. Well inside the
# Advanced tier's 5,000/day and the endpoint's 100 req/min.
ESTIBOT_ENRICH_TOP_N = int(os.getenv("ESTIBOT_ENRICH_TOP_N", "1000"))

_ESTIBOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS estibot (
    domain TEXT PRIMARY KEY,
    value TEXT,
    category TEXT,
    appraised_at TEXT NOT NULL
);
"""


def _estibot_write_sync(cache_path: str, rows: list[tuple]) -> None:
    conn = sqlite3.connect(cache_path)
    try:
        conn.executescript(_ESTIBOT_SCHEMA)
        conn.executemany(
            "INSERT OR REPLACE INTO estibot VALUES (?,?,?,?)", rows
        )
        conn.commit()
    finally:
        conn.close()


def _estibot_read_sync(cache_path: str, sql: str, params: tuple) -> list:
    if not os.path.exists(cache_path):
        return []
    conn = sqlite3.connect(f"file:{cache_path}?mode=ro", uri=True)
    try:
        conn.executescript  # no-op attr touch; schema created by writer only
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []  # table doesn't exist yet
    finally:
        conn.close()


async def estibot_values_for(
    domains: list[str], cache_path: str = ESTIBOT_CACHE_PATH
) -> dict[str, str]:
    """domain -> appraised value (as string) for cached appraisals."""
    if not domains:
        return {}
    placeholders = ",".join("?" for _ in domains)
    rows = await asyncio.to_thread(
        _estibot_read_sync,
        cache_path,
        f"SELECT domain, value FROM estibot WHERE domain IN ({placeholders})",
        tuple(d.lower() for d in domains),
    )
    return {r[0]: r[1] for r in rows if r[1] is not None}


# Serialize on-demand Estibot calls — their docs say one query at a time,
# and two dashboard tabs rendering simultaneously shouldn't double-call.
_estibot_fetch_lock = asyncio.Lock()


async def get_or_fetch_estibot(
    domains: list[str],
    cache_path: str = ESTIBOT_CACHE_PATH,
    client=None,
) -> dict[str, str]:
    """Cached Estibot values for `domains`, fetching misses on demand.

    Powers the EB chip on every dashboard card (2026-07-14, feature request):
    the dashboard sends the domains currently on screen; anything already
    appraised comes from cache, the rest go to Estibot in ONE cache-mode
    batch and are stored. Domains Estibot has never appraised simply come
    back absent — cards show only the GD number for those.
    """
    domains = [d.lower().strip() for d in domains if d and "." in d][:200]
    if not domains:
        return {}

    known = await estibot_values_for(domains, cache_path=cache_path)
    missing = [d for d in domains if d not in known]
    if not missing:
        return known

    if client is None:
        from app.config import get_settings
        from app.valuation.estibot import EstibotClient, EstibotClientConfig

        key = get_settings().estibot_api_key
        if not key:
            return known
        client = EstibotClient(EstibotClientConfig(api_key=key, max_batch_size=200))

    async with _estibot_fetch_lock:
        try:
            appraisals = await client.appraise(missing, cache_mode="cache")
        except Exception:  # noqa: BLE001 — EB values are a nice-to-have
            logger.exception("On-demand Estibot fetch failed")
            return known

    now_iso = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            d.lower(),
            str(a.appraised_value) if a.appraised_value is not None else None,
            a.category_root,
            now_iso,
        )
        for d, a in appraisals.items()
    ]
    if rows:
        await asyncio.to_thread(_estibot_write_sync, cache_path, rows)
        for d, a in appraisals.items():
            if a.appraised_value is not None:
                known[d.lower()] = str(a.appraised_value)
    return known


async def enrich_top_value_with_estibot(
    index_path: str = INDEX_PATH,
    cache_path: str = ESTIBOT_CACHE_PATH,
    client=None,
) -> int:
    """Appraise the top-N GD-valued domains via Estibot (cache mode).

    Returns the number of domains newly appraised. Silently no-ops when no
    API key is configured. `client` is injectable for tests.
    """
    from datetime import timedelta

    if client is None:
        from app.config import get_settings
        from app.valuation.estibot import EstibotClient, EstibotClientConfig

        key = get_settings().estibot_api_key
        if not key:
            return 0
        client = EstibotClient(
            EstibotClientConfig(api_key=key, max_batch_size=200)
        )

    top = await top_by_valuation(limit=ESTIBOT_ENRICH_TOP_N, index_path=index_path)
    if not top:
        return 0

    # Skip domains appraised within the TTL.
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=ESTIBOT_TTL_DAYS)
    ).isoformat()
    fresh_rows = await asyncio.to_thread(
        _estibot_read_sync,
        cache_path,
        "SELECT domain FROM estibot WHERE appraised_at > ?",
        (cutoff,),
    )
    fresh = {r[0] for r in fresh_rows}
    todo = [t.domain for t in top if t.domain not in fresh]
    if not todo:
        return 0

    appraisals = await client.appraise(todo, cache_mode="cache")
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            d.lower(),
            str(a.appraised_value) if a.appraised_value is not None else None,
            a.category_root,
            now_iso,
        )
        for d, a in appraisals.items()
    ]
    if rows:
        await asyncio.to_thread(_estibot_write_sync, cache_path, rows)
    return len(rows)


# --------------------------------------------------------------------------
# Query
# --------------------------------------------------------------------------


def _query_sync(index_path: str, sql: str, params: tuple) -> list:
    if not os.path.exists(index_path):
        return []
    conn = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


async def resolve_domain(domain: str, index_path: str = INDEX_PATH) -> Optional[IndexedListing]:
    """Exact-match lookup: the real listing_id (and snapshot) for a domain."""
    rows = await asyncio.to_thread(
        _query_sync,
        index_path,
        f"SELECT {_COLS} FROM listings WHERE domain = ?",
        (domain.lower().strip(),),
    )
    return _row_to_listing(rows[0]) if rows else None


async def resolve_many(
    domains: list[str], index_path: str = INDEX_PATH
) -> dict[str, IndexedListing]:
    """Exact-match lookup for many domains in ONE query."""
    ds = [d.lower().strip() for d in domains if d][:200]
    if not ds:
        return {}
    placeholders = ",".join("?" for _ in ds)
    rows = await asyncio.to_thread(
        _query_sync,
        index_path,
        f"SELECT {_COLS} FROM listings WHERE domain IN ({placeholders})",
        tuple(ds),
    )
    return {r[0]: _row_to_listing(r) for r in rows}


async def search_domains(
    query: str,
    limit: int = 50,
    index_path: str = INDEX_PATH,
) -> list[IndexedListing]:
    """Substring search across the FULL inventory, prefix matches first."""
    q = query.lower().strip()
    if not q:
        return []
    like = f"%{q}%"
    prefix = f"{q}%"
    rows = await asyncio.to_thread(
        _query_sync,
        index_path,
        # Prefix matches rank first, then shorter domains (The client prefers
        # short names), capped at LIMIT for sanity on 935k rows.
        f"""
        SELECT {_COLS} FROM listings
        WHERE domain LIKE ?
        ORDER BY (CASE WHEN domain LIKE ? THEN 0 ELSE 1 END), length(domain)
        LIMIT ?
        """,
        (like, prefix, int(limit)),
    )
    return [_row_to_listing(r) for r in rows]


# The client's TLD allowlist (2026-08-19 call): junk TLDs (.site, .cloud,
# .design, ...) are noise to him. Only these appear in curated views.
# Full-inventory SEARCH intentionally ignores this — an explicit query
# should find anything.
DEFAULT_TLD_ALLOWLIST = ("com", "ai", "co", "net", "org", "ca", "eu")

# And nothing under this GoDaddy valuation is worth his scan time.
DEFAULT_MIN_GD_EST = 1400


async def top_by_valuation(
    limit: int = 200,
    offset: int = 0,
    auction_type: Optional[str] = None,
    tld: Optional[str] = None,
    within_hours: Optional[float] = None,
    min_valuation: Optional[float] = None,
    allowed_tlds: Optional[tuple] = None,
    index_path: str = INDEX_PATH,
) -> list[IndexedListing]:
    """Highest GoDaddy-valued listings across the FULL inventory.

    The client's daily ritual (2026-07-14 call): he combs roughly the top 1,500
    listings by GoDaddy valuation. Our scored feed ranks by HIS rubric,
    which can bury objectively high-valuation names — this queries the raw
    935k by valuation directly.
    """
    where = ["valuation_num IS NOT NULL"]
    params: list = []
    if auction_type:
        where.append("auction_type = ?")
        params.append(auction_type)
    if tld:
        where.append("domain LIKE ?")
        params.append(f"%.{tld.lower().lstrip('.')}")
    if min_valuation is not None:
        where.append("valuation_num >= ?")
        params.append(float(min_valuation))
    if allowed_tlds:
        where.append(
            "(" + " OR ".join("domain LIKE ?" for _ in allowed_tlds) + ")"
        )
        params.extend([f"%.{t}" for t in allowed_tlds])
    if within_hours is not None:
        # The client's demo ask (2026-07-15): "most urgent are the ones expiring
        # sooner". end_time_utc is normalized ISO-8601 UTC text, so string
        # range comparison is chronologically correct, and idx_listings_end
        # makes it cheap.
        from datetime import timedelta as _td

        now = datetime.now(timezone.utc)
        where.append("end_time_utc >= ? AND end_time_utc <= ?")
        params.extend([
            now.isoformat(),
            (now + _td(hours=float(within_hours))).isoformat(),
        ])
    # valuation_num is indexed DESC — this is an index walk, not a
    # 935k-row CAST+sort (v2 speed fix, 2026-07-14).
    sql = (
        f"SELECT {_COLS} FROM listings WHERE "
        + " AND ".join(where)
        + " ORDER BY valuation_num DESC LIMIT ? OFFSET ?"
    )
    params.extend([int(limit), int(offset)])
    rows = await asyncio.to_thread(_query_sync, index_path, sql, tuple(params))
    return [_row_to_listing(r) for r in rows]


async def list_by_end_time(
    limit: int = 100,
    offset: int = 0,
    auction_type: Optional[str] = None,
    tld: Optional[str] = None,
    from_hours: float = 0.0,
    to_hours: Optional[float] = None,
    index_path: str = INDEX_PATH,
) -> list[IndexedListing]:
    """GoDaddy's expiring list, mirrored (The client, 2026-09-19 call).

    His hour-a-day-for-28-years ritual runs on GoDaddy's raw expiring list
    sorted by end time — no scoring, no valuation floor, no TLD allowlist.
    The sniper's curated views made him feel he was missing 90-95% of what
    he'd bid on (dentl.com, GD est $8k, never surfaced). This is that list,
    exactly, from the same inventory feed GoDaddy renders it from.

    from_hours/to_hours give the day-range isolation GoDaddy can't do
    ("show me ONLY day 3 to day 4, I already combed the earlier days"):
    both bound end_time_utc relative to now; idx_listings_end keeps it an
    index walk. NULL end times are excluded — a list ordered by end time
    has no place for rows without one.
    """
    from datetime import timedelta as _td

    now = datetime.now(timezone.utc)
    where = ["end_time_utc IS NOT NULL", "end_time_utc >= ?"]
    params: list = [(now + _td(hours=float(from_hours))).isoformat()]
    if to_hours is not None:
        where.append("end_time_utc <= ?")
        params.append((now + _td(hours=float(to_hours))).isoformat())
    if auction_type:
        where.append("auction_type = ?")
        params.append(auction_type)
    if tld:
        where.append("domain LIKE ?")
        params.append(f"%.{tld.lower().lstrip('.')}")
    sql = (
        f"SELECT {_COLS} FROM listings WHERE "
        + " AND ".join(where)
        + " ORDER BY end_time_utc ASC LIMIT ? OFFSET ?"
    )
    params.extend([int(limit), int(offset)])
    rows = await asyncio.to_thread(_query_sync, index_path, sql, tuple(params))
    return [_row_to_listing(r) for r in rows]


async def index_built_at(index_path: str = INDEX_PATH) -> Optional[str]:
    rows = await asyncio.to_thread(
        _query_sync, index_path, "SELECT value FROM meta WHERE key='built_at'", ()
    )
    return rows[0][0] if rows else None


# ---------------------------------------------------------------------------
# Renewal price estimates (2026-08-19, the client: "see the renewal price on
# each domain, dashboard wide"). GoDaddy standard non-promo renewal rates
# by TLD, USD/yr, rounded — close enough for buy/pass decisions. Closeouts
# get EXACT renewal+fees from the Instant Purchase preview at buy time;
# this table covers browsing. Update occasionally; last checked 2026-08.
# ---------------------------------------------------------------------------

TLD_RENEWAL_ESTIMATES: dict[str, float] = {
    "com": 21.99, "net": 22.99, "org": 25.99, "co": 37.99,
    "ai": 109.99, "io": 71.99, "ca": 18.99, "eu": 10.99,
    "us": 24.99, "biz": 26.99, "info": 28.99, "me": 27.99,
}


def renewal_estimate_for(domain: str) -> Optional[float]:
    tld = domain.rsplit(".", 1)[-1].lower() if "." in domain else ""
    return TLD_RENEWAL_ESTIMATES.get(tld)
