"""Self-bidding collision guard (2026-09-24, the client's #1 fear).

The client bids manually on auctions.godaddy.com AND the sniper bids for him.
His worry: the sniper fires his max on a domain he's ALREADY the high
bidder on, inflating the price against himself ("I'm jacking up my own
price"). This module is the single source of truth for "is the client already
bidding on this listing," read from the availability API's
`memberBiddingStatus` field.

Enum note: the only value we have CONFIRMED (GoDaddy's 2026-08 docs sample)
is "NOT_BIDDING". We have NOT confirmed the positive value(s) (likely
something like HIGH_BIDDER / OUTBID). So the guard is deliberately built to
engage ONLY on a definitive non-NOT_BIDDING signal:

  * None / absent            -> guard stays OUT of the way (sniper normal)
  * "NOT_BIDDING"            -> not engaged (sniper normal)
  * any other non-null value -> the client HAS a bid -> guard engages

This inverts the risk correctly: the only value we must be certain about is
the SAFE one, and we have it. An unknown value can only cause the sniper to
HOLD and alert (safe) — never to misfire against himself. Every observed
value is logged (see live path) so we can refine later, e.g. resume firing
on a confirmed OUTBID once we've seen it in the wild.
"""

from __future__ import annotations

from typing import Optional

# The one confirmed "safe to fire" value (GoDaddy docs sample, 2026-08).
NOT_BIDDING = "NOT_BIDDING"


def is_already_bidding(member_bidding_status: Optional[str]) -> bool:
    """True only when GoDaddy affirmatively reports the client has a bid.

    None/absent or NOT_BIDDING => False (guard stays out of the way).
    """
    if not member_bidding_status:
        return False
    return member_bidding_status.strip().upper() != NOT_BIDDING
