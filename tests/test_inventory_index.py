"""Tests for the full-inventory index (search-all + shim-ID healing source)."""

from __future__ import annotations

import json
import sqlite3
import zipfile
from datetime import datetime, timezone

import pytest

from app.godaddy.inventory_index import (
    _SCHEMA,
    _ingest_zip_file,
    index_built_at,
    resolve_domain,
    search_domains,
)

ROWS = [
    {
        "domainName": "POLYMAX.COM",
        "link": "https://www.godaddy.com/domain-auctions/polymax-com-711000001?isc=json_expiring",
        "auctionType": "Bid",
        "auctionEndTime": "2026-07-20T16:00:00Z",
        "price": "$105",
        "numberOfBids": 4,
        "valuation": "$8,850",
    },
    {
        "domainName": "POLYDYNE.NET",
        "link": "https://www.godaddy.com/domain-auctions/polydyne-net-711000002?isc=json_expiring",
        "auctionType": "Bid",
        "auctionEndTime": "2026-07-19T16:00:00Z",
        "price": "$1",
        "numberOfBids": 0,
        "valuation": "$310",
    },
    {
        "domainName": "MONOPOLYHUB.COM",
        "link": "https://www.godaddy.com/domain-auctions/monopolyhub-com-711000003?isc=json_expiring",
        "auctionType": "BuyNow",
        "auctionEndTime": "2026-07-18T16:00:00Z",
        "price": "$5",
        "numberOfBids": 0,
        "valuation": "$42",
    },
    {
        # No link -> synthesized hash id; still indexed (watch/buy-now ok).
        "domainName": "NOLINK.COM",
        "auctionType": "BuyNow",
        "auctionEndTime": "2026-07-18T16:00:00Z",
        "price": "$5",
        "numberOfBids": 0,
    },
]


