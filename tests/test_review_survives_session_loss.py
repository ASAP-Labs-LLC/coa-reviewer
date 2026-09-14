"""A reviewer's marks must outlive the UserState that holds them.

Bug report 2026-09-14 (bug_report_coa-reviewer_state-loss): after ~40
minutes of review on Custom Day, the browser still showed 80 samples with 40
marks while the server had nothing — every tab empty, `has_data: false`,
every PDF 404, Regenerate Pending "0 pending", Export empty. The heartbeat
returned 200 the whole time, so this was NOT a process restart (a fresh
process has no UserState and would 401 every route).

The path, from the code:

1. `triggerTimeout()` fires after 10 idle minutes and stops the heartbeat.
2. `_session_cleanup_worker` pops the UserState 2 minutes later — records,
   results and PDF cache gone, because nothing refreshed `last_active`.
3. The reviewer taps their card. `/api/portal-reauth` finds no UserState for
   the cookie, quietly builds an EMPTY one and answers `ok: true,
   restored: false`.
4. `submitReauth()` hides the overlay and carries on with the stale
   in-memory list. Nothing reloads, nothing warns.

A lunch break is enough to lose a morning's work. The same path is reached
after a process restart (heartbeat 401 → overlay → reauth → empty session),
which is why the fix is not "GC more slowly": the review has to survive on
disk, and a session rebuilt for any reason has to come back with it.

Marks are the expensive human work; sample lists and previews are cheap to
regenerate, so a restored sample that was never judged comes back `pending`
with no preview, and a judged one keeps its verdict.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")


@pytest.fixture
def review(monkeypatch, tmp_path):
    """(client, ustate, labcore_mock) — a live session with three records."""
    import app as app_module
    from app import SampleRecord, UserState

    labcore = MagicMock()
    labcore.authenticate_card.return_value = "RC"
    labcore.authenticate_user.return_value = "RC"
    monkeypatch.setattr(app_module.state, "labcore", labcore)
    monkeypatch.setattr(app_module, "REVIEW_STATE_DIR", tmp_path / "review_state")

    uid = "test-uid-survive"
    ustate = UserState(uid, "RC")
    for tab, lab in (("Custom Day", "090926-39363"),
                     ("Custom Day", "090926-39410"),
                     ("Custom Day", "090926-39422")):
        rec = SampleRecord(lab_id=lab, tab=tab, sample_id=int(lab[-5:]),
                           test_ids=[1, 2], order_id=7310)
        rec.preview_url = "http://preview/" + lab
        rec.status = "ready"
        ustate.add_record(rec)
    with app_module._sessions_lock:
        app_module.user_sessions[uid] = ustate

    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = uid

    yield client, ustate, labcore

    with app_module._sessions_lock:
        for k in [k for k, v in app_module.user_sessions.items() if v.name == "RC"]:
            app_module.user_sessions.pop(k, None)


def _mark(client, outcome, lab, reason=""):
    return client.post("/api/mark", json={
        "tab": "Custom Day", "lab_id": lab, "outcome": outcome, "reason": reason,
    })


def _reap(ustate):
    """Simulate the idle cleanup worker discarding the session."""
    import app as app_module
    with app_module._sessions_lock:
        app_module.user_sessions.pop(ustate.uid, None)


def _tab(client, tab="Custom Day"):
    return {s["lab_id"]: s for s in
            client.get(f"/api/tabs/{tab.replace(' ', '%20')}").get_json()["samples"]}


# ── the review is written down ───────────────────────────────────────────

def test_marking_a_sample_writes_the_review_to_disk(review, tmp_path) -> None:
    client, _, _ = review
    assert _mark(client, "good", "090926-39363").status_code == 200

    files = list((tmp_path / "review_state").glob("*.json"))
    assert files, "no review snapshot was written under DATA_DIR/review_state"
    doc = json.loads(files[0].read_text(encoding="utf-8"))
    by_lab = {r["lab_id"]: r for r in doc["records"]}
    assert by_lab["090926-39363"]["status"] == "good"
    assert any(r["lab_id"] == "090926-39363" and r["outcome"] == "Good"
               for r in doc["session_results"])


def test_a_failed_write_never_breaks_marking(review, monkeypatch, tmp_path) -> None:
    """Same rule as the change log: a dropped share costs a snapshot, not a
    reviewer's mark."""
    import app as app_module
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(app_module, "REVIEW_STATE_DIR", blocker / "review_state")
    client, ustate, _ = review

    assert _mark(client, "good", "090926-39363").status_code == 200
    assert ustate.records[("Custom Day", "090926-39363")].status == "good"


