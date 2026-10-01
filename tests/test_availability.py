"""
Tests for the TLD spread / domain availability client.

We don't hit the real GoDaddy API in these tests -- the client's `_request`
method is stubbed to return canned responses. This lets us pin response
parsing, batch splitting, and the spread-count math without burning real
API calls or needing the production key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

import pytest

from app.godaddy.availability import (
    DEFAULT_TLDS_TO_CHECK,
    AvailabilityClient,
    DomainAvailability,
    TLDSpreadResult,
)


# ---------------------------------------------------------------------------
# Stub GoDaddyClient so we can control responses without an HTTP layer.
# ---------------------------------------------------------------------------


@dataclass
class FakeResponse:
    status_code: int
    _text: str

    @property
    def text(self) -> str:
        return self._text

    def json(self) -> Any:
        return json.loads(self._text)


class FakeConfig:
    rest_base_url = "https://api.godaddy.com"


class FakeClient:
    """Minimal stand-in for GoDaddyClient. Captures the last request and
    returns a canned response."""

    def __init__(self):
        self.config = FakeConfig()
        self.requests: list[tuple] = []  # (method, url, json_body)
        self._next_response: Optional[FakeResponse] = None

    def set_response(self, status_code: int, body: dict | list) -> None:
        self._next_response = FakeResponse(
            status_code=status_code, _text=json.dumps(body)
        )

    async def _request(self, method, url, *, headers=None, content=None, json=None):
        self.requests.append((method, url, json))
        return self._next_response


# ---------------------------------------------------------------------------
# check_availability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_availability_parses_response():
    fc = FakeClient()
    fc.set_response(200, {
        "domains": [
            {"domain": "picnic.com",  "available": False, "definitive": True},
            {"domain": "picnic.net",  "available": False, "definitive": True},
            {"domain": "picnic.zzzz", "available": True,  "definitive": True},
        ]
    })
    ac = AvailabilityClient(fc)  # type: ignore[arg-type]

    result = await ac.check_availability(["picnic.com", "picnic.net", "picnic.zzzz"])

    assert "picnic.com" in result
    assert result["picnic.com"].available is False
    assert result["picnic.com"].definitive is True
    assert result["picnic.zzzz"].available is True


@pytest.mark.asyncio
async def test_check_availability_empty_input_returns_empty():
    fc = FakeClient()
    ac = AvailabilityClient(fc)  # type: ignore[arg-type]
    assert await ac.check_availability([]) == {}
    # And no API calls should have been made.
    assert fc.requests == []


@pytest.mark.asyncio
async def test_check_availability_handles_4xx():
    fc = FakeClient()
    fc.set_response(429, {"code": "TOO_MANY_REQUESTS"})
    ac = AvailabilityClient(fc)  # type: ignore[arg-type]
    # Should return empty dict rather than raising -- the caller decides
    # whether to retry the whole list.
    result = await ac.check_availability(["foo.com"])
    assert result == {}


@pytest.mark.asyncio
async def test_check_availability_batches_large_input():
    """When more than BULK_MAX_DOMAINS are passed in, the client should
    split into multiple POSTs and merge results."""
    fc = FakeClient()
    # Hard-code small batch size for the test by patching the class attribute.
    AvailabilityClient.BULK_MAX_DOMAINS = 3  # type: ignore[misc]
    fc.set_response(200, {"domains": [
        {"domain": "a.com", "available": True,  "definitive": True},
        {"domain": "b.com", "available": False, "definitive": True},
        {"domain": "c.com", "available": True,  "definitive": True},
    ]})
    ac = AvailabilityClient(fc)  # type: ignore[arg-type]

    await ac.check_availability(["a.com", "b.com", "c.com", "d.com", "e.com"])

    # Should be 2 batches: [a, b, c] and [d, e].
    assert len(fc.requests) == 2

    # Restore default for other tests.
    AvailabilityClient.BULK_MAX_DOMAINS = 500  # type: ignore[misc]


# ---------------------------------------------------------------------------
# check_tld_spread
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_tld_spread_counts_taken_and_available():
    fc = FakeClient()
    fc.set_response(200, {
        "domains": [
            {"domain": "picnic.com",  "available": False, "definitive": True},
            {"domain": "picnic.net",  "available": False, "definitive": True},
            {"domain": "picnic.org",  "available": False, "definitive": True},
            {"domain": "picnic.io",   "available": True,  "definitive": True},
            {"domain": "picnic.zzzz", "available": True,  "definitive": True},
        ]
    })
    ac = AvailabilityClient(fc)  # type: ignore[arg-type]

    result = await ac.check_tld_spread(
        "picnic", tlds=["com", "net", "org", "io", "zzzz"]
    )
    assert result.sld == "picnic"
    assert result.total_checked == 5
    assert result.taken_count == 3
    assert result.available_count == 2
    assert result.spread_ratio == 3 / 5


@pytest.mark.asyncio
async def test_check_tld_spread_handles_indeterminate():
    """Non-definitive FAST results should land in indeterminate_count, not be
    counted as taken-or-available."""
    fc = FakeClient()
    fc.set_response(200, {
        "domains": [
            {"domain": "picnic.com", "available": False, "definitive": True},
            {"domain": "picnic.net", "available": True,  "definitive": False},
        ]
    })
    ac = AvailabilityClient(fc)  # type: ignore[arg-type]
    result = await ac.check_tld_spread("picnic", tlds=["com", "net"])
    assert result.taken_count == 1
    assert result.available_count == 0
    assert result.indeterminate_count == 1


@pytest.mark.asyncio
async def test_check_tld_spread_handles_missing_rows():
    """If GoDaddy omits a domain from the response (malformed input or
    transient failure), it should land in indeterminate, not crash."""
    fc = FakeClient()
    fc.set_response(200, {
        "domains": [
            {"domain": "picnic.com", "available": False, "definitive": True},
            # picnic.io missing from response
        ]
    })
    ac = AvailabilityClient(fc)  # type: ignore[arg-type]
    result = await ac.check_tld_spread("picnic", tlds=["com", "io"])
    assert result.total_checked == 2
    assert result.taken_count == 1
    assert result.indeterminate_count == 1


@pytest.mark.asyncio
async def test_check_tld_spread_rejects_sld_with_dot():
    fc = FakeClient()
    ac = AvailabilityClient(fc)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="bare SLD"):
        await ac.check_tld_spread("picnic.com")


@pytest.mark.asyncio
async def test_check_tld_spread_dedups_tld_list():
    fc = FakeClient()
    fc.set_response(200, {"domains": [
        {"domain": "picnic.com", "available": False, "definitive": True},
        {"domain": "picnic.net", "available": False, "definitive": True},
    ]})
    ac = AvailabilityClient(fc)  # type: ignore[arg-type]
    result = await ac.check_tld_spread("picnic", tlds=["com", "net", "com", "net"])
    assert result.total_checked == 2  # duplicates removed


def test_default_tlds_includes_preferred_specified_cctlds():
    """The client specifically cited .DE (Germany), .BE (Belgium), .CH (Switzerland)
    on the picnic example. They must be in the default check list."""
    for tld in ("de", "be", "ch", "nl"):
        assert tld in DEFAULT_TLDS_TO_CHECK, f"missing {tld} from default TLDs"


def test_tld_spread_result_spread_ratio():
    r = TLDSpreadResult(
        sld="example",
        total_checked=10,
        taken_count=7,
        available_count=2,
        indeterminate_count=1,
    )
    assert r.spread_ratio == 0.7
    empty = TLDSpreadResult(sld="x", total_checked=0, taken_count=0, available_count=0, indeterminate_count=0)
    assert empty.spread_ratio == 0.0


# ---------------------------------------------------------------------------
# check_tld_spread_many
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_tld_spread_many():
    fc = FakeClient()
    fc.set_response(200, {"domains": [
        {"domain": "foo.com",  "available": False, "definitive": True},
        {"domain": "foo.net",  "available": True,  "definitive": True},
    ]})
    ac = AvailabilityClient(fc)  # type: ignore[arg-type]

    out = await ac.check_tld_spread_many(
        ["foo", "bar"],
        tlds=["com", "net"],
        concurrency=2,
        delay_between_calls_sec=0,
    )
    # Both should be populated even though the canned response is the same;
    # this confirms the loop runs once per SLD.
    assert "foo" in out
    assert "bar" in out
    # Two SLDs -> two batches.
    assert len(fc.requests) == 2
