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
    """(client, ustate, api, labcore, sse) — QBench, LabCore and the upload
    queue mocked; every broadcast SSE event captured in ``sse``."""
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
    monkeypatch.setattr(app_module.state, "upload_queue", MagicMock())
    api = MagicMock()
    api.delete_attachment.return_value = {"ok": True}
    api.fetch_sample.return_value = {"id": 5, "lab_id": LAB, "fw": "FW-1",
                                     "fuel_type": "", "work_order": "OLD",
                                     "custom_fields": {"tank": "T1"}}
    api.update_sample.return_value = {"data": [{"id": 5}]}
    monkeypatch.setattr(app_module.state, "api_client", api)
    monkeypatch.setattr(app_module.state, "logged_in", True)
    sse = []
    monkeypatch.setattr(app_module.state, "broadcast_sse", lambda d: sse.append(d))

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


# ── test results ─────────────────────────────────────────────────────────

def test_a_test_result_edit_records_before_and_after(env):
    client, _, _, _, sse = env
    assert client.patch("/api/tests/9", json={"value": "4.56"}).status_code == 200
    [row] = _rows("test_result")
    assert row["user"] == "Dana P"
    assert (row["field"], row["before"], row["after"]) == ("Water", "1.23", "4.56")
    assert row["detail"]["test_id"] == 9
    assert _store().snapshots(LAB, "qbench_test") == {"Water": "4.56"}
    assert _events_for(sse)[-1]["kind"] == "test_result"


def test_an_edit_to_the_same_value_is_recorded_as_unchanged(env):
    client, *_ = env
    client.patch("/api/tests/9", json={"value": "1.230"})
    [row] = _rows("test_result")
    assert row["detail"]["unchanged"] is True


def test_duplicate_test_names_get_distinct_fields(env):
    client, ustate, *_ = env
    rec = ustate.records[("Yesterday", LAB)]
    rec.tests_data.append({"test_id": 10, "test_name": "Water", "results": "2"})
    client.patch("/api/tests/10", json={"value": "3"})
    [row] = _rows("test_result")
    assert row["field"] == "Water (test 10)"


# ── attachments ──────────────────────────────────────────────────────────

def test_deleting_an_attachment_is_recorded(env):
    client, _, _, _, sse = env
    assert client.delete("/api/attachments/42").status_code == 200
    [row] = _rows("attachment_deleted")
    assert (row["field"], row["before"], row["after"]) == ("SIF.pdf", "SIF.pdf", None)
    assert _events_for(sse)[-1]["kind"] == "attachment_deleted"


# ── comments ─────────────────────────────────────────────────────────────

def test_a_comment_edit_records_the_last_seen_comments_as_before(env):
    client, *_ = env
    _store().update_snapshots(LAB, "qbench_comments", {"comments": "old note"})
    client.patch(f"/api/comments/{LAB}", json={"comments": "new note"})
    [row] = _rows("comments")
    assert (row["before"], row["after"]) == ("old note", "new note")
    # queued, not yet confirmed: the snapshot has not moved
    assert _store().snapshots(LAB, "qbench_comments") == {"comments": "old note"}


def test_a_comment_edit_with_nothing_seen_before_records_unknown(env):
    client, *_ = env
    client.patch(f"/api/comments/{LAB}", json={"comments": "new note"})
    [row] = _rows("comments")
    assert row["before"] is None and row["after"] == "new note"


def test_a_confirmed_comment_write_moves_the_snapshot():
    import app as app_module

    class Api:
        def update_sample_comments(self, sid, comments):
            return {"ok": True}
    q = app_module.UploadQueue(Api(), start_worker=False)
    q.on_comment_saved = app_module._comment_saved
    q._process({"kind": "comment", "sample_id": 5, "comments": "hi", "lab_id": LAB})
    assert _store().snapshots(LAB, "qbench_comments") == {"comments": "hi"}


def test_a_failed_comment_write_leaves_the_snapshot_alone(monkeypatch):
    import app as app_module

    class Api:
        def update_sample_comments(self, sid, comments):
            # app's own class: another test may have reloaded qbench_client
            raise app_module.QBenchAPIError("boom")
    q = app_module.UploadQueue(Api(), start_worker=False)
    q.on_comment_saved = app_module._comment_saved
    q._process({"kind": "comment", "sample_id": 5, "comments": "hi", "lab_id": LAB,
                "attempts": q.MAX_ATTEMPTS})
    assert _store().snapshots(LAB, "qbench_comments") == {}


def test_the_upload_queue_is_wired_to_record_confirmed_comments():
    import inspect
    import app as app_module
    src = inspect.getsource(app_module)
    assert src.count("on_comment_saved = _comment_saved") >= 2


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
