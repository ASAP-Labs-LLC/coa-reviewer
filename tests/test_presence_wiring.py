"""Presence and the shared store, wired into the running app.

``presence.py`` and ``shared_store.py`` are covered on their own; this is
about *where the app calls them*: every reviewer request and heartbeat
counts as "here", logout and the idle reaper end the span with the right
reason, the cleanup worker is what flushes, and a fresh process tidies up
whatever the last one left behind.
"""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("flask")

from presence import PresenceTracker
from shared_store import SharedStore


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """(client, tracker, store, uid) — the app's presence on a tmp store,
    with a logged-in reviewer called "Dana P"."""
    import app as app_module
    from app import UserState

    store = SharedStore(tmp_path / "coa_shared.db")
    tracker = PresenceTracker(store)
    monkeypatch.setattr(app_module.state, "shared", store)
    monkeypatch.setattr(app_module.state, "presence", tracker)

    uid = "test-uid-presence"
    with app_module._sessions_lock:
        app_module.user_sessions[uid] = UserState(uid, "Dana P")

    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = uid

    yield client, tracker, store, uid

    with app_module._sessions_lock:
        app_module.user_sessions.pop(uid, None)
    store.close()


# ── what AppState is built with ──────────────────────────────────────────

def test_state_has_a_shared_store_in_data_dir_and_a_tracker() -> None:
    import app as app_module
    assert isinstance(app_module.state.shared, SharedStore)
    assert app_module.state.shared.path == app_module.DATA_DIR / "coa_shared.db"
    assert isinstance(app_module.state.presence, PresenceTracker)


def _leave_a_mess(tmp_path):
    old = SharedStore(tmp_path / "coa_shared.db")
    assert old.apply_presence([{"op": "open", "user": "Dana P",
                                "started": 1000.0, "last_seen": 1060.0}])
    old.close()
    for name in ("switch-requested", "switch-accepted"):
        (tmp_path / name).write_text("{}", encoding="utf-8")


def test_the_serving_process_closes_spans_and_clears_switch_files_left_behind(
        tmp_path, monkeypatch, caplog) -> None:
    """A span still open, or a switch file still lying there, belongs to a
    process that is gone: a new one means the restart already happened."""
    import app as app_module

    _leave_a_mess(tmp_path)
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    store = SharedStore(tmp_path / "coa_shared.db")
    try:
        with caplog.at_level(logging.INFO):
            app_module._tidy_after_last_run(store)
        span = store.spans_between(0, 1e10)[0]
        assert span["open"] is False and span["end_reason"] == "restart"
        assert span["end"] == 1060.0
        assert not (tmp_path / "switch-requested").exists()
        assert not (tmp_path / "switch-accepted").exists()
        assert any(r.levelno == logging.WARNING and "leftover switch file" in r.getMessage()
                   for r in caplog.records)
    finally:
        store.close()


def test_building_state_touches_nothing_on_disk(tmp_path, monkeypatch, isolated_app_paths) -> None:
    """AppState is built at import, before the port guard. A duplicate
    launch that is about to exit must not close the live process's spans or
    delete its pending switch request, and importing the app must not create
    the database (tests/test_data_dir.py imports it with DATA_DIR = APP_DIR)."""
    import app as app_module

    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path / "fresh")
    (tmp_path / "fresh").mkdir()
    (tmp_path / "fresh" / "switch-requested").write_text("{}", encoding="utf-8")
    fresh = app_module.AppState()
    try:
        assert (tmp_path / "fresh" / "switch-requested").exists()
        assert not (tmp_path / "fresh" / "coa_shared.db").exists()
    finally:
        fresh.shared.close()


# ── touching ─────────────────────────────────────────────────────────────

def test_heartbeat_keeps_the_reviewer_online(wired) -> None:
    client, tracker, store, _ = wired
    assert client.post("/api/heartbeat").status_code == 200
    assert tracker.online() == ["Dana P"]
    assert store.spans_between(0, 1e10) == []   # memory-only until a flush


def test_any_reviewer_request_counts_as_here(wired) -> None:
    client, tracker, _, _ = wired
    client.get("/api/portal-session")
    assert tracker.online() == ["Dana P"]


def test_health_polls_do_not_count_as_here(wired) -> None:
    """Same rule as the activity clock: a health poll is the app asking
    after itself, not a person."""
    client, tracker, _, _ = wired
    client.get("/api/health")
    client.get("/healthz")
    assert tracker.online() == []


def test_an_anonymous_request_does_not_count(wired) -> None:
    import app as app_module
    _, tracker, _, _ = wired
    anon = app_module.app.test_client()
    anon.get("/")
    anon.get("/api/portal-session")
    assert tracker.online() == []


