"""FastAPI entry point. Run with: uvicorn app.main:app --reload"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auctions as auctions_router
from app.api import closeout as closeout_router
from app.api import lookup as lookup_router
from app.api import search as search_router
from app.api import settings as settings_router
from app.api import watchlist as watchlist_router
from app.config import get_settings
from app.notify.email import is_email_configured
from worker.notifier import create_notification_scheduler, notifier_disabled

cfg = get_settings()
logging.basicConfig(level=cfg.log_level.upper())

# Background tasks started in the lifespan handler. Keeping references
# prevents them from being garbage-collected, and lets us cancel on shutdown.
_background_tasks: list[asyncio.Task] = []

# APScheduler instances (reminder tick + daily end-time refresh). Held so
# shutdown can stop them cleanly on deploy.
_schedulers: list = []


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """FastAPI lifespan handler — seeds the SystemSettings singleton if
    missing, then starts the auto-buy trigger worker.

    Auto-seeding settings on boot means production deploys don't need a
    separate `python scripts/init_db.py` step. Safety caps default to
    conservative-but-usable values (per-tx $50 covers the typical $28
    closeout test buy; daily $200 leaves room for a few in a row).
    """
    # 1. Ensure the SystemSettings row exists. Idempotent — leaves an
    # existing row untouched.
    try:
        from datetime import datetime, timezone
        from decimal import Decimal

        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models.settings import SystemSettings

        async with SessionLocal() as session:
            existing = (
                await session.execute(
                    select(SystemSettings).where(SystemSettings.id == 1)
                )
            ).scalar_one_or_none()
            if existing is None:
                now = datetime.now(timezone.utc)
                seed = SystemSettings(
                    id=1,
                    per_transaction_cap_dollars=Decimal("50.00"),
                    daily_spend_cap_dollars=Decimal("200.00"),
                    closeout_only_mode=False,  # we now support BIDs too
                    kill_switch_active=False,
                    kill_switch_reason="",
                    sanity_check_multiplier=Decimal("10.00"),
                    updated_at=now,
                )
                session.add(seed)
                await session.commit()
                logging.getLogger(__name__).info(
                    "Seeded SystemSettings singleton with defaults "
                    "(per_tx=$50, daily=$200, closeout_only=False)."
                )
            else:
                logging.getLogger(__name__).info(
                    "SystemSettings row exists; leaving as-is "
                    f"(per_tx=${existing.per_transaction_cap_dollars}, "
                    f"daily=${existing.daily_spend_cap_dollars})."
                )
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).error(
            "Failed to seed SystemSettings on boot: %s. The API will return "
            "403 from governor-protected endpoints until this is fixed.",
            exc,
        )

    # 2. Start the trigger worker.
    if os.getenv("DISABLE_TRIGGER_WORKER", "").lower() not in ("true", "1", "yes"):
        from worker.trigger import WORKER_STATUS, run_trigger_loop

        task = asyncio.create_task(run_trigger_loop(), name="trigger_loop")
        _background_tasks.append(task)
        logging.getLogger(__name__).info("Trigger worker task spawned.")

        # GoDaddy-watchlist sweep (2026-09-24): imports the client's GoDaddy-side
        # stars into the sniper watchlist on a slow cadence. Independent of
        # the trigger loop; safe to lose (the /live piggyback still imports
        # whatever the dashboard touches).
        from worker.trigger import GD_WATCH_SWEEP_ENABLED, run_gd_watch_sweep_loop

        if GD_WATCH_SWEEP_ENABLED:
            _background_tasks.append(
                asyncio.create_task(run_gd_watch_sweep_loop(), name="gd_watch_sweep")
            )
            logging.getLogger(__name__).info("GD-watch sweep task spawned.")

        async def _trigger_watchdog() -> None:
            """Self-healing (2026-08-26 finacredit post-mortem): the trigger
            task froze mid-await for hours with ZERO log output, and a $51
            snipe never fired. The pulse makes stalls visible; this watchdog
            makes them survivable — a worker that hasn't ticked in 3 minutes
            gets cancelled and respawned, loudly."""
            from datetime import datetime as _dt
            from datetime import timezone as _tz

            log = logging.getLogger(__name__)
            while True:
                await asyncio.sleep(60)
                ref = WORKER_STATUS.get("last_tick_at") or WORKER_STATUS.get("started_at")
                if not ref:
                    continue
                try:
                    age = (_dt.now(_tz.utc) - _dt.fromisoformat(ref)).total_seconds()
                except ValueError:
                    continue
                if age <= 180:
                    continue
                log.error(
                    "WATCHDOG: trigger worker stalled (last activity %.0fs ago). "
                    "Cancelling and respawning the task.",
                    age,
                )
                for i, t in enumerate(_background_tasks):
                    if t.get_name() == "trigger_loop":
                        t.cancel()
                        try:
                            # Bounded (2026-09-24, the 19h freeze): a bare
                            # `await t` hung forever when the dying task's
                            # finally-block cleanup (httpx aclose on a wedged
                            # pool) never returned — freezing the watchdog
                            # itself. 15s then abandon the corpse and respawn.
                            await asyncio.wait_for(t, timeout=15)
                        except BaseException:  # noqa: BLE001 — must not kill the watchdog
                            pass
                        _background_tasks[i] = asyncio.create_task(
                            run_trigger_loop(), name="trigger_loop"
                        )
                        log.error("WATCHDOG: trigger worker respawned.")
                        break

        wd = asyncio.create_task(_trigger_watchdog(), name="trigger_watchdog")
        _background_tasks.append(wd)
    else:
        logging.getLogger(__name__).info(
            "Trigger worker disabled via DISABLE_TRIGGER_WORKER env var."
        )

    # 3. Start the full-inventory index refresher (feeds -> local SQLite).
    # Soft dependency: search + shim-ID healing degrade gracefully while
    # the first build (~2-4 min) runs or if a refresh fails.
    if os.getenv("DISABLE_INVENTORY_INDEX", "").lower() not in ("true", "1", "yes"):
        from app.godaddy.inventory_index import run_index_refresh_loop

        task = asyncio.create_task(run_index_refresh_loop(), name="inventory_index")
        _background_tasks.append(task)
        logging.getLogger(__name__).info("Inventory index refresher spawned.")

    # 4. Start the email-reminder scheduler (60s tick + daily end-time
    # refresh). Safe to run un-elected because fly.toml pins us to exactly
    # one machine; the sent-flags in notification_prefs are what make a
    # restart mid-window non-duplicating.
    if not notifier_disabled():
        try:
            _notification_scheduler = create_notification_scheduler()
            _notification_scheduler.start()
            _schedulers.append(_notification_scheduler)
            logging.getLogger(__name__).info(
                "Notification scheduler started (tick=%ss, daily end-time refresh at %s UTC). "
                "Email delivery is %s.",
                cfg.notification_tick_seconds,
                cfg.notification_end_time_refresh_utc,
                "ENABLED" if is_email_configured() else
                "DISABLED — set RESEND_API_KEY + NOTIFY_EMAIL_TO to turn it on",
            )
        except Exception as exc:  # noqa: BLE001 — reminders must never block boot
            logging.getLogger(__name__).error(
                "Failed to start the notification scheduler: %s. Reminder "
                "emails will not send until this is fixed.",
                exc,
            )
    else:
        logging.getLogger(__name__).info(
            "Notification scheduler disabled via DISABLE_NOTIFIER env var."
        )

    yield

    # Shutdown: stop schedulers, then cancel and await background tasks.
    for scheduler in _schedulers:
        scheduler.shutdown(wait=False)
    for task in _background_tasks:
        task.cancel()
    for task in _background_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="GoDaddy Auction Sniper",
    description="Domain auction monitor and sniper API.",
    version="0.1.0",
    lifespan=lifespan,
)

# Bearer-token auth on /api/*. Added BEFORE the CORS middleware so CORS ends
# up outermost — preflight OPTIONS must be answered without auth. See
# app/auth.py for the fail-closed rules.
from app.auth import BearerAuthMiddleware  # noqa: E402  (import near use, after app creation)

app.add_middleware(
    BearerAuthMiddleware,
    token=cfg.api_auth_token,
    godaddy_env=cfg.godaddy_env,
)

# CORS — allow the Cloudflare Pages dashboard (production), Cloudflare's
# preview deployments, and a few common local dev ports. CORS is origin
# allowlisting for browsers; real security is BearerAuthMiddleware above
# plus Cloudflare Access in front of the dashboard.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://your-dashboard.pages.dev",
        # Cloudflare Pages auto-generated preview deployments
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:8000",
        "http://localhost:8080",
    ],
    # Also allow any *.your-dashboard.pages.dev preview subdomain via regex.
    allow_origin_regex=r"https://[a-z0-9-]+\.auction-sniper\.pages\.dev",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    # Worker pulse (2026-08-26 finacredit post-mortem): a dead trigger loop
    # was indistinguishable from a quiet one. The dashboard polls this and
    # shows the engine state; last_tick_at older than ~2 min = STALLED.
    from worker.trigger import WORKER_STATUS

    return {
        "status": "ok",
        "godaddy_env": cfg.godaddy_env,
        "worker": dict(WORKER_STATUS),
    }


# Mount routers.
app.include_router(auctions_router.router, prefix="/api/auctions", tags=["auctions"])
app.include_router(watchlist_router.router, prefix="/api/watchlist", tags=["watchlist"])
app.include_router(closeout_router.router, prefix="/api/closeout", tags=["closeout"])
app.include_router(lookup_router.router, prefix="/api/lookup", tags=["lookup"])
app.include_router(search_router.router, prefix="/api/search", tags=["search"])

from app.api import purchases as purchases_router  # noqa: E402

app.include_router(purchases_router.router, prefix="/api/purchases", tags=["purchases"])
app.include_router(settings_router.router, prefix="/api", tags=["settings"])
