"""Tests for surfacing QBench errors to the client as actionable JSON.

Root cause (diagnosed 2026-06-24): /api/search had no try/except, so a QBench
429 became a raw Flask 500 with an HTML body; the frontend then failed to
JSON-parse it and showed an opaque "Search failed". A global QBenchAPIError
handler converts these into a clean, retryable JSON 503.
"""

from __future__ import annotations

import pytest

pytest.importorskip("flask")

import app  # noqa: E402
from qbench_client import QBenchAPIError  # noqa: E402


def test_qbench_error_handler_is_registered() -> None:
    handlers = app.app.error_handler_spec[None][None]
    assert QBenchAPIError in handlers


def test_qbench_error_response_is_json_503() -> None:
    with app.app.app_context():
        resp, status = app._qbench_error_response(
            QBenchAPIError("429 error at /samples: ratelimit exceeded 300 per 60 seconds")
        )
        assert status == 503
        body = resp.get_json()
        assert "error" in body
        # A human, retryable message — not an HTML 500 page.
        assert "retry" in body["error"].lower()
