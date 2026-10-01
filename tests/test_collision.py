"""Self-bidding collision guard (2026-09-24). The only CONFIRMED enum value
is NOT_BIDDING; the guard engages on any other affirmative value and stays
out of the way on None/NOT_BIDDING, so an unknown value can only cause a
safe HOLD, never a misfire."""

from app.godaddy.collision import is_already_bidding


def test_not_bidding_is_safe():
    assert is_already_bidding("NOT_BIDDING") is False
    assert is_already_bidding("not_bidding") is False
    assert is_already_bidding("  NOT_BIDDING  ") is False


def test_absent_stays_out_of_the_way():
    # None/empty: the guard does not engage — the sniper works normally.
    assert is_already_bidding(None) is False
    assert is_already_bidding("") is False


def test_any_affirmative_value_engages():
    # We don't know the exact positive enum, so ANY non-NOT_BIDDING value
    # must engage the guard (safe direction: hold + alert, never misfire).
    for v in ("HIGH_BIDDER", "OUTBID", "WINNING", "LOSING", "BIDDING", "SOMETHING_NEW"):
        assert is_already_bidding(v) is True
