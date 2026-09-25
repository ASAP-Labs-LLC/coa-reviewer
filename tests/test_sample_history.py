"""Every change COA Reviewer makes to a sample lands in its history, with the
value it replaced (spec §3, plan Task 7).

The change log (JSONL) stays the audit trail; these rows are the queryable
per-sample view the History tab reads. A failed before-read or a store that
is down must never block the edit itself.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")

LAB = "073126-41552"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """(client, ustate, api, labcore, sse) — QBench and LabCore mocked, a
    real (worker-less, wired) UploadQueue drained by ``_drain``; every
    broadcast SSE event captured in ``sse``."""
    import app as app_module
    from app import SampleRecord, UserState
    from change_log import ChangeLog

    monkeypatch.setattr(app_module.state, "change_log", ChangeLog(tmp_path / "cl"))
    lc = MagicMock()
    lc.create_task.return_value = {"ok": True, "task_id": 77}
    lc.complete_task.return_value = {"ok": True}
    lc.sample_data.return_value = {"lab_id": LAB, "fuel_type": "Diesel #1",
                                   "work_order": "03397832", "tests": []}
    monkeypatch.setattr(app_module.state, "labcore", lc)
    api = MagicMock()
    api.delete_attachment.return_value = {"ok": True}
    api.fetch_sample.return_value = {"id": 5, "lab_id": LAB, "fw": "FW-1",
                                     "fuel_type": "", "work_order": "OLD",
                                     "comments": "old note",
                                     "custom_fields": {"tank": "T1"}}
    api.fetch_tests_for_sample_ids.return_value = [
        {"id": 9, "sample_id": 5, "results": "1.23", "assay": {"name": "Water"}}]
    api.update_sample.return_value = {"data": [{"id": 5}]}
    monkeypatch.setattr(app_module.state, "api_client", api)
    monkeypatch.setattr(app_module.state, "logged_in", True)
    sse = []
    monkeypatch.setattr(app_module.state, "broadcast_sse", lambda d: sse.append(d))
    queue_ = app_module._wire_upload_queue(app_module.UploadQueue(api, start_worker=False))
    monkeypatch.setattr(app_module.state, "upload_queue", queue_)

    uid = "test-uid-history"
    ustate = UserState(uid, "Dana P")
    rec = SampleRecord(lab_id=LAB, tab="Yesterday", sample_id=5, test_ids=[9])
    rec.tests_data = [{"test_id": 9, "test_name": "Water", "results": "1.23"}]
    rec.attachments = [{"id": 42, "filename": "SIF.pdf"}]
    ustate.add_record(rec)
    with app_module._sessions_lock:
        app_module.user_sessions[uid] = ustate
    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = uid
    yield client, ustate, api, lc, sse
    with app_module._sessions_lock:
        app_module.user_sessions.pop(uid, None)


def _drain(*, fail: bool = False) -> int:
    """Process every queued upload now. ``fail`` makes each one the final
    attempt, so a QBench error is terminal instead of scheduling a retry."""
    import queue as _queue
    import app as app_module
    q = app_module.state.upload_queue
    if fail:
        q.MAX_ATTEMPTS = 0          # this instance only: no retries
    done = 0
    for _ in range(50):
        try:
            payload = q.queue.get_nowait()
        except _queue.Empty:
            break
        q._process(payload)
        done += 1
    return done


def _own_writes():
    import app as app_module
    return app_module.OWN_WRITES


def _store():
    import app as app_module
    return app_module.state.shared


def _rows(kind, lab_id=LAB):
    return [e for e in _store().history(lab_id) if e["kind"] == kind]


def _events_for(sse, lab_id=LAB):
    return [e for e in sse if e.get("type") == "sample_event" and e.get("lab_id") == lab_id]


@pytest.fixture
def store_down(monkeypatch, tmp_path):
    import app as app_module
    from shared_store import SharedStore
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(app_module.state, "shared", SharedStore(blocker / "sub" / "db"))


# ── test results: recorded when QBench answers ───────────────────────────

def test_a_test_result_edit_is_recorded_when_qbench_confirms(env):
    client, _, _, _, sse = env
    assert client.patch("/api/tests/9", json={"value": "4.56"}).status_code == 200
    assert _rows("test_result") == []                   # not yet: still queued
    assert _own_writes().fields(LAB, "qbench_test") == {"test:9"}
    _drain()
    [row] = _rows("test_result")
    assert row["user"] == "Dana P"
    assert (row["field"], row["before"], row["after"]) == ("Water", "1.23", "4.56")
    assert row["detail"]["test_id"] == 9
    assert _store().snapshots(LAB, "qbench_test") == {"test:9": "4.56"}
    assert _own_writes().fields(LAB, "qbench_test") == set()
    assert _events_for(sse)[-1]["kind"] == "test_result"


def test_before_comes_from_qbench_and_an_outside_change_is_recorded_first(env):
    client, ustate, api, _, _ = env
    _store().observe(LAB, "qbench_test", {"test:9": "1.00"}, seen_at=1.0)
    ustate.records[("Yesterday", LAB)].tests_data[0]["results"] = "1.00"   # stale cache
    client.patch("/api/tests/9", json={"value": "4.56"})
    _drain()
    [ext] = _rows("external_change")
    assert (ext["field"], ext["before"], ext["after"]) == ("Water", "1.00", "1.23")
    [row] = _rows("test_result")
    assert row["before"] == "1.23"


def test_a_failed_before_read_falls_back_to_the_cached_value(env, caplog):
    client, _, api, _, _ = env
    api.fetch_tests_for_sample_ids.side_effect = RuntimeError("429")
    client.patch("/api/tests/9", json={"value": "4.56"})
    _drain()
    [row] = _rows("test_result")
    assert row["before"] == "1.23"
    assert "before upload" in caplog.text


def test_test_results_compare_as_exact_text(env):
    client, *_ = env
    client.patch("/api/tests/9", json={"value": "1.230"})
    _drain()
    [row] = _rows("test_result")
    assert "unchanged" not in (row["detail"] or {})


def test_an_edit_to_the_same_value_is_recorded_as_unchanged(env):
    client, *_ = env
    client.patch("/api/tests/9", json={"value": "1.23"})
    _drain()
    [row] = _rows("test_result")
    assert row["detail"]["unchanged"] is True


def test_a_re_entered_value_is_uploaded_again_after_an_outside_change(env):
    """Dana saves 4.56 → a tech sets 7.0 in QBench → Dana re-enters 4.56.
    The re-entry must reach QBench, and the history must say 7.0 → 4.56
    because that is what happened (not a skipped 'already saved')."""
    client, ustate, api, _, _ = env
    qbench = {"results": "1.23"}
    api.fetch_tests_for_sample_ids.side_effect = lambda ids: [
        {"id": 9, "sample_id": 5, "results": qbench["results"], "assay": {"name": "Water"}}]
    api.update_test_result.side_effect = lambda tid, v: qbench.update(results=v) or {}
    client.patch("/api/tests/9", json={"value": "4.56"})
    _drain()
    qbench["results"] = "7.0"                           # the tech, directly in QBench
    ustate_cache_drop(env)
    client.get(f"/api/tests/{LAB}")
    client.patch("/api/tests/9", json={"value": "4.56"})
    _drain()
    assert qbench["results"] == "4.56"
    assert api.update_test_result.call_count == 2
    [ext] = _rows("external_change")
    assert (ext["before"], ext["after"]) == ("4.56", "7.0")
    newest = _rows("test_result")[0]
    assert (newest["before"], newest["after"]) == ("7.0", "4.56")
    assert "already_saved" not in (newest["detail"] or {})


def test_a_confirmed_edit_the_store_cannot_record_keeps_its_guard(env, monkeypatch):
    """Store down at confirm time: the snapshot could not move, so the field
    stays guarded (until it ages out) rather than reading as an outside
    change on the next GET."""
    client, *_ = env
    monkeypatch.setattr(_store(), "record_events", lambda *a, **k: False)
    client.patch("/api/tests/9", json={"value": "4.56"})
    _drain()
    assert _own_writes().fields(LAB, "qbench_test") == {"test:9"}


def test_a_burst_of_edits_on_one_sample_reads_qbench_once(env):
    client, ustate, api, _, _ = env
    ustate.records[("Yesterday", LAB)].test_ids = [9, 10]
    api.fetch_tests_for_sample_ids.return_value = [
        {"id": 9, "sample_id": 5, "results": "1.23", "assay": {"name": "Water"}},
        {"id": 10, "sample_id": 5, "results": "2", "assay": {"name": "Ash"}}]
    client.patch("/api/tests/9", json={"value": "4.56"})
    client.patch("/api/tests/10", json={"value": "3"})
    client.patch("/api/tests/9", json={"value": "4.57"})
    _drain()
    assert api.fetch_tests_for_sample_ids.call_count == 1
    rows = sorted(_rows("test_result"), key=lambda r: r["id"])
    assert [(r["field"], r["before"], r["after"]) for r in rows] == [
        ("Water", "1.23", "4.56"), ("Ash", "2", "3"), ("Water", "4.56", "4.57")]
    assert _rows("external_change") == []


def test_a_deep_queue_skips_the_pre_read(env, caplog):
    import logging
    import app as app_module
    caplog.set_level(logging.DEBUG)
    client, _, api, _, _ = env
    q = app_module.state.upload_queue
    for _ in range(app_module.PRE_READ_MAX_QUEUE_DEPTH + 1):
        q.queue.put({"kind": "noop"})
    client.patch("/api/tests/9", json={"value": "4.56"})
    payload = q.queue.queue[-1]
    q._process(payload)
    assert api.fetch_tests_for_sample_ids.call_count == 0
    assert "queue depth" in caplog.text
    [row] = _rows("test_result")
    assert row["before"] == "1.23"                   # the cached fallback


def test_a_failed_upload_is_recorded_as_failed_and_moves_nothing(env):
    import app as app_module
    client, _, api, _, _ = env
    api.update_test_result.side_effect = app_module.QBenchAPIError("503")
    _store().observe(LAB, "qbench_test", {"test:9": "1.23"}, seen_at=1.0)
    client.patch("/api/tests/9", json={"value": "4.56"})
    _drain(fail=True)
    [row] = _rows("test_result")
    assert row["detail"]["failed"] is True and row["after"] == "4.56"
    assert _store().snapshots(LAB, "qbench_test") == {"test:9": "1.23"}
    assert _own_writes().fields(LAB, "qbench_test") == set()
    # QBench still says 1.23: nothing outside COA Reviewer happened.
    ustate_cache_drop(env)
    client.get(f"/api/tests/{LAB}")
    assert _rows("external_change") == []


def ustate_cache_drop(env):
    env[1].records[("Yesterday", LAB)].tests_data = None


@pytest.mark.parametrize("qbench_says", ["1.23", "4.56"])
def test_a_read_while_the_upload_is_pending_judges_nothing(env, qbench_says):
    client, _, api, _, _ = env
    _store().observe(LAB, "qbench_test", {"test:9": "1.23"}, seen_at=1.0)
    client.patch("/api/tests/9", json={"value": "4.56"})
    api.fetch_tests_for_sample_ids.return_value[0]["results"] = qbench_says
    ustate_cache_drop(env)
    client.get(f"/api/tests/{LAB}")
    assert _rows("external_change") == []
    api.fetch_tests_for_sample_ids.return_value[0]["results"] = "1.23"
    _drain()
    api.fetch_tests_for_sample_ids.return_value[0]["results"] = "4.56"
    ustate_cache_drop(env)
    client.get(f"/api/tests/{LAB}")
    assert _rows("external_change") == []


def test_a_test_is_keyed_by_id_without_the_reviewers_cache(env):
    client, ustate, *_ = env
    ustate.records[("Yesterday", LAB)].tests_data = None
    client.patch("/api/tests/9", json={"value": "4.56"})
    _drain()
    [row] = _rows("test_result")
    assert row["field"] == "Water"                     # named by QBench's read
    assert _store().snapshots(LAB, "qbench_test") == {"test:9": "4.56"}


def test_an_edit_not_attributable_to_a_sample_is_logged(env, caplog):
    client, *_ = env
    assert client.patch("/api/tests/12345", json={"value": "1"}).status_code == 200
    assert "12345" in caplog.text and "no sample" in caplog.text


# ── attachments ──────────────────────────────────────────────────────────

def test_deleting_an_attachment_is_recorded(env):
    client, _, _, _, sse = env
    assert client.delete("/api/attachments/42").status_code == 200
    [row] = _rows("attachment_deleted")
    assert (row["field"], row["before"], row["after"]) == ("SIF.pdf", "SIF.pdf", None)
    assert _events_for(sse)[-1]["kind"] == "attachment_deleted"


# ── comments ─────────────────────────────────────────────────────────────

def test_a_comment_edit_is_recorded_with_qbenchs_before(env):
    client, *_ = env
    client.patch(f"/api/comments/{LAB}", json={"comments": "new note"})
    assert _rows("comments") == []
    assert _own_writes().fields(LAB, "qbench_comments") == {"comments"}
    _drain()
    [row] = _rows("comments")
    assert (row["before"], row["after"]) == ("old note", "new note")
    assert _store().snapshots(LAB, "qbench_comments") == {"comments": "new note"}
    assert _own_writes().fields(LAB, "qbench_comments") == set()


def test_a_comment_whose_before_read_fails_uses_the_newest_queued_one(env):
    client, _, api, _, _ = env
    _store().update_snapshots(LAB, "qbench_comments", {"comments": "seen"})
    api.fetch_sample.side_effect = RuntimeError("down")
    client.patch(f"/api/comments/{LAB}", json={"comments": "a"})
    client.patch(f"/api/comments/{LAB}", json={"comments": "b"})
    _drain()
    rows = sorted(_rows("comments"), key=lambda r: r["id"])
    assert [(r["before"], r["after"]) for r in rows] == [("seen", "a"), ("a", "b")]


def test_a_failed_comment_write_is_recorded_as_failed(env):
    import app as app_module
    client, _, api, _, _ = env
    api.update_sample_comments.side_effect = app_module.QBenchAPIError("boom")
    client.patch(f"/api/comments/{LAB}", json={"comments": "hi"})
    _drain(fail=True)
    [row] = _rows("comments")
    assert row["detail"]["failed"] is True
    # the pre-write read set the baseline; the failed write did not move it
    assert _store().snapshots(LAB, "qbench_comments") == {"comments": "old note"}
    assert _own_writes().fields(LAB, "qbench_comments") == set()


def test_every_upload_queue_is_wired_for_history():
    import inspect
    import app as app_module
    src = inspect.getsource(app_module)
    assert src.count("_wire_upload_queue(UploadQueue(") >= 2
    assert "on_comment_saved" not in src


# ── sample info ──────────────────────────────────────────────────────────

def test_a_sample_info_edit_records_each_field_with_its_before(env):
    client, _, api, _, sse = env
    resp = client.patch(f"/api/sample-info/{LAB}", json={"fw": "FW-9", "tank": "T1"})
    assert resp.status_code == 200
    rows = {r["field"]: r for r in _rows("sample_info")}
    assert (rows["fw"]["before"], rows["fw"]["after"]) == ("FW-1", "FW-9")
    assert rows["tank"]["before"] == "T1"
    assert rows["tank"]["detail"]["unchanged"] is True
    assert "unchanged" not in (rows["fw"]["detail"] or {})
    snap = _store().snapshots(LAB, "qbench_info")
    assert snap["fw"] == "FW-9" and snap["tank"] == "T1"
    assert _events_for(sse)[-1]["kind"] == "sample_info"


def test_a_failed_before_read_still_saves_and_records_unknown(env, caplog):
    client, _, api, _, _ = env
    api.fetch_sample.side_effect = RuntimeError("QBench 502")
    resp = client.patch(f"/api/sample-info/{LAB}", json={"fw": "FW-9"})
    assert resp.status_code == 200
    api.update_sample.assert_called_once()
    [row] = _rows("sample_info")
    assert row["before"] is None and row["after"] == "FW-9"
    assert "could not read" in caplog.text


def test_a_failed_patch_records_nothing(env):
    client, _, api, _, _ = env
    api.update_sample.side_effect = RuntimeError("nope")
    assert client.patch(f"/api/sample-info/{LAB}", json={"fw": "FW-9"}).status_code == 500
    assert _rows("sample_info") == []


def test_a_labvision_sync_records_each_field_with_source(env):
    client, *_ = env
    resp = client.post(f"/api/sync-sample-info/{LAB}", json={"mappings": [
        {"source": "fuel_type", "target": "fuel_type"},
        {"source": "work_order", "target": "work_order"}]})
    assert resp.status_code == 200
    rows = {r["field"]: r for r in _rows("sample_sync")}
    assert (rows["work_order"]["before"], rows["work_order"]["after"]) == ("OLD", "03397832")
    assert rows["fuel_type"]["before"] == "" and rows["fuel_type"]["after"] == "Diesel #1"
    assert rows["work_order"]["detail"]["source"] == "LabVision"
    assert _store().snapshots(LAB, "qbench_info")["work_order"] == "03397832"


def test_a_sync_whose_before_read_fails_still_syncs(env):
    client, _, api, _, _ = env
    api.fetch_sample.side_effect = RuntimeError("down")
    resp = client.post(f"/api/sync-sample-info/{LAB}", json={"mappings": [
        {"source": "fuel_type", "target": "fuel_type"}]})
    assert resp.status_code == 200
    [row] = _rows("sample_sync")
    assert row["before"] is None


def test_a_read_during_the_patch_does_not_judge_the_fields_being_written(env):
    """Another reviewer's GET lands while our PATCH is in flight."""
    import time
    import app as app_module
    client, _, api, _, _ = env
    _store().observe(LAB, "qbench_info", {"fw": "FW-1"}, seen_at=1.0)

    def patch_and_meanwhile_read(sid, payload):
        app_module._observe(LAB, "qbench_info", {"fw": "FW-9"}, seen_at=time.time(),
                            wait=True)
        return {"data": [{"id": 5}]}
    api.update_sample.side_effect = patch_and_meanwhile_read
    client.patch(f"/api/sample-info/{LAB}", json={"fw": "FW-9"})
    assert _rows("external_change") == []
    assert _own_writes().fields(LAB, "qbench_info") == set()


