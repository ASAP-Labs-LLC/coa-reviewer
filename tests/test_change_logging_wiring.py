"""Every change a reviewer makes reaches the audit log on the share.

The log itself is tested in test_change_log.py; these check that each route
that mutates something actually records it, in the right category, with
enough detail to answer "who changed this, from what, to what".
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")


@pytest.fixture
def logged(monkeypatch, tmp_path):
    """(client, ustate, changelog_dir) with the audit log redirected to tmp."""
    import app as app_module
    from app import SampleRecord, UserState
    from change_log import ChangeLog

    d = tmp_path / "changelog"
    monkeypatch.setattr(app_module.state, "change_log", ChangeLog(d))
    monkeypatch.setattr(app_module.state, "labcore", MagicMock())
    app_module.state.labcore.create_task.return_value = {"ok": True, "task_id": 77}
    app_module.state.labcore.complete_task.return_value = {"ok": True}

    queue = MagicMock()
    monkeypatch.setattr(app_module.state, "upload_queue", queue)

    api = MagicMock()
    api.delete_attachment.return_value = {"ok": True}
    monkeypatch.setattr(app_module.state, "api_client", api)
    # /api/sample-info refuses to do anything without a QBench session.
    monkeypatch.setattr(app_module.state, "logged_in", True)

    uid = "test-uid-log"
    ustate = UserState(uid, "RC")
    rec = SampleRecord(lab_id="073126-41552", tab="Yesterday", sample_id=5, test_ids=[9])
    rec.tests_data = [{"test_id": 9, "results": "1.23", "assay": "Water"}]
    ustate.add_record(rec)
    with app_module._sessions_lock:
        app_module.user_sessions[uid] = ustate

    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = uid

    yield client, ustate, d

    with app_module._sessions_lock:
        app_module.user_sessions.pop(uid, None)


def _rows(directory, category):
    hits = sorted(directory.glob(f"{category}-*.jsonl"))
    out = []
    for f in hits:
        out += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    return out


# ── reviews ──────────────────────────────────────────────────────────────

def test_marking_bad_is_logged_with_reason_and_listing(logged) -> None:
    client, _, d = logged
    client.post("/api/mark", json={"tab": "Yesterday", "lab_id": "073126-41552",
                                   "outcome": "bad", "reason": "Potency low",
                                   "cc_task_id": 77})

    row = _rows(d, "reviews")[-1]
    assert row["event"] == "mark"
    assert row["outcome"] == "Bad"
    assert row["user"] == "RC"
    assert row["lab_id"] == "073126-41552"
    assert row["reason"] == "Potency low"
    assert row["cc_task_id"] == 77


def test_marking_good_is_logged(logged) -> None:
    client, _, d = logged
    client.post("/api/mark", json={"tab": "Yesterday", "lab_id": "073126-41552",
                                   "outcome": "good"})
    assert _rows(d, "reviews")[-1]["outcome"] == "Good"


def test_unmarking_is_logged(logged) -> None:
    """Retracting a verdict is exactly the kind of change an audit log is for."""
    client, _, d = logged
    client.post("/api/mark", json={"tab": "Yesterday", "lab_id": "073126-41552",
                                   "outcome": "good"})
    client.post("/api/mark", json={"tab": "Yesterday", "lab_id": "073126-41552",
                                   "outcome": "uncheck"})

    row = _rows(d, "reviews")[-1]
    assert row["event"] == "unmark"
    assert row["lab_id"] == "073126-41552"


def test_a_rejected_mark_is_not_logged(logged) -> None:
    """Nothing changed, so nothing to record."""
    client, _, d = logged
    client.post("/api/mark", json={"tab": "Yesterday", "lab_id": "073126-41552",
                                   "outcome": "bad", "reason": "  "})
    assert _rows(d, "reviews") == []


# ── command centre ───────────────────────────────────────────────────────

def test_creating_a_listing_is_logged(logged) -> None:
    client, _, d = logged
    client.post("/api/cc/tasks", json={"initial_problem": "Water high",
                                       "status": "urgent", "department": "Lab",
                                       "sample_ids": [{"lab_id": "073126-41552"}]})

    row = _rows(d, "command_center")[-1]
    assert row["event"] == "listing_created"
    assert row["task_id"] == 77
    assert row["user"] == "RC"
    assert row["initial_problem"] == "Water high"
    assert row["lab_ids"] == ["073126-41552"]


def test_a_conflict_is_not_logged_as_a_creation(logged) -> None:
    """No listing was created, so the log must not claim one was."""
    import app as app_module
    client, _, d = logged
    app_module.state.labcore.create_task.return_value = {
        "conflict": True, "existing_tasks": [{"id": 3}]}

    client.post("/api/cc/tasks", json={"initial_problem": "x"})
    assert [r for r in _rows(d, "command_center")
            if r["event"] == "listing_created"] == []


def test_completing_a_listing_is_logged_with_its_notes(logged) -> None:
    client, _, d = logged
    client.post("/api/cc/tasks/7/complete", json={"notes": "Re-ran, confirmed"})

    row = _rows(d, "command_center")[-1]
    assert row["event"] == "listing_completed"
    assert row["task_id"] == 7
    assert row["notes"] == "Re-ran, confirmed"
    assert row["user"] == "RC"


# ── qbench edits ─────────────────────────────────────────────────────────

def test_a_test_result_edit_records_the_old_and_new_value(logged) -> None:
    """The whole point of this category: answering "who changed this result,
    and what was it before"."""
    client, _, d = logged
    client.patch("/api/tests/9", json={"value": "4.56"})

    row = _rows(d, "qbench_edits")[-1]
    assert row["event"] == "test_result"
    assert row["test_id"] == 9
    assert row["old_value"] == "1.23"
    assert row["new_value"] == "4.56"
    assert row["user"] == "RC"


def test_a_comment_edit_is_logged(logged) -> None:
    client, _, d = logged
    client.patch("/api/comments/073126-41552", json={"comments": "checked twice"})

    row = _rows(d, "qbench_edits")[-1]
    assert row["event"] == "comments"
    assert row["lab_id"] == "073126-41552"
    assert row["new_value"] == "checked twice"


def test_deleting_an_attachment_is_logged(logged) -> None:
    """Irreversible in QBench — it needs to be attributable."""
    client, _, d = logged
    client.delete("/api/attachments/42")

    row = _rows(d, "qbench_edits")[-1]
    assert row["event"] == "attachment_deleted"
    assert row["attachment_id"] == 42
    assert row["user"] == "RC"


def test_a_sample_info_edit_is_logged(logged) -> None:
    import app as app_module
    client, _, d = logged
    app_module.state.api_client.update_sample.return_value = {"data": [{"id": 5}]}
    app_module.state.api_client.fetch_samples_by_lab_id.return_value = [
        {"id": 5, "lab_id": "073126-41552"}]

    client.patch("/api/sample-info/073126-41552", json={"fw": "FW-9"})

    rows = [r for r in _rows(d, "qbench_edits") if r["event"] == "sample_info"]
    assert rows, "sample-info edits must be logged"
    assert rows[-1]["lab_id"] == "073126-41552"
    assert rows[-1]["fields"] == {"fw": "FW-9"}


# ── sessions ─────────────────────────────────────────────────────────────

def test_login_and_logout_are_logged(logged) -> None:
    import app as app_module
    client, _, d = logged
    app_module.state.labcore.authenticate_user.return_value = "RC"

    client.post("/api/portal-login", json={"username": "rc", "password": "pw"})
    client.post("/api/portal-logout", json={})

    events = [r["event"] for r in _rows(d, "sessions")]
    assert "login" in events
    assert "logout" in events
