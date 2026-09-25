"""Restart installs a staged release — the app's half of the protocol.

When a newer, healthy release is staged, Restart asks the updater to switch
(``restart_update``) instead of respawning the same version, then watches for
the updater's answer. What must hold whatever the updater does:

* the person who clicked Restart always gets a restart;
* once the updater has taken the request, this process never spawns its own
  replacement (a self-respawned child can outlive the updater's kill and
  double-bind the port — the 2026-07-31 incident), and neither does any
  restart while ``DATA_DIR/switching`` exists;
* an outcome file left over from an earlier request is never mistaken for
  the answer to this one.

No real process ever exits here: ``_graceful_shutdown`` is replaced by a
recorder (or, where its own body is under test, ``os._exit`` and the respawn
are), threads are captured instead of started, and ``_restart_sleep`` is
where the fake updater acts.
"""

from __future__ import annotations

import json
import logging
import threading

import pytest

pytest.importorskip("flask")

import restart_update


class _Presence:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.calls = []
        self.marker_at_close = None

    def close_all(self, reason):
        self.calls.append(("close_all", reason))
        self.marker_at_close = (self.data_dir / restart_update.MARKER_FILE).exists()
        return 0

    def flush(self):
        self.calls.append(("flush",))
        return 0

    def touch(self, user):
        pass


class Env:
    def __init__(self, app_module, data_dir):
        self.app = app_module
        self.data_dir = data_dir
        self.shutdowns = []      # (reason, respawn)
        self.spawned = []        # (target, args, name)
        self.sleeps = 0
        self.on_sleep = None     # the fake updater: called with the sleep count

    def stage(self, tag="v9.9.9", healthy=True):
        (self.data_dir / restart_update.STAGED_FILE).write_text(
            json.dumps({"tag": tag, "healthy": healthy}), encoding="utf-8")

    def marker(self):
        return json.loads((self.data_dir / restart_update.MARKER_FILE)
                          .read_text(encoding="utf-8"))

    def claim(self, state, **extra):
        """Do what the updater does: rename the marker into an outcome."""
        doc = self.marker()
        doc.update(extra)
        dest = restart_update.ACCEPTED_FILE if state == "accepted" else restart_update.REFUSED_FILE
        (self.data_dir / restart_update.MARKER_FILE).unlink()
        (self.data_dir / dest).write_text(json.dumps(doc), encoding="utf-8")

    def run_spawned(self):
        target, args, _ = self.spawned.pop(0)
        target(*args)


@pytest.fixture
def env(tmp_path, monkeypatch):
    import app as app_module

    e = Env(app_module, tmp_path)
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_module, "APP_VERSION", "v3.5.0")
    monkeypatch.setattr(app_module, "_restart_requested", False)
    monkeypatch.setattr(app_module, "_restart_tag", None)
    presence = _Presence(tmp_path)
    monkeypatch.setattr(app_module.state, "presence", presence)
    e.presence = presence

    def fake_shutdown(reason="unknown", *, respawn=True):
        e.shutdowns.append((reason, respawn))
    monkeypatch.setattr(app_module, "_graceful_shutdown", fake_shutdown)

    def fake_spawn(target, args, name):
        e.spawned.append((target, args, name))
    monkeypatch.setattr(app_module, "_spawn", fake_spawn)

    def fake_sleep(_seconds):
        e.sleeps += 1
        if e.on_sleep is not None:
            e.on_sleep(e.sleeps)
    monkeypatch.setattr(app_module, "_restart_sleep", fake_sleep)
    return e


def _await(env, pickup=3.0, accepted_wait=2.0):
    """Run the spawned watcher with small, poll-counted budgets."""
    target, args, name = env.spawned.pop(0)
    assert name == "await-switch"
    tag, at = args
    env.app._await_switch(tag, at, pickup=pickup, accepted_wait=accepted_wait, poll=1.0)


# ── choosing the path ────────────────────────────────────────────────────

def test_no_staged_update_is_a_plain_restart(env) -> None:
    assert env.app.request_restart("the tray icon", by="tray") is None
    assert not (env.data_dir / restart_update.MARKER_FILE).exists()
    assert env.shutdowns == []            # never synchronously
    env.run_spawned()
    assert env.shutdowns == [("manual restart", True)]


def test_a_staged_release_that_is_not_newer_is_a_plain_restart(env) -> None:
    env.stage(tag="v3.5.0")
    assert env.app.request_restart("x", by="Dana P") is None
    assert not (env.data_dir / restart_update.MARKER_FILE).exists()


def test_a_staged_upgrade_writes_the_request_and_waits(env) -> None:
    env.stage()
    assert env.app.request_restart("the Restart button", by="Dana P") == "v9.9.9"
    doc = env.marker()
    assert doc["tag"] == "v9.9.9" and doc["by"] == "Dana P"
    assert env.shutdowns == []
    assert [s[2] for s in env.spawned] == ["await-switch"]
    assert env.spawned[0][1] == ("v9.9.9", doc["at"])


