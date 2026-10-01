"""
Long-running scheduler for the daily inventory sync.

Fires worker.inventory_sync.sync_feed() on a cron schedule -- by default
once per day at 8:30 AM Pacific Time. GoDaddy publishes the inventory
feed around 7-8 AM PST, so 8:30 gives a 30-60 minute buffer for the
feed to be fully available.

Run with:
    python scripts/run_scheduler.py

The scheduler keeps running in the foreground; stop it with Ctrl-C.
For a persistent setup on macOS, wrap it in launchd (see
docs/scheduler-setup.md) or run in tmux/screen.

For one-off testing without waiting until tomorrow:
    python scripts/run_scheduler.py --run-now

The scheduler does NOT require Postgres. By default the daily run writes
to a JSON file at out/scored_<target>_<date>.json. Pass --persist to also
upsert into the auctions table (DB must be up + migrated).

Schedule examples:
    --schedule "8:30 PT"     daily at 8:30 AM Pacific (default)
    --schedule "13:00 PT"    daily at 1:00 PM Pacific
    --schedule "8:30 UTC"    daily at 8:30 AM UTC
    --schedule "*/6 hours"   every 6 hours starting from now

Mac sleep note: if the laptop is asleep at the scheduled time, the job
won't fire. The --run-on-startup-if-stale flag catches that case: when
the scheduler starts, it checks the last successful run timestamp and
fires an immediate sync if the last one was >stale_hours ago.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import re
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # noqa: E402
from apscheduler.triggers.cron import CronTrigger  # noqa: E402
from apscheduler.triggers.interval import IntervalTrigger  # noqa: E402

from worker.inventory_sync import sync_feed  # noqa: E402


logger = logging.getLogger("scheduler")


# State file -- tracks last successful run so --run-on-startup-if-stale works
# across restarts. Lives next to the output JSON so the working directory
# is the same as run_sync.py uses.
DEFAULT_STATE_FILE = REPO_ROOT / "out" / "scheduler_state.json"


# ---------------------------------------------------------------------------
# Schedule parsing
# ---------------------------------------------------------------------------


_TZ_ALIASES = {
    "PT": "America/Los_Angeles",
    "PST": "America/Los_Angeles",  # PST/PDT both resolve to LA; zoneinfo handles DST
    "PDT": "America/Los_Angeles",
    "MT": "America/Denver",
    "CT": "America/Chicago",
    "ET": "America/New_York",
    "UTC": "UTC",
    "GMT": "UTC",
}


def parse_schedule(spec: str):
    """Return an APScheduler trigger parsed from a human-friendly string.

    Supports:
      "HH:MM TZ"        -> CronTrigger at that local time daily
      "every N <unit>"  -> IntervalTrigger; unit in seconds|minutes|hours|days
      "*/N <unit>"      -> same as above (cron-ish shorthand)
    """
    spec = spec.strip()

    # "8:30 PT" / "13:00 UTC"
    m = re.match(r"^(\d{1,2}):(\d{2})\s+([A-Z]+)$", spec)
    if m:
        hour, minute, tz_label = int(m.group(1)), int(m.group(2)), m.group(3)
        tz_name = _TZ_ALIASES.get(tz_label, tz_label)
        return CronTrigger(hour=hour, minute=minute, timezone=ZoneInfo(tz_name))

    # "every 6 hours" / "*/30 minutes" / "every 90 seconds"
    m = re.match(r"^(?:every|\*/)\s*(\d+)\s+(seconds?|minutes?|hours?|days?)$", spec)
    if m:
        n, unit = int(m.group(1)), m.group(2).rstrip("s")
        kwargs = {f"{unit}s": n}
        return IntervalTrigger(**kwargs)

    raise ValueError(
        f"Unrecognized schedule spec: {spec!r}. "
        "Examples: '8:30 PT', '13:00 UTC', 'every 6 hours', '*/30 minutes'"
    )


# ---------------------------------------------------------------------------
# State persistence (last-run timestamp for stale detection)
# ---------------------------------------------------------------------------


def load_last_run(state_file: Path) -> Optional[datetime]:
    import json
    if not state_file.exists():
        return None
    try:
        data = json.loads(state_file.read_text())
        s = data.get("last_successful_run_utc")
        if not s:
            return None
        return datetime.fromisoformat(s)
    except (ValueError, OSError):
        return None


def save_last_run(state_file: Path, when_utc: datetime) -> None:
    import json
    state_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_successful_run_utc": when_utc.isoformat(),
        "schema_version": 1,
    }
    state_file.write_text(json.dumps(payload, indent=2))


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------


async def run_sync_job(
    target: str,
    top_n_preview: int,
    check_tld_spread_for_top_n: int,
    persist_to_db: bool,
    state_file: Path,
    output_dir: Path,
) -> None:
    """The scheduled job. Wraps sync_feed with date-stamped output paths
    and last-run-timestamp persistence. Each scheduled fire gets its OWN
    per-run log subdirectory under logs/sync/ so a long-running scheduler
    accumulates one trail per day.
    """
    from app.logging_setup import configure_run_logging  # local: avoid import on tests

    started = datetime.now(timezone.utc)
    date_stamp = started.strftime("%Y%m%d")
    output_path = output_dir / f"scored_{target}_{date_stamp}.json"

    # Per-fire log subdirectory under logs/sync/. Distinct from the
    # scheduler's own log (which captures orchestration events like
    # "fired", "caught up", etc.). This one is the sync job's own trail.
    run_paths = configure_run_logging(
        "sync",
        run_id=f"sync_{started.strftime('%H%M%S')}",
        also_stdout=False,  # scheduler already logs to stdout
    )

    logger.info(
        "scheduled sync starting | target=%s | top=%d | tld_spread=%d | persist=%s | logs=%s",
        target, top_n_preview, check_tld_spread_for_top_n, persist_to_db,
        run_paths.base_dir,
    )
    try:
        await sync_feed(
            target=target,
            output_path=output_path,
            top_n_preview=top_n_preview,
            persist_to_db=persist_to_db,
            check_tld_spread_for_top_n=check_tld_spread_for_top_n,
            audit_log_path=run_paths.audit_jsonl,
        )
        save_last_run(state_file, started)
        logger.info("scheduled sync completed; state saved to %s", state_file)
    except Exception:
        logger.exception("scheduled sync FAILED; will retry on next scheduled trigger")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(level: str) -> None:
    """Configure scheduler-level logging via the centralized helper.

    This sets up logs/scheduler/YYYYMMDD/scheduler_<HHMMSS>.{log,errors.log,api.jsonl}
    plus stdout. Individual scheduled sync jobs get their own per-fire
    sub-trail under logs/sync/ via run_sync_job().
    """
    from app.logging_setup import configure_run_logging
    configure_run_logging("scheduler", level=level)


# ---------------------------------------------------------------------------
# CLI + main loop
# ---------------------------------------------------------------------------


async def main_async(args) -> int:
    state_file = Path(args.state_file)
    output_dir = Path(args.output_dir)

    # If --run-now, fire one job and exit.
    if args.run_now:
        await run_sync_job(
            target=args.target,
            top_n_preview=args.top,
            check_tld_spread_for_top_n=args.check_tld_spread,
            persist_to_db=args.persist,
            state_file=state_file,
            output_dir=output_dir,
        )
        return 0

    # Otherwise: set up the scheduler.
    scheduler = AsyncIOScheduler(timezone=ZoneInfo("UTC"))
    trigger = parse_schedule(args.schedule)
    scheduler.add_job(
        run_sync_job,
        trigger=trigger,
        kwargs={
            "target": args.target,
            "top_n_preview": args.top,
            "check_tld_spread_for_top_n": args.check_tld_spread,
            "persist_to_db": args.persist,
            "state_file": state_file,
            "output_dir": output_dir,
        },
        id="daily_inventory_sync",
        name="daily inventory sync",
        misfire_grace_time=60 * 60,  # fire even if up to 1h late (e.g. Mac just woke)
        coalesce=True,                # if multiple were missed, fire only once
        max_instances=1,              # never run two in parallel
        replace_existing=True,
    )

    # Catch up on startup if last successful run was stale (Mac was asleep, etc.)
    if args.run_on_startup_if_stale > 0:
        last = load_last_run(state_file)
        threshold = timedelta(hours=args.run_on_startup_if_stale)
        if last is None:
            logger.info("no prior run on record -- firing initial catch-up sync")
            should_run = True
        else:
            age = datetime.now(timezone.utc) - last.astimezone(timezone.utc)
            should_run = age > threshold
            logger.info(
                "last successful run was %s (%.1fh ago); stale threshold %dh; "
                "catch-up needed: %s",
                last.isoformat(), age.total_seconds() / 3600,
                args.run_on_startup_if_stale, should_run,
            )
        if should_run:
            # Don't await -- let the scheduler start in parallel.
            asyncio.create_task(run_sync_job(
                target=args.target,
                top_n_preview=args.top,
                check_tld_spread_for_top_n=args.check_tld_spread,
                persist_to_db=args.persist,
                state_file=state_file,
                output_dir=output_dir,
            ))

    scheduler.start()
    logger.info("scheduler started | next run: %s", scheduler.get_job("daily_inventory_sync").next_run_time)
    logger.info("schedule: %s | target=%s | top=%d | tld_spread=%d | persist=%s",
                args.schedule, args.target, args.top, args.check_tld_spread, args.persist)
    logger.info("ctrl-c to stop")

    # Graceful shutdown on SIGINT / SIGTERM.
    stop_event = asyncio.Event()

    def _stop():
        logger.info("shutdown signal received; stopping scheduler...")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            # add_signal_handler isn't available on Windows; the default
            # KeyboardInterrupt handling still works there.
            pass

    await stop_event.wait()
    scheduler.shutdown(wait=False)
    logger.info("scheduler stopped cleanly")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Long-running scheduler for the daily inventory sync."
    )
    parser.add_argument(
        "--schedule",
        default="8:30 PT",
        help=(
            "When to fire the daily sync. Examples: '8:30 PT' (default), "
            "'13:00 UTC', 'every 6 hours', '*/30 minutes'."
        ),
    )
    parser.add_argument(
        "--target",
        choices=["closeouts", "expiring", "both"],
        default="expiring",
        help="Which inventory feed(s) to sync each day (default: expiring).",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=50,
        help="How many top-scored to print to console / log (default: 50).",
    )
    parser.add_argument(
        "--check-tld-spread",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Enrich top N highest-scoring listings with GoDaddy TLD spread "
            "data (default: 0 = off). Recommended: 100."
        ),
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="Upsert scored listings into Postgres each run.",
    )
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Fire one sync immediately and exit, instead of starting the scheduler.",
    )
    parser.add_argument(
        "--run-on-startup-if-stale",
        type=int,
        default=20,
        metavar="HOURS",
        help=(
            "On startup, fire an immediate catch-up sync if the last successful "
            "run was older than this many hours. Default 20h, which catches the "
            "common case of a Mac asleep through the scheduled time. Set 0 to "
            "disable."
        ),
    )
    parser.add_argument(
        "--state-file",
        default=str(DEFAULT_STATE_FILE),
        help=f"Where to persist last-run timestamp (default: {DEFAULT_STATE_FILE}).",
    )
    parser.add_argument(
        "--output-dir",
        default=str(REPO_ROOT / "out"),
        help="Directory for the per-run scored JSON files (default: ./out/).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    # Log files now live under logs/scheduler/YYYYMMDD/ -- see app/logging_setup.py.
    setup_logging(args.log_level)

    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
