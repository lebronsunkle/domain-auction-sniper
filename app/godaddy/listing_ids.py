"""Real-vs-synthetic listing id discrimination.

History (2026-07-11): GoDaddy's public inventory feeds carry no explicit id
field, so the parser synthesized 63-bit blake2b hashes of the domain — and
the dashboard's "+ Add domain" flow shimmed `Date.now()`. Neither is a real
auction id, and the REST bid endpoint rejects both. We now extract real ids
from the feed's `link` URL, but synthetic ids still exist in three places:

  1. Historical rows in the DB / old watchlist entries (blake2b, ~1e18)
  2. "+ Add domain" entries until the resolver ships (Date.now(), ~1.7e12)
  3. Feed rows whose `link` is missing/malformed (blake2b fallback)

Real GoDaddy auction ids observed in the wild are 9-10 digits (~7e8).
REAL_ID_CEILING gives two orders of magnitude of headroom above that while
sitting well below both synthetic ranges. A blake2b hash landing under the
ceiling by chance is a ~1-in-10^7 event, and the failure mode is benign
(the bid endpoint returns LISTING_NOT_FOUND and we record the error).

Every layer that can fire a real-money bid checks this: the REST client
(last line of defense), the trigger worker (disarm with an explanatory
note), and the watchlist API (reject arming at save time so the user finds
out immediately, not in the final 60 seconds of an auction).
"""

from __future__ import annotations

# One safety margin above the largest observed real id (~7.1e8), far below
# Date.now() (~1.7e12) and blake2b hashes (avg ~4.6e18).
REAL_ID_CEILING = 100_000_000_000  # 1e11


def is_real_listing_id(listing_id: int | None) -> bool:
    """True if this looks like an actual GoDaddy auction id (bid-safe)."""
    return listing_id is not None and 0 < listing_id < REAL_ID_CEILING
