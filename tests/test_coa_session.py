"""Tests for COASession stale-session recovery and poll cadence.

Root cause (diagnosed 2026-06-24): when the Playwright-scraped QBench web
session expires, QBench answers POST /report/preview with HTTP 200 carrying
the HTML login page. ``generate_preview`` called ``resp.json()`` (which
throws), swallowed the error, and returned ``None`` — so the caller's
auto-relogin branch never fired and every preview failed until a manual
restart. These tests lock in: (a) detecting the login page, (b) raising
``SessionExpiredError`` instead of returning None, (c) throttled relogin so
concurrent threads don't storm, and (d) a faster poll cadence.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("flask")

import app  # noqa: E402


LOGIN_HTML = (
    '<!DOCTYPE html>\n<html><head><title>QBench</title></head>'
    '<body><input id="qbenchLimsLoginEmail"></body></html>'
)


# ── _is_login_html ─────────────────────────────────────────────────────────


def test_is_login_html_detects_login_page_on_200() -> None:
    assert app._is_login_html(200, LOGIN_HTML) is True


def test_is_login_html_false_for_json_payload() -> None:
    assert app._is_login_html(200, '{"id": 42, "render_status": "PENDING"}') is False


def test_is_login_html_false_for_empty_body() -> None:
    assert app._is_login_html(200, "") is False


# ── generate_preview raises on stale session ───────────────────────────────


class _FakeResp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        # QBench returns the login HTML, so JSON decode fails exactly as
        # it does in production.
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp

    def post(self, *args, **kwargs):
        return self._resp


def test_generate_preview_raises_session_expired_on_login_html(monkeypatch) -> None:
    cs = app.COASession("user", "pw")
    cs.csrf_token = "tok"  # pass the "logged in" guard
    cs._session = _FakeSession(_FakeResp(200, LOGIN_HTML))

    with pytest.raises(app.SessionExpiredError):
        cs.generate_preview(sample_id=1, test_ids=[2], order_id=3)


# ── relogin throttle ───────────────────────────────────────────────────────


def test_relogin_is_throttled_against_concurrent_storms(monkeypatch) -> None:
    cs = app.COASession("user", "pw")
    calls = {"n": 0}

    def fake_login(headless=True):
        calls["n"] += 1

    monkeypatch.setattr(cs, "login", fake_login)

    cs.relogin()
    cs.relogin()  # immediately again — should be throttled (a peer just refreshed)
    assert calls["n"] == 1

    # Once the throttle window has passed, a relogin proceeds again.
    cs._last_relogin = time.monotonic() - (app.COASession.RELOGIN_THROTTLE_SECONDS + 1)
    cs.relogin()
    assert calls["n"] == 2


# ── poll cadence ───────────────────────────────────────────────────────────


def test_poll_delays_start_fast_and_cap(monkeypatch) -> None:
    delays = list(app._preview_poll_delays())
    assert delays[0] <= 1.0          # first check well under the old 3s floor
    assert max(delays) <= 3.0        # never slower than the old fixed cadence
    assert delays == sorted(delays)  # monotonically non-decreasing
    assert sum(delays) >= 110        # still covers the ~120s render timeout
