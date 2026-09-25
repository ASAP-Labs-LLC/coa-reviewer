"""``/api/sample-history/<lab_id>``, ``/api/activity``, ``/api/activity/range``
and the ``/activity`` page route (plan Task 8).

These are thin routes: the real logic lives in ``shared_store.py`` (storage,
already tested in ``tests/test_shared_store.py``) and ``activity.py`` (the
pure day builder, already tested in ``tests/test_activity_builder.py``). What
matters here is the *contract* ``static/js/activity.js`` actually reads:

* the three API routes are 401 (JSON) without a portal session;
* history comes back newest-first;
* ``limit`` and ``date`` are validated at the boundary (400, never a 500);
* the ``/api/activity`` payload carries ``online`` / ``is_today`` /
  ``truncated`` alongside ``activity.build_day``'s own keys;
* ``/api/activity/range`` reports ``first`` / ``last`` / ``today``, ``null``
  when the store has nothing yet;
* ``/activity`` itself needs no session (the page handles a 401 from its own
  API calls) and renders the app version.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

pytest.importorskip("flask")

LAB = "092326-00001"


@pytest.fixture
def portal_client(monkeypatch):
    """A test client signed in as "Dana P" — the isolated shared store from
    ``tests/conftest.py``'s autouse fixture is already in place."""
    import app as app_module
    from app import UserState

    uid = "test-uid-activity"
    ustate = UserState(uid, "Dana P")
    with app_module._sessions_lock:
        app_module.user_sessions[uid] = ustate
    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = uid
    yield client
    with app_module._sessions_lock:
        app_module.user_sessions.pop(uid, None)


@pytest.fixture
def anon_client():
    import app as app_module

    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _store():
    import app as app_module
    return app_module.state.shared


# ── 401 without a session ───────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    f"/api/sample-history/{LAB}", "/api/activity", "/api/activity/range",
])
def test_requires_portal_session(anon_client, path):
    resp = anon_client.get(path)
    assert resp.status_code == 401
    assert resp.get_json()["portal_auth"] is False


# ── /api/sample-history ─────────────────────────────────────────────────────

def test_history_is_newest_first(portal_client):
    store = _store()
    store.record_event(LAB, "mark", user="Dana P", field="tests", after="good")
    store.record_event(LAB, "test_result", user="Ryan C", field="Water",
                       before="1.2", after="1.4")
    resp = portal_client.get(f"/api/sample-history/{LAB}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["lab_id"] == LAB
    assert [e["kind"] for e in body["events"]] == ["test_result", "mark"]


def test_history_limit_is_respected(portal_client):
    store = _store()
    for i in range(5):
        store.record_event(LAB, "comments", user="u", after=str(i))
    resp = portal_client.get(f"/api/sample-history/{LAB}?limit=2")
    assert resp.status_code == 200
    assert len(resp.get_json()["events"]) == 2


def test_history_rejects_a_non_integer_limit(portal_client):
    resp = portal_client.get(f"/api/sample-history/{LAB}?limit=abc")
    assert resp.status_code == 400
    assert "limit" in resp.get_json()["error"]


def test_history_for_an_unknown_lab_id_is_an_empty_list(portal_client):
    resp = portal_client.get("/api/sample-history/no-such-lab")
    assert resp.status_code == 200
    assert resp.get_json()["events"] == []


# ── /api/activity ────────────────────────────────────────────────────────────

def test_activity_rejects_a_bad_date(portal_client):
    resp = portal_client.get("/api/activity?date=not-a-date")
    assert resp.status_code == 400
    assert "date" in resp.get_json()["error"]


def test_activity_rejects_a_future_date(portal_client):
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    resp = portal_client.get(f"/api/activity?date={tomorrow}")
    assert resp.status_code == 400


def test_activity_before_any_recorded_history_is_an_empty_day(portal_client):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    resp = portal_client.get(f"/api/activity?date={yesterday}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["users"] == []
    assert body["is_today"] is False
    assert body["online"] == []


def test_activity_payload_has_the_keys_the_frontend_reads(portal_client, monkeypatch):
    import app as app_module

    store = _store()
    store.open_span("Dana P", datetime.now().timestamp() - 60)
    store.record_event(LAB, "mark", user="Dana P", field="tests", after="good")
    monkeypatch.setattr(app_module.state.presence, "online", lambda: ["Dana P"])

    resp = portal_client.get("/api/activity")
    assert resp.status_code == 200
    body = resp.get_json()
    for key in ("date", "users", "bounds", "summary", "truncated", "online", "is_today"):
        assert key in body, f"missing {key!r} in {sorted(body)}"
    assert body["is_today"] is True
    assert body["online"] == ["Dana P"]
    assert body["users"][0]["user"] == "Dana P"
    assert body["summary"]["changes"] == 1


def test_activity_online_is_empty_for_a_past_day_even_if_someone_is_online_now(
        portal_client, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module.state.presence, "online", lambda: ["Dana P"])
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    resp = portal_client.get(f"/api/activity?date={yesterday}")
    body = resp.get_json()
    assert body["is_today"] is False
    assert body["online"] == []
    assert body["summary"]["online_now"] == 0


def test_activity_sets_truncated_when_a_range_query_hits_the_cap(portal_client, monkeypatch):
    import app as app_module
    import shared_store

    events = [{"user": "u", "at": datetime.now().timestamp(), "kind": "mark"}
             for _ in range(shared_store.MAX_RANGE_ROWS)]
    monkeypatch.setattr(app_module.state.shared, "event_marks_between",
                        lambda *a, **k: events)
    resp = portal_client.get("/api/activity")
    assert resp.status_code == 200
    assert resp.get_json()["truncated"] is True


# ── /api/activity/range ──────────────────────────────────────────────────────

def test_range_is_all_nulls_on_an_empty_store(portal_client):
    resp = portal_client.get("/api/activity/range")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["first"] is None and body["last"] is None
    assert body["today"] == date.today().isoformat()


def test_range_reports_the_recorded_span(portal_client):
    store = _store()
    store.record_event(LAB, "mark", user="u", after="good")
    resp = portal_client.get("/api/activity/range")
    body = resp.get_json()
    assert body["first"] == date.today().isoformat()
    assert body["last"] == date.today().isoformat()


# ── /activity page ───────────────────────────────────────────────────────────

def test_activity_page_needs_no_session(anon_client):
    resp = anon_client.get("/activity")
    assert resp.status_code == 200
    assert b'id="activity-root"' in resp.data


def test_activity_page_renders_the_version(anon_client):
    import app as app_module

    resp = anon_client.get("/activity")
    assert resp.status_code == 200
    assert app_module.APP_VERSION.lstrip("v").encode() in resp.data
