from __future__ import annotations

import datetime as dt
import threading
import time

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
    rejected and reopen a fresh span — starting at the moment we noticed,
    not inheriting the stale original start time — rather than silently
    losing it."""
    tr, store, clock = env
    tr.touch("u")
    tr.flush()
    span_id = store.spans_between(0, 1e9)[0]["id"]
    assert store.close_span(span_id, clock.t, "restart")
    clock.t += 5
    reopen_point = clock.t
    tr.touch("u")               # tracker still thinks the old span is open
    first = tr.flush()          # the touch is rejected; span queued to reopen
    assert first == 0
    second = tr.flush()         # reopens fresh on the next attempt
    assert second == 1
    spans = store.spans_between(0, 1e9)
    assert len(spans) == 2
    assert spans[0]["end_reason"] == "restart"
    assert spans[1]["open"]
    assert spans[1]["start"] == reopen_point   # not the old span's original start


def test_midnight_split_is_exact_and_not_zero_length(env):
    """A span that survives past local midnight (continuous touches, never
    gapping) is split into two exactly at the boundary: the old span ends
    at midnight (reason "midnight"), the new one starts at midnight — and
    the first touch after midnight must not be lost, so the new span's
    last_seen reflects it rather than leaving a zero-length row."""
    tr, store, clock = env
    midnight_date = dt.datetime.fromtimestamp(clock.t).date()
    next_midnight_ts = dt.datetime(
        midnight_date.year, midnight_date.month, midnight_date.day).timestamp() + 86400
    clock.t = next_midnight_ts - 30       # 23:59:30
    tr.touch("u")
    clock.t = next_midnight_ts + 29       # 00:00:29 (59s later, < GAP_SECONDS)
    tr.touch("u")
    tr.flush()
    spans = store.spans_between(0, next_midnight_ts + 100)
    assert len(spans) == 2
    assert spans[0]["end"] == next_midnight_ts and spans[0]["end_reason"] == "midnight"
    assert spans[1]["start"] == next_midnight_ts
    assert spans[1]["end"] == next_midnight_ts + 29   # the touch isn't lost
    assert spans[1]["open"]


# ── N3: a span's identity survives being retired mid-flush ─────────────────

def test_open_result_applies_even_if_span_retired_during_the_db_call(env):
    """If the user logs out while an "open" apply_presence call for their
    span is in flight, the id that call returns must still land on the
    (now-retired) span object — otherwise the next flush can't find it by
    id to close it, and instead opens a *second*, duplicate row while the
    first is left open forever."""
    tr, store, clock = env
    real_apply = store.apply_presence

    def wrapper(ops):
        if any(op["op"] == "open" for op in ops):
            tr.end("u", "logout")   # concurrent logout while this call is "in flight"
        return real_apply(ops)

    store.apply_presence = wrapper
    tr.touch("u")
    tr.flush()    # the open's result must be applied despite the concurrent end()
    tr.flush()    # closes using that id rather than opening a second row
    spans = store.spans_between(0, 1e12)
    assert len(spans) == 1
    assert spans[0]["end_reason"] == "logout" and not spans[0]["open"]


# ── N4: flush() is single-flight ────────────────────────────────────────────

def test_concurrent_flushes_do_not_duplicate_a_span(env):
    tr, store, clock = env
    tr.touch("u")
    real_apply = store.apply_presence

    def slow_apply(ops):
        time.sleep(0.05)
        return real_apply(ops)

    store.apply_presence = slow_apply
    results = []

    def run():
        results.append(tr.flush())

    threads = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [0, 1]   # one flushed, the other skipped its cycle
    assert len(store.spans_between(0, 1e12)) == 1


def test_flush_blocking_waits_instead_of_skipping(env):
    tr, store, clock = env
    tr.touch("u")
    real_apply = store.apply_presence

    def slow_apply(ops):
        time.sleep(0.05)
        return real_apply(ops)

    store.apply_presence = slow_apply
    results = []

    def run(blocking):
        results.append(tr.flush(blocking=blocking, timeout=1.0))

    t1 = threading.Thread(target=run, args=(False,))
    t1.start()
    time.sleep(0.01)   # let t1 grab the flush lock first
    run(True)          # blocks until t1 releases it, then runs for real
    t1.join()
    assert sorted(results) == [0, 1]
    assert len(store.spans_between(0, 1e12)) == 1


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


def test_bare_flush_is_non_blocking_and_skips_a_busy_cycle(env):
    """The default flush() call (what a periodic worker uses) must not
    block: if a flush is already running, it skips this cycle rather than
    waiting, so the worker's loop cadence (FLUSH_SECONDS) is never stalled
    by a slow write."""
    tr, store, clock = env
    tr.touch("u")
    real_apply = store.apply_presence

    def slow_apply(ops):
        time.sleep(0.1)
        return real_apply(ops)

    store.apply_presence = slow_apply
    t0 = time.perf_counter()
    t1 = threading.Thread(target=tr.flush)
    t1.start()
    time.sleep(0.02)   # let t1 acquire the flush lock
    skipped = tr.flush()   # bare call: must return immediately, not wait ~0.1s
    elapsed = time.perf_counter() - t0
    t1.join()
    assert skipped == 0
    assert elapsed < 0.09
