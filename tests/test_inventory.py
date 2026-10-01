"""Parser tests against the live closeouts feed schema.

Rows shaped after real payloads observed on 2026-05-21 — see
`fieldDescription` in closeout_listings.json meta block.
"""

from __future__ import annotations

from decimal import Decimal

from app.godaddy.inventory import (
    _domain_to_listing_id,
    _normalize,
    _to_decimal,
)


def _sample_row(**overrides):
    row = {
        "domainName": "LAKEPOINT.ME",
        "auctionType": "BuyNow",
        "auctionEndTime": "2026-05-21T16:00:00Z",
        "price": "$5",
        "numberOfBids": 0,
        "valuation": "$232",
    }
    row.update(overrides)
    return row


def test_to_decimal_strips_currency_punctuation():
    assert _to_decimal("$5") == Decimal("5")
    assert _to_decimal("$1,200") == Decimal("1200")
    assert _to_decimal("$ 42.50") == Decimal("42.50")
    assert _to_decimal("0.99") == Decimal("0.99")


def test_to_decimal_handles_empty_and_garbage():
    assert _to_decimal(None) is None
    assert _to_decimal("") is None
    assert _to_decimal("null") is None
    assert _to_decimal("$") is None
    assert _to_decimal("not-a-number") is None


def test_listing_id_extracted_from_link_url():
    """The real auction id lives in the `link` URL (discovered 2026-07-11).
    This is THE id the REST bid endpoint requires — everything synthesized
    before this fix was un-biddable."""
    from app.godaddy.inventory import _listing_id_from_link

    assert _listing_id_from_link(
        "https://www.godaddy.com/domain-auctions/anchoredyou-org-710693928?isc=json_expiring"
    ) == 710693928
    assert _listing_id_from_link(
        "https://www.godaddy.com/domain-auctions/zerphi-com-708763421?isc=json_closeouts"
    ) == 708763421
    # Domain with digits: id is still the LAST numeric token.
    assert _listing_id_from_link(
        "https://www.godaddy.com/domain-auctions/4x4parts-com-123456789"
    ) == 123456789
    # Trailing slash variant.
    assert _listing_id_from_link(
        "https://www.godaddy.com/domain-auctions/foo-com-55555555/"
    ) == 55555555
    # No id present -> 0 (caller falls back to hash).
    assert _listing_id_from_link("") == 0
    assert _listing_id_from_link("https://www.godaddy.com/domain-auctions/") == 0
    assert _listing_id_from_link("https://www.godaddy.com/help") == 0


def test_normalize_uses_link_id_over_hash():
    listing = _normalize(
        _sample_row(link="https://www.godaddy.com/domain-auctions/lakepoint-me-710000001?isc=x"),
        default_auction_type="CLOSEOUT",
    )
    assert listing.listing_id == 710000001


def test_normalize_falls_back_to_hash_without_link():
    listing = _normalize(_sample_row(), default_auction_type="CLOSEOUT")
    assert listing.listing_id == _domain_to_listing_id("lakepoint.me")


def test_real_vs_synthetic_id_discrimination():
    from app.godaddy.listing_ids import is_real_listing_id

    assert is_real_listing_id(710693928)          # real feed id
    assert is_real_listing_id(1)                  # tiny but plausible
    assert not is_real_listing_id(None)
    assert not is_real_listing_id(0)
    assert not is_real_listing_id(-5)
    assert not is_real_listing_id(1752130000000)  # Date.now() shim (~1.7e12)
    assert not is_real_listing_id(_domain_to_listing_id("lakepoint.me"))  # blake2b


def test_bid_request_rejects_synthetic_listing_id():
    """Last line of defense: a synthetic id must never serialize into a bid
    payload, no matter what upstream logic missed."""
    import pytest as _pytest

    from app.godaddy.rest import BidRequest

    shim = BidRequest(listing_id=1752130000000, amount_dollars=Decimal("10"))
    with _pytest.raises(ValueError, match="synthetic"):
        shim.to_payload(Decimal("25.00"))

    real = BidRequest(listing_id=710693928, amount_dollars=Decimal("10"))
    payload = real.to_payload(Decimal("25.00"))
    assert payload["listingId"] == 710693928


def test_domain_to_listing_id_is_stable_and_fits_signed_bigint():
    a = _domain_to_listing_id("lakepoint.me")
    b = _domain_to_listing_id("lakepoint.me")
    assert a == b
    assert _domain_to_listing_id("LAKEPOINT.ME") == a  # case-insensitive
    assert a != _domain_to_listing_id("otherdomain.com")
    assert 0 < a < 2**63


def test_normalize_live_closeouts_row():
    listing = _normalize(_sample_row(), default_auction_type="CLOSEOUT")
    assert listing.domain == "lakepoint.me"
    assert listing.tld == "me"
    assert listing.auction_type == "CLOSEOUT"   # "BuyNow" maps to closeout
    assert listing.current_price_dollars == Decimal("5")
    assert listing.estimated_value_dollars == Decimal("232")
    assert listing.has_bids is False
    assert listing.bid_count == 0
    assert listing.listing_id > 0   # synthesized from domain — no feed id