def test_a_patch_qbench_refused_releases_its_fields(env):
    import app as app_module
    client, _, api, _, _ = env
    api.update_sample.side_effect = app_module.QBenchAPIError("400 bad field")
    client.patch(f"/api/sample-info/{LAB}", json={"fw": "FW-9"})
    assert _own_writes().fields(LAB, "qbench_info") == set()


def test_a_patch_that_may_have_landed_keeps_its_fields_guarded(env):
    """A timeout: QBench may have applied it, so the next read must not
    call our value an outside change."""
    import requests
    client, _, api, _, _ = env
    api.update_sample.side_effect = requests.exceptions.ReadTimeout("slow")
    assert client.patch(f"/api/sample-info/{LAB}", json={"fw": "FW-9"}).status_code == 500
    assert _own_writes().fields(LAB, "qbench_info") == {"fw"}


def test_a_patch_the_store_cannot_record_keeps_its_fields_guarded(env, monkeypatch):
    client, *_ = env
    monkeypatch.setattr(_store(), "record_events", lambda *a, **k: False)
    assert client.patch(f"/api/sample-info/{LAB}", json={"fw": "FW-9"}).status_code == 200
    assert _own_writes().fields(LAB, "qbench_info") == {"fw"}


# ── I3: the snapshot is what QBench stores, so the next read agrees ──────

