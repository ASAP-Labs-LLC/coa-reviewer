"""Frontend guards for keycard login.

LabLink's readers are keyboard wedges: they type the code then press Enter
into whatever has focus. So the scan view needs a focused capture field that
consumes Enter itself — the same shape LabStation's login_dialog uses.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")


def test_a_card_scan_view_exists() -> None:
    assert 'id="portal-card-view"' in INDEX_HTML
    assert 'id="portal-card-input"' in INDEX_HTML


def test_the_reviewer_can_fall_back_to_password_login() -> None:
    """A lost or unregistered card must not lock anyone out."""
    assert 'id="portal-use-password"' in INDEX_HTML
    assert 'id="portal-use-card"' in INDEX_HTML
    assert 'id="portal-password-view"' in INDEX_HTML


def test_the_scan_field_is_wired_to_the_card_endpoint() -> None:
    assert "/api/portal-card-login" in APP_JS


def test_the_scan_field_captures_enter() -> None:
    """A wedge ends its scan with Enter; without handling it the keystroke
    escapes and the code is never submitted."""
    body = APP_JS[APP_JS.index("function setupCardLogin"):]
    body = body[:body.index("\nfunction ")]
    assert "Enter" in body
    assert "portal-card-input" in body


def test_the_card_code_is_cleared_after_each_scan() -> None:
    """The field holds a credential; leaving it populated leaves it on screen
    and lets a second Enter replay the login."""
    body = APP_JS[APP_JS.index("function setupCardLogin"):]
    body = body[:body.index("\nfunction ")]
    assert ".value = \"\"" in body or ".value=''" in body


def test_a_card_login_skips_the_typed_initials_step() -> None:
    """The whole point: identity comes from the LabLink account, so nothing
    should ask the reviewer to type a name."""
    body = APP_JS[APP_JS.index("async function submitCardLogin"):]
    body = body[:body.index("\nfunction ") if "\nfunction " in body else len(body)]
    assert "portal-name" not in body