def test_a_broken_tracker_never_breaks_a_request(wired, monkeypatch) -> None:
    client, tracker, _, _ = wired

    def boom(_user):
        raise RuntimeError("presence exploded")
    monkeypatch.setattr(tracker, "touch", boom)
    assert client.post("/api/heartbeat").status_code == 200


# ── ending ───────────────────────────────────────────────────────────────

def test_logout_ends_the_span_as_logout(wired) -> None:
    client, tracker, store, _ = wired
    client.post("/api/heartbeat")
    client.post("/api/portal-logout")
    assert tracker.online() == []
    tracker.flush()
    span = store.spans_between(0, 1e10)[0]
    assert span["end_reason"] == "logout" and span["open"] is False


def test_the_idle_reaper_ends_the_span_as_timeout(wired, monkeypatch) -> None:
    import app as app_module
    client, tracker, store, uid = wired
    client.post("/api/heartbeat")
    with app_module._sessions_lock:
        ustate = app_module.user_sessions[uid]
    monkeypatch.setattr(ustate, "persist", lambda: None)
    ustate.last_active = 0.0

    reaped = app_module._reap_idle_sessions(app_module.SESSION_CLEANUP_SECONDS + 10.0)

    assert [u.name for u in reaped] == ["Dana P"]
    tracker.flush()
    span = store.spans_between(0, 1e10)[0]
    assert span["end_reason"] == "timeout" and span["open"] is False


# ── the cleanup worker flushes ───────────────────────────────────────────

class _Recorder:
    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def _call(self, name, *args):
        self.calls.append(name)
        if name in self.fail:
            raise RuntimeError(f"{name} failed")
        return 0

    def sweep(self):
        return self._call("sweep")

    def flush(self, blocking=False, timeout=5.0):
        return self._call("flush")


def test_cleanup_cycle_sweeps_then_flushes(monkeypatch) -> None:
    import app as app_module
    rec = _Recorder()
    monkeypatch.setattr(app_module.state, "presence", rec)
    app_module._session_cleanup_cycle(0.0)
    assert rec.calls == ["sweep", "flush"]


def test_a_failing_sweep_still_flushes(monkeypatch, caplog) -> None:
    import app as app_module
    rec = _Recorder(fail={"sweep"})
    monkeypatch.setattr(app_module.state, "presence", rec)
    with caplog.at_level(logging.ERROR):
        app_module._session_cleanup_cycle(0.0)
    assert rec.calls == ["sweep", "flush"]
    assert any("sweep" in r.getMessage() for r in caplog.records)


def test_a_failing_reaper_still_flushes(monkeypatch) -> None:
    import app as app_module
    rec = _Recorder()
    monkeypatch.setattr(app_module.state, "presence", rec)

    def boom(_now):
        raise RuntimeError("reaper failed")
    monkeypatch.setattr(app_module, "_reap_idle_sessions", boom)
    app_module._session_cleanup_cycle(0.0)
    assert rec.calls == ["sweep", "flush"]


def test_the_worker_flushes_at_the_presence_cadence(monkeypatch) -> None:
    """Presence loses at most one flush interval on a crash; the worker is
    what sets that interval."""
    import app as app_module
    from presence import FLUSH_SECONDS
    rec = _Recorder()
    monkeypatch.setattr(app_module.state, "presence", rec)
    monkeypatch.setattr(app_module, "_reap_idle_sessions", lambda _now: [])
    slept = []
    app_module._session_cleanup_worker(cycles=3, sleep=slept.append)
    assert rec.calls.count("flush") == 3
    assert len(slept) == 3 and all(0 < s <= FLUSH_SECONDS for s in slept)


def test_a_failing_touch_logs_once_then_rate_limits(wired, monkeypatch, caplog) -> None:
    import app as app_module
    client, tracker, _, _ = wired

    def boom(_user):
        raise RuntimeError("presence exploded")
    monkeypatch.setattr(tracker, "touch", boom)
    monkeypatch.setattr(app_module, "_touch_failures", 0)
    monkeypatch.setattr(app_module, "_touch_fail_logged_at", None)
    with caplog.at_level(logging.ERROR):
        for _ in range(5):
            client.post("/api/heartbeat")
    failures = [r for r in caplog.records if "presence touch failed" in r.getMessage()]
    assert len(failures) == 1 and failures[0].exc_info

    monkeypatch.setattr(app_module, "TOUCH_FAIL_LOG_INTERVAL_SECONDS", 0.0)
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        client.post("/api/heartbeat")
    again = [r for r in caplog.records if "presence touch failed" in r.getMessage()]
    assert len(again) == 1 and "6" in again[0].getMessage()


# ── /api/health ──────────────────────────────────────────────────────────

def test_api_health_reports_the_version(monkeypatch) -> None:
    import app as app_module
    monkeypatch.setattr(app_module, "APP_VERSION", "v3.5.0")
    body = app_module.app.test_client().get("/api/health").get_json()
    assert body["ok"] is True and body["version"] == "v3.5.0"