@pytest.mark.parametrize("field,sent,qbench_returns", [
    ("tags", ["a", "b"], ["a", "b"]),
    ("tags", '["a", "b"]', ["a", "b"]),
    ("Rush", True, True),
    ("Rush", "true", True),
    ("tank_capacity", "500", 500.0),
    ("time_of_collection", "2026-09-25 10:00", "2026-09-25 10:00"),
])
def test_an_edit_round_trips_without_a_phantom_change(env, field, sent, qbench_returns):
    client, _, api, _, _ = env
    client.patch(f"/api/sample-info/{LAB}", json={field: sent})
    api.fetch_sample.return_value = {**api.fetch_sample.return_value, field: qbench_returns}
    client.get(f"/api/sample-info/{LAB}")
    assert _rows("external_change") == []


def test_the_snapshot_prefers_qbenchs_echo(env):
    client, _, api, _, _ = env
    api.update_sample.return_value = {"data": [{"id": 5, "time_of_collection":
                                                "2026-09-25T10:00:00"}]}
    client.patch(f"/api/sample-info/{LAB}", json={"time_of_collection": "2026-09-25 10:00"})
    assert _store().snapshots(LAB, "qbench_info")["time_of_collection"] == \
        "2026-09-25T10:00:00"
    api.fetch_sample.return_value = {**api.fetch_sample.return_value,
                                     "time_of_collection": "2026-09-25T10:00:00"}
    client.get(f"/api/sample-info/{LAB}")
    assert _rows("external_change") == []


