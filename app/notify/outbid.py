"""Renders the outbid alert (2026-09-29, the client).

Separate from the reminder copy and the transports so wording can be
unit-tested without touching HTTP. One event, one message: "you've been
outbid on X." Kept short — he reads it on a phone and decides in seconds
whether to open the dashboard and raise his max.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass
class OutbidContext:
    domain: str
    current_price: Optional[Decimal] = None   # the bid that beat him
    max_bid_dollars: Optional[Decimal] = None  # his ceiling that got passed
    dashboard_url: str = "https://your-dashboard.pages.dev/dashboard/"


def _money(value: Optional[Decimal]) -> str:
    if value is None:
        return "—"
    return f"${value:,.2f}"


def render_outbid_alert(ctx: OutbidContext) -> tuple[str, str, str, str]:
    """Return (subject, html, text, sms_body) for an outbid alert."""
    subject = f"Outbid on {ctx.domain}"

    lead = f"You've been outbid on {ctx.domain}."
    detail_bits = []
    if ctx.current_price is not None:
        detail_bits.append(f"Current bid is now {_money(ctx.current_price)}")
    if ctx.max_bid_dollars is not None:
        detail_bits.append(f"your max was {_money(ctx.max_bid_dollars)}")
    detail = ", ".join(detail_bits)
    if detail:
        detail = detail[0].upper() + detail[1:] + "."

    html = (
        f"<p style='font-size:16px'><strong>{lead}</strong></p>"
        + (f"<p style='font-size:15px'>{detail}</p>" if detail else "")
        + f"<p style='font-size:15px'>Open the sniper to raise your max if you "
        f"still want it:<br><a href='{ctx.dashboard_url}'>{ctx.dashboard_url}</a></p>"
    )

    text_lines = [lead]
    if detail:
        text_lines.append(detail)
    text_lines.append(f"Raise your max here: {ctx.dashboard_url}")
    text = "\n".join(text_lines)

    # SMS: tight, no URL padding beyond the essentials.
    sms_bits = [lead]
    if ctx.current_price is not None:
        sms_bits.append(f"Now at {_money(ctx.current_price)}.")
    sms_bits.append(ctx.dashboard_url)
    sms_body = " ".join(sms_bits)

    return subject, html, text, sms_body
