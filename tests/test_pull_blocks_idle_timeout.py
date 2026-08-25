"""A reviewer waiting on their own pull is not idle.

Measured on the real preview path (bench/qbench_fake.py driving app.py's own
COASession): at a QBench render time of 8s, a 250-sample pull is still running
at 630s. The frontend's 10-minute inactivity timer fires, `triggerTimeout()`
closes the EventSource — and SSE is the ONLY channel carrying `sample_status`,
so the outstanding samples can never be reported ready to that browser. The
reviewer is locked out two samples short of a finished pull. At 16s render,
115 samples are stranded.

Waiting is not activity: the timer only resets on mousemove, click, keydown
and scroll, and a reviewer watching a progress list touches none of them. So
the app times out precisely the person who is waiting on it.

Production corroboration, from the repo's own audit log: 32 logins and 1
logout, and same-user re-logins average 3.5 on days over 70 samples against
0.5 on lighter days.

The fix must not become "never time out". A pull that stalls completely still
has to release the session, so the suppression is bounded by recent progress
rather than by the pull merely being unfinished.

Source-level, like tests/test_cc_frontend.py.
"""

from __future__ import annotations

import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "static" / "js" / "app.js"
SOURCE = APP_JS.read_text(encoding="utf-8")


def _interval_body() -> str:
    m = re.search(r"inactivityTimer = setInterval\((.*?)\}, 30000\);", SOURCE, re.S)
    assert m, "could not isolate the inactivity interval"
    return m.group(1)


def test_an_unfinished_pull_defers_the_timeout() -> None:
    body = _interval_body()
    assert "_anyUnfinished" in body, (
        "the idle check does not consider whether a pull is still running, so "
        "it closes the SSE stream the pull needs to report its results"
    )


def test_the_deferral_is_bounded_by_recent_progress() -> None:
    """A stalled pull must still release the session."""
    body = _interval_body()
    assert re.search(r"_lastPullProgress|PULL_STALL", body), (
        "an unfinished pull defers the timeout forever; a stalled pull would "
        "never release the session"
    )


def test_pull_progress_is_recorded_when_a_sample_reports() -> None:
    m = re.search(r"function updateSampleStatus\(.*?\)\s*\{(.*?)\n\}", SOURCE, re.S)
    assert m, "could not isolate updateSampleStatus()"
    assert "_lastPullProgress" in m.group(1), (
        "nothing records that the pull is still making progress"
    )


def test_the_stall_window_is_declared_and_sane() -> None:
    m = re.search(r"const PULL_STALL_MS\s*=\s*([^;]+);", SOURCE)
    assert m, "expected a named PULL_STALL_MS window"
    expr = m.group(1)
    value = eval(expr, {"__builtins__": {}}, {})  # simple arithmetic literal
    assert 30_000 <= value <= 300_000, (
        f"PULL_STALL_MS={value} is outside a sensible 30s-5min window"
    )