# ── Command Center listings ──────────────────────────────────────────────

def test_creating_a_listing_records_it_on_every_sample(env):
    client, _, _, _, sse = env
    client.post("/api/cc/tasks", json={
        "initial_problem": "Water high", "type": "double_check",
        "sample_ids": [{"lab_id": LAB}, {"lab_id": "073126-41553"}]})
    for lab in (LAB, "073126-41553"):
        [row] = _rows("listing_created", lab)
        assert row["after"] == "Water high"
        assert row["detail"] == {"task_id": 77, "type": "double_check"}
        assert _events_for(sse, lab)


def test_a_conflict_records_no_listing(env):
    client, _, _, lc, _ = env
    lc.create_task.return_value = {"ok": False, "conflict": True}
    client.post("/api/cc/tasks", json={"initial_problem": "x",
                                       "sample_ids": [{"lab_id": LAB}]})
    assert _rows("listing_created") == []


def test_completing_a_listing_records_it_for_the_lab_id_sent(env):
    client, *_ = env
    client.post("/api/cc/tasks/7/complete", json={"notes": "Re-ran", "lab_id": LAB})
    [row] = _rows("listing_completed")
    assert row["after"] == "Re-ran" and row["detail"] == {"task_id": 7}


def test_completing_records_every_lab_id_that_looks_like_one(env, caplog):
    client, *_ = env
    client.post("/api/cc/tasks/7/complete", json={
        "notes": "x", "lab_id": LAB, "lab_ids": ["073126-41553", "DROP TABLE", LAB]})
    assert len(_rows("listing_completed")) == 1
    assert len(_rows("listing_completed", "073126-41553")) == 1
    assert "not a lab id" in caplog.text


