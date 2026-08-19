"""TDD spec: when QBench's /report/preview returns a non-JSON body,
COASession.generate_preview must log the HTTP status code AND a snippet
of the response body — otherwise we cannot tell from the logs whether
QBench returned an HTML login page, an empty 200, a 5xx error page, etc.

The production logs currently show "Expecting value: line 1 column 1
(char 0)" for every sample — that's the symptom (resp.json() on an empty
or non-JSON body). Without the status code + body snippet, the *cause*
is invisible.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import app


class _NonJsonResponse:
    """Stand-in HTTP response whose body is not valid JSON."""

    def __init__(self, status_code: int = 200, text: str = "") -> None:
        self.status_code = status_code
        self.text = text

    def raise_for_status(self) -> None:
        # Mirror requests: only 4xx/5xx raise. 2xx is silent.
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return json.loads(self.text)  # raises JSONDecodeError on empty/HTML


def _make_logged_in_session(post_response) -> app.COASession:
    s = app.COASession(username="u", password="p")
    # Bypass the "not logged in" early-return guard
    s.csrf_token = "fake-csrf"
    s.playwright_cookies = [{"name": "x", "value": "y", "domain": ""}]
    s._session = MagicMock()
    s._session.post.return_value = post_response
    return s


def test_error_log_includes_status_code_when_body_is_empty(caplog) -> None:
    session = _make_logged_in_session(_NonJsonResponse(status_code=200, text=""))
    caplog.set_level(logging.ERROR, logger="app")

    result = session.generate_preview(sample_id=12345, test_ids=[1])

    assert result is None
    combined = " ".join(r.getMessage() for r in caplog.records)
    assert "12345" in combined, "log must mention the sample_id"
    assert "status=200" in combined or "status: 200" in combined or " 200" in combined, (
        "log must include the HTTP status code so we can tell 200-with-empty-body "
        "apart from a server error: got " + repr(combined)
    )


def test_error_log_includes_body_snippet_when_body_is_html(caplog) -> None:
    html = "<html><body>Please log in</body></html>"
    session = _make_logged_in_session(_NonJsonResponse(status_code=200, text=html))
    caplog.set_level(logging.ERROR, logger="app")

    result = session.generate_preview(sample_id=98765, test_ids=[42])

    assert result is None
    combined = " ".join(r.getMessage() for r in caplog.records)
    assert "Please log in" in combined or "<html>" in combined, (
        "log must include a snippet of the actual response body so we can "
        "see what QBench sent instead of JSON: got " + repr(combined)
    )


def test_error_log_truncates_long_bodies(caplog) -> None:
    """A 100 KB error page must not dump 100 KB into the log."""
    huge = "X" * 100_000
    session = _make_logged_in_session(_NonJsonResponse(status_code=200, text=huge))
    caplog.set_level(logging.ERROR, logger="app")

    session.generate_preview(sample_id=1, test_ids=[1])

    combined = " ".join(r.getMessage() for r in caplog.records)
    # The diagnostic snippet must be capped — pick a reasonable upper bound.
    assert len(combined) < 2_000, (
        "error log must truncate response body to a snippet, not dump the "
        f"full response (got {len(combined)} chars)"
    )
