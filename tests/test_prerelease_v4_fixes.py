"""Pre-release fixes for v4.0.0.

1. A pre-v4 mark carries no ``mode``. The two tabs whose mode is unambiguous
   keep it (Intaked → info, Re-review → tests); on every other tab the mark
   is mode-agnostic until the session first speaks in a mode, and is then
   stamped with that mode. Before this, all of them were forced to "tests",
   so a reviewer who marked Yesterday in Info mode this morning saw the
   samples pending, with no export rows, after the upgrade.
2. The Time Online tab polls ``/api/activity*`` every 120 s; those polls are
   not a person using the app and must not keep them "online", block the
   3 AM restart, or keep an idle-gated deploy waiting.
3. ``external_change`` history rows are not a person and are left out of
   Time Online.
4. Restart with a staged update saves pending verdicts and dirty sessions
   before asking for the switch (taskkill /F skips _graceful_shutdown).
"""
from __future__ import annotations

import time
from datetime import date

import pytest

pytest.importorskip("flask")


# ── 1. legacy marks keep their mode ─────────────────────────────────────────

def _legacy_doc(tab: str, lab_id: str, status: str = "good") -> dict:
    """What v3.5.0's ``UserState.snapshot()`` wrote: no ``mode`` anywhere."""
    now = time.time()
    today = date.today().isoformat()
    return {
        "version": 2,
        "name": "Dana P",
        "saved_at": now,
        "records": [{
            "tab": tab, "lab_id": lab_id, "sample_id": "123", "test_ids": [],
            "order_id": None, "status": status, "reason": "", "cc_task_id": None,
            "cc_task": None, "info": {},
        }],
        "session_results": [{
            "lab_id": lab_id, "sample_id": "123", "tab": tab,
            "outcome": "Good" if status == "good" else "Bad", "reason": "",
            "reviewer": "Dana P", "date": today,
        }],
        "verdicts": [{
            "tab": tab, "lab_id": lab_id, "status": status, "reason": "",
            "cc_task_id": None, "judged_at": now - 60, "date": today,
        }],
    }


@pytest.fixture
def legacy_session():
    import app as app_module
    from app import UserState

    made = []

    def make(doc):
        uid = f"test-uid-legacy-{len(made)}"
        ustate = UserState(uid, "Dana P")
        ustate.hydrate(doc)
        with app_module._sessions_lock:
            app_module.user_sessions[uid] = ustate
        app_module.app.config["TESTING"] = True
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["uid"] = uid
        made.append(uid)
        return ustate, client

    yield make
    with app_module._sessions_lock:
        for uid in made:
            app_module.user_sessions.pop(uid, None)


def _status(client, tab, mode, lab_id):
    resp = client.get(f"/api/tabs/{tab}?mode={mode}")
    assert resp.status_code == 200
    return {s["lab_id"]: s["status"] for s in resp.get_json()["samples"]}[lab_id]


@pytest.mark.parametrize("tab", ["Yesterday", "Due Out", "Search", "Custom Day"])
def test_a_legacy_info_mode_mark_comes_back_in_info_mode(legacy_session, tab):
    ustate, client = legacy_session(_legacy_doc(tab, "092326-00001"))
    assert _status(client, tab, "info", "092326-00001") == "good"
    rows = ustate.results_for_mode()
    assert [(r["lab_id"], r["outcome"], r["mode"]) for r in rows] == [
        ("092326-00001", "Good", "info")]
    resp = client.post("/api/export", json={"tabs": [tab], "include_links": False})
    assert resp.status_code == 200
    assert b"092326-00001" in resp.data


@pytest.mark.parametrize("tab", ["Yesterday", "Due Out"])
def test_a_legacy_tests_mode_mark_comes_back_in_tests_mode(legacy_session, tab):
    ustate, client = legacy_session(_legacy_doc(tab, "092326-00002", status="bad"))
    assert _status(client, tab, "tests", "092326-00002") == "bad"
    rows = ustate.results_for_mode()
    assert [(r["lab_id"], r["outcome"], r["mode"]) for r in rows] == [
        ("092326-00002", "Bad", "tests")]


def test_once_applied_a_legacy_mark_belongs_to_that_mode(legacy_session):
    """Stamped with the mode it was first seen in, it behaves like any v4
    mark afterwards: switching mode sets it aside, switching back restores."""
    ustate, client = legacy_session(_legacy_doc("Yesterday", "092326-00003"))
    assert _status(client, "Yesterday", "info", "092326-00003") == "good"
    assert _status(client, "Yesterday", "tests", "092326-00003") != "good"
    assert ustate.results_for_mode() == []
    assert ustate.verdicts[("Yesterday", "092326-00003")]["mode"] == "info"
    assert _status(client, "Yesterday", "info", "092326-00003") == "good"
    assert len(ustate.results_for_mode()) == 1


def test_a_legacy_intaked_mark_stays_info_only(legacy_session):
    ustate, client = legacy_session(_legacy_doc("Intaked", "092326-00004"))
    assert _status(client, "Intaked", "tests", "092326-00004") != "good"
    assert ustate.results_for_mode() == []
    assert _status(client, "Intaked", "info", "092326-00004") == "good"
    assert len(ustate.results_for_mode()) == 1