def test_completing_without_a_lab_id_records_nothing_and_succeeds(env):
    client, *_ = env
    assert client.post("/api/cc/tasks/7/complete", json={"notes": "x"}).status_code == 200
    assert _rows("listing_completed") == []


def test_the_frontend_sends_the_lab_id_when_completing():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "static" / "js" / "app.js").read_text(
        encoding="utf-8")
    start = js.index("/complete`")
    assert "lab_id" in js[start:start + 400]


# ── the store being down never breaks an edit ────────────────────────────

def test_every_edit_still_succeeds_with_the_store_down(env, store_down):
    client, *_ = env
    assert client.patch("/api/tests/9", json={"value": "4.56"}).status_code == 200
    assert client.delete("/api/attachments/42").status_code == 200
    assert client.patch(f"/api/comments/{LAB}", json={"comments": "x"}).status_code == 200
    assert client.patch(f"/api/sample-info/{LAB}", json={"fw": "FW-9"}).status_code == 200
    assert client.post(f"/api/sync-sample-info/{LAB}", json={"mappings": [
        {"source": "fuel_type", "target": "fuel_type"}]}).status_code == 200
    assert client.post("/api/cc/tasks", json={
        "initial_problem": "x", "sample_ids": [{"lab_id": LAB}]}).status_code == 200
    assert client.post("/api/cc/tasks/7/complete",
                       json={"notes": "x", "lab_id": LAB}).status_code == 200
    _drain()
    # Confirmed but unrecorded: the snapshot is behind QBench, so the fields
    # stay guarded until they age out instead of reading as outside changes.
    assert _own_writes().fields(LAB, "qbench_test") == {"test:9"}
    assert _own_writes().fields(LAB, "qbench_comments") == {"comments"}


# ── snapshot pruning ─────────────────────────────────────────────────────

def test_old_snapshots_are_pruned_once_a_day():
    import app as app_module
    store = _store()
    now = 400 * 86400.0
    store.update_snapshots(LAB, "qbench_test", {"test:1": "old"}, seen_at=now - 181 * 86400)
    store.update_snapshots(LAB, "qbench_test", {"test:2": "new"}, seen_at=now - 10 * 86400)
    assert app_module._maybe_prune_snapshots(now) == 1
    assert store.snapshots(LAB, "qbench_test") == {"test:2": "new"}
    store.update_snapshots(LAB, "qbench_test", {"test:3": "old"}, seen_at=now - 200 * 86400)
    assert app_module._maybe_prune_snapshots(now + 3600) is None      # already ran today
    assert app_module._maybe_prune_snapshots(now + 86401) == 1


def test_the_cleanup_cycle_prunes_snapshots(monkeypatch):
    import app as app_module
    calls = []
    monkeypatch.setattr(app_module, "_maybe_prune_snapshots", lambda now: calls.append(now))
    app_module._session_cleanup_cycle(123.0)
    assert calls == [123.0]
