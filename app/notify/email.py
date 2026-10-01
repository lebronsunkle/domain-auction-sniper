"""Resend transport.

Talks to Resend's REST API directly with httpx instead of pulling in their
SDK — the send is one authenticated POST, and the Fly machine has 256MB to
work with, so a dependency that wraps `requests` isn't worth it.

Failure policy: raise. The caller (worker/notifier.py) claims a sent-flag
before calling in here and releases the claim when this raises, so a failed
send is retried on the next tick rather than being silently swallowed.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"
SEND_TIMEOUT_SECONDS = 10.0


class EmailSendError(Exception):
    """Resend rejected the message, or we couldn't reach them."""


def is_email_configured() -> bool:
    """True when we have both an API key and somewhere to send.

    The notifier checks this BEFORE claiming any sent-flag: with no key
    configured we must leave every rung armed, so that adding the key later
    delivers the reminders instead of finding them all already marked sent.
    """
    cfg = get_settings()
    return bool(cfg.resend_api_key and cfg.notify_email_to)


async def send_email(
    to: str,
    subject: str,
    html: str,
    text: Optional[str] = None,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """Send one email through Resend. Returns the provider message id.

    `client` is injectable so tests can assert on the request without
    touching the network.
    """
    cfg = get_settings()
    if not cfg.resend_api_key:
        raise EmailSendError("RESEND_API_KEY is not set")
    if not to:
        raise EmailSendError("No recipient address")

    payload: dict = {
        "from": cfg.notify_email_from,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=SEND_TIMEOUT_SECONDS)
    try:
        response = await client.post(
            RESEND_ENDPOINT,
            json=payload,
            headers={
                "Authorization": f"Bearer {cfg.resend_api_key}",
                "Content-Type": "application/json",
            },
        )
    except httpx.HTTPError as exc:
        raise EmailSendError(f"Resend request failed: {exc}") from exc
    finally:
        if owns_client:
            await client.aclose()

    if response.status_code >= 400:
        # Resend puts a human-readable reason in the body; surface it, since
        # the usual causes are actionable (unverified from-domain, bad key).
        raise EmailSendError(
            f"Resend returned HTTP {response.status_code}: {response.text[:400]}"
        )

    try:
        message_id = response.json().get("id", "")
    except ValueError:
        message_id = ""
    logger.info("Sent reminder email to %s (resend id=%s)", to, message_id or "?")
    return message_id
