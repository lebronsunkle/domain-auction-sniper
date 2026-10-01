"""Renders the reminder email.

Kept apart from the Resend transport so the copy can be unit-tested without
mocking HTTP, and so changing the wording never risks the send path.

The tone target: the client reads these on a phone, mid-day, and needs to
decide in five seconds whether to open the dashboard. So the subject line
carries the domain, the deadline, and nothing else, and the body leads with
whether the sniper is actually armed — a reminder for a domain with no max
bid set is really a "you haven't set this up yet" warning.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from app.godaddy.listing_ids import is_real_listing_id


@dataclass
class ReminderContext:
    domain: str
    listing_id: int
    threshold_label: str  # "24 hours", "30 minutes", ...
    end_time_utc: datetime
    seconds_remaining: float
    auction_type: Optional[str] = None
    current_price: Optional[Decimal] = None
    estimated_value: Optional[Decimal] = None
    max_bid_dollars: Optional[Decimal] = None
    is_armed: bool = True
    dashboard_url: str = "https://your-dashboard.pages.dev"


def _money(value: Optional[Decimal]) -> str:
    if value is None:
        return "—"
    as_float = float(value)
    if as_float >= 100:
        return f"${as_float:,.0f}"
    return f"${as_float:,.2f}".rstrip("0").rstrip(".")


def _remaining_phrase(seconds: float) -> str:
    """"1 day 4 hours" / "3 hours 12 minutes" / "28 minutes"."""
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _auction_url(ctx: ReminderContext) -> Optional[str]:
    """GoDaddy's listing page, when we have a real auction id.

    Entries added via "+ Add domain" or an old sync can carry a synthetic
    id (see app/godaddy/listing_ids.py); linking those would land the client on
    a 404, so we leave the link out and point at the dashboard instead.
    """
    if not is_real_listing_id(ctx.listing_id):
        return None
    return f"https://auctions.godaddy.com/trpItemListing.aspx?miid={ctx.listing_id}"


def render_reminder(ctx: ReminderContext) -> tuple[str, str, str]:
    """Returns (subject, html_body, text_body)."""
    remaining = _remaining_phrase(ctx.seconds_remaining)
    subject = f"{ctx.domain} — auction ends in {remaining}"

    end_display = ctx.end_time_utc.strftime("%a %b %-d, %H:%M UTC")
    auction_url = _auction_url(ctx)

    # The lead line is the actionable bit: armed with a ceiling, or not.
    if not ctx.is_armed:
        status_line = "This entry is DISARMED — the sniper will not bid."
        status_color = "#e11d48"
    elif ctx.max_bid_dollars is None:
        status_line = "No max bid set — the sniper will not bid on this one."
        status_color = "#e11d48"
    else:
        status_line = (
            f"Armed: the sniper will bid up to {_money(ctx.max_bid_dollars)} "
            "in the final seconds."
        )
        status_color = "#059669"

    facts = [
        ("Ends", f"{end_display} ({remaining} left)"),
        ("Reminder", f"{ctx.threshold_label} out"),
    ]
    if ctx.auction_type:
        facts.append(("Type", ctx.auction_type.replace("_", " ").title()))
    if ctx.current_price is not None:
        label = "Buy Now price" if ctx.auction_type == "CLOSEOUT" else "Current bid"
        facts.append((label, _money(ctx.current_price)))
    if ctx.estimated_value is not None:
        facts.append(("GoDaddy estimate", _money(ctx.estimated_value)))

    rows = "".join(
        f'<tr><td style="padding:4px 16px 4px 0;color:#64748b;">{label}</td>'
        f'<td style="padding:4px 0;font-weight:600;color:#0f172a;">{value}</td></tr>'
        for label, value in facts
    )

    link_html = (
        f'<a href="{auction_url}" style="color:#4f46e5;">View on GoDaddy</a> &nbsp;·&nbsp; '
        if auction_url
        else ""
    )

    html = f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
            max-width:520px;color:#0f172a;">
  <h2 style="margin:0 0 4px;font-size:20px;">{ctx.domain}</h2>
  <p style="margin:0 0 16px;font-size:15px;color:{status_color};font-weight:600;">
    {status_line}
  </p>
  <table style="font-size:14px;border-collapse:collapse;margin-bottom:20px;">
    {rows}
  </table>
  <p style="font-size:14px;">
    {link_html}<a href="{ctx.dashboard_url}" style="color:#4f46e5;">Open dashboard</a>
  </p>
  <p style="font-size:12px;color:#94a3b8;margin-top:24px;">
    You're getting this because notifications are on for {ctx.domain}.
    Turn them off with the bell icon on the watchlist row.
  </p>
</div>"""

    text_lines = [
        f"{ctx.domain} — auction ends in {remaining}",
        "",
        status_line,
        "",
    ]
    text_lines += [f"{label}: {value}" for label, value in facts]
    text_lines.append("")
    if auction_url:
        text_lines.append(f"GoDaddy: {auction_url}")
    text_lines.append(f"Dashboard: {ctx.dashboard_url}")
    text = "\n".join(text_lines)

    return subject, html, text
