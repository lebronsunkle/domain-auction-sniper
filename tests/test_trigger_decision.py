"""Tests for the trigger worker's decision logic.

These exercise just `_should_fire` and `_parse_ladder` — the pure functions
that determine whether to buy. Network and DB are out of scope here; we test
the integration path separately against OTE.

The decision logic is the highest-stakes single function in the system —
a bug here either misses good buys or fires bad ones with real money. We
cover both trigger modes (max_bid, ladder), the gotchas (Decimal
precision, exact-match tolerance), and the no-trigger negative path.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from worker.trigger import _parse_ladder, _should_fire


# ---------------------------------------------------------------------------
# _should_fire
# ---------------------------------------------------------------------------


class TestMaxBidTrigger:
    def test_fires_when_current_price_equals_max_bid(self):
        d = _should_fire(
            current_price=Decimal("25.00"),
            max_bid=Decimal("25.00"),
            ladder=[],
        )
        assert d.fire is True

    def test_fires_when_current_price_below_max_bid(self):
        d = _should_fire(
            current_price=Decimal("12.99"),
            max_bid=Decimal("25.00"),
            ladder=[],
        )
        assert d.fire is True

    def test_holds_when_current_price_above_max_bid(self):
        d = _should_fire(
            current_price=Decimal("30.00"),
            max_bid=Decimal("25.00"),
            ladder=[],
        )
        assert d.fire is False

    def test_holds_when_max_bid_unset_and_no_ladder(self):
        d = _should_fire(
            current_price=Decimal("12.99"),
            max_bid=None,
            ladder=[],
        )
        assert d.fire is False


class TestLadderTrigger:
    def test_fires_on_exact_ladder_match(self):
        d = _should_fire(
            current_price=Decimal("11.00"),
            max_bid=None,
            ladder=[Decimal("50"), Decimal("25"), Decimal("11"), Decimal("5")],
        )
        assert d.fire is True
        assert "ladder" in d.reason

    def test_fires_within_one_cent_tolerance(self):
        d = _should_fire(
            current_price=Decimal("11.005"),  # within 1¢ of 11.00
            max_bid=None,
            ladder=[Decimal("11.00")],
        )
        assert d.fire is True

    def test_holds_when_price_between_ladder_rungs(self):
        d = _should_fire(
            current_price=Decimal("18.00"),  # between 25 and 11
            max_bid=None,
            ladder=[Decimal("25"), Decimal("11")],
        )
        assert d.fire is False


class TestBothModes:
    def test_ladder_rung_above_max_bid_does_not_fire(self):
        # CHANGED 2026-07-11 (audit R4): max_bid is the user's stated
        # ceiling — a ladder rung above it must NOT fire. Previously this
        # test pinned the opposite (ladder overriding the ceiling), which
        # could have spent $50 against a stated $5 max.
        d = _should_fire(
            current_price=Decimal("11.00"),
            max_bid=Decimal("5.00"),
            ladder=[Decimal("11.00")],
        )
        assert d.fire is False
        assert "exceeds max_bid" in d.reason

    def test_max_bid_fires_when_no_ladder_match(self):
        # current = 4 (below both ladder rungs and max_bid)
        d = _should_fire(
            current_price=Decimal("4.00"),
            max_bid=Decimal("10.00"),
            ladder=[Decimal("11.00"), Decimal("5.00")],
        )
        assert d.fire is True
        assert "max_bid" in d.reason


# ---------------------------------------------------------------------------
# _parse_ladder
# ---------------------------------------------------------------------------


class TestParseLadder:
    def test_parses_valid_json_array(self):
        out = _parse_ladder('["50", "25", "11", "5"]')
        assert out == [Decimal("50"), Decimal("25"), Decimal("11"), Decimal("5")]

    def test_handles_numeric_input(self):
        out = _parse_ladder("[50, 25, 11, 5]")
        assert out == [Decimal("50"), Decimal("25"), Decimal("11"), Decimal("5")]

    def test_empty_string_returns_empty_list(self):
        assert _parse_ladder("") == []

    def test_none_returns_empty_list(self):
        assert _parse_ladder(None) == []

    def test_malformed_json_returns_empty_list(self):
        # We're defensive: bad data shouldn't crash the worker. The downstream
        # decision logic just sees an empty ladder.
        assert _parse_ladder("not valid json") == []

    def test_decimal_precision_preserved(self):
        out = _parse_ladder('["11.99", "0.01"]')
        assert out[0] == Decimal("11.99")
        assert out[1] == Decimal("0.01")
