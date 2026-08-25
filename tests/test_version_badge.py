"""The running version is visible in the UI.

A self-deploying app makes "which build am I looking at?" a question
reviewers and whoever is debugging with them actually have to answer. The
updater already stamps it: CI writes the tag into a VERSION file, `app.py`
reads it into APP_VERSION, and /healthz reports it. Nothing showed it to a
person.

`/healthz` is deliberately not the source for this — it takes no auth and
makes no outbound call, and the frontend already fetches /api/config at boot,
so the stamp rides along on a call that happens anyway.

A working checkout has no VERSION file and reports "dev", which is exactly
what a dev box should display.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
INDEX = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")


@pytest.fixture
def client(monkeypatch):
    import app as app_module
    from app import UserState
    monkeypatch.setattr(app_module.state, "labcore", MagicMock())
    uid = "test-uid-version"
    with app_module._sessions_lock:
        app_module.user_sessions[uid] = UserState(uid, "RC")
    app_module.app.config["TESTING"] = True
    c = app_module.app.test_client()
    with c.session_transaction() as sess:
        sess["uid"] = uid
    yield c
    with app_module._sessions_lock:
        app_module.user_sessions.pop(uid, None)


def test_config_reports_the_running_version(client) -> None:
    import app as app_module
    body = client.get("/api/config").get_json()
    assert "version" in body, "/api/config does not carry the version stamp"
    assert body["version"] == app_module.APP_VERSION


def test_a_working_checkout_reports_dev(tmp_path) -> None:
    """No VERSION file must read as 'dev', never crash or show blank."""
    import app as app_module
    assert app_module._read_version(tmp_path / "nope") == "dev"


def test_the_page_has_a_version_element() -> None:
    assert re.search(r'id="app-version"', INDEX), (
        "templates/index.html has no #app-version element to render into"
    )


def test_the_frontend_fills_it_from_the_config_payload() -> None:
    """Assert the whole chain: config payload -> setter -> the element."""
    assert re.search(r"setAppVersion\(\s*cfg\.version\s*\)", APP_JS), (
        "the version from /api/config is never handed to the setter"
    )
    m = re.search(r"function setAppVersion\(.*?\)\s*\{(.*?)\n\}", APP_JS, re.S)
    assert m, "setAppVersion() is not defined"
    body = m.group(1)
    assert "app-version" in body, "the setter does not target #app-version"
    assert "textContent" in body, "the setter never writes any text"


def test_it_is_pinned_to_the_bottom_right() -> None:
    m = re.search(r"#app-version\s*\{([^}]*)\}", CSS)
    assert m, "no #app-version rule in app.css"
    rule = m.group(1)
    assert "position: fixed" in rule or "position:fixed" in rule
    assert "bottom" in rule and "right" in rule, (
        f"#app-version is not anchored bottom-right: {rule.strip()!r}"
    )


def test_it_does_not_swallow_clicks() -> None:
    """A fixed overlay sitting above the UI must not eat interactions."""
    m = re.search(r"#app-version\s*\{([^}]*)\}", CSS)
    assert m and "pointer-events: none" in m.group(1), (
        "the badge would intercept clicks meant for the app underneath"
    )

def test_the_badge_is_filled_before_any_api_call(client, monkeypatch) -> None:
    """It must read on the login and boot screens.

    /api/config is only fetched inside initQBenchApp(), i.e. after portal
    login AND the review-mode picker. Relying on it alone left the badge
    blank on exactly the screens where someone is asking which build this
    is — verified in a real browser, not inferred. So the server renders the
    stamp into the page.
    """
    import app as app_module
    # A distinctive stamp on purpose: the real value on a working checkout is
    # "dev", which occurs incidentally in the page and makes the assertion
    # pass without proving anything.
    stamp = "9.9.9-badge-test"
    monkeypatch.setattr(app_module, "APP_VERSION", stamp)
    html = client.get("/").get_data(as_text=True)
    assert stamp in html, (
        "the served HTML does not carry the version, so the badge is empty "
        "until the reviewer has logged in and chosen a mode"
    )
