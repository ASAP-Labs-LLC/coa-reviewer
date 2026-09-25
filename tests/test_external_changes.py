"""Changes made outside COA Reviewer (spec §3b), store level.

``observe`` compares what a read just saw with the last value COA Reviewer
saw or wrote for each field. A difference nobody here made is recorded as an
``external_change`` history row; COA Reviewer's own edits update the snapshot
in the same transaction as their history row so they never resurface.
"""
from __future__ import annotations

import time

import pytest

import shared_store
from shared_store import (EXTERNAL_ACTOR, MAX_OBSERVE_FIELDS, SharedStore, same_value)


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


def _external(store, lab_id="092326-00001"):
    return [e for e in store.history(lab_id) if e["kind"] == "external_change"]


def _count_writes(monkeypatch, store):
    calls = {"n": 0}
    real = store._run

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)
    monkeypatch.setattr(store, "_run", counting)
    return calls


# ── baseline and differences ─────────────────────────────────────────────

def test_first_sighting_is_a_silent_baseline(store):
    n = store.observe("092326-00001", "qbench_test", {"Water": "12.1", "Sulfur": "0.5"})
    assert n == 0
    assert store.history("092326-00001") == []
    assert store.snapshots("092326-00001", "qbench_test") == {"Water": "12.1", "Sulfur": "0.5"}


def test_a_difference_is_recorded_with_before_after_and_since(store, clock):
    store.observe("092326-00001", "qbench_test", {"Water": "12.1"})
    first_seen = clock.t
    clock.t += 600
    n = store.observe("092326-00001", "qbench_test", {"Water": "12.5"})
    assert n == 1
    [e] = _external(store)
    assert e["user"] == EXTERNAL_ACTOR
    assert e["field"] == "Water"
    assert (e["before"], e["after"]) == ("12.1", "12.5")
    assert e["detail"]["source"] == "qbench_test"
    assert e["detail"]["since"] == first_seen
    assert e["detail"]["detected_at"] == clock.t
    assert "changed_at" not in e["detail"]
    # the snapshot moved on: seeing 12.5 again is not another change
    assert store.observe("092326-00001", "qbench_test", {"Water": "12.5"}) == 0
    assert len(_external(store)) == 1


def test_actor_hint_and_changed_at_are_kept(store):
    store.observe("A", "labvision_test", {"Water": ""})
    store.observe("A", "labvision_test", {"Water": "11.9"}, actor_hint="kejuan",
                  changed_at="2026-09-25 09:12:00")
    [e] = _external(store, "A")
    assert e["user"] == "kejuan"
    assert e["detail"]["changed_at"] == "2026-09-25 09:12:00"


def test_per_field_meta_overrides_the_call_wide_hint(store):
    store.observe("A", "labvision_test", {"Water": "1", "Ash": "2"})
    store.observe("A", "labvision_test", {"Water": "3", "Ash": "4"}, actor_hint="x",
                  field_meta={"Ash": {"actor": "dana", "changed_at": "t1"}})
    got = {e["field"]: e for e in _external(store, "A")}
    assert got["Water"]["user"] == "x"
    assert got["Ash"]["user"] == "dana" and got["Ash"]["detail"]["changed_at"] == "t1"


def test_new_field_later_is_a_baseline_not_a_change(store):
    store.observe("A", "qbench_info", {"fw": "1"})
    assert store.observe("A", "qbench_info", {"fw": "1", "tank": "T2"}) == 0
    assert _external(store, "A") == []


def test_sources_are_independent(store):
    store.observe("A", "qbench_test", {"Water": "1"})
    assert store.observe("A", "labvision_test", {"Water": "2"}) == 0


# ── normalisation ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("a,b", [
    ("12", "12.00"), ("12", " 12 "), ("0.5", ".5"), ("-0", "0"),
    ("a  b", " a b "), ("line one\n\nline two", "line one line two"),
    (None, ""), (None, "   "), (12, "12.0"), ("1" * 40, "1" * 40 + ".0"),
])
def test_equal_after_normalising(a, b):
    assert same_value(a, b)


@pytest.mark.parametrize("a,b", [
    ("12", "12.1"), ("Pass", "pass"), ("1e3", "1000"), ("NaN", "nan0"),
    ("inf", "Infinity"), ("12", ""), ("0.1", "0.10000000000000000001"),
])
def test_different_after_normalising(a, b):
    assert not same_value(a, b)


def test_nan_and_inf_are_compared_as_text():
    assert same_value("NaN", "NaN")
    assert not same_value("NaN", "nan ")   # text, case-sensitive
    assert not same_value("inf", "1e999")


