"""
Centralized configuration. All env vars are declared here so missing values
fail loudly at startup, not at first API call.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Loaded from environment / .env."""

    # --- GoDaddy API credentials --------------------------------------------
    # OTE for development, prod for live. Same env var, different values per env.
    godaddy_api_key: str = Field(..., description="sso-key portion before the colon")
    godaddy_api_secret: str = Field(..., description="sso-key portion after the colon")
    godaddy_customer_id: str = Field(
        ...,
        description="Customer UUID. Extract via auth_idp cookie + jwt.io; see README.",
    )

    # Which environment we're hitting. Drives the base URL.
    godaddy_env: Literal["ote", "production"] = Field(
        "ote", description="Set to 'production' only when ready for live trading."
    )

    # --- Database / cache ---------------------------------------------------
    database_url: str = Field(
        ...,
        description="Postgres async URL, e.g. postgresql+asyncpg://user:pw@host/db",
    )
    redis_url: str = Field("redis://localhost:6379/0")

    # --- App ----------------------------------------------------------------
    log_level: str = "INFO"

    # Shared bearer token required on every /api request. Generate with
    # `openssl rand -hex 32` and set as a Fly secret. Empty string means:
    # auth disabled in OTE/dev, fail-closed (503 on all /api) in production.
    api_auth_token: str = Field(
        "", description="Bearer token for the public API. See app/auth.py."
    )

    # Estibot API key (Advanced tier). Unblocked 2026-07-14 — the new
    # public-api.estibot.com endpoint needs only this key, no IP whitelist.
    # Empty = Estibot enrichment silently disabled.
    estibot_api_key: str = Field("", description="Estibot API key; see docs/estibot-integration.md")

    # --- Email reminders (Resend) -------------------------------------------
    # Empty key = notifications are evaluated and logged but never sent. That
    # keeps local dev and OTE from mailing anyone while still exercising the
    # threshold logic.
    resend_api_key: str = Field(
        "", description="Resend API key (re_...). Set as a Fly secret."
    )
    # Must be on a domain verified in Resend, otherwise their API 403s.
    notify_email_from: str = Field(
        "Auction Sniper <alerts@example.com>",
        description="From address for reminder emails; domain must be verified in Resend.",
    )
    # Default recipient. A watchlist entry can override it per-entry.
    notify_email_to: str = Field(
        "", description="Where auction reminders go. Empty = reminders disabled."
    )
    # How often the notifier evaluates the reminder ladder.
    notification_tick_seconds: int = 60

    # --- Outbid alerts (2026-09-29, the client) ---------------------------------
    # A dedicated email (and, once provisioned, text) when the client is outbid on
    # something he's bidding on — and nothing else, so he isn't flooded. This
    # recipient is separate from the reminder recipient above on purpose.
    outbid_alert_email_to: str = Field(
        "",
        description="Where outbid alerts go. Empty = outbid email disabled.",
    )
    # SMS via Twilio. All three must be set for texts to send; until then the
    # alert is email-only (the SMS leg is skipped silently).
    twilio_account_sid: str = Field("", description="Twilio Account SID. Fly secret.")
    twilio_auth_token: str = Field("", description="Twilio auth token. Fly secret.")
    twilio_from_number: str = Field("", description="Twilio sending number, E.164 (+1...).")
    outbid_alert_sms_to: str = Field(
        "", description="the client's mobile in E.164 (+1...). Empty = outbid text disabled."
    )
    # Daily refresh of watchlisted auctions' end times (UTC hour:minute).
    # 17:00 UTC sits just after the 16:30 UTC GitHub Actions inventory sync,
    # so we re-check end times against the freshest feed of the day.
    notification_end_time_refresh_utc: str = "17:00"

    # --- Worker tuning ------------------------------------------------------
    # How often the watchlist refresh runs (minutes).
    watchlist_refresh_interval_minutes: int = 15

    # How aggressively the trigger worker checks an imminent (last few minutes)
    # auction. We do NOT poll GoDaddy for price/status; this controls our own
    # local trigger scheduler granularity.
    trigger_loop_interval_seconds: int = 1

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def rest_base_url(self) -> str:
        return (
            "https://api.ote-godaddy.com"
            if self.godaddy_env == "ote"
            else "https://api.godaddy.com"
        )


def get_settings() -> Settings:
    """Cached settings accessor. Use this in FastAPI deps and worker init."""
    return Settings()  # pydantic-settings reads env on construction
