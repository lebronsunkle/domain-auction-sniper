"""
Closeout price ladder predictor.

GoDaddy publishes a known fixed-price schedule for closeouts:
   - Start of closeout window: $50
   - Drops daily over 5 days
   - End of window (day 5): $5
   - After day 5: domain leaves closeout state

Because the schedule is published and deterministic, we DO NOT need to poll
GoDaddy to know what a closeout's price will be at any future moment — we
compute it. This is the foundation of the trigger worker: it lets us know
exactly when each watchlist target price will be hit, so we can schedule
ourselves to fire the purchase API call at that precise moment instead of
polling continuously (which would get us rate-limited or IP-blocked).

The exact ladder shape was confirmed in Mihai's training and the GoDaddy
help docs. As of v1 we assume:
   Day 0 (start)        $50.00
   Day 1                $35.00  (interpolated; will be refined when we
   Day 2                $20.00   see real closeout pricing in production)
   Day 3                $11.00
   Day 4                  $5.00
   Day 5 (end)            $5.00

If GoDaddy's actual ladder differs, update LADDER below — only one constant
needs to change, the predictor logic stays the same.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional


# Ladder steps: (days_after_start, price_dollars).
# Sorted by days ascending. Predictor uses the last step whose threshold
# has been crossed.
LADDER: list[tuple[int, Decimal]] = [
    (0, Decimal("50.00")),
    (1, Decimal("35.00")),
    (2, Decimal("20.00")),
    (3, Decimal("11.00")),
    (4, Decimal("5.00")),
    (5, Decimal("5.00")),  # final day; same as day 4 floor
]

CLOSEOUT_WINDOW_DAYS = 5


@dataclass
class PriceAtTime:
    timestamp_utc: datetime
    price_dollars: Decimal
    days_into_window: int


def price_at(start_time_utc: datetime, when_utc: datetime) -> Optional[PriceAtTime]:
    """Return the predicted price at `when_utc` for a closeout that started
    at `start_time_utc`. Returns None if `when_utc` is before the closeout
    started, or after the closeout window has ended.
    """
    if when_utc < start_time_utc:
        return None

    days_in = (when_utc - start_time_utc).days
    if days_in > CLOSEOUT_WINDOW_DAYS:
        return None  # closeout window has ended

    # Find the LAST ladder step whose threshold is <= days_in.
    matched_price = LADDER[0][1]
    for threshold_days, price in LADDER:
        if days_in >= threshold_days:
            matched_price = price
        else:
            break

    return PriceAtTime(
        timestamp_utc=when_utc,
        price_dollars=matched_price,
        days_into_window=days_in,
    )


def time_price_hits(
    start_time_utc: datetime,
    target_price_dollars: Decimal,
) -> Optional[datetime]:
    """Return the UTC time at which the closeout's price drops to or below
    `target_price_dollars`. Returns None if the target is above the starting
    price (would already have been hit) or below the floor price (will never
    be hit within the window).

    The returned time is the moment of the price step transition — what we'd
    use to schedule a trigger.
    """
    if target_price_dollars >= LADDER[0][1]:
        # Target is >= start price, meaning the price is already at or below
        # the target right now. Return the start time so the trigger fires
        # immediately.
        return start_time_utc

    if target_price_dollars < LADDER[-1][1]:
        # Target is below the floor — the price will never get that low.
        return None

    # Find the first ladder step where the step's price is <= target.
    for threshold_days, price in LADDER:
        if price <= target_price_dollars:
            return start_time_utc + timedelta(days=threshold_days)

    return None


def is_in_closeout_window(start_time_utc: datetime, now_utc: Optional[datetime] = None) -> bool:
    """Has this closeout already ended? Useful for filtering stale watchlist entries."""
    now_utc = now_utc or datetime.now(timezone.utc)
    if now_utc < start_time_utc:
        return False
    if now_utc > start_time_utc + timedelta(days=CLOSEOUT_WINDOW_DAYS):
        return False
    return True


def next_price_drop(
    start_time_utc: datetime,
    now_utc: Optional[datetime] = None,
) -> Optional[tuple[datetime, Decimal]]:
    """Return (when, new_price) for the next price drop after `now_utc`.
    Returns None if no more drops will happen (closeout has ended or is on
    its last day)."""
    now_utc = now_utc or datetime.now(timezone.utc)
    for threshold_days, price in LADDER:
        drop_time = start_time_utc + timedelta(days=threshold_days)
        if drop_time > now_utc:
            return (drop_time, price)
    return None
