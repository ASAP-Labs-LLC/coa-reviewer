"""The QBench web login, wired into the app.

What a reviewer sees: a login saved once is used after every update, the
manual login is always there, a login that can't be remembered still works for
this session (and says so plainly), and nothing about files, paths or
encryption ever reaches the browser. What the server guarantees: the password
leaves web_app_config.json only once it is safely somewhere else, and a
health check never signs in to QBench.

QBench itself is never contacted: ``COASession`` is replaced by a fake.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

pytest.importorskip("flask")

import app as app_module  # noqa: E402
from qbench_login import LoginStore  # noqa: E402

USER = "reviewer@example.com"
SECRET = "pw-" + "only-for-tests-" + "91c2"
APP_SRC = Path(app_module.__file__).read_text(encoding="utf-8")


class FakeSession:
    """Stands in for COASession; records who it was asked to sign in as."""

    instances: list = []
    fail_with = None

    def __init__(self, username, password, report_config_id=None):
        self.username = username
        self.password = password
        FakeSession.instances.append(self)

    def login(self, headless=True):
        if FakeSession.fail_with:
            raise RuntimeError(FakeSession.fail_with)


@pytest.fixture
def env(monkeypatch, tmp_path, isolated_app_paths):
    """A signed-in portal client, a fresh config, a throwaway login store."""
    cfg_path, _ = isolated_app_paths
    store = LoginStore(tmp_path / "appdata" / "coa-qbench-login.json",
                       tmp_path / "data" / "qbench_login.json")
    monkeypatch.setattr(app_module, "login_store", store)
    monkeypatch.setattr(app_module.state, "config", dict(app_module.DEFAULT_CONFIG))
    monkeypatch.setattr(app_module.state, "logged_in", False)
    monkeypatch.setattr(app_module.state, "coa_session", None)
    monkeypatch.setattr(app_module.state, "upload_queue", None)
    monkeypatch.setattr(app_module, "COASession", FakeSession)
    monkeypatch.setattr(app_module, "PLAYWRIGHT_AVAILABLE", True)
    monkeypatch.setattr(app_module, "UploadQueue", lambda client: object())
    monkeypatch.setattr(app_module, "_wire_upload_queue", lambda q: q)
    monkeypatch.delenv("COA_HEALTH_CHECK", raising=False)
    FakeSession.instances = []
    FakeSession.fail_with = None

    uid = "test-uid-qbench-login"
    with app_module._sessions_lock:
        app_module.user_sessions[uid] = app_module.UserState(uid, "RC")
    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = uid
    yield client, store, cfg_path
    with app_module._sessions_lock:
        app_module.user_sessions.pop(uid, None)


def _unusable(path: Path) -> None:
    path.parent.parent.mkdir(parents=True, exist_ok=True)
    path.parent.write_text("not a directory")


def _legacy_config(cfg_path: Path, username=USER, password=SECRET) -> None:
    cfg = dict(app_module.DEFAULT_CONFIG, qbench_username=username,
               qbench_password=password)
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    app_module.state.config = app_module.load_config()


# ── where the login comes from ───────────────────────────────────────────

def test_the_store_wins_over_the_config(env):
    _, store, cfg_path = env
    _legacy_config(cfg_path, "old@example.com", "old-pass")
    store.save(USER, SECRET)
    assert app_module.get_qbench_login() == (USER, SECRET)


def test_the_config_is_the_legacy_fallback(env):
    _, _, cfg_path = env
    _legacy_config(cfg_path)
    assert app_module.get_qbench_login() == (USER, SECRET)


def test_no_login_anywhere(env):
    assert app_module.get_qbench_login() is None


def test_the_real_store_falls_back_to_data_dir_and_survives_an_update(
        monkeypatch, tmp_path):
    """%APPDATA% unusable: the app keeps the login in DATA_DIR by itself,
    and the next process (a release swap keeps DATA_DIR) finds it."""
    primary = tmp_path / "Roaming" / "ASAPLabs" / "coa-qbench-login.json"
    _unusable(primary)
    monkeypatch.setenv("COA_QBENCH_LOGIN_PATH", str(primary))
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    first = app_module._new_login_store(data_dir)
    assert first.save(USER, SECRET) is True
    assert (data_dir / "qbench_login.json").is_file()

    after_update = app_module._new_login_store(data_dir)
    assert after_update.load() == (USER, SECRET)


# ── moving the password out of web_app_config.json ──────────────────────

def test_migration_moves_config_creds_into_the_store(env, caplog):
    _, store, cfg_path = env
    _legacy_config(cfg_path)
    with caplog.at_level(logging.INFO):
        assert app_module.migrate_login_out_of_config() is True
    assert store.load() == (USER, SECRET)
    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert on_disk["qbench_password"] == ""
    assert on_disk["qbench_username"] == USER          # kept for display
    assert app_module.state.config["qbench_password"] == ""
    assert any("moved saved QBench login out of web_app_config.json into"
               in r.getMessage() for r in caplog.records)
    assert all(SECRET not in r.getMessage() for r in caplog.records)


def test_migration_leaves_the_config_alone_when_the_store_cannot_save(env):
    _, store, cfg_path = env
    _unusable(store.primary)
    _unusable(store.fallback)
    _legacy_config(cfg_path)
    assert app_module.migrate_login_out_of_config() is False
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["qbench_password"] == SECRET
    assert app_module.get_qbench_login() == (USER, SECRET)   # still usable


def test_migration_uses_the_fallback_when_appdata_is_unusable(env):
    _, store, cfg_path = env
    _unusable(store.primary)
    _legacy_config(cfg_path)
    assert app_module.migrate_login_out_of_config() is True
    assert store.fallback.is_file()
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["qbench_password"] == ""


def test_migration_does_nothing_when_the_store_already_has_a_login(env):
    _, store, cfg_path = env
    store.save("stored@example.com", "stored-pass")
    _legacy_config(cfg_path)
    before = cfg_path.read_text(encoding="utf-8")
    assert app_module.migrate_login_out_of_config() is False
    assert cfg_path.read_text(encoding="utf-8") == before
    assert store.load() == ("stored@example.com", "stored-pass")


def test_migration_does_nothing_without_config_creds(env):
    _, store, cfg_path = env
    assert app_module.migrate_login_out_of_config() is False
    assert store.load() is None
    assert not cfg_path.exists()


def test_migration_runs_after_the_port_guard_not_at_import():
    main = APP_SRC[APP_SRC.index('if __name__ == "__main__":'):]
    assert "_start_login_store()" in main
    assert main.index("_wait_for_port(") < main.index("_start_login_store()")
    top = APP_SRC[:APP_SRC.index('if __name__ == "__main__":')]
    assert not re.search(r"^_start_login_store\(\)|^migrate_login_out_of_config\(\)",
                         top, re.M)


# ── /api/login ───────────────────────────────────────────────────────────

def test_login_with_save_goes_to_the_store_not_the_config(env):
    client, store, cfg_path = env
    body = client.post("/api/login", json={"username": USER, "password": SECRET,
                                           "save": True}).get_json()
    assert body == {"ok": True, "saved": True}
    assert store.load() == (USER, SECRET)
    assert not cfg_path.exists() or \
        SECRET not in cfg_path.read_text(encoding="utf-8")
    assert app_module.state.config.get("qbench_password", "") == ""


def test_login_with_save_clears_a_legacy_config_password(env):
    client, store, cfg_path = env
    store.save("stored@example.com", "stored-pass")
    _legacy_config(cfg_path, USER, "legacy-pass")
    client.post("/api/login", json={"username": USER, "password": SECRET, "save": True})
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["qbench_password"] == ""


def test_login_without_save_keeps_an_existing_saved_login(env):
    client, store, _ = env
    store.save("stored@example.com", "stored-pass")
    body = client.post("/api/login", json={"username": USER, "password": SECRET,
                                           "save": False}).get_json()
    assert body == {"ok": True, "saved": False}
    assert store.load() == ("stored@example.com", "stored-pass")


def test_a_failed_login_is_not_saved(env):
    client, store, _ = env
    FakeSession.fail_with = "bad password"
    resp = client.post("/api/login", json={"username": USER, "password": SECRET,
                                           "save": True})
    assert resp.status_code == 401
    assert store.load() is None


def test_login_works_when_nothing_can_be_saved(env):
    client, store, _ = env
    _unusable(store.primary)
    _unusable(store.fallback)
    body = client.post("/api/login", json={"username": USER, "password": SECRET,
                                           "save": True}).get_json()
    assert body == {"ok": True, "saved": False}
    assert app_module.state.logged_in is True


# ── /api/qbench-login/forget ─────────────────────────────────────────────

def test_forget_clears_both_locations_and_the_config(env):
    client, store, cfg_path = env
    store.save(USER, SECRET)
    LoginStore(store.fallback, None).save(USER, "older-pass")
    _legacy_config(cfg_path, USER, "legacy-pass")
    body = client.post("/api/qbench-login/forget").get_json()
    assert body == {"ok": True}
    assert not store.primary.exists() and not store.fallback.exists()
    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert on_disk["qbench_username"] == "" and on_disk["qbench_password"] == ""
    assert app_module.get_qbench_login() is None


def test_forget_requires_a_portal_session(env):
    anon = app_module.app.test_client()
    assert anon.post("/api/qbench-login/forget").status_code == 401


# ── /api/config ──────────────────────────────────────────────────────────

def test_config_reports_a_stored_login_without_exposing_where(env):
    client, store, _ = env
    store.save(USER, SECRET)
    resp = client.get("/api/config")
    body = resp.get_json()
    assert body["username"] == USER
    assert body["has_password"] is True
    text = resp.get_data(as_text=True)
    assert SECRET not in text
    assert str(store.primary.parent) not in text and "login_store" not in text


def test_config_without_any_login(env):
    client, _, _ = env
    body = client.get("/api/config").get_json()
    assert body["has_password"] is False


def test_config_shows_the_remembered_username_after_migration(env):
    client, _, cfg_path = env
    _legacy_config(cfg_path)
    app_module.migrate_login_out_of_config()
    body = client.get("/api/config").get_json()
    assert body["username"] == USER and body["has_password"] is True


# ── auto-login ───────────────────────────────────────────────────────────

def test_auto_login_uses_the_stored_login(env):
    _, store, _ = env
    store.save(USER, SECRET)
    app_module.auto_login_from_saved_creds()
    assert [s.username for s in FakeSession.instances] == [USER]
    assert app_module.state.logged_in is True


def test_auto_login_is_skipped_during_a_health_check(env, monkeypatch, caplog):
    _, store, _ = env
    store.save(USER, SECRET)
    monkeypatch.setenv("COA_HEALTH_CHECK", "1")
    with caplog.at_level(logging.INFO):
        app_module.auto_login_from_saved_creds()
    assert FakeSession.instances == []
    assert app_module.state.logged_in is False
    assert any("health check" in r.getMessage() and "not signing in to QBench"
               in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("", False), ("0", False), ("false", False), ("no", False),
])
def test_health_check_flag_parsing(monkeypatch, value, expected):
    monkeypatch.setenv("COA_HEALTH_CHECK", value)
    assert app_module._is_health_check() is expected


def test_main_decides_auto_login_from_get_qbench_login():
    main = APP_SRC[APP_SRC.index('if __name__ == "__main__":'):]
    assert "get_qbench_login()" in main
    assert 'cfg.get("qbench_password")' not in main
    assert "_is_health_check()" in main


def test_a_credential_problem_never_crashes_boot(env, monkeypatch):
    """Worst case is the manual login, never a dead process."""
    class Exploding:
        def load(self):
            raise RuntimeError("boom")

        def select(self):
            raise RuntimeError("boom")

        def save(self, *a):
            raise RuntimeError("boom")

    monkeypatch.setattr(app_module, "login_store", Exploding())
    app_module._start_login_store()                    # does not raise
    assert app_module.get_qbench_login() is None
    app_module.auto_login_from_saved_creds()            # does not raise


# ── manual and automatic logins must not undo each other ───────────────

import threading  # noqa: E402


class BlockingSession(FakeSession):
    """The saved login's session: login() waits until the test releases it,
    so a manual login can finish first. Other usernames sign in at once."""

    entered = None
    release = None
    auto_user = "stale@example.com"
    auto_fails = True

    def login(self, headless=True):
        if self.username != BlockingSession.auto_user:
            return
        BlockingSession.entered.set()
        assert BlockingSession.release.wait(5), "test never released the auto-login"
        if BlockingSession.auto_fails:
            raise RuntimeError("Invalid password")


def _race(env, monkeypatch, *, auto_fails: bool):
    client, store, _ = env
    store.save(BlockingSession.auto_user, "old-pass")
    BlockingSession.entered = threading.Event()
    BlockingSession.release = threading.Event()
    BlockingSession.auto_fails = auto_fails
    monkeypatch.setattr(app_module, "COASession", BlockingSession)
    events = []
    monkeypatch.setattr(app_module.state, "broadcast_sse", events.append)

    auto = threading.Thread(target=app_module.auto_login_from_saved_creds)
    auto.start()
    assert BlockingSession.entered.wait(5)
    body = client.post("/api/login", json={"username": USER, "password": SECRET,
                                           "save": False}).get_json()
    assert body["ok"] is True
    manual = app_module.state.coa_session
    assert manual is not None and manual.username == USER

    BlockingSession.release.set()
    auto.join(5)
    assert not auto.is_alive()
    return manual, [e for e in events if e.get("type") == "auto_login_done"]


def test_a_late_auto_login_failure_leaves_the_manual_login_alone(env, monkeypatch):
    manual, done = _race(env, monkeypatch, auto_fails=True)
    assert app_module.state.coa_session is manual
    assert app_module.state.logged_in is True
    assert done == [], "a superseded auto-login must not tell browsers it failed"


def test_a_late_auto_login_success_does_not_replace_the_manual_session(env, monkeypatch):
    manual, done = _race(env, monkeypatch, auto_fails=False)
    assert app_module.state.coa_session is manual
    assert app_module.state.logged_in is True
    assert done == []


def test_a_superseded_auto_login_says_so_in_the_log(env, monkeypatch, caplog):
    with caplog.at_level(logging.INFO):
        _race(env, monkeypatch, auto_fails=True)
    assert any("auto-login superseded by a manual login" in r.getMessage()
               for r in caplog.records)


def test_an_auto_login_failure_on_its_own_is_reported(env, monkeypatch):
    _, store, _ = env
    store.save(USER, SECRET)
    FakeSession.fail_with = "Invalid password"
    events = []
    monkeypatch.setattr(app_module.state, "broadcast_sse", events.append)
    app_module.auto_login_from_saved_creds()
    done = [e for e in events if e.get("type") == "auto_login_done"]
    assert done and done[-1]["ok"] is False
    assert app_module.state.coa_session is None
    assert app_module.state.logged_in is False
