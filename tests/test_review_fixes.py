"""Round-2 fixes from the adversarial code review (2026-06-24).

Locks in: the UploadQueue worker can never die (so writes never silently
stall), transient network errors are retried (not dropped), the SIF cache
doesn't poison an order on a transient fetch error, and comment SSE
confirmations reach only the originating reviewer.
"""

from __future__ import annotations

import queue as _queue
import threading

import pytest
import requests

pytest.importorskip("flask")

import app  # noqa: E402
from qbench_client import QBenchAPIError  # noqa: E402


# ── UploadQueue worker survival ────────────────────────────────────────────


class _Client:
    def __init__(self, comment_exc=None, test_exc=None):
        self.comment_calls = 0
        self.test_calls = 0
        self._comment_exc = comment_exc
        self._test_exc = test_exc

    def update_sample_comments(self, sample_id, comments):
        self.comment_calls += 1
        if self._comment_exc:
            raise self._comment_exc
        return {"ok": True}

    def update_test_result(self, test_id, value):
        self.test_calls += 1
        if self._test_exc:
            raise self._test_exc
        return {"ok": True}


def _q(client):
    return app.UploadQueue(client, start_worker=False)


def test_safe_process_never_raises_so_worker_cannot_die() -> None:
    q = _q(_Client())

    def boom(_payload):
        raise RuntimeError("unexpected")

    q._process = boom  # simulate an unguarded failure deep in a write
    # Must not propagate — the worker loop relies on this to stay alive.
    q._safe_process({"kind": "test", "test_id": 1, "value": "x", "attempts": 0})


def test_comment_network_error_is_retried_not_dropped() -> None:
    client = _Client(comment_exc=requests.exceptions.ConnectionError("reset"))
    q = _q(client)
    scheduled = []
    q._schedule_retry = lambda payload, delay: scheduled.append(payload)

    q._process({"kind": "comment", "sample_id": 1, "comments": "x",
                "lab_id": "010126-1", "attempts": 0})

    assert len(scheduled) == 1
    assert scheduled[0]["attempts"] == 1


def test_test_edit_network_error_is_retried_not_dropped() -> None:
    client = _Client(test_exc=requests.exceptions.Timeout("slow"))
    q = _q(client)
    scheduled = []
    q._schedule_retry = lambda payload, delay: scheduled.append(payload)

    q._process({"kind": "test", "test_id": 9, "value": "v", "attempts": 0})

    assert len(scheduled) == 1
    assert scheduled[0]["attempts"] == 1


# ── SIF cache must not poison an order on a transient fetch error ──────────


class _FlakyClient:
    def __init__(self):
        self.calls = 0

    def fetch_order_attachments(self, order_id):
        self.calls += 1
        raise requests.exceptions.ConnectionError("transient")


def test_sif_transient_fetch_error_is_not_cached_as_no_sif() -> None:
    client = _FlakyClient()
    cache = {}
    lock = threading.Lock()

    assert app._sif_load_order_pdf(5, client, cache, lock) is None
    # A second sample in the same order must RE-TRY, not inherit a poisoned
    # permanent "no SIF".
    assert app._sif_load_order_pdf(5, client, cache, lock) is None
    assert client.calls == 2
    assert 5 not in cache  # nothing negative cached


# ── comment SSE scoped to the originating session ─────────────────────────


def test_emit_to_user_targets_only_that_session() -> None:
    a = app.UserState("uid-a", "Alice")
    b = app.UserState("uid-b", "Bob")
    qa, qb = _queue.Queue(), _queue.Queue()
    a._sse_queues.append(qa)
    b._sse_queues.append(qb)

    with app._sessions_lock:
        app.user_sessions["uid-a"] = a
        app.user_sessions["uid-b"] = b
    try:
        app.state.emit_to_user("uid-a", {"type": "comment_saved", "lab_id": "010126-1"})
        assert qa.get_nowait()["type"] == "comment_saved"
        assert qb.empty()  # Bob must NOT see Alice's confirmation
    finally:
        with app._sessions_lock:
            app.user_sessions.pop("uid-a", None)
            app.user_sessions.pop("uid-b", None)
