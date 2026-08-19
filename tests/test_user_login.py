"""LabLink username/password login.

The password path is the alternative to tapping a card — not a different
identity system. It authenticates the reviewer's *LabLink account* against
LabCore's POST /api/login, exactly like the card does, and the canonical
account name LabCore returns becomes the session identity.

What this replaced: a hardcoded shared portal password plus a free-text
"Your Name" box. Anyone who knew the password could type any name, so
Command Center's created_by/completed_by and the audit log recorded a
self-declared string rather than a real account.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")


@pytest.fixture
def portal(monkeypatch, tmp_path):
    """(client, mock_labcore, changelog_dir) with no session yet."""
    import app as app_module
    from change_log import ChangeLog

    mock = MagicMock()
    monkeypatch.setattr(app_module.state, "labcore", mock)
    d = tmp_path / "cl"
    monkeypatch.setattr(app_module.state, "change_log", ChangeLog(d))

    app_module.app.config["TESTING"] = True
    return app_module.app.test_client(), mock, d


def _sessions(directory):
    rows = []
    for f in sorted(directory.glob("sessions-*.jsonl")):
        rows += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    return rows


def _login(client, mock, name="Ryan C"):
    """Sign in through the password path and return the session uid."""
    mock.authenticate_user.return_value = name
    resp = client.post("/api/portal-login",
                       json={"username": name, "password": "pw"})
    assert resp.status_code == 200, resp.get_json()
    with client.session_transaction() as sess:
        return sess["uid"]


# ── the client call ──────────────────────────────────────────────────────

def test_client_authenticates_a_user_against_labcore() -> None:
    """Same endpoint the card uses, with real credentials in both fields."""
    from labcore_client import LabCoreClient
    import requests

    calls = {}

    class _Resp:
        status_code = 200
        def json(self): return {"token": "t0k", "username": "Ryan C"}

    def fake_post(url, json=None, timeout=None):
        calls["url"] = url
        calls["json"] = json
        return _Resp()

    client = LabCoreClient(base_url="https://labvision.asaplabs.net")
    orig, requests.post = requests.post, fake_post
    try:
        user = client.authenticate_user("ryan c", "s3cret")
    finally:
        requests.post = orig

    assert user == "Ryan C"
    assert calls["url"].endswith("/api/login")
    assert calls["json"] == {"username": "ryan c", "password": "s3cret"}


def test_client_returns_labcores_canonical_casing() -> None:
    """LabCore resolves the stored casing; the session should show that, not
    whatever the reviewer typed, so attribution is stable across logins."""
    from labcore_client import LabCoreClient
    import requests

    class _Resp:
        status_code = 200
        def json(self): return {"token": "t", "username": "Ryan C"}

    client = LabCoreClient(base_url="https://labvision.asaplabs.net")
    orig, requests.post = requests.post, lambda *a, **k: _Resp()
    try:
        assert client.authenticate_user("RYAN C", "pw") == "Ryan C"
    finally:
        requests.post = orig


def test_client_returns_none_for_bad_credentials() -> None:
    from labcore_client import LabCoreClient
    import requests

    class _Resp:
        status_code = 401
        def json(self): return {"error": "Invalid credentials."}

    client = LabCoreClient(base_url="https://labvision.asaplabs.net")
    orig, requests.post = requests.post, lambda *a, **k: _Resp()
    try:
        assert client.authenticate_user("ryan", "wrong") is None
    finally:
        requests.post = orig


def test_client_does_not_call_labcore_with_a_blank_field() -> None:
    """LabCore 400s on an empty field; there is nothing to ask about."""
    from labcore_client import LabCoreClient
    import requests

    called = []
    orig, requests.post = requests.post, lambda *a, **k: called.append(1)
    try:
        client = LabCoreClient(base_url="https://labvision.asaplabs.net")
        assert client.authenticate_user("", "pw") is None
        assert client.authenticate_user("ryan", "") is None
    finally:
        requests.post = orig
    assert called == []


def test_client_raises_when_labcore_is_unreachable() -> None:
    """"Wrong password" and "couldn't ask" must not look the same."""
    from labcore_client import LabCoreClient, LabCoreUnavailable
    from tests.conftest import free_port

    client = LabCoreClient(base_url=f"http://127.0.0.1:{free_port()}")
    with pytest.raises(LabCoreUnavailable):
        client.authenticate_user("ryan", "pw")


# ── the login route ──────────────────────────────────────────────────────

def test_valid_credentials_start_a_session_under_the_labcore_username(portal) -> None:
    import app as app_module
    client, mock, _ = portal
    mock.authenticate_user.return_value = "Ryan C"

    resp = client.post("/api/portal-login",
                       json={"username": "ryan c", "password": "pw"})

    assert resp.status_code == 200
    assert resp.get_json()["name"] == "Ryan C"
    with client.session_transaction() as sess:
        uid = sess["uid"]
    with app_module._sessions_lock:
        assert app_module.user_sessions[uid].name == "Ryan C"


def test_the_typed_name_field_can_no_longer_set_the_identity(portal) -> None:
    """The whole point of the change: identity comes from LabCore, so a
    leftover client sending name= cannot forge who did the review."""
    client, mock, _ = portal
    mock.authenticate_user.return_value = "Ryan C"

    resp = client.post("/api/portal-login", json={
        "username": "ryan c", "password": "pw", "name": "Somebody Else"})

    assert resp.status_code == 200
    assert resp.get_json()["name"] == "Ryan C"


def test_bad_credentials_are_rejected(portal) -> None:
    client, mock, _ = portal
    mock.authenticate_user.return_value = None

    resp = client.post("/api/portal-login",
                       json={"username": "ryan", "password": "wrong"})

    assert resp.status_code == 401
    assert client.get("/api/portal-session").get_json()["logged_in"] is False


def test_a_missing_field_is_rejected_without_calling_labcore(portal) -> None:
    client, mock, _ = portal

    resp = client.post("/api/portal-login", json={"username": "ryan"})

    assert resp.status_code == 400
    assert mock.authenticate_user.call_count == 0


def test_an_unreachable_labcore_says_so_rather_than_blaming_the_password(portal) -> None:
    from labcore_client import LabCoreUnavailable
    client, mock, _ = portal
    mock.authenticate_user.side_effect = LabCoreUnavailable("down")

    resp = client.post("/api/portal-login",
                       json={"username": "ryan", "password": "pw"})

    assert resp.status_code == 503
    assert resp.get_json()["labcore_down"] is True


def test_a_password_login_is_recorded_under_the_labcore_account(portal) -> None:
    client, mock, d = portal
    mock.authenticate_user.return_value = "Ryan C"
    client.post("/api/portal-login", json={"username": "ryan c", "password": "pw"})

    row = _sessions(d)[-1]
    assert row["event"] == "login"
    assert row["user"] == "Ryan C"
    assert row["method"] == "password"


def test_the_password_is_never_written_to_the_log(portal) -> None:
    client, mock, d = portal
    mock.authenticate_user.return_value = "Ryan C"
    client.post("/api/portal-login",
                json={"username": "ryan c", "password": "hunter2xyz"})

    assert "hunter2xyz" not in json.dumps(_sessions(d))


def test_there_is_no_hardcoded_portal_password_left(portal) -> None:
    """A shared password that still works is exactly the unattributable
    login this replaced."""
    import app as app_module

    assert not hasattr(app_module, "APP_LOGIN_USERNAME")
    assert not hasattr(app_module, "APP_LOGIN_PASSWORD")

    from pathlib import Path
    src = (Path(app_module.__file__)).read_text()
    assert "A$aprocks!1" not in src


# ── the timeout re-auth route ────────────────────────────────────────────

def test_reauth_by_password_restores_the_same_users_session(portal) -> None:
    import app as app_module
    client, mock, _ = portal
    uid = _login(client, mock)

    mock.authenticate_user.return_value = "Ryan C"
    resp = client.post("/api/portal-reauth",
                       json={"username": "Ryan C", "password": "pw"})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["restored"] is True and body["name"] == "Ryan C"
    with client.session_transaction() as sess:
        assert sess["uid"] == uid          # same session, work preserved


def test_reauth_by_card_restores_the_same_users_session(portal) -> None:
    """Tapping back in is the normal move at a terminal."""
    client, mock, _ = portal
    _login(client, mock)

    mock.authenticate_card.return_value = "Ryan C"
    resp = client.post("/api/portal-reauth", json={"code": "04A2B3C4D5"})

    assert resp.status_code == 200
    assert resp.get_json()["restored"] is True


def test_reauth_matches_the_account_case_insensitively(portal) -> None:
    client, mock, _ = portal
    _login(client, mock, "Ryan C")

    mock.authenticate_user.return_value = "ryan c"
    resp = client.post("/api/portal-reauth",
                       json={"username": "ryan c", "password": "pw"})

    assert resp.status_code == 200


def test_a_different_account_cannot_take_over_the_session(portal) -> None:
    """Otherwise a colleague unlocks the screen and inherits the review —
    every listing filed afterwards would be attributed to the wrong person."""
    import app as app_module
    client, mock, _ = portal
    uid = _login(client, mock, "Ryan C")

    mock.authenticate_user.return_value = "Dana P"
    resp = client.post("/api/portal-reauth",
                       json={"username": "dana p", "password": "pw"})

    assert resp.status_code == 403
    with app_module._sessions_lock:
        assert app_module.user_sessions[uid].name == "Ryan C"


def test_reauth_with_bad_credentials_is_rejected(portal) -> None:
    client, mock, _ = portal
    _login(client, mock)

    mock.authenticate_user.return_value = None
    resp = client.post("/api/portal-reauth",
                       json={"username": "Ryan C", "password": "wrong"})

    assert resp.status_code == 401


def test_reauth_after_the_session_was_collected_starts_a_fresh_one(portal) -> None:
    """Nothing survives to match against, so whoever authenticates gets a new
    session under their own LabLink account — never a client-supplied name."""
    import app as app_module
    client, mock, _ = portal
    uid = _login(client, mock, "Ryan C")
    with app_module._sessions_lock:
        app_module.user_sessions.pop(uid)

    mock.authenticate_user.return_value = "Dana P"
    resp = client.post("/api/portal-reauth", json={
        "username": "dana p", "password": "pw", "name": "Somebody Else"})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["restored"] is False and body["name"] == "Dana P"
    with client.session_transaction() as sess:
        new_uid = sess["uid"]
    with app_module._sessions_lock:
        assert app_module.user_sessions[new_uid].name == "Dana P"


def test_reauth_reports_an_unreachable_labcore(portal) -> None:
    from labcore_client import LabCoreUnavailable
    client, mock, _ = portal
    _login(client, mock)

    mock.authenticate_user.side_effect = LabCoreUnavailable("down")
    resp = client.post("/api/portal-reauth",
                       json={"username": "Ryan C", "password": "pw"})

    assert resp.status_code == 503
    assert resp.get_json()["labcore_down"] is True


# ── the login screen ─────────────────────────────────────────────────────

def test_the_login_form_no_longer_asks_for_a_typed_name() -> None:
    from pathlib import Path
    import app as app_module

    html = (Path(app_module.__file__).parent / "templates" / "index.html").read_text()
    assert 'id="portal-name"' not in html
    assert 'id="reauth-name"' not in html


def test_the_frontend_sends_no_name_on_login() -> None:
    from pathlib import Path
    import app as app_module

    js = (Path(app_module.__file__).parent / "static" / "js" / "app.js").read_text()
    assert "#portal-name" not in js
    assert "#reauth-name" not in js
