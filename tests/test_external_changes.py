"""Changes made outside COA Reviewer (spec §3b).

``observe`` compares what a read just saw with the last value COA Reviewer
saw or wrote for each field. A difference nobody here made is recorded as an
``external_change`` history row; COA Reviewer's own edits update the snapshot
in the same transaction as their history row so they never resurface, and a
read that started before the snapshot was last confirmed is too old to judge.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime

import pytest

import shared_store
from shared_store import (EXTERNAL_ACTOR, MAX_OBSERVE_FIELDS, WRITER_BUSY, SharedStore,
                          same_value)


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


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")


# ── baseline and differences ─────────────────────────────────────────────

def test_first_sighting_is_a_silent_baseline(store):
    n = store.observe("092326-00001", "qbench_test", {"test:1": "12.1", "test:2": "0.5"})
    assert n == 0
    assert store.history("092326-00001") == []
    assert store.snapshots("092326-00001", "qbench_test") == {"test:1": "12.1", "test:2": "0.5"}


def test_a_difference_is_recorded_with_before_after_and_since(store, clock):
    store.observe("092326-00001", "qbench_test", {"test:1": "12.1"})
    first_seen = clock.t
    clock.t += 600
    n = store.observe("092326-00001", "qbench_test", {"test:1": "12.5"},
                      field_meta={"test:1": {"label": "Water"}})
    assert n == 1
    [e] = _external(store)
    assert e["user"] == EXTERNAL_ACTOR
    assert e["field"] == "Water"                       # the label, not the key
    assert e["detail"]["key"] == "test:1"
    assert (e["before"], e["after"]) == ("12.1", "12.5")
    assert e["detail"]["source"] == "qbench_test"
    assert e["detail"]["since"] == first_seen
    assert e["detail"]["detected_at"] == clock.t
    assert "changed_at" not in e["detail"]
    assert store.observe("092326-00001", "qbench_test", {"test:1": "12.5"}) == 0
    assert len(_external(store)) == 1


def test_actor_hint_and_changed_at_in_the_window_are_kept(store, clock):
    store.observe("A", "labvision_test", {"Water": ""})
    clock.t += 3600
    when = _iso(clock.t - 1800)
    store.observe("A", "labvision_test", {"Water": "11.9"}, actor_hint="kejuan",
                  changed_at=when)
    [e] = _external(store, "A")
    assert e["user"] == "kejuan"
    assert e["detail"]["changed_at"] == when


@pytest.mark.parametrize("offset", [-7200, +7200])
def test_a_changed_at_outside_since_and_detected_is_dropped(store, clock, offset):
    store.observe("A", "labvision_test", {"Water": ""})
    clock.t += 3600
    store.observe("A", "labvision_test", {"Water": "1"}, changed_at=_iso(clock.t + offset))
    [e] = _external(store, "A")
    assert "changed_at" not in e["detail"]


def test_an_unparsable_changed_at_is_dropped(store, clock):
    store.observe("A", "labvision_test", {"Water": ""})
    clock.t += 60
    store.observe("A", "labvision_test", {"Water": "1"}, changed_at="yesterday-ish")
    [e] = _external(store, "A")
    assert "changed_at" not in e["detail"]


def test_per_field_meta_overrides_the_call_wide_hint(store, clock):
    store.observe("A", "labvision_test", {"Water": "1", "Ash": "2"})
    clock.t += 60
    store.observe("A", "labvision_test", {"Water": "3", "Ash": "4"}, actor_hint="x",
                  field_meta={"Ash": {"actor": "dana"}})
    got = {e["field"]: e for e in _external(store, "A")}
    assert got["Water"]["user"] == "x"
    assert got["Ash"]["user"] == "dana"


def test_new_field_later_is_a_baseline_not_a_change(store):
    store.observe("A", "qbench_info", {"fw": "1"})
    assert store.observe("A", "qbench_info", {"fw": "1", "tank": "T2"}) == 0
    assert _external(store, "A") == []


def test_sources_are_independent(store):
    store.observe("A", "qbench_test", {"Water": "1"})
    assert store.observe("A", "labvision_test", {"Water": "2"}) == 0


def test_zero_is_a_value_not_blank(store, clock):
    store.observe("A", "qbench_test", {"test:1": 0})
    assert store.snapshots("A", "qbench_test") == {"test:1": "0"}
    clock.t += 1
    assert store.observe("A", "qbench_test", {"test:1": ""}) == 1


# ── normalisation ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("a,b", [
    ("12", "12.00"), ("12", " 12 "), ("0.5", ".5"), ("-0", "0"),
    ("a  b", " a b "), ("line one\n\nline two", "line one line two"),
    (None, ""), (None, "   "), (12, "12.0"), ("1" * 40, "1" * 40 + ".0"),
])
def test_equal_after_numeric_normalising(a, b):
    assert same_value(a, b)


@pytest.mark.parametrize("a,b", [
    ("12", "12.1"), ("Pass", "pass"), ("1e3", "1000"), ("NaN", "nan0"),
    ("inf", "Infinity"), ("12", ""), ("0.1", "0.10000000000000000001"),
])
def test_different_after_normalising(a, b):
    assert not same_value(a, b)


@pytest.mark.parametrize("a,b,equal", [
    ("12.0", "12.00", False), ("12", "12", True), (" 12  ", "12", True),
    ("a\n b", "a b", True), (None, "", True), (0, "0", True),
])
def test_text_mode_compares_exact_normalised_text(a, b, equal):
    assert same_value(a, b, numeric=False) is equal


def test_nan_and_inf_are_compared_as_text():
    assert same_value("NaN", "NaN")
    assert not same_value("NaN", "nan ")
    assert not same_value("inf", "1e999")


def test_very_long_strings_compare_in_full():
    long_a = "9" * 100_000
    assert same_value(long_a, long_a)
    assert not same_value(long_a, "8" + long_a[1:])
    assert not same_value(long_a, long_a[:-1] + "8")   # past MAX_TEXT, still seen


def test_a_change_past_the_stored_text_is_still_detected(store, clock):
    long_a = "x" * (shared_store.MAX_TEXT + 100)
    store.observe("A", "qbench_comments", {"comments": long_a})
    clock.t += 1
    assert store.observe("A", "qbench_comments", {"comments": long_a[:-1] + "y"}) == 1


def test_qbench_info_compares_numbers_but_tests_compare_text(store, clock):
    store.observe("A", "qbench_info", {"tank_capacity": "12"})
    store.observe("A", "qbench_test", {"test:1": "12.0"})
    clock.t += 1
    assert store.observe("A", "qbench_info", {"tank_capacity": "12.00 "}) == 0
    assert store.observe("A", "qbench_test", {"test:1": "12.00"}) == 1


# ── own edits never resurface; stale reads never judge ───────────────────

def test_own_edit_with_snapshot_is_not_reported_later(store, clock):
    store.observe("A", "qbench_test", {"test:9": "12.1"})
    clock.t += 1
    assert store.record_event("A", "test_result", user="Dana P", field="Water",
                              before="12.1", after="12.9",
                              snapshot=("qbench_test", "test:9", "12.9"))
    clock.t += 1
    assert store.observe("A", "qbench_test", {"test:9": "12.9"}) == 0
    assert [e["kind"] for e in store.history("A")] == ["test_result"]


def test_a_read_older_than_the_snapshot_is_ignored(store, clock, caplog):
    """The read started, our edit landed, the (old) answer came back."""
    import logging
    caplog.set_level(logging.DEBUG, logger="coa.shared_store")
    store.observe("A", "qbench_test", {"test:9": "12.1"})
    read_started = clock.t + 5
    clock.t += 10
    store.record_event("A", "test_result", user="Dana P", field="Water",
                       before="12.1", after="12.9", snapshot=("qbench_test", "test:9", "12.9"))
    clock.t += 5
    assert store.observe("A", "qbench_test", {"test:9": "12.1"}, seen_at=read_started) == 0
    assert _external(store, "A") == []
    assert store.snapshots("A", "qbench_test") == {"test:9": "12.9"}
    assert "older than" in caplog.text


def test_concurrent_observers_record_one_change(tmp_path):
    s = SharedStore(tmp_path / "db.sqlite")
    s.observe("A", "qbench_test", {"test:1": "1"}, seen_at=100.0)
    barrier = threading.Barrier(4)
    out = []

    def worker():
        barrier.wait()
        out.append(s.observe("A", "qbench_test", {"test:1": "2"}, seen_at=200.0))
    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(out) == [0, 0, 0, 1]
    assert len(_external(s, "A")) == 1
    s.close()


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


def test_update_snapshots_records_nothing(store, clock):
    assert store.update_snapshots("A", "qbench_comments", {"comments": "hi"})
    assert store.history("A") == []
    clock.t += 1
    assert store.observe("A", "qbench_comments", {"comments": "hi"}) == 0


def test_bad_snapshot_source_is_a_programming_error(store):
    with pytest.raises(ValueError):
        store.record_event("A", "comments", user="x", snapshot=("nope", "f", "v"))
    with pytest.raises(ValueError):
        store.observe("A", "nope", {"f": "v"})


# ── many at once ─────────────────────────────────────────────────────────

def test_observe_many_reads_once_and_writes_once(store, clock, monkeypatch):
    items = [{"lab_id": f"L{i}", "source": "qbench_test", "values": {"test:1": str(i)}}
             for i in range(50)]
    assert store.observe_many(items) == {}
    writes = _count_writes(monkeypatch, store)
    reads = {"n": 0}
    real_read = store._run_read

    def counting(*a, **k):
        reads["n"] += 1
        return real_read(*a, **k)
    monkeypatch.setattr(store, "_run_read", counting)
    clock.t += 1
    items[3]["values"] = {"test:1": "changed"}
    items[7]["values"] = {"test:1": "changed"}
    assert store.observe_many(items) == {"L3": 1, "L7": 1}
    assert reads["n"] == 1 and writes["n"] == 1


def test_observe_many_nothing_changed_writes_nothing(store, clock, monkeypatch):
    items = [{"lab_id": f"L{i}", "source": "qbench_test", "values": {"test:1": "x"}}
             for i in range(20)]
    store.observe_many(items)
    writes = _count_writes(monkeypatch, store)
    clock.t += 1
    assert store.observe_many(items) == {}
    assert writes["n"] == 0


def test_observe_many_is_bounded(store):
    items = [{"lab_id": f"L{i}", "source": "qbench_test", "values": {"t": "1"}}
             for i in range(shared_store.MAX_OBSERVE_ITEMS + 1)]
    with pytest.raises(ValueError):
        store.observe_many(items)


# ── writes only when something changed; never wait when asked not to ────

def test_read_only_fast_path_does_no_write(store, monkeypatch):
    store.observe("A", "qbench_test", {"Water": "12.1", "Ash": "3"})
    writes = _count_writes(monkeypatch, store)
    for _ in range(5):
        assert store.observe("A", "qbench_test", {"Water": " 12.1", "Ash": "3"}) == 0
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
    values = {f"test:{i}": str(i) for i in range(30)}
    store.observe("A", "qbench_test", values)
    writes = _count_writes(monkeypatch, store)
    started = time.perf_counter()
    for _ in range(100):
        assert store.observe("A", "qbench_test", values) == 0
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert writes["n"] == 0
    assert elapsed_ms < 200, f"100 observes took {elapsed_ms:.0f} ms"


def test_a_busy_writer_is_not_waited_for_when_asked(store, clock):
    store.observe("A", "qbench_test", {"test:1": "1"})
    clock.t += 1
    held = threading.Event()
    release = threading.Event()

    def hold():
        with store._lock:
            held.set()
            release.wait(5)
    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    started = time.perf_counter()
    got = store.observe("A", "qbench_test", {"test:1": "2"}, wait=False)
    assert got == WRITER_BUSY
    assert time.perf_counter() - started < 0.5
    release.set()
    t.join(5)
    assert store.observe("A", "qbench_test", {"test:1": "2"}) == 1


# ── bounds, failure, and an older database ───────────────────────────────

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
    assert s.observe_many([{"lab_id": "A", "source": "qbench_test",
                            "values": {"Water": "1"}}]) is None
    assert s.snapshots("A", "qbench_test") is None
    assert s.update_snapshots("A", "qbench_test", {"Water": "1"}) is False
    assert s.record_event("A", "comments", user="x",
                          snapshot=("qbench_comments", "comments", "y")) is False


def test_a_snapshot_table_without_digest_is_upgraded(tmp_path):
    path = tmp_path / "db.sqlite"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE field_snapshots (lab_id TEXT NOT NULL, source TEXT NOT NULL,"
                " field TEXT NOT NULL, value TEXT, seen_at REAL NOT NULL,"
                " PRIMARY KEY (lab_id, source, field))")
    raw.execute("INSERT INTO field_snapshots VALUES ('A','qbench_test','test:1','1',1.0)")
    raw.commit()
    raw.close()
    s = SharedStore(path)
    assert s.observe("A", "qbench_test", {"test:1": "1"}) == 0
    assert s.observe("A", "qbench_test", {"test:1": "2"}) == 1
    s.close()


def test_external_change_is_a_known_kind():
    assert "external_change" in shared_store.EVENT_KINDS
    assert shared_store.SOURCES == ("qbench_test", "qbench_info", "qbench_comments",
                                    "labvision_test")




# ══ route level: reads observe, own edits don't resurface ═════════════════

LAB = "073126-41552"


@pytest.fixture
def routes(monkeypatch, tmp_path):
    """(client, ustate, api, labcore, sse) with QBench and LabCore mocked."""
    from unittest.mock import MagicMock
    pytest.importorskip("flask")
    import app as app_module
    from app import SampleRecord, UserState
    from change_log import ChangeLog

    monkeypatch.setattr(app_module.state, "change_log", ChangeLog(tmp_path / "cl"))
    monkeypatch.setattr(app_module.state, "upload_queue", MagicMock())
    monkeypatch.setattr(app_module.state, "logged_in", True)
    api = MagicMock()
    api.fetch_tests_for_sample_ids.return_value = [
        {"id": 9, "sample_id": 5, "results": "1.23", "assay": {"name": "Water"}}]
    api.fetch_sample.return_value = {"id": 5, "lab_id": LAB, "fw": "FW-1",
                                     "comments": "first", "custom_fields": {"tank": "T1"}}
    api.update_sample.return_value = {"data": [{"id": 5}]}
    monkeypatch.setattr(app_module.state, "api_client", api)
    lc = MagicMock()
    lc.sample_data.return_value = {"lab_id": LAB, "tests": [
        {"test": "Water", "result": "", "operator": ""}]}
    lc.reruns.return_value = []
    monkeypatch.setattr(app_module.state, "labcore", lc)
    sse = []
    monkeypatch.setattr(app_module.state, "broadcast_sse", lambda d: sse.append(d))

    uid = "test-uid-external"
    ustate = UserState(uid, "Dana P")
    ustate.add_record(SampleRecord(lab_id=LAB, tab="Yesterday", sample_id=5, test_ids=[9]))
    with app_module._sessions_lock:
        app_module.user_sessions[uid] = ustate
    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = uid
    yield client, ustate, api, lc, sse
    with app_module._sessions_lock:
        app_module.user_sessions.pop(uid, None)


def _app_external(lab_id=LAB):
    import app as app_module
    return [e for e in app_module.state.shared.history(lab_id)
            if e["kind"] == "external_change"]


def _drop_test_cache(ustate):
    ustate.records[("Yesterday", LAB)].tests_data = None


def test_a_test_result_changed_in_qbench_between_reads_is_recorded_once(routes):
    client, ustate, api, _, sse = routes
    client.get(f"/api/tests/{LAB}")
    assert _app_external() == []
    api.fetch_tests_for_sample_ids.return_value[0]["results"] = "1.50"
    _drop_test_cache(ustate)
    assert client.get(f"/api/tests/{LAB}").status_code == 200
    _drop_test_cache(ustate)
    client.get(f"/api/tests/{LAB}")
    [e] = _app_external()
    assert (e["field"], e["before"], e["after"]) == ("Water", "1.23", "1.50")
    assert e["user"] == "Outside COA Reviewer" and e["detail"]["source"] == "qbench_test"
    assert any(x.get("type") == "sample_event" and x.get("kind") == "external_change"
               for x in sse)


def test_qbench_gives_its_own_change_time_when_it_has_one(routes):
    client, ustate, api, _, _ = routes
    client.get(f"/api/tests/{LAB}")
    stamp = datetime.fromtimestamp(time.time()).strftime("%Y-%m-%dT%H:%M:%S")
    api.fetch_tests_for_sample_ids.return_value[0].update(
        {"results": "2", "last_updated": stamp})
    _drop_test_cache(ustate)
    client.get(f"/api/tests/{LAB}")
    [e] = _app_external()
    assert e["detail"]["changed_at"] == stamp


def test_own_test_edit_then_read_is_not_external(routes):
    client, ustate, api, _, _ = routes
    client.get(f"/api/tests/{LAB}")
    client.patch("/api/tests/9", json={"value": "4.56"})
    api.fetch_tests_for_sample_ids.return_value[0]["results"] = "4.56"
    _drop_test_cache(ustate)
    client.get(f"/api/tests/{LAB}")
    assert _app_external() == []


def test_sample_info_changed_in_qbench_is_recorded(routes):
    client, _, api, _, _ = routes
    client.get(f"/api/sample-info/{LAB}")
    api.fetch_sample.return_value = {"id": 5, "lab_id": LAB, "fw": "FW-2",
                                     "custom_fields": {"tank": "T1"}}
    client.get(f"/api/sample-info/{LAB}")
    [e] = _app_external()
    assert (e["field"], e["before"], e["after"]) == ("fw", "FW-1", "FW-2")
    assert e["detail"]["source"] == "qbench_info"


def test_own_sample_info_edit_then_read_is_not_external(routes):
    client, _, api, _, _ = routes
    client.get(f"/api/sample-info/{LAB}")
    client.patch(f"/api/sample-info/{LAB}", json={"fw": "FW-9", "tank": "T7"})
    api.fetch_sample.return_value = {"id": 5, "lab_id": LAB, "fw": "FW-9",
                                     "comments": "first", "custom_fields": {"tank": "T7"}}
    client.get(f"/api/sample-info/{LAB}")
    assert _app_external() == []


def test_a_change_before_our_edit_is_caught_by_the_before_read(routes):
    """QBench changed fw behind our back, then the reviewer edited tank:
    the pre-edit read records the outside change first."""
    client, _, api, _, _ = routes
    client.get(f"/api/sample-info/{LAB}")
    api.fetch_sample.return_value = {"id": 5, "lab_id": LAB, "fw": "FW-OUT",
                                     "custom_fields": {"tank": "T1"}}
    client.patch(f"/api/sample-info/{LAB}", json={"tank": "T7"})
    [e] = _app_external()
    assert e["field"] == "fw" and e["after"] == "FW-OUT"


def test_comments_changed_in_qbench_are_recorded(routes):
    client, _, api, _, _ = routes
    client.get(f"/api/comments/{LAB}")
    api.fetch_sample.return_value = {"id": 5, "comments": "edited in QBench"}
    client.get(f"/api/comments/{LAB}")
    [e] = _app_external()
    assert (e["before"], e["after"]) == ("first", "edited in QBench")


def test_own_comment_confirmed_then_read_is_not_external(routes, monkeypatch):
    import app as app_module
    client, _, api, _, _ = routes
    q = app_module._wire_upload_queue(app_module.UploadQueue(api, start_worker=False))
    monkeypatch.setattr(app_module.state, "upload_queue", q)
    client.get(f"/api/comments/{LAB}")
    client.patch(f"/api/comments/{LAB}", json={"comments": "mine"})
    client.get(f"/api/comments/{LAB}")           # still queued: QBench says "first"
    assert _app_external() == []
    q._process(q.queue.get_nowait())              # the queue writes and confirms it
    api.fetch_sample.return_value = {"id": 5, "comments": "mine"}
    client.get(f"/api/comments/{LAB}")
    assert _app_external() == []


def test_a_labvision_result_is_attributed_to_its_operator(routes):
    client, _, _, lc, _ = routes
    client.get(f"/api/sync-preview/{LAB}")
    stamp = datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d %H:%M:%S")
    lc.sample_data.return_value = {"lab_id": LAB, "tests": [
        {"test": "Water", "result": "0.02", "operator": "kejuan", "updated_at": stamp}]}
    assert client.get(f"/api/sync-preview/{LAB}").status_code == 200
    [e] = [x for x in _app_external() if x["detail"]["source"] == "labvision_test"]
    assert e["user"] == "kejuan"
    assert (e["before"], e["after"]) == ("", "0.02")
    assert e["detail"]["changed_at"] == stamp


def test_sync_preview_observes_the_qbench_fields_it_reads(routes):
    client, _, api, _, _ = routes
    client.get(f"/api/sync-preview/{LAB}")
    api.fetch_sample.return_value = {"id": 5, "lab_id": LAB, "fw": "FW-3",
                                     "comments": "first", "custom_fields": {"tank": "T1"}}
    client.get(f"/api/sync-preview/{LAB}")
    [e] = _app_external()
    assert e["field"] == "fw" and e["detail"]["source"] == "qbench_info"


def test_reads_still_work_with_the_store_down(routes, monkeypatch, tmp_path):
    import app as app_module
    client, *_ = routes
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(app_module.state, "shared", SharedStore(blocker / "sub" / "db"))
    assert client.get(f"/api/tests/{LAB}").status_code == 200
    assert client.get(f"/api/sample-info/{LAB}").status_code == 200
    assert client.get(f"/api/comments/{LAB}").status_code == 200
    assert client.get(f"/api/sync-preview/{LAB}").status_code == 200


def test_reads_still_work_when_observe_raises(routes, monkeypatch):
    import app as app_module
    client, *_ = routes

    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(app_module.state.shared, "observe", boom)
    assert client.get(f"/api/tests/{LAB}").status_code == 200
    assert client.get(f"/api/comments/{LAB}").status_code == 200


def test_a_tab_pull_observes_the_results_it_already_fetched(routes):
    import app as app_module
    from datetime import date
    _, ustate, api, _, sse = routes
    api.fetch_samples_by_lab_id_prefix.return_value = [
        {"id": 5, "lab_id": LAB}, {"id": 6, "lab_id": "073126-41553"},
        {"id": 7, "lab_id": "073126-41554"}]
    api.fetch_tests_for_sample_ids.return_value = [
        {"id": 9, "sample_id": 5, "results": "1.23", "assay": {"name": "Water"}},
        {"id": 10, "sample_id": 6, "results": 0, "assay": {"name": "Ash"}},
        {"id": 11, "sample_id": 7, "results": "x", "assay": {"name": "Ash"}}]
    app_module.fetch_samples_for_tab("Yesterday", date(2026, 7, 31), ustate)
    assert app_module.state.shared.snapshots(LAB, "qbench_test") == {"test:9": "1.23"}
    assert app_module.state.shared.snapshots("073126-41553", "qbench_test") == {"test:10": "0"}
    api.fetch_tests_for_sample_ids.return_value[0]["results"] = "9"
    api.fetch_tests_for_sample_ids.return_value[1]["results"] = "1"
    sse.clear()
    app_module.fetch_samples_for_tab("Yesterday", date(2026, 7, 31), ustate)
    [e] = _app_external()
    assert (e["field"], e["after"]) == ("Water", "9")
    assert api.fetch_tests_for_sample_ids.call_count == 2   # no extra API call
    history = [x for x in sse if x.get("type") in ("sample_event", "sample_events")]
    assert history == [{"type": "sample_events", "kind": "external_change",
                        "lab_ids": [LAB, "073126-41553"]}]


def test_a_malformed_sample_does_not_stop_the_tab_observing(routes):
    import app as app_module
    from datetime import date
    _, ustate, api, _, _ = routes
    api.fetch_samples_by_lab_id_prefix.return_value = [
        {"id": 5, "lab_id": LAB}, {"id": 6, "lab_id": "073126-41553"}]
    api.fetch_tests_for_sample_ids.return_value = [
        {"id": 9, "sample_id": 5, "results": "1", "assay": "not a dict"},
        {"id": 10, "sample_id": 6, "results": "2", "assay": {"name": "Ash"}}]
    app_module.fetch_samples_for_tab("Yesterday", date(2026, 7, 31), ustate)
    assert app_module.state.shared.snapshots("073126-41553", "qbench_test") == {"test:10": "2"}


def test_a_read_that_started_before_our_write_landed_judges_nothing(routes):
    import app as app_module
    client, ustate, api, _, _ = routes
    client.get(f"/api/tests/{LAB}")                    # baseline 1.23

    def our_write_lands_mid_read(ids):
        app_module.state.shared.update_snapshots(LAB, "qbench_test", {"test:9": "4.56"})
        return [{"id": 9, "sample_id": 5, "results": "1.23", "assay": {"name": "Water"}}]
    api.fetch_tests_for_sample_ids.side_effect = our_write_lands_mid_read
    _drop_test_cache(ustate)
    client.get(f"/api/tests/{LAB}")
    assert _app_external() == []
    assert app_module.state.shared.snapshots(LAB, "qbench_test") == {"test:9": "4.56"}


def test_a_request_never_waits_for_a_busy_writer(routes, monkeypatch):
    """The observation is handed to the background queue instead."""
    import app as app_module
    from shared_store import WRITER_BUSY
    client, ustate, api, _, _ = routes
    client.get(f"/api/tests/{LAB}")
    real = app_module.state.shared.observe_many
    waits = []

    def busy_on_request_thread(items, *, wait=True):
        waits.append(wait)
        return WRITER_BUSY if not wait else real(items, wait=True)
    monkeypatch.setattr(app_module.state.shared, "observe_many", busy_on_request_thread)
    submitted = []
    monkeypatch.setattr(app_module.OBSERVE_QUEUE, "submit", lambda items: submitted.append(items))
    api.fetch_tests_for_sample_ids.return_value[0]["results"] = "2"
    _drop_test_cache(ustate)
    assert client.get(f"/api/tests/{LAB}").status_code == 200
    assert waits[-1] is False and len(submitted) == 1
    app_module.ObserveQueue.drain_one(submitted[0])
    [e] = _app_external()
    assert e["after"] == "2"


def test_the_observe_queue_is_bounded(monkeypatch, caplog):
    import app as app_module
    q = app_module.ObserveQueue()
    monkeypatch.setattr(q, "_ensure_worker", lambda: None)
    for _ in range(q.MAXSIZE):
        assert q.submit([{}])
    assert q.submit([{}]) is False
    assert "observe queue full" in caplog.text


def test_labvision_duplicate_names_are_told_apart(routes):
    import app as app_module
    client, _, _, lc, _ = routes
    lc.sample_data.return_value = {"lab_id": LAB, "tests": [
        {"test": "Water", "result": "1", "id": 31}, {"test": "Water", "result": "2", "id": 32},
        {"test": "Ash", "result": "3"}, {"test": "Ash", "result": "4"}]}
    client.get(f"/api/sync-preview/{LAB}")
    assert app_module.state.shared.snapshots(LAB, "labvision_test") == {
        "Water (#31)": "1", "Water (#32)": "2", "Ash (1)": "3", "Ash (2)": "4"}


def test_own_writes_expire_and_are_bounded(caplog):
    import app as app_module
    now = {"t": 0.0}
    ow = app_module.OwnWrites(clock=lambda: now["t"])
    ow.begin("A", "qbench_test", "test:1")
    assert ow.fields("A", "qbench_test") == {"test:1"}
    now["t"] += ow.MAX_AGE_SECONDS + 1
    assert ow.fields("A", "qbench_test") == set()
    assert "in flight for over" in caplog.text
    monkey = app_module.OwnWrites()
    monkey.MAX_ENTRIES = 3
    for i in range(4):
        monkey.begin("A", "qbench_test", f"test:{i}")
    assert monkey.fields("A", "qbench_test") == {"test:1", "test:2", "test:3"}


def test_prune_is_bounded(tmp_path):
    s = SharedStore(tmp_path / "db.sqlite")
    for i in range(30):
        s.update_snapshots(f"L{i}", "qbench_test", {"t": "1"}, seen_at=10.0)
    assert s.prune_snapshots(before=100.0, limit=25) == 25
    assert s.prune_snapshots(before=100.0, limit=25) == 5
    assert s.prune_snapshots(before=100.0, limit=25) == 0
    with pytest.raises(ValueError):
        s.prune_snapshots(before=100.0, limit=shared_store.MAX_PRUNE_ROWS + 1)
    s.close()