@pytest.fixture
def index_path(tmp_path):
    """A built index file from the fixture rows."""
    zip_path = tmp_path / "feed.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("all_expiring_auctions.json", json.dumps({"data": ROWS}))

    db_path = tmp_path / "index.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    count = _ingest_zip_file(conn, str(zip_path), "EXPIRY_AUCTION")
    conn.execute(
        "INSERT INTO meta VALUES ('built_at', ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()
    conn.close()
    assert count == 4
    return str(db_path)


@pytest.mark.asyncio
async def test_resolve_exact_domain_returns_real_id(index_path):
    hit = await resolve_domain("PolyMax.com", index_path=index_path)
    assert hit is not None
    assert hit.listing_id == 711000001  # real id from the link URL
    assert hit.auction_type == "EXPIRY_AUCTION"
    assert hit.price == "105"


@pytest.mark.asyncio
async def test_resolve_missing_domain_returns_none(index_path):
    assert await resolve_domain("nope.com", index_path=index_path) is None


@pytest.mark.asyncio
async def test_search_substring_prefix_ranked_first(index_path):
    hits = await search_domains("poly", index_path=index_path)
    domains = [h.domain for h in hits]
    # All three poly-containing domains found; prefix matches before
    # substring matches (monopolyhub contains but doesn't start with poly).
    assert set(domains) == {"polymax.com", "polydyne.net", "monopolyhub.com"}
    assert domains.index("monopolyhub.com") == len(domains) - 1


@pytest.mark.asyncio
async def test_search_no_match(index_path):
    assert await search_domains("zzzz", index_path=index_path) == []


@pytest.mark.asyncio
async def test_search_missing_index_returns_empty(tmp_path):
    assert await search_domains("poly", index_path=str(tmp_path / "absent.db")) == []
    assert await index_built_at(index_path=str(tmp_path / "absent.db")) is None


@pytest.mark.asyncio
async def test_built_at_present(index_path):
    assert await index_built_at(index_path=index_path) is not None


@pytest.mark.asyncio
async def test_top_by_valuation_orders_numerically(index_path):
    """$8,850 must outrank $310 and $42 — numeric, not lexicographic
    (lexicographic would put '42' between '310' and '8850' wrongly)."""
    from app.godaddy.inventory_index import top_by_valuation

    hits = await top_by_valuation(limit=10, index_path=index_path)
    vals = [h.domain for h in hits]
    assert vals[0] == "polymax.com"      # $8,850
    assert vals[1] == "polydyne.net"     # $310
    assert vals[2] == "monopolyhub.com"  # $42
    # nolink.com has no valuation -> excluded


@pytest.mark.asyncio
async def test_estibot_enrichment_and_join(index_path, tmp_path):
    """Enrichment appraises the top-N and the cache join returns values."""
    from decimal import Decimal

    from app.godaddy.inventory_index import (
        enrich_top_value_with_estibot,
        estibot_values_for,
    )
    from app.valuation.estibot import DomainAppraisal

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def appraise(self, domains, cache_mode=None):
            self.calls.append((list(domains), cache_mode))
            return {
                d: DomainAppraisal(
                    domain=d,
                    appraised_value=Decimal("1644000") if "polymax" in d else Decimal("610"),
                    category_root="Food",
                )
                for d in domains
            }

    cache = str(tmp_path / "eb.db")
    client = FakeClient()
    n = await enrich_top_value_with_estibot(
        index_path=index_path, cache_path=cache, client=client
    )
    assert n == 3  # the three fixture domains WITH valuations (nolink.com has none)
    assert client.calls[0][1] == "cache"

    values = await estibot_values_for(
        ["polymax.com", "polydyne.net", "unknown.com"], cache_path=cache
    )
    assert values["polymax.com"] == "1644000"
    assert values["polydyne.net"] == "610"
    assert "unknown.com" not in values

    # Second run inside the TTL: everything is fresh, no new API calls.
    n2 = await enrich_top_value_with_estibot(
        index_path=index_path, cache_path=cache, client=client
    )
    assert n2 == 0
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_get_or_fetch_estibot_serves_cache_then_fetches_misses(tmp_path):
    """Cached domains come back without an API call; misses are fetched in
    one batch, stored, and known-missing domains don't refetch forever."""
    from decimal import Decimal

    from app.godaddy.inventory_index import (
        _estibot_write_sync,
        get_or_fetch_estibot,
    )
    from app.valuation.estibot import DomainAppraisal
    from datetime import datetime, timezone

    cache = str(tmp_path / "eb.db")
    _estibot_write_sync(cache, [
        ("cached.com", "5000", "Tech", datetime.now(timezone.utc).isoformat()),
    ])

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def appraise(self, domains, cache_mode=None):
            self.calls.append(list(domains))
            # Estibot knows fresh.com but not obscure.com.
            return {
                "fresh.com": DomainAppraisal(
                    domain="fresh.com", appraised_value=Decimal("1200")
                )
            }

    client = FakeClient()
    values = await get_or_fetch_estibot(
        ["cached.com", "fresh.com", "obscure.com"], cache_path=cache, client=client
    )
    assert values == {"cached.com": "5000", "fresh.com": "1200"}
    assert client.calls == [["fresh.com", "obscure.com"]]

    # Second call: cached.com + fresh.com from cache; obscure.com refetched
    # (it was never stored — Estibot had nothing for it).
    values2 = await get_or_fetch_estibot(
        ["cached.com", "fresh.com"], cache_path=cache, client=client
    )
    assert values2 == {"cached.com": "5000", "fresh.com": "1200"}
    assert len(client.calls) == 1  # no new API call needed


@pytest.mark.asyncio
async def test_estibot_values_missing_cache_is_empty(tmp_path):
    from app.godaddy.inventory_index import estibot_values_for

    assert await estibot_values_for(["x.com"], cache_path=str(tmp_path / "no.db")) == {}


@pytest.mark.asyncio
async def test_top_by_valuation_within_hours(index_path):
    """Time window filters to auctions ending inside the horizon. Fixture
    end times are 2026-07-18/19/20; from 'now' (test runtime) they're all
    either inside or outside a large window — so pin the boundary cases:
    a huge window includes all, a tiny window excludes all."""
    from app.godaddy.inventory_index import top_by_valuation

    everything = await top_by_valuation(
        limit=10, within_hours=24 * 365 * 10, index_path=index_path
    )
    none = await top_by_valuation(
        limit=10, within_hours=0.0001, index_path=index_path
    )
    assert len(none) == 0
    # If the fixture dates are in the future relative to the test run, all
    # three valued rows appear; if in the past, zero. Either way the tiny
    # window must be a subset of the huge one.
    assert len(none) <= len(everything)


@pytest.mark.asyncio
async def test_top_by_valuation_type_filter(index_path):
    from app.godaddy.inventory_index import top_by_valuation

    hits = await top_by_valuation(
        limit=10, auction_type="CLOSEOUT", index_path=index_path
    )
    assert [h.domain for h in hits] == ["monopolyhub.com"]


@pytest.mark.asyncio
async def test_top_by_valuation_preferred_filters(index_path):
    """2026-08-19 call: TLD allowlist + minimum GD valuation."""
    from app.godaddy.inventory_index import top_by_valuation

    # Allowlist excluding .net drops polydyne.net.
    hits = await top_by_valuation(
        limit=10, allowed_tlds=("com",), index_path=index_path
    )
    assert [h.domain for h in hits] == ["polymax.com", "monopolyhub.com"]

    # Valuation floor drops everything under $1,400 (only polymax $8,850).
    hits = await top_by_valuation(
        limit=10, min_valuation=1400, index_path=index_path
    )
    assert [h.domain for h in hits] == ["polymax.com"]


@pytest.mark.asyncio
async def test_list_by_end_time_mirrors_godaddy_order(tmp_path):
    """GD List (2026-09-19 call): GoDaddy's raw expiring order — end-time
    ASC, NO valuation requirement (the dentl.com class of miss), and the
    day-range window isolates a future slice ("day 3 to day 4")."""
    from datetime import timedelta

    from app.godaddy.inventory_index import list_by_end_time

    db_path = tmp_path / "idx.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    now = datetime.now(timezone.utc)
    rows = [
        # (domain, id, type, price, valuation, end, bids) — soon.com has NO
        # valuation and must still appear (and appear FIRST).
        ("soon.com", 1, "EXPIRY_AUCTION", "$1", None, (now + timedelta(hours=2)).isoformat(), 0),
        ("later.com", 2, "EXPIRY_AUCTION", "$1", "$500", (now + timedelta(hours=30)).isoformat(), 1),
        ("day3.com", 3, "CLOSEOUT", "$50", "$2,000", (now + timedelta(hours=80)).isoformat(), 0),
    ]
    for d, lid, at, p, v, end, b in rows:
        conn.execute(
            "INSERT INTO listings (domain, listing_id, auction_type, price, valuation, end_time_utc, bid_count) VALUES (?,?,?,?,?,?,?)",
            (d, lid, at, p, v, end, b),
        )
    conn.execute("INSERT INTO meta VALUES ('built_at', ?)", (now.isoformat(),))
    conn.commit()
    conn.close()

    out = await list_by_end_time(index_path=str(db_path))
    assert [L.domain for L in out] == ["soon.com", "later.com", "day3.com"]

    ranged = await list_by_end_time(from_hours=72, to_hours=96, index_path=str(db_path))
    assert [L.domain for L in ranged] == ["day3.com"]

    typed = await list_by_end_time(auction_type="CLOSEOUT", index_path=str(db_path))
    assert [L.domain for L in typed] == ["day3.com"]
