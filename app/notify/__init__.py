"""Outbound notifications (email reminders for watchlisted auctions)."""

from .email import EmailSendError, is_email_configured, send_email
from .reminder import ReminderContext, render_reminder

__all__ = [
    "send_email",
    "is_email_configured",
    "EmailSendError",
    "render_reminder",
    "ReminderContext",
]
