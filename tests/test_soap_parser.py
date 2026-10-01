"""Parser tests against real GoDaddy SOAP responses.

Failure-path fixture is the actual response captured 2026-05-21 — see
the audit-log printout from scripts/smoke_soap_estimate.py.
"""

from __future__ import annotations

import re

from app.godaddy.soap import _extract_inner_result


FAILURE_RESPONSE_OBSERVED = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
    '<soap:Body>'
    '<EstimateCloseoutDomainPriceResponse xmlns="GdAuctionsBiddingWSAPI_v2">'
    '<EstimateCloseoutDomainPriceResult>'
    '&lt;EstimateCloseoutDomainPrice Result="Failure" '
    'Message="Domain name: tru0.com is not currently available on GoDaddy Auctions"/&gt;'
    '</EstimateCloseoutDomainPriceResult>'
    '</EstimateCloseoutDomainPriceResponse>'
    '</soap:Body>'
    '</soap:Envelope>'
)

# Hypothetical success shape — based on the inferred ASMX pattern. Used to
# verify the regex extraction continues to work once we observe a real one.
SUCCESS_RESPONSE_HYPOTHETICAL = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
    '<soap:Body>'
    '<EstimateCloseoutDomainPriceResponse xmlns="GdAuctionsBiddingWSAPI_v2">'
    '<EstimateCloseoutDomainPriceResult>'
    '&lt;EstimateCloseoutDomainPrice Result="Success"&gt;'
    '&lt;listingPrice&gt;5000000&lt;/listingPrice&gt;'
    '&lt;renewalPrice&gt;19990000&lt;/renewalPrice&gt;'
    '&lt;icannFee&gt;180000&lt;/icannFee&gt;'
    '&lt;tax&gt;0&lt;/tax&gt;'
    '&lt;totalPrice&gt;25170000&lt;/totalPrice&gt;'
    '&lt;closeoutDomainPriceKey&gt;abc123-xyz&lt;/closeoutDomainPriceKey&gt;'
    '&lt;/EstimateCloseoutDomainPrice&gt;'
    '</EstimateCloseoutDomainPriceResult>'
    '</EstimateCloseoutDomainPriceResponse>'
    '</soap:Body>'
    '</soap:Envelope>'
)


def test_extract_inner_unwraps_and_unescapes():
    inner = _extract_inner_result(FAILURE_RESPONSE_OBSERVED)
    assert inner.startswith('<EstimateCloseoutDomainPrice ')
    assert 'Result="Failure"' in inner
    assert 'tru0.com' in inner


def test_extract_inner_passthrough_when_no_wrapper():
    # Unwrapped shape from the original training handout — still unescape.
    raw = '<EstimateCloseoutDomainPrice Result="Failure" Message="nope"/>'
    assert _extract_inner_result(raw) == raw


def test_failure_regex_matches_observed_response():
    inner = _extract_inner_result(FAILURE_RESPONSE_OBSERVED)
    m = re.search(
        r'<EstimateCloseoutDomainPrice[^>]*Result="Failure"[^>]*Message="([^"]*)"',
        inner,
    )
    assert m is not None
    assert "tru0.com is not currently available" in m.group(1)


def test_success_field_extraction_against_hypothetical():
    inner = _extract_inner_result(SUCCESS_RESPONSE_HYPOTHETICAL)
    # Verify inner unescape produces parseable tags for our regex matchers.
    assert "<listingPrice>5000000</listingPrice>" in inner
    assert "<closeoutDomainPriceKey>abc123-xyz</closeoutDomainPriceKey>" in inner


# ---------------------------------------------------------------------------
# REAL success response captured 2026-05-29 against optibold.com
# This is THE wire format we have to parse. Pin it as a regression test so
# future "improvements" to the parser don't accidentally break it.
# ---------------------------------------------------------------------------

OPTIBOLD_SUCCESS_RESPONSE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
    '<soap:Body>'
    '<EstimateCloseoutDomainPriceResponse xmlns="GdAuctionsBiddingWSAPI_v2">'
    '<EstimateCloseoutDomainPriceResult>'
    '&lt;EstimateCloseoutDomainPrice Result="Success" Domain="optibold.com" '
    'Price="$5.00" TransferPrice="$10.51" ICANNFee="$0.18" Taxes="$0.39" '
    'PrivateRegistration="N/A" Total="$16.08" '
    'closeoutDomainPriceKey ="NwAwADIAOAAwADQAOQAyADkAfAA1AC4AMAAwAHwAMQA2ADAAOAB8AEYAYQBsAHMAZQB8ADMAOQA3ADEAMgAyADEAOAB8ADkANwAxADEAMQA3ADUAMQB8ADUALwAyADgALwAyADAAMgA2ACAAMgA6ADIANgA6ADAAMAAgAFAATQA=" '
    '/&gt;'
    '</EstimateCloseoutDomainPriceResult>'
    '</EstimateCloseoutDomainPriceResponse>'
    '</soap:Body>'
    '</soap:Envelope>'
)


def test_real_optibold_success_response_parses():
    """Parser must handle the actual wire format:
    attributes on a self-closing <EstimateCloseoutDomainPrice .../> tag,
    prices as dollar strings ('$5.00'), and the closeoutDomainPriceKey
    attribute possibly having whitespace before the `=`."""
    import asyncio
    from dataclasses import dataclass
    from app.godaddy.soap import SoapClient

    @dataclass
    class _FakeResponse:
        text: str
        status_code: int = 200

    class _FakeClient:
        class config:
            soap_endpoint_url = "https://example.invalid"
        async def _request(self, method, url, headers=None, content=None, json=None):
            return _FakeResponse(text=OPTIBOLD_SUCCESS_RESPONSE)

    soap = SoapClient(_FakeClient())
    estimate = asyncio.run(soap.estimate_closeout_price("optibold.com"))

    assert estimate.success is True, f"expected success, got: {estimate.failure_message}"
    assert estimate.domain_name == "optibold.com"
    # Prices arrived as dollar strings; parser should have converted to micros.
    # $5.00 -> 5_000_000 micros, $16.08 -> 16_080_000 micros, etc.
    assert estimate.listing_price_micros == 5_000_000
    assert estimate.renewal_micros == 10_510_000  # TransferPrice
    assert estimate.icann_fee_micros == 180_000
    assert estimate.tax_micros == 390_000
    assert estimate.total_micros == 16_080_000
    # The opaque price key. Pin a substring rather than the whole base64 blob.
    assert estimate.price_key is not None
    assert estimate.price_key.startswith("NwAwADIAOAAwADQAOQA")
    # Helper properties.
    from decimal import Decimal as _D
    assert estimate.total_dollars == _D("16.08")
    assert estimate.listing_price_dollars == _D("5.00")