def test_normalize_handles_priced_with_bids():
    listing = _normalize(
        _sample_row(domainName="example.com", price="$40", numberOfBids=3),
        default_auction_type="CLOSEOUT",
    )
    assert listing.current_price_dollars == Decimal("40")
    assert listing.bid_count == 3
    assert listing.has_bids is True


def test_normalize_listing_id_stable_across_runs():
    a = _normalize(_sample_row(), default_auction_type="CLOSEOUT")
    b = _normalize(_sample_row(), default_auction_type="CLOSEOUT")
    assert a.listing_id == b.listing_id  # idempotent -- upsert path relies on this


# ---------------------------------------------------------------------------
# 10-day expiring-auction feed (added 2026-05-21 per the client's scope call)
# ---------------------------------------------------------------------------


def _expiring_sample_row(**overrides):
    """Row shape we'd expect from all_expiring_auctions.json.zip. The exact
    field names from this feed haven't been pinned with a live capture yet --
    we test against the most-likely names and confirm the tolerant parser
    handles them. Update after the first real fetch_expiring() run."""
    row = {
        "domainName": "futurefund.com",
        "auctionType": "PublicAuction",
        "auctionEndTime": "2026-05-31T16:00:00Z",
        "currentBid": "$12",
        "numberOfBids": 3,
        "valuation": "$1450",
    }
    row.update(overrides)
    return row


def test_normalize_expiring_auction_type_publicauction():
    """The expiring feed reports auctionType="PublicAuction". The normalizer
    must map that to EXPIRY_AUCTION so the dashboard can filter on it."""
    listing = _normalize(_expiring_sample_row(), default_auction_type="EXPIRY_AUCTION")
    assert listing.auction_type == "EXPIRY_AUCTION"
    assert listing.domain == "futurefund.com"
    assert listing.current_price_dollars == Decimal("12")
    assert listing.has_bids is True
    assert listing.bid_count == 3


def test_normalize_expiring_auction_type_variants():
    """Several expiring-auction type strings should all map to EXPIRY_AUCTION."""
    for raw_type in ("PublicAuction", "Auction", "ExpiryAuction", "BID", "Expiring"):
        listing = _normalize(
            _expiring_sample_row(auctionType=raw_type),
            default_auction_type="EXPIRY_AUCTION",
        )
        assert listing.auction_type == "EXPIRY_AUCTION", (
            f"auctionType={raw_type!r} should map to EXPIRY_AUCTION"
        )


def test_normalize_expiring_falls_through_to_default_when_unknown_type():
    """Unknown auctionType strings fall through to the default the caller passed
    in -- so calling _normalize with default='EXPIRY_AUCTION' gets EXPIRY_AUCTION
    even when the feed emits a type string we haven't catalogued yet."""
    listing = _normalize(
        _expiring_sample_row(auctionType="SomeNewTypeWeHaveNotSeen"),
        default_auction_type="EXPIRY_AUCTION",
    )
    assert listing.auction_type == "EXPIRY_AUCTION"


def test_normalize_closeout_still_wins_over_expiring_default():
    """If a row explicitly identifies as a closeout, it must not be re-classified
    even if the caller's default is EXPIRY_AUCTION. Defense against future feed
    cross-contamination."""
    listing = _normalize(
        _expiring_sample_row(auctionType="BuyNow"),
        default_auction_type="EXPIRY_AUCTION",
    )
    assert listing.auction_type == "CLOSEOUT"


def test_normalize_currentbid_field_picked_up():
    """Expiring auctions report price as currentBid, not price/AuctionPrice."""
    listing = _normalize(
        _expiring_sample_row(currentBid="$42.50"),
        default_auction_type="EXPIRY_AUCTION",
    )
    assert listing.current_price_dollars == Decimal("42.50")


def test_select_output_listings_reserves_closeouts():
    """2026-08-18: closeouts must survive the top-N cut so the dashboard's
    Buy Now filter isn't empty. Bids dominate high scores; reservation
    guarantees closeout representation."""
    from app.godaddy.inventory import _select_output_listings

    class S:
        def __init__(self, score, atype):
            self.score = score
            self.auction_type = atype

    # 100 high-scoring bids, 20 low-scoring closeouts.
    scored = [S(1000 - i, "EXPIRY_AUCTION") for i in range(100)]
    scored += [S(10 - i * 0.01, "CLOSEOUT") for i in range(20)]
    out = _select_output_listings(scored, output_limit=50, min_closeouts=15)
    closeouts = [s for s in out if s.auction_type == "CLOSEOUT"]
    assert len(out) == 50
    assert len(closeouts) == 15          # reserved slots honored
    assert out == sorted(out, key=lambda s: s.score, reverse=True)


def test_select_output_listings_no_limit_passthrough():
    from app.godaddy.inventory import _select_output_listings

    class S:
        score = 1
        auction_type = "CLOSEOUT"

    rows = [S(), S()]
    assert _select_output_listings(rows, 0) is rows