def test_a_legacy_re_review_mark_stays_tests_only(legacy_session):
    ustate, client = legacy_session(_legacy_doc("Re-review", "092326-00005"))
    assert _status(client, "Re-review", "info", "092326-00005") != "good"
    assert ustate.results_for_mode() == []


def test_a_legacy_ledger_entry_reapplies_in_the_session_mode():
    """The Start Pulling path: only the ledger comes back, and a re-pulled
    sample picks the mark up in whichever mode the session is in."""
    from app import SampleRecord, UserState

    doc = _legacy_doc("Due Out", "092326-00006")
    doc["records"], doc["session_results"] = [], []
    ustate = UserState("test-uid-ledger", "Dana P")
    ustate.hydrate(doc)
    ustate.mode = "info"
    rec = SampleRecord(lab_id="092326-00006", tab="Due Out")
    ustate.add_record(rec)
    assert rec.status == "good" and rec.verdict_mode == "info"
    assert ustate.verdicts[("Due Out", "092326-00006")]["mode"] == "info"
    assert [r["mode"] for r in ustate.results_for_mode()] == ["info"]


# ── 2. Time Online polls are not activity ───────────────────────────────────

@pytest.fixture
def signed_in(monkeypatch):
    import app as app_module
    from app import UserState

    uid = "test-uid-time-online"
    ustate = UserState(uid, "Dana P")
    with app_module._sessions_lock:
        app_module.user_sessions[uid] = ustate
    touched = []
    monkeypatch.setattr(app_module, "_presence_touch", lambda name: touched.append(name))
    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = uid
    yield app_module, ustate, client, touched
    with app_module._sessions_lock:
        app_module.user_sessions.pop(uid, None)


@pytest.mark.parametrize("path", ["/api/activity", "/api/activity/range"])
def test_time_online_polls_do_not_count_as_activity(signed_in, path):
    app_module, ustate, client, touched = signed_in
    app_module._last_request_time = time.time() - 600
    before = app_module._last_request_time
    ustate.last_active = time.time() - 600
    last_active = ustate.last_active

    assert client.get(path).status_code == 200

    assert app_module._last_request_time == before
    assert ustate.last_active == last_active
    assert touched == []


@pytest.mark.parametrize("path", ["/api/activity", "/api/activity/range"])
def test_time_online_polls_still_need_a_session(path):
    import app as app_module

    app_module.app.config["TESTING"] = True
    resp = app_module.app.test_client().get(path)
    assert resp.status_code == 401


def test_sample_history_still_counts_as_activity(signed_in):
    """Opened from the review screen by a person — that is activity."""
    app_module, ustate, client, touched = signed_in
    app_module._last_request_time = time.time() - 600
    ustate.last_active = time.time() - 600
    assert client.get("/api/sample-history/092326-00001").status_code == 200
    assert app_module._last_request_time > time.time() - 5
    assert ustate.last_active > time.time() - 5
    assert touched == ["Dana P"]


# ── 3. external changes are not a person ────────────────────────────────────

def test_event_marks_between_leaves_out_external_changes():
    import app as app_module

    store = app_module.state.shared
    t0 = time.time()
    store.record_event("092326-00001", "mark", user="Dana P", field="tests",
                       after="good")
    store.record_event("092326-00001", "external_change", user="Outside COA Reviewer",
                       field="Water", before="1", after="2")
    rows = store.event_marks_between(t0 - 5, time.time() + 5)
    assert [r["user"] for r in rows] == ["Dana P"]
    assert all(r["kind"] != "external_change" for r in rows)


# ── 4. a switch saves shared-verdict state first ────────────────────────────

def test_a_switch_saves_pending_verdicts_and_dirty_sessions(tmp_path, monkeypatch):
    import app as app_module

    calls = []

    class _Presence:
        def flush(self, blocking=False, timeout=5.0):
            calls.append("presence")

    monkeypatch.setattr(app_module.state, "presence", _Presence())
    monkeypatch.setattr(app_module.pending_verdicts, "save",
                        lambda: calls.append("pending"))
    monkeypatch.setattr(app_module, "_persist_dirty_sessions",
                        lambda *a, **k: calls.append("sessions") or 0)
    app_module._save_presence_before_switch()
    assert set(calls) == {"presence", "pending", "sessions"}


def test_a_failing_save_never_blocks_the_switch(monkeypatch):
    import app as app_module

    def boom(*_a, **_k):
        raise RuntimeError("disk full")

    class _Presence:
        def flush(self, blocking=False, timeout=5.0):
            raise RuntimeError("presence down")

    saved = []
    monkeypatch.setattr(app_module.state, "presence", _Presence())
    monkeypatch.setattr(app_module.pending_verdicts, "save", boom)
    monkeypatch.setattr(app_module, "_persist_dirty_sessions",
                        lambda *a, **k: saved.append("sessions") or 0)
    app_module._save_presence_before_switch()          # never raises
    assert saved == ["sessions"]                      # one failure doesn't skip the next
