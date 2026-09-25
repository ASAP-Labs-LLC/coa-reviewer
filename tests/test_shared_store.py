"""SharedStore: verdicts, history, presence — real SQLite in tmp_path."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from shared_store import (MAX_BATCH, MAX_HISTORY, READER_POOL_SIZE, SharedStore)


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    s = SharedStore(tmp_path / "coa_shared.db", now=clock)
    yield s
    s.close()


def test_set_and_read_verdict(store):
    assert store.set_verdict("092326-00001", "tests", "good", by="Dana P", sample_id=7)
    got = store.verdicts_for(["092326-00001"])
    v = got["092326-00001"]["tests"]
    assert v["outcome"] == "good" and v["by"] == "Dana P" and v["sample_id"] == 7


def test_verdict_is_per_mode(store):
    store.set_verdict("A", "tests", "good", by="x")
    store.set_verdict("A", "info", "bad", by="y", reason="tank wrong")
    got = store.verdicts_for(["A"])["A"]
    assert got["tests"]["outcome"] == "good"
    assert got["info"]["outcome"] == "bad" and got["info"]["reason"] == "tank wrong"


def test_set_verdict_replaces(store, clock):
    store.set_verdict("A", "tests", "bad", by="x", reason="r")
    clock.t += 10
    store.set_verdict("A", "tests", "good", by="y")
    v = store.verdicts_for(["A"])["A"]["tests"]
    assert v["outcome"] == "good" and v["by"] == "y" and v["reason"] == "" and v["at"] == clock.t


def test_clear_verdict_is_a_tombstone_not_a_delete(store):
    """clear_verdict now upserts outcome='cleared' instead of deleting the
    row, so a caller can tell "someone un-marked this at T" (a cleared row)
    from "nobody has ever judged this" (an absent key)."""
    store.set_verdict("A", "tests", "good", by="x")
    assert store.clear_verdict("A", "tests", by="y")
    v = store.verdicts_for(["A"])["A"]["tests"]
    assert v["outcome"] == "cleared" and v["by"] == "y"
    assert "A" in store.verdicts_for(["A"])  # row still present, not gone


def test_clear_verdict_requires_user(store):
    with pytest.raises(ValueError):
        store.clear_verdict("A", "tests", by="  ")


def test_verdicts_for_batches_beyond_parameter_limit(store):
    ids = [f"L{i:05d}" for i in range(MAX_BATCH * 2 + 5)]
    for i in ids[::97]:
        store.set_verdict(i, "tests", "good", by="x")
    got = store.verdicts_for(ids)
    assert set(got) == set(ids[::97])


def test_verdicts_for_empty_input(store):
    assert store.verdicts_for([]) == {}


def test_verdicts_for_strips_and_caps_input(store, caplog):
    caplog.set_level(logging.WARNING)
    ids = [f" L{i:05d} " for i in range(10)]
    got = store.verdicts_for(ids)
    assert got == {}   # nothing marked yet; just confirms whitespace didn't error
    huge = [f"L{i:06d}" for i in range(10_050)]
    store.verdicts_for(huge)
    assert "capped" in caplog.text.lower()


@pytest.mark.parametrize("args", [
    ("", "tests", "good"), ("A", "nope", "good"), ("A", "tests", "meh"),
])
def test_set_verdict_rejects_bad_input(store, args):
    with pytest.raises(ValueError):
        store.set_verdict(*args, by="x")


def test_set_verdict_requires_user(store):
    with pytest.raises(ValueError):
        store.set_verdict("A", "tests", "good", by="  ")


def test_record_event_and_history_newest_first(store, clock):
    store.record_event("A", "test_result", user="Dana P", field="moisture",
                       before="11.2", after="12.0")
    clock.t += 5
    store.record_event("A", "mark", user="Ryan C", field="tests", after="good",
                       detail={"reason": ""})
    hist = store.history("A")
    assert [h["kind"] for h in hist] == ["mark", "test_result"]
    assert hist[1]["before"] == "11.2" and hist[1]["after"] == "12.0"
    assert hist[0]["detail"] == {"reason": ""}


def test_history_coerces_non_text_values(store):
    store.record_event("A", "sample_info", user="u", field="n", before=None, after=3.5)
    h = store.history("A")[0]
    assert h["before"] is None and h["after"] == "3.5"


def test_history_limit_is_clamped(store):
    for i in range(MAX_HISTORY + 20):
        store.record_event("A", "comments", user="u", after=str(i))
    assert len(store.history("A", limit=10_000)) == MAX_HISTORY
    assert len(store.history("A", limit=0)) == 1


def test_record_event_rejects_unknown_kind(store):
    with pytest.raises(ValueError):
        store.record_event("A", "made_up", user="u")


def test_detail_string_values_are_pretruncated(store):
    """One long string value in detail is truncated per-field so the
    overall JSON document stays small and valid."""
    from shared_store import MAX_TEXT
    store.record_event("A", "comments", user="u", after="x",
                       detail={"reason": "y" * (MAX_TEXT * 2)})
    h = store.history("A")[0]
    assert len(h["detail"]["reason"]) <= MAX_TEXT // 4


def test_detail_still_too_big_after_pretruncation_stores_sentinel(store):
    """Many large fields can still add up past MAX_TEXT even after each is
    pre-truncated; that case stores a small sentinel instead of truncating
    the JSON mid-document (which would make it undecodable)."""
    from shared_store import MAX_TEXT
    huge = {f"field_{i}": "y" * (MAX_TEXT // 4) for i in range(20)}
    store.record_event("A", "comments", user="u", after="x", detail=huge)
    h = store.history("A")[0]
    assert h["detail"] == {"truncated": True}


def test_events_between(store, clock):
    store.record_event("A", "mark", user="u", after="good")
    clock.t += 100
    store.record_event("B", "mark", user="v", after="bad")
    rows = store.events_between(clock.t - 50, clock.t + 1)
    assert [r["lab_id"] for r in rows] == ["B"]


def test_event_marks_between(store, clock):
    store.record_event("A", "mark", user="u", after="good")
    clock.t += 100
    store.record_event("B", "unmark", user="v", after=None)
    rows = store.event_marks_between(clock.t - 50, clock.t + 1)
    assert rows == [{"user": "v", "at": clock.t, "kind": "unmark"}]


def test_presence_span_lifecycle(store):
    sid = store.open_span("Dana P", 100.0)
    assert isinstance(sid, int)
    assert store.touch_span(sid, 160.0)
    assert store.close_span(sid, 170.0, "logout")
    spans = store.spans_between(0, 1000)
    assert spans == [{"id": sid, "user": "Dana P", "start": 100.0, "end": 170.0,
                      "open": False, "end_reason": "logout"}]


def test_touch_span_on_closed_span_returns_false(store):
    sid = store.open_span("x", 100.0)
    assert store.close_span(sid, 110.0, "logout")
    assert store.touch_span(sid, 120.0) is False


def test_close_span_on_closed_span_returns_false(store):
    sid = store.open_span("x", 100.0)
    assert store.close_span(sid, 110.0, "logout")
    assert store.close_span(sid, 120.0, "logout") is False


def test_close_open_spans_on_restart(store):
    a = store.open_span("x", 100.0)
    store.touch_span(a, 150.0)
    assert store.close_open_spans("restart") == 1
    s = store.spans_between(0, 1000)[0]
    assert s["end"] == 150.0 and s["end_reason"] == "restart" and s["open"] is False


def test_spans_between_boundaries(store):
    a = store.open_span("x", 100.0)
    store.close_span(a, 200.0, "logout")
    b = store.open_span("y", 200.0)
    store.close_span(b, 300.0, "logout")
    # a span ending exactly at the query start is included
    assert [s["id"] for s in store.spans_between(200.0, 500.0)] == [a, b]
    # a span starting exactly at the query end is excluded
    assert [s["id"] for s in store.spans_between(0.0, 200.0)] == [a]


def test_spans_between_includes_open_and_overlapping(store):
    a = store.open_span("x", 100.0)
    store.touch_span(a, 500.0)
    assert len(store.spans_between(400, 450)) == 1
    assert store.spans_between(600, 700) == []


def test_recorded_range(store):
    assert store.recorded_range() == (None, None)
    store.open_span("x", 100.0)
    store.record_event("A", "mark", user="u", after="good")
    lo, hi = store.recorded_range()
    assert lo == 100.0 and hi >= 100.0


def test_meta_flag(store):
    assert store.get_meta("k") is None
    assert store.set_meta("k", "v")
    assert store.get_meta("k") == "v"


# ── apply_mark: one atomic upsert + event ──────────────────────────────────

def test_apply_mark_first_time_has_no_before(store, clock):
    result = store.apply_mark("A", "tests", "good", by="Dana P", tab="Yesterday")
    assert result["before"] is None
    assert result["verdicts"]["tests"]["outcome"] == "good"
    hist = store.history("A")
    assert hist[0]["kind"] == "mark" and hist[0]["before"] is None
    assert hist[0]["after"] == "good" and hist[0]["field"] == "tests"
    assert hist[0]["detail"] == {"tab": "Yesterday"}


def test_apply_mark_records_previous_outcome(store):
    store.apply_mark("A", "tests", "good", by="x")
    result = store.apply_mark("A", "tests", "bad", by="y", reason="wrong result")
    assert result["before"] == "good"
    assert result["verdicts"]["tests"]["outcome"] == "bad"
    hist = store.history("A")
    assert hist[0]["before"] == "good" and hist[0]["after"] == "bad"
    assert hist[0]["detail"] == {"reason": "wrong result"}


def test_apply_mark_clear_is_an_unmark_event_with_no_after(store):
    store.apply_mark("A", "tests", "good", by="x")
    result = store.apply_mark("A", "tests", "cleared", by="y")
    assert result["before"] == "good"
    assert result["verdicts"]["tests"]["outcome"] == "cleared"
    hist = store.history("A")
    assert hist[0]["kind"] == "unmark"
    assert hist[0]["after"] is None and hist[0]["before"] == "good"


def test_apply_mark_after_a_clear_reports_no_before(store):
    store.apply_mark("A", "tests", "good", by="x")
    store.apply_mark("A", "tests", "cleared", by="x")
    result = store.apply_mark("A", "tests", "good", by="z")
    assert result["before"] is None   # a cleared verdict is not "a previous outcome"


def test_apply_mark_rejects_bad_outcome(store):
    with pytest.raises(ValueError):
        store.apply_mark("A", "tests", "meh", by="x")


def test_apply_mark_is_atomic_on_failure(store):
    """If the event-insert half of the transaction fails, the verdict half
    must not be left written. A trigger that raises on a marked insert
    simulates the failure without reaching into sqlite3's (immutable, in
    this Python) C-level connection type."""
    store.apply_mark("A", "tests", "good", by="x")
    conn = store._connection()
    conn.execute(
        "CREATE TRIGGER fail_sample_events BEFORE INSERT ON sample_events "
        "WHEN NEW.user = 'FORCE_FAIL' "
        "BEGIN SELECT RAISE(ABORT, 'simulated failure'); END")
    try:
        result = store.apply_mark("A", "tests", "bad", by="FORCE_FAIL")
    finally:
        conn2 = store._connection()
        if conn2 is not None:
            conn2.execute("DROP TRIGGER IF EXISTS fail_sample_events")
    assert result is None
    v = store.verdicts_for(["A"])["A"]["tests"]
    assert v["outcome"] == "good" and v["by"] == "x"   # unchanged: rolled back
    conn = store._connection()
    assert conn is not None and conn.in_transaction is False  # N2: rolled back cleanly


# ── lock / corruption handling ──────────────────────────────────────────────

def test_busy_write_returns_default_fast_and_next_write_succeeds(tmp_path):
    path = tmp_path / "db.sqlite"
    s = SharedStore(path)
    s.set_verdict("seed", "tests", "good", by="x")   # ensure schema exists
    blocker = sqlite3.connect(str(path), timeout=1.0)
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute("INSERT INTO meta (key, value) VALUES ('lock','x')")
    try:
        t0 = time.perf_counter()
        ok = s.set_verdict("A", "tests", "good", by="x")
        elapsed = time.perf_counter() - t0
        assert ok is False
        assert elapsed < 2.0
    finally:
        blocker.rollback()
        blocker.close()
    assert s.set_verdict("A", "tests", "good", by="x") is True
    s.close()


def test_corrupt_database_is_quarantined_and_store_recovers(tmp_path, caplog):
    caplog.set_level(logging.ERROR)
    path = tmp_path / "coa_shared.db"
    s = SharedStore(path)
    s.set_verdict("A", "tests", "good", by="x")
    s.close()
    with open(path, "r+b") as f:
        f.seek(0)
        f.write(b"not a sqlite database, definitely not" * 50)
    s2 = SharedStore(path)
    assert s2.set_verdict("A", "tests", "good", by="x") is True
    quarantined = list(tmp_path.glob("coa_shared.db.corrupt-*"))
    assert len(quarantined) == 1
    assert "corrupt" in caplog.text.lower()
    s2.close()


def test_unwritable_path_degrades(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    s = SharedStore(blocker / "sub" / "db.sqlite")
    assert s.set_verdict("A", "tests", "good", by="x") is False
    assert s.verdicts_for(["A"]) is None
    assert s.history("A") == []
    assert s.open_span("x", 1.0) is None
    assert "shared store unavailable" in caplog.text


def test_survives_reopen(tmp_path):
    p = tmp_path / "db.sqlite"
    s = SharedStore(p)
    s.set_verdict("A", "tests", "good", by="x")
    s.close()
    s2 = SharedStore(p)
    assert s2.verdicts_for(["A"])["A"]["tests"]["by"] == "x"
    s2.close()


def test_concurrent_writers(store):
    def work(n):
        for i in range(50):
            store.record_event(f"L{n}", "comments", user="u", after=str(i))
    ts = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(len(store.history(f"L{n}")) for n in range(8)) == 400


def test_reader_pool_concurrency(store):
    """More readers than READER_POOL_SIZE must still all succeed (queueing
    behind the pool rather than erroring), while a writer keeps writing."""
    for i in range(20):
        store.record_event("A", "comments", user="u", after=str(i))

    errors = []

    def read_many():
        try:
            for _ in range(20):
                store.history("A")
        except Exception as exc:  # pragma: no cover - failure path only
            errors.append(exc)

    def write_more():
        for i in range(20, 60):
            store.record_event("A", "comments", user="u", after=str(i))

    readers = [threading.Thread(target=read_many) for _ in range(READER_POOL_SIZE * 3)]
    writer = threading.Thread(target=write_more)
    [t.start() for t in readers + [writer]]
    [t.join() for t in readers + [writer]]
    assert errors == []
    assert len(store.history("A", limit=MAX_HISTORY)) == 60


def test_performance_budget(store):
    """Smoke guard, not a benchmark: this only exists to catch an
    accidentally-quadratic query path, not to gate CI on absolute timing."""
    ids = [f"L{i:05d}" for i in range(300)]
    t0 = time.perf_counter()
    for i in ids:
        store.set_verdict(i, "tests", "good", by="x")
        store.record_event(i, "mark", user="x", after="good")
    per_mark_ms = (time.perf_counter() - t0) * 1000 / len(ids)
    t1 = time.perf_counter()
    store.verdicts_for(ids)
    load_ms = (time.perf_counter() - t1) * 1000
    assert per_mark_ms < 50, per_mark_ms
    assert load_ms < 250, load_ms


# ── N1: reader pool must not leak on a non-sqlite exception ────────────────

def test_run_read_does_not_leak_reader_on_non_sqlite_exception(store):
    def boom(c):
        raise TypeError("boom")
    for _ in range(READER_POOL_SIZE + 2):
        with pytest.raises(TypeError):
            store._run_read("boom", boom, None)
    # the pool must have recovered: a normal read works right away, not
    # after waiting out READER_ACQUIRE_TIMEOUT_SECONDS for an exhausted pool
    t0 = time.perf_counter()
    assert store.history("A") == []
    assert (time.perf_counter() - t0) < 1.0


@pytest.mark.parametrize("method,args", [
    ("events_between", ("not-a-number", 100)),
    ("event_marks_between", (0, "not-a-number")),
    ("spans_between", ("nope", 100)),
])
def test_reads_validate_numeric_args_before_touching_the_reader_pool(store, method, args):
    with pytest.raises(ValueError):
        getattr(store, method)(*args)
    assert store._readers._created == 0   # never even tried to acquire one


# ── N2: a malformed batch must never leave the writer mid-transaction ──────

def test_apply_presence_invalid_op_never_begins_a_transaction(store):
    with pytest.raises(ValueError):
        store.apply_presence([{"op": "bogus"}])
    conn = store._connection()
    assert conn is not None and conn.in_transaction is False


def test_apply_presence_invalid_op_leaves_earlier_valid_ops_unapplied(store):
    """The whole batch is validated before BEGIN, so one bad op means
    nothing in the batch lands — not even the valid ones ahead of it."""
    ops = [{"op": "open", "user": "u", "started": 1.0, "last_seen": 1.0},
          {"op": "bogus"}]
    with pytest.raises(ValueError):
        store.apply_presence(ops)
    assert store.spans_between(0, 1e12) == []
    conn = store._connection()
    assert conn is not None and conn.in_transaction is False


def test_apply_mark_invalid_outcome_never_begins_a_transaction(store):
    with pytest.raises(ValueError):
        store.apply_mark("A", "tests", "not-a-real-outcome", by="x")
    conn = store._connection()
    assert conn is not None and conn.in_transaction is False


# ── minors: $prev must never resolve to a bool result ───────────────────────

def test_prev_span_id_must_follow_an_open(store):
    ops = [{"op": "touch", "span_id": 999999, "last_seen": 1.0},
          {"op": "close", "span_id": "$prev", "ended_at": 2.0, "reason": "logout"}]
    with pytest.raises(ValueError):
        store.apply_presence(ops)


def test_resolve_span_id_rejects_bool():
    with pytest.raises(ValueError):
        SharedStore._resolve_span_id("$prev", [True])
    with pytest.raises(ValueError):
        SharedStore._resolve_span_id(True, [])


# ── N5: quarantine must drop handles first and not loop on a bad rename ────

def test_quarantine_bumps_reader_generation_and_recovers(tmp_path, caplog):
    """The connection that first notices corruption is a *fresh* one — a
    live connection's cached schema/page state won't necessarily notice
    bytes rewritten underneath it, same as in production where the process
    that finds the corruption is usually one that just (re)opened."""
    caplog.set_level(logging.ERROR)
    path = tmp_path / "coa_shared.db"
    s = SharedStore(path)
    s.set_verdict("A", "tests", "good", by="x")
    s.close()
    with open(path, "r+b") as f:
        f.write(b"not a sqlite database, definitely not" * 50)
    s2 = SharedStore(path)
    gen_before = s2._readers._generation
    assert s2.set_verdict("B", "tests", "good", by="y") is True
    assert s2._readers._generation > gen_before
    quarantined = list(tmp_path.glob("coa_shared.db.corrupt-*"))
    assert len(quarantined) == 1
    got = s2.verdicts_for(["A", "B"])
    assert "A" not in got              # old data gone with the corrupt file
    assert got["B"]["tests"]["by"] == "y"
    s2.close()


def test_quarantine_disabled_after_rename_failure_does_not_loop(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.ERROR)
    path = tmp_path / "coa_shared.db"
    s = SharedStore(path)
    s.set_verdict("A", "tests", "good", by="x")
    s.close()
    path.write_bytes(b"not a sqlite database, definitely not" * 50)

    calls = {"n": 0}

    def failing_rename(self, target):
        calls["n"] += 1
        raise OSError(32, "simulated WinError 32: file in use")

    monkeypatch.setattr(Path, "rename", failing_rename)

    s2 = SharedStore(path)
    assert s2.set_verdict("B", "tests", "good", by="y") is False
    first_attempts = calls["n"]
    assert first_attempts >= 1
    assert s2._quarantine_disabled is True

    assert s2.set_verdict("C", "tests", "good", by="z") is False
    assert calls["n"] == first_attempts   # no retry loop on the failing rename
    s2.close()


# ── minors: error classification keeps the connection for non-fatal errors ──

def test_classify_recognizes_numeric_primary_code(store):
    """A real sqlite3.Error carries a genuine ``sqlite_errorcode`` set by
    the C extension; a syntax error is SQLITE_ERROR (1), which must
    classify as "keep" (bad query, not damage or contention)."""
    from shared_store import _classify
    conn = store._connection()
    try:
        conn.execute("THIS IS NOT VALID SQL")
    except sqlite3.Error as exc:
        assert _classify(exc) == "keep"
    else:
        pytest.fail("expected a sqlite3.Error")


def test_constraint_style_errors_keep_the_connection_no_backoff(store):
    """CONSTRAINT/MISUSE/RANGE/a plain SQLITE_ERROR is a bad query, not
    file damage or contention: the connection is kept and no backoff is
    engaged, unlike a real I/O error."""
    conn_before = store._connection()

    def boom(c):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: verdicts.lab_id")
    assert store._run("t", boom, False) is False
    assert store._conn is conn_before      # connection kept, not dropped
    assert store._backoff_until is None    # no backoff engaged


def test_no_such_table_still_drops_the_connection(store):
    """"no such table" means the schema is missing — unlike a generic bad
    query, that connection really is no good, so it still drops."""
    def boom(c):
        raise sqlite3.OperationalError("no such table: bogus")
    assert store._run("t", boom, False) is False
    assert store._conn is None


def test_readers_share_writer_backoff(tmp_path):
    s = SharedStore(tmp_path / "db")
    s.set_verdict("A", "tests", "good", by="x")   # establish schema
    s._backoff_until = time.monotonic() + 10       # force writer backoff
    assert s.history("A") == []
    assert s._readers._created == 0                 # skipped opening, didn't try
    s.close()
