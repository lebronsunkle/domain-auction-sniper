"""Twilio SMS transport (2026-09-29, the client's outbid text alerts).

Mirrors app/notify/email.py: one authenticated POST, no SDK — the Fly
machine is memory-tight and the send is a single form-encoded request.

Config-gated: is_sms_configured() is False until account SID, auth token,
from-number, AND a recipient are all set. Until then the outbid alert is
email-only and the SMS leg is skipped silently — never an error, so a
half-configured Twilio can't break the outbid pass.

Failure policy: raise EmailSendError's sibling SmsSendError. The caller
stamps the sent-flag BEFORE sending (double-send safety), and the email leg
is attempted independently, so a failed text never suppresses the email.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

TWILIO_ENDPOINT = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
SEND_TIMEOUT_SECONDS = 10.0


class SmsSendError(Exception):
    """Twilio rejected the message, or we couldn't reach them."""


def is_sms_configured() -> bool:
    """True only when every piece needed to send a text is present."""
    cfg = get_settings()
    return bool(
        cfg.twilio_account_sid
        and cfg.twilio_auth_token
        and cfg.twilio_from_number
        and cfg.outbid_alert_sms_to
    )


async def send_sms(
    to: str,
    body: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """Send one SMS through Twilio. Returns the provider message SID.

    `client` is injectable so tests can assert on the request without
    touching the network.
    """
    cfg = get_settings()
    if not (cfg.twilio_account_sid and cfg.twilio_auth_token and cfg.twilio_from_number):
        raise SmsSendError("Twilio is not fully configured")
    if not to:
        raise SmsSendError("No recipient number")

    url = TWILIO_ENDPOINT.format(sid=cfg.twilio_account_sid)
    data = {"To": to, "From": cfg.twilio_from_number, "Body": body}

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=SEND_TIMEOUT_SECONDS)
    try:
        response = await client.post(
            url,
            data=data,
            auth=(cfg.twilio_account_sid, cfg.twilio_auth_token),
        )
    except httpx.HTTPError as exc:
        raise SmsSendError(f"Twilio request failed: {exc}") from exc
    finally:
        if owns_client:
            await client.aclose()

    if response.status_code >= 400:
        raise SmsSendError(
            f"Twilio returned HTTP {response.status_code}: {response.text[:400]}"
        )

    try:
        sid = response.json().get("sid", "")
    except ValueError:
        sid = ""
    logger.info("Sent outbid SMS to %s (twilio sid=%s)", to, sid or "?")
    return sid