def test_very_long_strings_compare_without_blowing_up():
    long_a = "9" * 100_000
    assert same_value(long_a, long_a)
    early = "8" + long_a[1:]
    assert not same_value(long_a, early)
    # Values are bounded to MAX_TEXT exactly as stored, so a difference past
    # that point cannot be seen — documented, and it keeps each compare cheap.
    assert same_value(long_a, long_a[:-1] + "8")
    assert shared_store.MAX_TEXT < 100_000


def test_12_vs_12_00_is_not_a_change(store):
    store.observe("A", "qbench_test", {"Water": "12"})
    assert store.observe("A", "qbench_test", {"Water": "12.00 "}) == 0
    assert _external(store, "A") == []


# ── own edits never resurface ────────────────────────────────────────────

def test_own_edit_with_snapshot_is_not_reported_later(store):
    store.observe("A", "qbench_test", {"Water": "12.1"})
    assert store.record_event("A", "test_result", user="Dana P", field="Water",
                              before="12.1", after="12.9",
                              snapshot=("qbench_test", "Water", "12.9"))
    assert store.observe("A", "qbench_test", {"Water": "12.9"}) == 0
    kinds = [e["kind"] for e in store.history("A")]
    assert kinds == ["test_result"]


def test_record_events_batches_rows_and_snapshots(store):
    ok = store.record_events("A", [
        {"kind": "sample_info", "user": "Dana P", "field": "fw", "before": "1",
         "after": "2", "snapshot": ("qbench_info", "fw", "2")},
        {"kind": "sample_info", "user": "Dana P", "field": "tank", "before": None,
         "after": "T", "snapshot": ("qbench_info", "tank", "T")},
    ])
    assert ok
    assert store.snapshots("A", "qbench_info") == {"fw": "2", "tank": "T"}
    assert len(store.history("A")) == 2


def test_update_snapshots_records_nothing(store):
    assert store.update_snapshots("A", "qbench_comments", {"comments": "hi"})
    assert store.history("A") == []
    assert store.observe("A", "qbench_comments", {"comments": "hi"}) == 0


def test_bad_snapshot_source_is_a_programming_error(store):
    with pytest.raises(ValueError):
        store.record_event("A", "comments", user="x", snapshot=("nope", "f", "v"))
    with pytest.raises(ValueError):
        store.observe("A", "nope", {"f": "v"})


# ── writes only when something changed ───────────────────────────────────

def test_read_only_fast_path_does_no_write(store, monkeypatch):
    store.observe("A", "qbench_test", {"Water": "12.1", "Ash": "3"})
    writes = _count_writes(monkeypatch, store)
    for _ in range(5):
        assert store.observe("A", "qbench_test", {"Water": "12.10", "Ash": "3"}) == 0
    assert writes["n"] == 0


def test_stale_seen_at_is_refreshed_once(store, clock, monkeypatch):
    store.observe("A", "qbench_test", {"Water": "1"})
    clock.t += shared_store.SNAPSHOT_REFRESH_SECONDS + 1
    writes = _count_writes(monkeypatch, store)
    assert store.observe("A", "qbench_test", {"Water": "1"}) == 0
    assert store.observe("A", "qbench_test", {"Water": "1"}) == 0
    assert writes["n"] == 1
    clock.t += 5
    store.observe("A", "qbench_test", {"Water": "2"})
    [e] = _external(store, "A")
    assert e["detail"]["since"] == clock.t - 5


def test_hundred_unchanged_observes_are_fast_and_write_nothing(store, monkeypatch):
    values = {f"Test {i}": str(i) for i in range(30)}
    store.observe("A", "qbench_test", values)
    writes = _count_writes(monkeypatch, store)
    started = time.perf_counter()
    for _ in range(100):
        assert store.observe("A", "qbench_test", values) == 0
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert writes["n"] == 0
    assert elapsed_ms < 200, f"100 observes took {elapsed_ms:.0f} ms"


# ── bounds and failure ───────────────────────────────────────────────────

def test_fields_are_bounded(store, caplog):
    values = {f"f{i}": "v" for i in range(MAX_OBSERVE_FIELDS + 50)}
    assert store.observe("A", "qbench_info", values) == 0
    assert len(store.snapshots("A", "qbench_info")) == MAX_OBSERVE_FIELDS
    assert "capped" in caplog.text


def test_store_down_returns_none_and_never_raises(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    s = SharedStore(blocker / "sub" / "db.sqlite")
    assert s.observe("A", "qbench_test", {"Water": "1"}) is None
    assert s.snapshots("A", "qbench_test") is None
    assert s.update_snapshots("A", "qbench_test", {"Water": "1"}) is False
    assert s.record_event("A", "comments", user="x",
                          snapshot=("qbench_comments", "comments", "y")) is False


def test_external_change_is_a_known_kind():
    assert "external_change" in shared_store.EVENT_KINDS
    assert shared_store.SOURCES == ("qbench_test", "qbench_info", "qbench_comments",
                                    "labvision_test")
