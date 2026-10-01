"""Tests for the email reminder ladder.

The two behaviors that matter most here are the ones that would embarrass us
in the client's inbox:

  * a restart mid-window must not re-send a reminder already sent, and
  * switching the bell on 20 minutes before close must send ONE email, not
    a burst of four.

Both are properties of the pure functions in worker/notifier.py, so they're
tested here without a database or an SMTP-shaped mock in sight.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models.notification import REMINDER_THRESHOLDS, NotificationPref
from worker.notifier import due_thresholds, rearm_for_extension


NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)

# Ladder rungs, by name, for readable assertions.
T_24H, T_12H, T_3H, T_30M = REMINDER_THRESHOLDS


def make_pref(**kwargs) -> NotificationPref:
    return NotificationPref(
        watchlist_entry_id=1,
        enabled=True,
        created_at=NOW,
        updated_at=NOW,
        **kwargs,
    )


def labels(thresholds) -> list[str]:
    return [t.label for t in thresholds]


# ---------------------------------------------------------------------------
# due_thresholds
# ---------------------------------------------------------------------------


def test_nothing_due_before_the_first_rung():
    pref = make_pref()
    # Two days out — the 24h reminder hasn't been crossed yet.
    assert due_thresholds(pref, seconds_remaining=48 * 3600) == []


def test_crossing_24h_arms_only_the_24h_rung():
    pref = make_pref()
    assert labels(due_thresholds(pref, seconds_remaining=23.5 * 3600)) == ["24 hours"]


def test_already_sent_rung_is_not_due_again():
    """The restart case: flags live in Postgres, so a fresh process that
    re-evaluates the same window sends nothing."""
    pref = make_pref(sent_24h_at=NOW - timedelta(minutes=5))
    assert due_thresholds(pref, seconds_remaining=23.5 * 3600) == []


def test_late_enable_reports_every_crossed_rung_most_urgent_last():
    """Bell switched on 20 minutes before close: all four rungs read as due.

    The notifier sends only the LAST one and marks the rest sent — this test
    pins the ordering that behavior depends on.
    """
    pref = make_pref()
    due = due_thresholds(pref, seconds_remaining=20 * 60)
    assert labels(due) == ["24 hours", "12 hours", "3 hours", "30 minutes"]
    assert due[-1] is T_30M


def test_ended_auction_is_never_due():
    pref = make_pref()
    assert due_thresholds(pref, seconds_remaining=0) == []
    assert due_thresholds(pref, seconds_remaining=-600) == []


# ---------------------------------------------------------------------------
# rearm_for_extension
# ---------------------------------------------------------------------------


def test_first_observation_takes_a_baseline_without_rearming():
    pref = make_pref(sent_24h_at=NOW)
    end = NOW + timedelta(hours=10)
    assert rearm_for_extension(pref, end, NOW) == []
    assert pref.armed_end_time_utc == end
    assert pref.sent_24h_at == NOW  # untouched


def test_small_clock_jitter_is_not_an_extension():
    """Feed, SOAP and the trigger worker disagree by seconds routinely."""
    end = NOW + timedelta(hours=2)
    pref = make_pref(armed_end_time_utc=end, sent_3h_at=NOW)
    assert rearm_for_extension(pref, end + timedelta(seconds=30), NOW) == []
    assert pref.sent_3h_at == NOW
    assert pref.last_extended_at is None


def test_extension_rearms_only_the_rungs_now_in_the_future():
    """20 minutes out (30m already sent), extended to 6 hours out.

    The 30m and 3h rungs are ahead of us again and must re-fire. The 12h and
    24h rungs are NOT — 6 hours is still inside them, and re-sending a
    "12 hours left" email for an auction 6 hours out would be a lie.
    """
    original_end = NOW + timedelta(minutes=20)
    pref = make_pref(
        armed_end_time_utc=original_end,
        sent_24h_at=NOW - timedelta(hours=23),
        sent_12h_at=NOW - timedelta(hours=11),
        sent_3h_at=NOW - timedelta(hours=2),
        sent_30m_at=NOW - timedelta(minutes=10),
    )
    new_end = NOW + timedelta(hours=6)

    cleared = rearm_for_extension(pref, new_end, NOW)

    assert labels(cleared) == ["3 hours", "30 minutes"]
    assert pref.sent_3h_at is None
    assert pref.sent_30m_at is None
    assert pref.sent_24h_at is not None
    assert pref.sent_12h_at is not None
    assert pref.armed_end_time_utc == new_end
    assert pref.last_extended_at == NOW


def test_extension_then_countdown_refires_the_cleared_rungs():
    """End-to-end on the pure functions: after a re-arm, the cleared rungs
    come back as due when the clock runs down to them again."""
    pref = make_pref(
        armed_end_time_utc=NOW + timedelta(minutes=20),
        sent_30m_at=NOW - timedelta(minutes=10),
    )
    rearm_for_extension(pref, NOW + timedelta(hours=6), NOW)

    # 25 minutes before the NEW end time.
    due = due_thresholds(pref, seconds_remaining=25 * 60)
    assert T_30M in due


def test_auction_brought_forward_rebaselines_without_rearming():
    """A correction that moves the end EARLIER isn't an extension — nothing
    should re-fire, but we must arm against the new clock."""
    pref = make_pref(
        armed_end_time_utc=NOW + timedelta(hours=10),
        sent_24h_at=NOW - timedelta(hours=14),
    )
    new_end = NOW + timedelta(hours=2)

    assert rearm_for_extension(pref, new_end, NOW) == []
    assert pref.armed_end_time_utc == new_end
    assert pref.sent_24h_at is not None
    assert pref.last_extended_at is None


def test_rearm_ignores_rungs_that_were_never_sent():
    pref = make_pref(armed_end_time_utc=NOW + timedelta(minutes=20))
    assert rearm_for_extension(pref, NOW + timedelta(hours=6), NOW) == []