def test_presence_is_saved_before_the_request_is_written(env) -> None:
    """The switch kills us with taskkill /F, which skips _graceful_shutdown —
    so whatever presence has in memory must be on disk before we ask."""
    env.stage()
    env.app.request_restart("x", by="Dana P")
    assert ("close_all", "restart") in env.presence.calls
    assert env.presence.marker_at_close is False


def test_a_second_request_while_one_is_pending_does_nothing(env) -> None:
    env.stage()
    first = env.app.request_restart("x", by="Dana P")
    written = env.marker()
    second = env.app.request_restart("y", by="Lee K")
    assert first == second == "v9.9.9"
    assert env.marker() == written
    assert len(env.spawned) == 1


def test_concurrent_requests_start_exactly_one_restart(env) -> None:
    env.stage()
    barrier = threading.Barrier(8)
    results = []

    def click(i):
        barrier.wait()
        results.append(env.app.request_restart(f"click {i}", by=f"user{i}"))
    threads = [threading.Thread(target=click, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert results == ["v9.9.9"] * 8
    assert len(env.spawned) == 1


def test_a_failed_write_falls_back_and_leaves_no_marker(env, monkeypatch) -> None:
    env.stage()

    def half_written(data_dir, tag, *, by, now):
        (data_dir / restart_update.MARKER_FILE).write_text("{", encoding="utf-8")
        return False
    monkeypatch.setattr(restart_update, "write_switch_request", half_written)

    assert env.app.request_restart("x", by="Dana P") is None
    assert not (env.data_dir / restart_update.MARKER_FILE).exists()
    env.run_spawned()
    assert env.shutdowns == [("manual restart", True)]


def test_a_broken_presence_does_not_stop_the_restart(env, monkeypatch) -> None:
    env.stage()

    def boom(_reason):
        raise RuntimeError("presence down")
    monkeypatch.setattr(env.presence, "close_all", boom)
    assert env.app.request_restart("x", by="Dana P") == "v9.9.9"


# ── what the updater answers ─────────────────────────────────────────────

def test_refused_restarts_normally_at_once(env, caplog) -> None:
    env.stage()
    env.app.request_restart("x", by="Dana P")
    env.on_sleep = lambda n: env.claim("refused", why="app is paused") if n == 1 else None

    with caplog.at_level(logging.WARNING, logger="coa.restart"):
        _await(env, pickup=30.0)

    assert env.shutdowns == [("manual restart", True)]
    assert env.sleeps <= 2
    assert not (env.data_dir / restart_update.REFUSED_FILE).exists()
    assert any("app is paused" in r.getMessage() for r in caplog.records)


def test_accepted_but_not_killed_exits_without_respawning(env) -> None:
    env.stage()
    env.app.request_restart("x", by="Dana P")
    env.on_sleep = lambda n: env.claim("accepted") if n == 1 else None

    _await(env, pickup=30.0, accepted_wait=5.0)

    assert env.shutdowns == [("manual restart", False)]
    assert env.sleeps >= 5                # it did wait to be killed first


def test_marker_gone_without_an_outcome_exits_without_respawning(env) -> None:
    """The updater's claim can fall back to deleting the marker. Something
    took it; we cannot know it was refused, so we must not respawn."""
    env.stage()
    env.app.request_restart("x", by="Dana P")
    env.on_sleep = lambda n: ((env.data_dir / restart_update.MARKER_FILE).unlink()
                              if n == 1 else None)

    _await(env, pickup=30.0, accepted_wait=2.0)

    assert env.shutdowns == [("manual restart", False)]


def test_nobody_takes_it_so_it_is_withdrawn_and_restarts_normally(env, caplog) -> None:
    env.stage()
    env.app.request_restart("x", by="Dana P")

    with caplog.at_level(logging.WARNING, logger="coa.restart"):
        _await(env, pickup=3.0)

    assert env.shutdowns == [("manual restart", True)]
    assert not (env.data_dir / restart_update.MARKER_FILE).exists()
    assert env.sleeps == 3
    assert any("did not take" in r.getMessage() for r in caplog.records)


def test_claimed_in_the_same_instant_as_the_withdrawal(env, monkeypatch) -> None:
    """Withdraw finds the marker already gone: the updater won the race and
    its answer is still to come."""
    env.stage()
    env.app.request_restart("x", by="Dana P")

    def lost_race(_data_dir):
        env.claim("accepted")
        return False
    monkeypatch.setattr(restart_update, "withdraw_switch_request", lost_race)

    _await(env, pickup=2.0, accepted_wait=2.0)

    assert env.shutdowns == [("manual restart", False)]


def test_an_outcome_from_an_earlier_request_is_ignored(env) -> None:
    """A leftover ``switch-accepted`` with someone else's ``at`` must not
    read as "accepted" — that would exit without a respawn for nothing."""
    env.stage()
    (env.data_dir / restart_update.ACCEPTED_FILE).write_text(
        json.dumps({"tag": "v9.9.8", "by": "old", "at": 12.0}), encoding="utf-8")
    env.app.request_restart("x", by="Dana P")

    _await(env, pickup=3.0)

    assert env.shutdowns == [("manual restart", True)]    # withdrawn, normal


def test_only_one_shutdown_per_request(env) -> None:
    env.stage()
    env.app.request_restart("x", by="Dana P")
    env.on_sleep = lambda n: env.claim("accepted") if n == 2 else None
    _await(env, pickup=30.0, accepted_wait=3.0)
    assert len(env.shutdowns) == 1


# ── the route and the tray ───────────────────────────────────────────────

@pytest.fixture
def client(env):
    from app import UserState

    uid = "test-uid-restart"
    with env.app._sessions_lock:
        env.app.user_sessions[uid] = UserState(uid, "Dana P")
    env.app.app.config["TESTING"] = True
    c = env.app.app.test_client()
    with c.session_transaction() as sess:
        sess["uid"] = uid
    yield c
    with env.app._sessions_lock:
        env.app.user_sessions.pop(uid, None)


def test_api_restart_names_the_update_being_installed(env, client) -> None:
    env.stage()
    body = client.post("/api/restart").get_json()
    assert body["ok"] is True and body["update"] == "v9.9.9"
    assert env.marker()["by"] == "Dana P"


def test_api_restart_update_is_null_for_a_plain_restart(env, client) -> None:
    body = client.post("/api/restart").get_json()
    assert body["ok"] is True and body["update"] is None


def test_the_tray_restarts_as_tray() -> None:
    import app as app_module
    from pathlib import Path
    src = Path(app_module.__file__).read_text(encoding="utf-8")
    main = src[src.index('if __name__ == "__main__"'):]
    assert 'request_restart("the tray icon", by="tray")' in main


# ── _graceful_shutdown itself ────────────────────────────────────────────

class _Exited(Exception):
    pass


@pytest.fixture
def real_shutdown(tmp_path, monkeypatch):
    """The real _graceful_shutdown with its irreversible edges replaced."""
    import app as app_module
    import tray

    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    monkeypatch.delenv("COA_WATCHER_ACTIVE", raising=False)
    spawned = []
    monkeypatch.setattr(app_module, "_self_respawn", lambda: spawned.append(True))
    monkeypatch.setattr(app_module, "_restart_sleep", lambda _s: None)
    monkeypatch.setattr(tray, "stop_tray", lambda: None)
    presence = _Presence(tmp_path)
    monkeypatch.setattr(app_module.state, "presence", presence)

    def fake_exit(code):
        raise _Exited(code)
    monkeypatch.setattr(app_module.os, "_exit", fake_exit)
    return app_module, spawned, presence


def test_shutdown_saves_presence_and_respawns(real_shutdown) -> None:
    app_module, spawned, presence = real_shutdown
    with pytest.raises(_Exited):
        app_module._graceful_shutdown("manual restart")
    assert spawned == [True]
    assert ("close_all", "restart") in presence.calls


def test_shutdown_without_respawn_does_not_respawn(real_shutdown) -> None:
    app_module, spawned, _ = real_shutdown
    with pytest.raises(_Exited):
        app_module._graceful_shutdown("manual restart", respawn=False)
    assert spawned == []


def test_nothing_respawns_while_the_updater_is_switching(real_shutdown, tmp_path) -> None:
    app_module, spawned, _ = real_shutdown
    (tmp_path / "switching").write_text("switch in progress", encoding="utf-8")
    with pytest.raises(_Exited):
        app_module._graceful_shutdown("auto-restart")
    assert spawned == []


def test_a_broken_presence_does_not_stop_the_shutdown(real_shutdown, monkeypatch) -> None:
    app_module, spawned, presence = real_shutdown

    def boom(_reason):
        raise RuntimeError("presence down")
    monkeypatch.setattr(presence, "close_all", boom)
    with pytest.raises(_Exited):
        app_module._graceful_shutdown("manual restart")
    assert spawned == [True]


# ── nothing unexpected may cost the restart ──────────────────────────────

def test_an_unexpected_error_preparing_still_restarts(env, monkeypatch) -> None:
    env.stage()

    def boom(*_a, **_k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(restart_update, "write_switch_request", boom)

    assert env.app.request_restart("x", by="Dana P") is None
    env.run_spawned()
    assert env.shutdowns == [("manual restart", True)]


def test_a_watcher_error_before_anyone_took_it_restarts_normally(env, monkeypatch) -> None:
    env.stage()
    env.app.request_restart("x", by="Dana P")

    def boom(_data_dir):
        raise RuntimeError("unreadable")
    monkeypatch.setattr(restart_update, "read_switch_outcome", boom)
    _await(env)
    assert env.shutdowns == [("manual restart", True)]
    assert not (env.data_dir / restart_update.MARKER_FILE).exists()


def test_a_watcher_error_after_it_was_taken_does_not_respawn(env, monkeypatch) -> None:
    env.stage()
    env.app.request_restart("x", by="Dana P")
    env.on_sleep = lambda n: env.claim("accepted") if n == 1 else None
    real = restart_update.read_switch_outcome

    def flaky(data_dir):
        if env.sleeps >= 1 and not (data_dir / restart_update.MARKER_FILE).exists():
            raise RuntimeError("unreadable")
        return real(data_dir)
    monkeypatch.setattr(restart_update, "read_switch_outcome", flaky)
    _await(env)
    assert env.shutdowns == [("manual restart", False)]
