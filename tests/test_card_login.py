"""LabLink keycard login.

NFC readers across LabLink are keyboard wedges: they type the card's code and
press Enter. LabCore's POST /api/login treats a registered card code in
*either* field as a login and returns the canonical account name, so COA
Reviewer can authenticate a card without knowing anything about the reader.

The point of doing it this way: the reviewer's identity becomes a real
LabLink account instead of hand-typed initials, which is what makes the audit
log and Command Center's created_by/completed_by attributable.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")


@pytest.fixture
def card(monkeypatch, tmp_path):
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
        rows += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    return rows


# ── the client call ──────────────────────────────────────────────────────

def test_client_authenticates_a_card_against_labcore() -> None:
    """The code goes in as the password; LabCore accepts a card code in
    either field, and password is the field a wedge normally lands in."""
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
        user = client.authenticate_card("04A2B3C4D5")
    finally:
        requests.post = orig

    assert user == "Ryan C"
    assert calls["url"].endswith("/api/login")
    assert "04A2B3C4D5" in (calls["json"]["password"], calls["json"]["username"])


def test_client_returns_none_for_an_unregistered_card() -> None:
    from labcore_client import LabCoreClient
    import requests

    class _Resp:
        status_code = 401
        def json(self): return {"error": "Invalid credentials."}

    client = LabCoreClient(base_url="https://labvision.asaplabs.net")
    orig, requests.post = requests.post, lambda *a, **k: _Resp()
    try:
        assert client.authenticate_card("nope") is None
    finally:
        requests.post = orig


def test_client_raises_when_labcore_is_unreachable() -> None:
    """A card that "just doesn't work" with no explanation is the worst
    outcome — the caller needs to distinguish this from a bad card."""
    from labcore_client import LabCoreClient, LabCoreUnavailable
    from tests.conftest import free_port

    client = LabCoreClient(base_url=f"http://127.0.0.1:{free_port()}")
    with pytest.raises(LabCoreUnavailable):
        client.authenticate_card("04A2B3C4D5")


# ── the route ────────────────────────────────────────────────────────────

def test_a_valid_card_starts_a_session_under_the_labcore_username(card) -> None:
    """No portal password, no typed initials — the card is the whole login."""
    client, mock, _ = card
    mock.authenticate_card.return_value = "Ryan C"

    resp = client.post("/api/portal-card-login", json={"code": "04A2B3C4D5"})

    assert resp.status_code == 200
    assert resp.get_json()["name"] == "Ryan C"
    assert client.get("/api/portal-session").get_json()["logged_in"] is True


def test_the_card_identity_is_what_gets_attributed(card) -> None:
    """This is the reason for card login: listings and log entries name a
    real account rather than whatever letters someone typed."""
    import app as app_module
    client, mock, _ = card
    mock.authenticate_card.return_value = "Ryan C"
    client.post("/api/portal-card-login", json={"code": "04A2B3C4D5"})

    with client.session_transaction() as sess:
        uid = sess["uid"]
    with app_module._sessions_lock:
        assert app_module.user_sessions[uid].name == "Ryan C"


def test_an_unregistered_card_is_rejected(card) -> None:
    client, mock, _ = card
    mock.authenticate_card.return_value = None

    resp = client.post("/api/portal-card-login", json={"code": "ffff"})
    assert resp.status_code == 401
    assert client.get("/api/portal-session").get_json()["logged_in"] is False


def test_an_empty_scan_is_rejected_without_calling_labcore(card) -> None:
    """Readers emit stray Enters; an empty scan is not a login attempt."""
    client, mock, _ = card
    resp = client.post("/api/portal-card-login", json={"code": "   "})

    assert resp.status_code == 400
    assert mock.authenticate_card.call_count == 0


def test_an_unreachable_labcore_says_so_rather_than_blaming_the_card(card) -> None:
    from labcore_client import LabCoreUnavailable
    client, mock, _ = card
    mock.authenticate_card.side_effect = LabCoreUnavailable("down")

    resp = client.post("/api/portal-card-login", json={"code": "04A2B3C4D5"})
    assert resp.status_code == 503
    assert resp.get_json()["labcore_down"] is True


def test_a_card_login_is_recorded_in_the_session_log(card) -> None:
    client, mock, d = card
    mock.authenticate_card.return_value = "Ryan C"
    client.post("/api/portal-card-login", json={"code": "04A2B3C4D5"})

    row = _sessions(d)[-1]
    assert row["event"] == "login"
    assert row["user"] == "Ryan C"
    assert row["method"] == "card"


def test_the_card_code_is_never_written_to_the_log(card) -> None:
    """A card code is a credential. Logging it would let anyone with read
    access to the share clone a login."""
    client, mock, d = card
    mock.authenticate_card.return_value = "Ryan C"
    client.post("/api/portal-card-login", json={"code": "04A2B3C4D5"})

    assert "04A2B3C4D5" not in json.dumps(_sessions(d))


def test_password_login_still_works(card) -> None:
    """Kept as the fallback for a lost or unregistered card — and it resolves
    to the same LabLink account the card would. See tests/test_user_login.py."""
    client, mock, _ = card
    mock.authenticate_user.return_value = "Ryan C"

    resp = client.post("/api/portal-login",
                       json={"username": "ryan c", "password": "pw"})

    assert resp.status_code == 200
    assert resp.get_json()["name"] == "Ryan C"
