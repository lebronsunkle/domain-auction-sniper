"""
CLI wrapper for the inventory sync worker. Run this once to pull the latest
feed(s), score everything, and dump the scored results.

Usage:
    python scripts/run_sync.py                          # closeouts only (default)
    python scripts/run_sync.py --target expiring        # 10-day expiry feed only
    python scripts/run_sync.py --target both            # both feeds, deduped
    python scripts/run_sync.py --output ./out/scored.json
    python scripts/run_sync.py --top 50

The script does NOT require the database to be set up. Output goes to console
(top scored domains + per-type distribution summary) and optionally to a JSON file.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Make project root importable.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.logging_setup import configure_run_logging  # noqa: E402
from worker.inventory_sync import sync_feed  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Pull and score GoDaddy inventory feeds.")
    parser.add_argument(
        "--target",
        choices=["closeouts", "expiring", "both"],
        default="closeouts",
        help=(
            "Which inventory feed(s) to sync. 'closeouts' (default) pulls the "
            "5-day fixed-price window; 'expiring' pulls the 10-day expiry auctions "
            "(visibility only, no purchase in v1); 'both' pulls both and dedupes."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "Path to write the full scored JSON output. Defaults to "
            "./out/scored_<target>.json"
        ),
    )
    parser.add_argument(
        "--top",
        type=int,
        default=50,
        help="How many top-scored domains to print to console (default: 50)",
    )
    parser.add_argument(
        "--no-output",
        action="store_true",
        help="Skip writing the JSON file; print only.",
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="Upsert scored listings into Postgres (requires DB + migrations).",
    )
    parser.add_argument(
        "--check-tld-spread",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Enrich the top N highest-scoring listings with GoDaddy TLD spread data "
            "(how many other TLDs of the same SLD are already registered). "
            "Each SLD = 1 bulk API call. Recommended N: 100-500. Default 0 = off."
        ),
    )
    parser.add_argument(
        "--output-limit",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Only write the top N scored listings to the output JSON file. The "
            "console preview and per-theme breakdown still use the full set. "
            "Use in CI to avoid the 850 MB full-feed JSON. 0 = no limit."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    # Centralized logging: writes to logs/sync/<date>/sync_<HHMMSS>.log plus
    # an errors-only sibling and an API audit JSONL. See app/logging_setup.py.
    log_paths = configure_run_logging("sync", level=args.log_level)
    logging.getLogger(__name__).info(
        "sync starting | target=%s top=%d tld_spread=%d persist=%s output_limit=%d",
        args.target, args.top, args.check_tld_spread, args.persist, args.output_limit,
    )

    output_path = (
        None
        if args.no_output
        else (args.output or Path(f"./out/scored_{args.target}.json"))
    )

    asyncio.run(sync_feed(
        target=args.target,
        output_path=output_path,
        top_n_preview=args.top,
        persist_to_db=args.persist,
        check_tld_spread_for_top_n=args.check_tld_spread,
        output_limit=args.output_limit,
        audit_log_path=log_paths.audit_jsonl,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
