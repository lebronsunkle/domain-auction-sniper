"""
GoDaddy auction inventory feed parser.

Published daily at https://inventory.auctions.godaddy.com/ as zip files.
The available file list is in metadata.json. v1 surfaces TWO feeds:

  - closeouts:   closeout_listings.json.zip
                 5-day fixed-price window, $50 -> $5 ladder.
                 Purchase via the SOAP endpoint.

  - expiring:    all_expiring_auctions.json.zip
                 10-day expiry-auction window. Per the client's 2026-05-21 call,
                 visibility on this list is the highest-leverage daily
                 timesave: he currently spends 30-45 min/day manually
                 scanning expiring domains, and the high-value drops often
                 don't even reach closeout. We surface these for scoring +
                 watchlist but DO NOT auto-purchase them in v1 (auction
                 bidding is deferred — see plan v3, section 2).

Closeout pricing model (per GoDaddy training):
   - 5 day fixed-price window after the 10-day expiry auction ends with no bids
   - Starts at $50, drops toward $5 over the 5 days
   - The total cost we'll actually pay is listing + renewal + ICANN fee + tax
     (see SoapClient.estimate_closeout_price for the breakdown)
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import AsyncIterator, Iterator, Optional

import httpx

logger = logging.getLogger(__name__)

INVENTORY_BASE_URL = "https://inventory.auctions.godaddy.com"


# Known feed files. Add more as we expand scope; v1 only pulls closeouts.
class FeedFile:
    METADATA = "metadata.json"
    CLOSEOUT_JSON = "closeout_listings.json.zip"
    CLOSEOUT_XML = "closeouts.xml.zip"
    ALL_JSON = "all_listings.json.zip"
    EXPIRING_JSON = "all_expiring_auctions.json.zip"
    LISTINGS_WITH_PAGEVIEWS = "listings_with_pageviews.json.zip"  # small file, useful for testing


@dataclass
class InventoryListing:
    """Normalized closeout listing record.

    Field naming inside the GoDaddy feed has drifted historically, so the
    parser is tolerant and routes multiple source names to these targets.
    """

    listing_id: int
    domain: str
    tld: str
    auction_type: str  # always "CLOSEOUT" in this v1 path
    current_price_dollars: Optional[Decimal]
    end_time_utc: Optional[datetime]
    has_bids: bool = False
    bid_count: int = 0
    estimated_value_dollars: Optional[Decimal] = None
    # Domain signals riding along in the feed (2026-08-19, the client's popup
    # asks): Semrush indexed-page count and TLD-spread counts.
    semrush_indexed_pages: Optional[int] = None
    exact_match_tlds: Optional[int] = None
    developed_tlds: Optional[int] = None
    # The client's #1 gauge (May calls: "160k+ searches = high-confidence buy").
    # Free in the feed since GoDaddy's 2026-08 signals update.
    semrush_search_volume: Optional[int] = None
    semrush_cpc: Optional[float] = None
    raw: dict = field(default_factory=dict)

    @property
    def sld(self) -> str:
        return self.domain.split(".")[0] if "." in self.domain else self.domain


class InventoryFetcher:
    """Downloads and parses the daily inventory feed."""

    def __init__(self, base_url: str = INVENTORY_BASE_URL, timeout: float = 120.0):
        self.base_url = base_url
        self.timeout = timeout

    async def fetch_metadata(self) -> dict:
        """Pull metadata.json — the index of available feed files."""
        url = f"{self.base_url}/{FeedFile.METADATA}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.json()

    async def fetch_closeouts(self) -> list[InventoryListing]:
        """Download the closeouts feed, return parsed listings.

        Returns a fully-materialized list rather than an async iterator because
        the closeouts feed is small enough (~20MB compressed) to hold in memory,
        and downstream code (scoring, DB writes) is much easier to test against
        a list than against a streaming iterator.
        """
        return await self._fetch_feed(
            FeedFile.CLOSEOUT_JSON, default_auction_type="CLOSEOUT", label="closeout"
        )

    async def fetch_expiring(self) -> list[InventoryListing]:
        """Download the 10-day expiry-auction feed, return parsed listings.

        These are the domains in the public 10-day expiry-auction window.
        Per the 2026-05-21 call with the client, surfacing this list is the
        single biggest daily-workflow improvement we can ship in v1 — he
        currently spends 30-45 minutes a day scanning this manually.

        v1 SCOPE NOTE: we score and surface these listings (visibility)
        but do NOT auto-purchase them. Public auction bidding is a
        deferred surface in plan v3.
        """
        return await self._fetch_feed(
            FeedFile.EXPIRING_JSON,
            default_auction_type="EXPIRY_AUCTION",
            label="expiring-auction",
        )

    async def _fetch_feed(
        self,
        feed_file: str,
        *,
        default_auction_type: str,
        label: str,
    ) -> list[InventoryListing]:
        """Shared download + parse path for the per-feed fetch methods."""
        url = f"{self.base_url}/{feed_file}"
        logger.info("Downloading %s feed: %s", label, url)
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.content
            logger.info("Downloaded %d bytes (%s)", len(data), label)

        listings = list(self._parse_zip(data, auction_type=default_auction_type))
        logger.info("Parsed %d %s listings", len(listings), label)
        return listings

    def _parse_zip(self, zip_bytes: bytes, *, auction_type: str) -> Iterator[InventoryListing]:
        """Extract files from the zip and route to JSON/CSV/XML parsers."""
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for name in zf.namelist():
                logger.info("Reading inner file: %s", name)
                with zf.open(name) as f:
                    data = f.read()
                if name.endswith(".json"):
                    yield from _parse_json(data, auction_type=auction_type)
                elif name.endswith(".csv"):
                    yield from _parse_csv(data, auction_type=auction_type)
                elif name.endswith(".xml"):
                    logger.warning("XML parsing not implemented (file: %s) — skipping", name)


def _parse_json(data: bytes, *, auction_type: str) -> Iterator[InventoryListing]:
    """Closeouts JSON is typically an array of listing dicts, but some feed
    variants wrap it as {"listings": [...]} or similar."""
    parsed = json.loads(data)
    if isinstance(parsed, dict):
        # Try common wrapper keys.
        for k in ("listings", "Listings", "items", "data"):
            if k in parsed and isinstance(parsed[k], list):
                parsed = parsed[k]
                break
        else:
            # Not a recognized wrapper — log and skip rather than crash.
            logger.warning(
                "Unexpected JSON structure (dict without listings array). Keys: %s",
                list(parsed.keys())[:10],
            )
            return
    for row in parsed:
        if isinstance(row, dict):
            yield _normalize(row, auction_type)


def _parse_csv(data: bytes, *, auction_type: str) -> Iterator[InventoryListing]:
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    for row in reader:
        if row:
            yield _normalize(row, auction_type)


def _normalize(row: dict, default_auction_type: str) -> InventoryListing:
    """Map a feed row into our InventoryListing shape. Tolerant of name drift."""
    domain = (
        row.get("DomainName")
        or row.get("domain")
        or row.get("Domain")
        or row.get("domainName")
        or ""
    ).lower().strip()

    tld = ""
    if "." in domain:
        tld = domain.rsplit(".", 1)[-1]

    listing_id_raw = (
        row.get("AuctionId")
        or row.get("ListingId")
        or row.get("listingId")
        or row.get("auctionId")
        or row.get("id")
    )
    if listing_id_raw is None:
        # No explicit id field — but the REAL auction id is embedded in the
        # listing's `link` URL (confirmed 2026-07-11 against both live feeds):
        #   "link": ".../domain-auctions/anchoredyou-org-710693928?isc=..."
        # This id is what the REST bid endpoint requires. Before this was
        # discovered we synthesized blake2b hashes for EVERY listing, which
        # meant expiry-auction sniping could never have worked (the bid
        # endpoint rejects unknown listing ids).
        listing_id = _listing_id_from_link(
            row.get("link") or row.get("Link") or row.get("url") or ""
        )
        if not listing_id:
            # Last resort: stable 63-bit hash so the upsert-by-id path still
            # works for bookkeeping. NOT usable for bidding — the guards in
            # app/godaddy/listing_ids.py keep these away from the bid endpoint.
            listing_id = _domain_to_listing_id(domain) if domain else 0
    else:
        try:
            listing_id = int(listing_id_raw)
        except (ValueError, TypeError):
            listing_id = _domain_to_listing_id(domain) if domain else 0

    # Infer auction type from the row if present; otherwise fall back to default.
    # Feed-specific quirks we've observed:
    #   - Closeouts feed reports auctionType="BuyNow" — that's a closeout.
    #   - Expiring-auctions feed may report "PublicAuction", "Auction",
    #     "ExpiryAuction", or similar — all of those are EXPIRY_AUCTION here.
    # When unrecognized, fall through to the default the caller passed in
    # (which is what _fetch_feed sets per feed source).
    raw_type = (row.get("AuctionType") or row.get("Type") or row.get("auctionType") or "").upper()
    if "CLOSEOUT" in raw_type or "CLOSE_OUT" in raw_type or "BUYNOW" in raw_type:
        auction_type = "CLOSEOUT"
    elif (
        "EXPIR" in raw_type
        or "PUBLICAUCTION" in raw_type
        or raw_type == "AUCTION"
        or raw_type == "BID"
    ):
        auction_type = "EXPIRY_AUCTION"
    else:
        auction_type = default_auction_type

    price = _to_decimal(
        row.get("AuctionPrice")
        or row.get("Price")
        or row.get("StartPrice")
        or row.get("price")
        or row.get("currentPrice")
        or row.get("currentBid")
        or row.get("CurrentBid")
    )

    end = (
        row.get("AuctionEndTime")
        or row.get("EndTime")
        or row.get("endTime")
        or row.get("auctionEndTime")
    )
    end_time = _to_datetime_utc(end)

    bid_count = _to_int(
        row.get("NumberOfBids")
        or row.get("numberOfBids")
        or row.get("BidCount")
        or row.get("bidCount")
        or 0
    )

    estimated = _to_decimal(
        row.get("EstimatedValue")
        or row.get("GdValue")
        or row.get("ValueEstimate")
        or row.get("estimatedValue")
        or row.get("valuation")
    )

    # NB: we intentionally do NOT populate `raw` here. Keeping the full feed
    # row attached to every InventoryListing object eats ~1.5 GB across the
    # 935k-row expiring feed and OOM-killed the GH Actions runner. The `raw`
    # field still exists on the dataclass (default empty dict) for the rare
    # case where a caller wants to inspect the original payload; just pass
    # the row in directly at the call site.
    def _opt_int(*names):
        for n in names:
            v = row.get(n)
            if v not in (None, "", "null"):
                try:
                    return int(v)
                except (TypeError, ValueError):
                    continue
        return None

    return InventoryListing(
        listing_id=listing_id,
        domain=domain,
        tld=tld,
        auction_type=auction_type,
        current_price_dollars=price,
        end_time_utc=end_time,
        has_bids=bid_count > 0,
        bid_count=bid_count,
        estimated_value_dollars=estimated,
        semrush_indexed_pages=_opt_int("semrushIndexedPages", "SemrushIndexedPages", "indexedPages"),
        exact_match_tlds=_opt_int("exactMatchTlds", "ExactMatchTlds"),
        developed_tlds=_opt_int("developedTlds", "DevelopedTlds"),
        semrush_search_volume=_opt_int("semrushSearchVolume", "SemrushSearchVolume"),
        semrush_cpc=_opt_float(row, "semrushCpc", "SemrushCpc"),
    )


def _opt_float(row: dict, *names) -> Optional[float]:
    for n in names:
        v = row.get(n)
        if v not in (None, "", "null"):
            try:
                return float(str(v).replace("$", "").replace(",", ""))
            except (TypeError, ValueError):
                continue
    return None


_DECIMAL_STRIP = re.compile(r"[\$,\s]")


def _to_decimal(value) -> Optional[Decimal]:
    if value in (None, "", "null"):
        return None
    # Feed serializes money as strings like "$5" / "$1,200" — strip currency punctuation.
    cleaned = _DECIMAL_STRIP.sub("", str(value))
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except Exception:
        return None


def _listing_id_from_link(link: str) -> int:
    """Extract the real GoDaddy auction id from a listing page URL.

    Observed shape (2026-07-11):
        https://www.godaddy.com/domain-auctions/anchoredyou-org-710693928?isc=json_expiring
    The id is the final dash-separated numeric token of the path. Domains
    containing digits (e.g. 4x4parts-com-123456789) are safe: we anchor on
    the LAST numeric token immediately before end/query/fragment.

    Returns 0 when no id can be found (caller falls back to the hash).
    """
    if not link:
        return 0
    # Strip query/fragment, then take the last path segment.
    path = link.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    last_segment = path.rsplit("/", 1)[-1]
    m = re.search(r"-(\d{5,12})$", last_segment)
    if not m:
        return 0
    try:
        return int(m.group(1))
    except ValueError:  # pragma: no cover — regex guarantees digits
        return 0


def _select_output_listings(scored, output_limit: int, min_closeouts: int = 1500):
    """Pick which scored listings get written to the dashboard JSON.

    2026-08-18: score-ranked top-N systematically buried closeouts (bids
    dominate the high scores), leaving the client's "Buy Now only" filter
    empty. When a limit applies, reserve slots for the top-scored
    closeouts: final list = top overall + top closeouts, deduped, capped
    at output_limit, score-sorted.
    """
    if not output_limit or output_limit <= 0 or len(scored) <= output_limit:
        return scored
    closeouts = [s for s in scored if s.auction_type == "CLOSEOUT"][:min_closeouts]
    chosen = {id(s) for s in closeouts}
    out = list(closeouts)
    for s in scored:
        if len(out) >= output_limit:
            break
        if id(s) not in chosen:
            out.append(s)
    out.sort(key=lambda s: s.score, reverse=True)
    return out[:output_limit]


def _domain_to_listing_id(domain: str) -> int:
    """Stable 63-bit int derived from the (lowercased) domain.

    Used when the feed carries no id field (closeouts feed). Fits in a signed
    BigInteger column and preserves the unique-by-id upsert path.
    """
    h = hashlib.blake2b(domain.lower().encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(h, "big") >> 1


def _to_int(value) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def _to_datetime_utc(value) -> Optional[datetime]:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Common GoDaddy formats: ISO 8601 with Z, or "YYYY-MM-DD HH:MM:SS"
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    logger.warning("Could not parse datetime: %r", value)
    return None
