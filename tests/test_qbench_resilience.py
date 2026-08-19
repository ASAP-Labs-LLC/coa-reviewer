"""Tests for QBench client rate-limit resilience.

Root cause (diagnosed 2026-06-24): the local GLOBAL_RATE_LIMITER default was
340 calls/min, ABOVE QBench's real server ceiling of 300 per 60 seconds, so
the limiter structurally could not prevent 429s — confirmed by 1,949
rate-limit log events. These tests lock in (a) a default cap below QBench's
ceiling and (b) a 429 backoff that honors the server's retry hint.
"""

from __future__ import annotations

import pytest

import qbench_client as qc


# ── default rate-limit ceiling ─────────────────────────────────────────────


def test_default_rate_limit_is_below_qbench_server_ceiling() -> None:
    # QBench enforces 300 requests / 60 seconds. The client cap MUST sit
    # below that (with margin) or the limiter can't prevent 429s.
    assert qc.MAX_CALLS_PER_MINUTE < 300
    assert qc.GLOBAL_RATE_LIMITER.max_calls < 300


# ── _parse_retry_after ─────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, headers=None, text=""):
        self.headers = headers or {}
        self.text = text


def test_parse_retry_after_reads_standard_header() -> None:
    resp = _FakeResp(headers={"Retry-After": "8"})
    assert qc._parse_retry_after(resp) == 8.0


def test_parse_retry_after_reads_qbench_body_hint() -> None:
    body = (
        '{"error_description": "ratelimit exceeded 300 per 60 seconds '
        '[current count: 350]. Retry in 7 seconds"}'
    )
    resp = _FakeResp(text=body)
    assert qc._parse_retry_after(resp) == 7.0


def test_parse_retry_after_returns_none_without_hint() -> None:
    resp = _FakeResp(text='{"data": []}')
    assert qc._parse_retry_after(resp) is None


# ── 429 backoff in request() ───────────────────────────────────────────────


class _SeqResp:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code, *, headers=None, text="", payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _make_client(monkeypatch):
    client = qc.QBenchAPIClient()
    # No network: fixed token, no real rate-limit sleeping.
    monkeypatch.setattr(client, "get_access_token", lambda *, force=False: "tok")
    monkeypatch.setattr(client.rate_limiter, "acquire", lambda: None)
    return client


def test_request_retries_429_then_succeeds(monkeypatch) -> None:
    client = _make_client(monkeypatch)

    responses = [
        _SeqResp(429, text='ratelimit exceeded 300 per 60 seconds. Retry in 2 seconds'),
        _SeqResp(200, payload={"data": [1]}),
    ]
    calls = {"n": 0}

    def fake_request(method, url, **kwargs):
        r = responses[calls["n"]]
        calls["n"] += 1
        return r

    slept = []
    monkeypatch.setattr(client.session, "request", fake_request)
    monkeypatch.setattr(qc.time, "sleep", lambda s: slept.append(s))

    resp = client.request("GET", "/samples")

    assert resp.status_code == 200
    assert calls["n"] == 2
    # Honored the server's "Retry in 2 seconds" hint.
    assert slept == [2.0]


def test_request_429_backoff_is_capped(monkeypatch) -> None:
    client = _make_client(monkeypatch)
    # Always 429, no hint → exponential backoff, capped at 30s, more than 5 tries.
    monkeypatch.setattr(
        client.session, "request",
        lambda *a, **k: _SeqResp(429, text="ratelimit exceeded"),
    )
    slept = []
    monkeypatch.setattr(qc.time, "sleep", lambda s: slept.append(s))

    with pytest.raises(qc.QBenchAPIError):
        client.request("GET", "/samples")

    assert len(slept) >= 3                          # retries more than once
    assert max(slept) <= 30                          # per-wait capped
    # Total backoff is bounded so a hard-throttled request can't park a
    # preview-pool worker for minutes.
    assert sum(slept) <= qc.MAX_TOTAL_BACKOFF_SECONDS + 30