# ── the bug: a reaped session came back empty on re-auth ─────────────────

def test_a_reaped_session_comes_back_with_its_marks_on_reauth(review) -> None:
    """The exact production path: idle → cleanup pops the session → card tap."""
    client, ustate, _ = review
    _mark(client, "good", "090926-39363")
    _mark(client, "bad", "090926-39410", reason="Potency reads low")
    _reap(ustate)

    resp = client.post("/api/portal-reauth", json={"code": "CARD-1"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    assert client.get("/api/config").get_json()["has_data"] is True
    samples = _tab(client)
    assert samples["090926-39363"]["status"] == "good"
    assert samples["090926-39410"]["status"] == "bad"
    assert samples["090926-39410"]["reason"] == "Potency reads low"


def test_a_restored_review_still_exports(review) -> None:
    """Export builds from session_results, which must come back too."""
    client, ustate, _ = review
    _mark(client, "good", "090926-39363")
    _reap(ustate)
    client.post("/api/portal-reauth", json={"code": "CARD-1"})

    resp = client.post("/api/export", json={"tabs": ["Custom Day"], "include_links": False})
    assert resp.status_code == 200, resp.get_json()
    assert "090926-39363" in resp.get_json()["csv"]


def test_reauth_says_whether_the_review_was_recovered_from_disk(review) -> None:
    """The client must be able to tell 'same session' from 'rebuilt' from
    'started over' — they call for different messages."""
    client, ustate, _ = review
    _mark(client, "good", "090926-39363")
    _reap(ustate)

    data = client.post("/api/portal-reauth", json={"code": "CARD-1"}).get_json()
    assert data["restored"] is False
    assert data["recovered"] is True


def test_reauth_with_nothing_on_disk_reports_starting_over(review) -> None:
    client, ustate, _ = review
    _reap(ustate)

    data = client.post("/api/portal-reauth", json={"code": "CARD-1"}).get_json()
    assert data["ok"] is True
    assert data["restored"] is False
    assert data["recovered"] is False


# ── the same after a process restart: a fresh login ──────────────────────

def test_a_fresh_login_restores_a_recent_review(review) -> None:
    """After a restart the cookie's session is gone and the boot screen sends
    the reviewer through portal login. That login must bring the review back
    so `has_data` triggers the tab reload."""
    client, ustate, _ = review
    _mark(client, "good", "090926-39363")
    _reap(ustate)
    with client.session_transaction() as sess:
        sess.clear()

    resp = client.post("/api/portal-login", json={"username": "rc", "password": "pw"})
    assert resp.status_code == 200
    assert client.get("/api/config").get_json()["has_data"] is True
    assert _tab(client)["090926-39363"]["status"] == "good"


def test_a_card_login_restores_a_recent_review(review) -> None:
    client, ustate, _ = review
    _mark(client, "good", "090926-39363")
    _reap(ustate)
    with client.session_transaction() as sess:
        sess.clear()

    client.post("/api/portal-card-login", json={"code": "CARD-1"})
    assert _tab(client)["090926-39363"]["status"] == "good"


def test_a_stale_snapshot_is_not_restored(review, tmp_path) -> None:
    """Yesterday's half-finished review must not greet tomorrow's login."""
    import app as app_module
    client, ustate, _ = review
    _mark(client, "good", "090926-39363")
    _reap(ustate)

    f = next((tmp_path / "review_state").glob("*.json"))
    doc = json.loads(f.read_text(encoding="utf-8"))
    doc["saved_at"] = time.time() - app_module.REVIEW_STATE_MAX_AGE_SECONDS - 60
    f.write_text(json.dumps(doc), encoding="utf-8")

    data = client.post("/api/portal-reauth", json={"code": "CARD-1"}).get_json()
    assert data["recovered"] is False
    assert client.get("/api/config").get_json()["has_data"] is False


def test_a_snapshot_belongs_to_the_account_that_wrote_it(review) -> None:
    client, ustate, labcore = review
    _mark(client, "good", "090926-39363")
    _reap(ustate)
    labcore.authenticate_card.return_value = "Dana P"

    client.post("/api/portal-reauth", json={"code": "CARD-2"})
    assert client.get("/api/config").get_json()["has_data"] is False


# ── what comes back, and in what state ───────────────────────────────────

def test_unjudged_samples_come_back_pending_without_a_preview(review) -> None:
    """Preview URLs are short-lived and the PDF cache is gone; claiming
    `has_preview` would put a 404 in the viewer. Honest state re-renders."""
    client, ustate, _ = review
    _mark(client, "good", "090926-39363")
    _reap(ustate)
    client.post("/api/portal-reauth", json={"code": "CARD-1"})

    samples = _tab(client)
    assert samples["090926-39422"]["status"] == "pending"
    assert samples["090926-39422"]["has_preview"] is False
    assert samples["090926-39363"]["status"] == "good"
    assert samples["090926-39363"]["has_preview"] is False


def test_restored_records_keep_what_a_re_render_needs(review) -> None:
    import app as app_module
    client, ustate, _ = review
    _mark(client, "good", "090926-39363")
    _reap(ustate)
    client.post("/api/portal-reauth", json={"code": "CARD-1"})

    with app_module._sessions_lock:
        new_state = next(v for v in app_module.user_sessions.values() if v.name == "RC")
    rec = new_state.records[("Custom Day", "090926-39422")]
    assert rec.sample_id == 39422
    assert rec.test_ids == [1, 2]
    assert rec.order_id == 7310


# ── the cleanup worker ───────────────────────────────────────────────────

def test_the_cleanup_worker_saves_a_session_before_reaping_it(review, tmp_path) -> None:
    """Belt and braces: even a session that never marked anything keeps its
    pulled list."""
    import app as app_module
    client, ustate, _ = review
    ustate.last_active = time.time() - app_module.SESSION_CLEANUP_SECONDS - 1

    reaped = app_module._reap_idle_sessions(time.time())

    assert [u.uid for u in reaped] == [ustate.uid]
    with app_module._sessions_lock:
        assert ustate.uid not in app_module.user_sessions
    files = list((tmp_path / "review_state").glob("*.json"))
    assert files, "reaping discarded the session without writing it down"
    doc = json.loads(files[0].read_text(encoding="utf-8"))
    assert {r["lab_id"] for r in doc["records"]} == {
        "090926-39363", "090926-39410", "090926-39422"}


def test_the_cleanup_worker_leaves_live_sessions_alone(review) -> None:
    import app as app_module
    client, ustate, _ = review
    ustate.last_active = time.time()

    assert app_module._reap_idle_sessions(time.time()) == []
    with app_module._sessions_lock:
        assert ustate.uid in app_module.user_sessions


# ── the snapshot follows the review ──────────────────────────────────────

def test_start_pulling_clears_the_saved_review(review, monkeypatch, tmp_path) -> None:
    """Start Pulling is the reviewer saying 'new day'; the snapshot must not
    resurrect the old list on the next login."""
    import app as app_module
    client, ustate, _ = review
    _mark(client, "good", "090926-39363")

    monkeypatch.setattr(app_module.state, "logged_in", True)
    monkeypatch.setattr(app_module, "fetch_samples_for_tab", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "fetch_re_review_samples", lambda *a, **k: None)
    assert client.post("/api/start", json={"mode": "tests"}).status_code == 200

    doc = json.loads(next((tmp_path / "review_state").glob("*.json")).read_text(encoding="utf-8"))
    assert doc["records"] == []
    assert doc["session_results"] == []


def test_loading_a_tab_writes_the_list_down(review, monkeypatch, tmp_path) -> None:
    """A pulled list is worth keeping before the first mark, so a session
    lost mid-pull comes back as a list rather than a blank tab."""
    import app as app_module
    from datetime import date
    client, ustate, _ = review

    api = MagicMock()
    api.fetch_samples_by_lab_id_prefix.return_value = [
        {"id": 501, "lab_id": "091026-40001", "order_id": 9},
    ]
    api.fetch_tests_for_sample_ids.return_value = [{"id": 77, "sample_id": 501}]
    monkeypatch.setattr(app_module.state, "api_client", api)

    app_module.fetch_samples_for_tab("Yesterday", date(2026, 9, 10), ustate)

    doc = json.loads(next((tmp_path / "review_state").glob("*.json")).read_text(encoding="utf-8"))
    assert any(r["lab_id"] == "091026-40001" and r["tab"] == "Yesterday"
               for r in doc["records"])
