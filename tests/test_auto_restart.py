"""Tests for the 3 AM auto-restart decision.

Root cause (diagnosed 2026-06-24): the once-a-day guard (_auto_restart_done_today)
is a per-process module global. After a restart the fresh process starts with
it unset, so if the clock is still in the 3 AM hour it immediately qualifies
again and restarts ~37s later — a restart storm churning the Playwright login
dozens of times each night. The fix adds an uptime guard so a just-respawned
process won't re-trigger.
"""

from __future__ import annotations

import pytest

pytest.importorskip("flask")

import app  # noqa: E402

HOUR = app.AUTO_RESTART_HOUR
BIG_UPTIME = app.AUTO_RESTART_MIN_UPTIME_SECONDS + 10
SMALL_UPTIME = 30.0  # just respawned


def test_no_restart_outside_target_hour() -> None:
    assert app._should_auto_restart(
        hour=HOUR + 1, today_str="2026-06-24", done_today=None,
        uptime_seconds=BIG_UPTIME, active_count=0, idle_seconds=99999,
    ) is False


def test_no_restart_when_already_done_today() -> None:
    assert app._should_auto_restart(
        hour=HOUR, today_str="2026-06-24", done_today="2026-06-24",
        uptime_seconds=BIG_UPTIME, active_count=0, idle_seconds=99999,
    ) is False


def test_no_restart_when_just_respawned_storm_guard() -> None:
    # The storm: hour matches, nobody active, but the process only just
    # started — it must NOT restart again.
    assert app._should_auto_restart(
        hour=HOUR, today_str="2026-06-24", done_today=None,
        uptime_seconds=SMALL_UPTIME, active_count=0, idle_seconds=99999,
    ) is False


def test_no_restart_when_users_active_and_recent() -> None:
    assert app._should_auto_restart(
        hour=HOUR, today_str="2026-06-24", done_today=None,
        uptime_seconds=BIG_UPTIME, active_count=2,
        idle_seconds=app.AUTO_RESTART_IDLE_SECONDS - 1,
    ) is False


def test_restart_when_idle_in_window_and_long_running() -> None:
    assert app._should_auto_restart(
        hour=HOUR, today_str="2026-06-24", done_today=None,
        uptime_seconds=BIG_UPTIME, active_count=0, idle_seconds=99999,
    ) is True
