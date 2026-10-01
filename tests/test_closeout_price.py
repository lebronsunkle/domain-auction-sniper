"""
Tests for the closeout price ladder predictor.

Pins the documented behavior: prices follow the published $50 -> $5 ladder
over 5 days. The predictor is what lets us know *when* to fire a purchase
trigger without polling GoDaddy.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.godaddy.closeout_price import (
    CLOSEOUT_WINDOW_DAYS,
    LADDER,
    is_in_closeout_window,
    next_price_drop,
    price_at,
    time_price_hits,
)


# ---------------------------------------------------------------------------
# Ladder structure
# ---------------------------------------------------------------------------


def test_ladder_starts_at_fifty():
    assert LADDER[0] == (0, Decimal("50.00"))


def test_ladder_ends_at_five():
    assert LADDER[-1][1] == Decimal("5.00")


def test_ladder_monotonically_decreasing():
    prices = [step[1] for step in LADDER]
    assert all(prices[i] >= prices[i + 1] for i in range(len(prices) - 1))


def test_window_is_five_days():
    assert CLOSEOUT_WINDOW_DAYS == 5


# ---------------------------------------------------------------------------
# Price at time
# ---------------------------------------------------------------------------


START = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("days_after_start, expected_price", [
    (0, Decimal("50.00")),
    (1, Decimal("35.00")),
    (2, Decimal("20.00")),
    (3, Decimal("11.00")),
    (4, Decimal("5.00")),
    (5, Decimal("5.00")),
])
def test_price_at_each_ladder_step(days_after_start: int, expected_price: Decimal):
    when = START + timedelta(days=days_after_start)
    result = price_at(START, when)
    assert result is not None
    assert result.price_dollars == expected_price


def test_price_before_start_returns_none():
    result = price_at(START, START - timedelta(hours=1))
    assert result is None


def test_price_after_window_returns_none():
    result = price_at(START, START + timedelta(days=6))
    assert result is None


def test_price_mid_day_uses_previous_step():
    """Halfway through day 1 (so 1.5 days in), price should still be the day-1
    step ($35), not the day-2 step ($20). Pins floor-step semantics."""
    when = START + timedelta(days=1, hours=12)
    result = price_at(START, when)
    assert result.price_dollars == Decimal("35.00")
    assert result.days_into_window == 1


# ---------------------------------------------------------------------------
# Time when target price is hit
# ---------------------------------------------------------------------------


def test_target_at_starting_price_returns_start_time():
    assert time_price_hits(START, Decimal("50.00")) == START


def test_target_above_starting_price_returns_start_time():
    """Already at/below target; fire immediately."""
    assert time_price_hits(START, Decimal("75.00")) == START


def test_target_eleven_dollars_hits_on_day_three():
    assert time_price_hits(START, Decimal("11.00")) == START + timedelta(days=3)


def test_target_between_steps_hits_at_next_step_below():
    """Target $7 is between $11 (day 3) and $5 (day 4) — we should fire when
    price drops to $5 on day 4 because that's the first step that's <= $7."""
    assert time_price_hits(START, Decimal("7.00")) == START + timedelta(days=4)


def test_target_below_floor_never_hits():
    assert time_price_hits(START, Decimal("2.00")) is None


def test_target_exactly_at_floor_hits_at_floor_step():
    assert time_price_hits(START, Decimal("5.00")) == START + timedelta(days=4)


# ---------------------------------------------------------------------------
# Window membership
# ---------------------------------------------------------------------------


def test_in_window_immediately_after_start():
    assert is_in_closeout_window(START, START + timedelta(minutes=1))


def test_in_window_on_last_day():
    assert is_in_closeout_window(START, START + timedelta(days=5))


def test_not_in_window_before_start():
    assert not is_in_closeout_window(START, START - timedelta(hours=1))


def test_not_in_window_after_end():
    assert not is_in_closeout_window(START, START + timedelta(days=6))


# ---------------------------------------------------------------------------
# Next price drop
# ---------------------------------------------------------------------------


def test_next_drop_from_just_after_start_is_day_one():
    drop = next_price_drop(START, START + timedelta(hours=1))
    assert drop is not None
    drop_time, price = drop
    assert drop_time == START + timedelta(days=1)
    assert price == Decimal("35.00")


def test_next_drop_from_mid_day_three_is_day_four():
    drop = next_price_drop(START, START + timedelta(days=3, hours=12))
    assert drop is not None
    drop_time, price = drop
    assert drop_time == START + timedelta(days=4)
    assert price == Decimal("5.00")


def test_no_more_drops_after_floor_reached():
    """Once we're past day 4 (the floor step), there are no more drops because
    day 5 has the same price as day 4."""
    # The ladder repeats the $5 step on day 5, so after day 5 there are no drops.
    drop = next_price_drop(START, START + timedelta(days=5, hours=1))
    assert drop is None
