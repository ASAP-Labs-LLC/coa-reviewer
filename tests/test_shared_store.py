"""SharedStore: verdicts, history, presence — real SQLite in tmp_path."""
from __future__ import annotations

import sqlite3
import time

import pytest

from shared_store import (MAX_BATCH, MAX_HISTORY, SharedStore)


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


def test_clear_verdict(store):
    store.set_verdict("A", "tests", "good", by="x")
    assert store.clear_verdict("A", "tests")
    assert store.verdicts_for(["A"]) == {}


def test_verdicts_for_batches_beyond_parameter_limit(store):
    ids = [f"L{i:05d}" for i in range(MAX_BATCH * 2 + 5)]
    for i in ids[::97]:
        store.set_verdict(i, "tests", "good", by="x")
    got = store.verdicts_for(ids)
    assert set(got) == set(ids[::97])


def test_verdicts_for_empty_input(store):
    assert store.verdicts_for([]) == {}


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


def test_events_between(store, clock):
    store.record_event("A", "mark", user="u", after="good")
    clock.t += 100
    store.record_event("B", "mark", user="v", after="bad")
    rows = store.events_between(clock.t - 50, clock.t + 1)
    assert [r["lab_id"] for r in rows] == ["B"]


def test_presence_span_lifecycle(store):
    sid = store.open_span("Dana P", 100.0)
    assert isinstance(sid, int)
    assert store.touch_span(sid, 160.0)
    assert store.close_span(sid, 170.0, "logout")
    spans = store.spans_between(0, 1000)
    assert spans == [{"id": sid, "user": "Dana P", "start": 100.0, "end": 170.0,
                      "open": False, "end_reason": "logout"}]


def test_close_open_spans_on_restart(store):
    a = store.open_span("x", 100.0)
    store.touch_span(a, 150.0)
    assert store.close_open_spans("restart") == 1
    s = store.spans_between(0, 1000)[0]
    assert s["end"] == 150.0 and s["end_reason"] == "restart" and s["open"] is False


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
    import threading
    def work(n):
        for i in range(50):
            store.record_event(f"L{n}", "comments", user="u", after=str(i))
    ts = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(len(store.history(f"L{n}")) for n in range(8)) == 400


def test_performance_budget(store):
    """A mark is one upsert + one insert; a tab load one batched select."""
    ids = [f"L{i:05d}" for i in range(300)]
    t0 = time.perf_counter()
    for i in ids:
        store.set_verdict(i, "tests", "good", by="x")
        store.record_event(i, "mark", user="x", after="good")
    per_mark_ms = (time.perf_counter() - t0) * 1000 / len(ids)
    t1 = time.perf_counter()
    store.verdicts_for(ids)
    load_ms = (time.perf_counter() - t1) * 1000
    assert per_mark_ms < 25, per_mark_ms
    assert load_ms < 100, load_ms
