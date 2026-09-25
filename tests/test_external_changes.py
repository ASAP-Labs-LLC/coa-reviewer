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
    api.fetch_tests_for_sample_ids.return_value[0].update(
        {"results": "2", "last_updated": "2026-09-25T09:12:00"})
    _drop_test_cache(ustate)
    client.get(f"/api/tests/{LAB}")
    [e] = _app_external()
    assert e["detail"]["changed_at"] == "2026-09-25T09:12:00"


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


def test_own_comment_confirmed_then_read_is_not_external(routes):
    import app as app_module
    client, _, api, _, _ = routes
    client.get(f"/api/comments/{LAB}")
    client.patch(f"/api/comments/{LAB}", json={"comments": "mine"})
    app_module._comment_saved(LAB, "mine")        # the queue confirmed it
    api.fetch_sample.return_value = {"id": 5, "comments": "mine"}
    client.get(f"/api/comments/{LAB}")
    assert _app_external() == []


def test_a_labvision_result_is_attributed_to_its_operator(routes):
    client, _, _, lc, _ = routes
    client.get(f"/api/sync-preview/{LAB}")
    lc.sample_data.return_value = {"lab_id": LAB, "tests": [
        {"test": "Water", "result": "0.02", "operator": "kejuan",
         "updated_at": "2026-09-25 10:01:00"}]}
    assert client.get(f"/api/sync-preview/{LAB}").status_code == 200
    [e] = [x for x in _app_external() if x["detail"]["source"] == "labvision_test"]
    assert e["user"] == "kejuan"
    assert (e["before"], e["after"]) == ("", "0.02")
    assert e["detail"]["changed_at"] == "2026-09-25 10:01:00"


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
    _, ustate, api, _, _ = routes
    api.fetch_samples_by_lab_id_prefix.return_value = [{"id": 5, "lab_id": LAB}]
    app_module.fetch_samples_for_tab("Yesterday", date(2026, 7, 31), ustate)
    assert app_module.state.shared.snapshots(LAB, "qbench_test") == {"Water": "1.23"}
    api.fetch_tests_for_sample_ids.return_value[0]["results"] = "9"
    app_module.fetch_samples_for_tab("Yesterday", date(2026, 7, 31), ustate)
    [e] = _app_external()
    assert e["after"] == "9"
    assert api.fetch_tests_for_sample_ids.call_count == 2   # no extra API call
