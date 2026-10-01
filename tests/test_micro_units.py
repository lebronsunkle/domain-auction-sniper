"""
Tests for the dollars <-> micro-units conversion helper.

This is the most important test file in the project. If these tests pass, no
malformed amount can reach the GoDaddy bid endpoint through this helper. If
they ever start failing, do not deploy.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.godaddy.micro_units import (
    MAX_REASONABLE_DOLLARS,
    MICROS_PER_DOLLAR,
    MicroUnitsError,
    assert_within_transaction_cap,
    dollars_to_micros,
    micros_to_dollars,
    set_audit_hook,
)


# ---------------------------------------------------------------------------
# Conversion factor confirmed against Swagger spec example.
# ---------------------------------------------------------------------------

def test_conversion_factor_is_one_million():
    assert MICROS_PER_DOLLAR == 1_000_000


def test_swagger_example_one_hundred_dollars():
    # From the official Swagger spec example: bidAmountUsd: 100000000 = $100.
    assert dollars_to_micros(100) == 100_000_000


# ---------------------------------------------------------------------------
# Happy path: every typical input shape works.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("amount, expected_micros", [
    (1, 1_000_000),
    (10, 10_000_000),
    (34, 34_000_000),                 # min bid increment from training transcript
    (50, 50_000_000),                 # closeout starting price
    (100, 100_000_000),
    (1_000, 1_000_000_000),
    (2_600, 2_600_000_000),           # The client's $2,600 bid from the cautionary story
    (5_000, 5_000_000_000),           # typical per-tx cap
    (10_000, 10_000_000_000),
])
def test_dollars_to_micros_integers(amount, expected_micros):
    assert dollars_to_micros(amount) == expected_micros


@pytest.mark.parametrize("amount, expected_micros", [
    ("1", 1_000_000),
    ("100", 100_000_000),
    ("2600", 2_600_000_000),
    ("2,600", 2_600_000_000),         # with thousands separator
    ("$2,600", 2_600_000_000),        # with dollar sign
    ("$2,600.00", 2_600_000_000),
    ("  100  ", 100_000_000),         # with whitespace
])
def test_dollars_to_micros_strings(amount, expected_micros):
    assert dollars_to_micros(amount) == expected_micros


@pytest.mark.parametrize("amount, expected_micros", [
    (1.50, 1_500_000),
    (34.99, 34_990_000),
    (100.01, 100_010_000),
    (2599.99, 2_599_990_000),
])
def test_dollars_to_micros_floats(amount, expected_micros):
    assert dollars_to_micros(amount) == expected_micros


@pytest.mark.parametrize("amount", [
    Decimal("1.00"),
    Decimal("100.50"),
    Decimal("9999.99"),
])
def test_dollars_to_micros_decimals(amount):
    # Decimal inputs should round-trip cleanly through the conversion.
    result = dollars_to_micros(amount)
    assert micros_to_dollars(result) == amount.quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Rejection cases: malformed, zero, negative, above ceiling, non-finite.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_input", [
    0,
    0.0,
    "0",
    -1,
    -100,
    "-50",
    Decimal("-0.01"),
])
def test_dollars_to_micros_rejects_zero_and_negative(bad_input):
    with pytest.raises(MicroUnitsError, match="must be positive"):
        dollars_to_micros(bad_input)


@pytest.mark.parametrize("bad_input", [
    "not a number",
    "abc",
    "$$$",
    "1.2.3",
    "",
    "   ",
])
def test_dollars_to_micros_rejects_unparseable_strings(bad_input):
    with pytest.raises(MicroUnitsError):
        dollars_to_micros(bad_input)


@pytest.mark.parametrize("bad_input", [
    None,
    [],
    {},
    object(),
])
def test_dollars_to_micros_rejects_wrong_types(bad_input):
    with pytest.raises(MicroUnitsError, match="Unsupported"):
        dollars_to_micros(bad_input)


def test_dollars_to_micros_rejects_nan():
    with pytest.raises(MicroUnitsError, match="finite"):
        dollars_to_micros(float("nan"))


def test_dollars_to_micros_rejects_infinity():
    with pytest.raises(MicroUnitsError, match="finite"):
        dollars_to_micros(float("inf"))


def test_dollars_to_micros_rejects_above_ceiling():
    # The ceiling is $1,000,000. Test exactly at it (allowed) and just above.
    assert dollars_to_micros(MAX_REASONABLE_DOLLARS) > 0
    with pytest.raises(MicroUnitsError, match="exceeds absolute safety ceiling"):
        dollars_to_micros(MAX_REASONABLE_DOLLARS + Decimal("0.01"))
    with pytest.raises(MicroUnitsError):
        dollars_to_micros(10_000_000)


# ---------------------------------------------------------------------------
# Rounding: half-up at the cent level. No fractions of a cent reach the API.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("amount, expected_micros", [
    (1.005, 1_010_000),    # rounds up
    (1.004, 1_000_000),    # rounds down
    (1.499, 1_500_000),    # rounds up
    (1.494, 1_490_000),    # rounds down
])
def test_rounding_to_nearest_cent(amount, expected_micros):
    assert dollars_to_micros(amount) == expected_micros


# ---------------------------------------------------------------------------
# The cautionary story: $2,600 must never silently become $26,000.
# ---------------------------------------------------------------------------

def test_the_preferred_story_two_thousand_six_hundred():
    """
    From the training transcript: the client meant to bid $2,600 but somehow
    placed a $26,000 bid. The system can't catch a typo, but it CAN ensure
    that the typed value is the value sent. This test pins that promise:
    $2,600 means 2_600_000_000 micros, never more.
    """
    assert dollars_to_micros(2600) == 2_600_000_000
    assert dollars_to_micros("2600") == 2_600_000_000
    assert dollars_to_micros(2600.00) == 2_600_000_000

    # And $26,000 is clearly an order-of-magnitude different in the output.
    assert dollars_to_micros(26000) == 26_000_000_000
    assert dollars_to_micros(2600) * 10 == dollars_to_micros(26000)


def test_per_tx_cap_blocks_the_typo():
    """
    If the per-transaction cap is set to $5,000 (default), an accidental
    $26,000 bid is rejected before micro conversion. This is the second
    layer of defense behind the safety governors module.
    """
    cap = Decimal("5000.00")

    # $2,600 should pass.
    assert_within_transaction_cap(2600, cap)

    # $26,000 should be rejected.
    with pytest.raises(MicroUnitsError, match="exceeds per-transaction cap"):
        assert_within_transaction_cap(26000, cap)


# ---------------------------------------------------------------------------
# Round-trip integrity.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("amount", [
    Decimal("1.00"),
    Decimal("34.99"),
    Decimal("100.00"),
    Decimal("2600.00"),
    Decimal("9999.99"),
])
def test_round_trip(amount):
    micros = dollars_to_micros(amount)
    back = micros_to_dollars(micros)
    assert back == amount


# ---------------------------------------------------------------------------
# Reverse conversion.
# ---------------------------------------------------------------------------

def test_micros_to_dollars_rejects_non_int():
    with pytest.raises(MicroUnitsError, match="must be an int"):
        micros_to_dollars(1.5)  # type: ignore[arg-type]
    with pytest.raises(MicroUnitsError, match="must be an int"):
        micros_to_dollars("1000000")  # type: ignore[arg-type]


def test_micros_to_dollars_rejects_negative():
    with pytest.raises(MicroUnitsError, match="non-negative"):
        micros_to_dollars(-1)


def test_micros_to_dollars_zero_allowed():
    # Zero is allowed on the reverse direction because we read back from API
    # responses which can include zero amounts in some error paths.
    assert micros_to_dollars(0) == Decimal("0.00")


# ---------------------------------------------------------------------------
# Audit hook is called.
# ---------------------------------------------------------------------------

def test_audit_hook_receives_conversions():
    captured = []

    def hook(op, dollars, micros):
        captured.append((op, dollars, micros))

    set_audit_hook(hook)
    try:
        dollars_to_micros(100)
        micros_to_dollars(50_000_000)
        assert len(captured) == 2
        assert captured[0] == ("dollars_to_micros", Decimal("100.00"), 100_000_000)
        assert captured[1] == ("micros_to_dollars", Decimal("50.00"), 50_000_000)
    finally:
        set_audit_hook(None)  # type: ignore[arg-type]
