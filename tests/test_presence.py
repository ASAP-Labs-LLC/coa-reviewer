from __future__ import annotations

from datetime import date

import pytest

from presence import FLUSH_SECONDS, GAP_SECONDS, PresenceTracker
from shared_store import SharedStore


class Clock:
    def __init__(self, t=10_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    store = SharedStore(tmp_path / "db", now=clock)
    yield PresenceTracker(store, now=clock), store, clock
    store.close()


def test_first_touch_is_memory_only_until_flush(env):
    """C2: touch() never talks to the store — flush() does."""
    tr, store, clock = env
    tr.touch("Dana P")
    assert store.spans_between(0, 1e9) == []   # nothing written yet
    assert tr.online() == ["Dana P"]            # but it's tracked in memory


def test_flush_writes_the_latest_touch(env):
    tr, store, clock = env
    tr.touch("Dana P")
    clock.t += 5
    tr.touch("Dana P")
    clock.t += 5
    tr.touch("Dana P")
    written = tr.flush()
    assert written == 1
    spans = store.spans_between(0, 1e9)
    assert len(spans) == 1
    assert spans[0]["open"] and spans[0]["user"] == "Dana P"
    assert spans[0]["end"] == clock.t   # last_seen reflects the latest touch


def test_touch_then_flush_then_more_touches_batches_a_touch_op(env):
    tr, store, clock = env
    tr.touch("Dana P")
    tr.flush()
    clock.t += 5
    tr.touch("Dana P")
    written = tr.flush()
    assert written == 1
    assert store.spans_between(0, 1e9)[0]["end"] == clock.t


def test_case_insensitive_identity(env):
    tr, store, clock = env
    tr.touch("Dana P")
    tr.touch("dana p")
    tr.flush()
    assert len(store.spans_between(0, 1e9)) == 1


def test_gap_closes_and_reopens(env):
    tr, store, clock = env
    tr.touch("u")
    start = clock.t
    clock.t += 60
    tr.touch("u")
    clock.t += GAP_SECONDS + 1
    tr.touch("u")
    tr.flush()
    spans = store.spans_between(0, 1e9)
    assert len(spans) == 2
    assert spans[0]["end"] == start + 60 and spans[0]["end_reason"] == "gap"
    assert spans[1]["open"]


def test_logout_closes_now(env):
    tr, store, clock = env
    tr.touch("u")
    clock.t += 40
    tr.end("u", "logout")
    tr.flush()
    s = store.spans_between(0, 1e9)[0]
    assert s["end"] == clock.t and s["end_reason"] == "logout"
    assert tr.online() == []


def test_timeout_closes_at_last_seen(env):
    tr, store, clock = env
    tr.touch("u")
    seen = clock.t
    clock.t += 700
    tr.end("u", "timeout")
    tr.flush()
    assert store.spans_between(0, 1e9)[0]["end"] == seen


def test_sweep_closes_idle(env):
    tr, store, clock = env
    tr.touch("a")
    clock.t += GAP_SECONDS + 5
    tr.touch("b")
    assert tr.sweep() == 1
    assert tr.online() == ["b"]
    tr.flush()
    a_span = [s for s in store.spans_between(0, 1e9) if s["user"] == "a"][0]
    assert a_span["end_reason"] == "timeout"


def test_blank_user_ignored(env):
    tr, store, clock = env
    tr.touch("  ")
    tr.flush()
    assert store.spans_between(0, 1e9) == []


def test_store_down_does_not_raise(tmp_path):
    blocker = tmp_path / "f"
    blocker.write_text("x")
    tr = PresenceTracker(SharedStore(blocker / "x" / "db"))
    tr.touch("u")
    tr.end("u", "logout")
    assert tr.sweep() == 0
    assert tr.flush() == 0


def test_store_down_then_up_flushes_later(tmp_path):
    """If the store is briefly unavailable when a span opens, the id must
    not be lost — the next flush should open it once the store recovers."""
    good_path = tmp_path / "db"
    store = SharedStore(good_path)
    tr = PresenceTracker(store)
    # simulate "down" by pointing flush at a store that can't open, then a
    # working one, using the tracker's own retry-on-next-flush behaviour:
    # apply_presence failing once (store swapped mid-flight) must not drop
    # the pending span.
    real_apply = store.apply_presence
    calls = {"n": 0}

    def flaky(ops):
        calls["n"] += 1
        if calls["n"] == 1:
            return None   # first flush: store "unavailable"
        return real_apply(ops)

    store.apply_presence = flaky
    tr.touch("u")
    assert tr.flush() == 0          # first attempt fails; span still pending
    assert tr.flush() == 1          # second attempt succeeds
    assert len(store.spans_between(0, 1e12)) == 1
    store.close()


def test_closed_behind_our_back_reopens(env):
    """If another process (or a restart sweep) closes a span's row directly
    in the store, the next touch's flush must notice the touch was
    rejected and reopen a fresh span rather than silently losing it."""
    tr, store, clock = env
    tr.touch("u")
    tr.flush()
    span_id = store.spans_between(0, 1e9)[0]["id"]
    assert store.close_span(span_id, clock.t, "restart")
    clock.t += 5
    tr.touch("u")               # tracker still thinks the old span is open
    first = tr.flush()          # the touch is rejected; span queued to reopen
    assert first == 0
    second = tr.flush()         # reopens fresh on the next attempt
    assert second == 1
    spans = store.spans_between(0, 1e9)
    assert len(spans) == 2
    assert spans[0]["end_reason"] == "restart"
    assert spans[1]["open"]


def test_midnight_split(env):
    """A span that survives past local midnight (continuous touches, never
    gapping) is split into two: one ending at the last touch before
    midnight, a fresh one starting at the first touch after."""
    tr, store, clock = env
    before_midnight = date.fromtimestamp(clock.t + 40 * 3600)
    # advance the clock to just before a local midnight
    import datetime as dt
    midnight = dt.datetime.fromtimestamp(clock.t).date()
    next_midnight_ts = dt.datetime(
        midnight.year, midnight.month, midnight.day).timestamp() + 86400
    clock.t = next_midnight_ts - 30       # 30s before midnight
    tr.touch("u")
    clock.t = next_midnight_ts + 30       # 30s after midnight (60s gap, < GAP_SECONDS)
    tr.touch("u")
    tr.flush()
    spans = store.spans_between(0, 1e9)
    assert len(spans) == 2
    assert spans[0]["end_reason"] == "gap"
    assert spans[0]["end"] < next_midnight_ts <= spans[1]["start"]
    assert spans[1]["open"]


def test_close_all_queues_and_flushes(env):
    tr, store, clock = env
    tr.touch("a")
    tr.touch("b")
    tr.flush()
    clock.t += 10
    written = tr.close_all("restart")
    assert written == 2
    spans = store.spans_between(0, 1e9)
    assert len(spans) == 2
    assert all(s["end_reason"] == "restart" and not s["open"] for s in spans)
    assert tr.online() == []


def test_flush_cadence_constant_is_unchanged():
    """FLUSH_SECONDS is the cadence the app's own periodic worker should
    call flush() at; presence.py no longer flushes on its own."""
    assert FLUSH_SECONDS == 30.0
