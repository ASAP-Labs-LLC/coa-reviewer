"""Tests for UploadQueue durable, retrying QBench writes.

Root cause (diagnosed 2026-06-24): comment saves bypassed the UploadQueue and
called QBench synchronously with no retry, so a 429 dropped the comment and
returned HTTP 500 — while test edits (which DO go through the queue) survived.
These tests lock in routing comments through the queue with the same
server-side retry, plus an SSE confirmation so a closed browser window never
loses a comment.
"""

from __future__ import annotations

import pytest

pytest.importorskip("flask")

import app  # noqa: E402
from qbench_client import QBenchAPIError  # noqa: E402


class _FakeClient:
    def __init__(self, comment_fail_times: int = 0):
        self.comment_calls = []
        self.test_calls = []
        self._comment_fail_times = comment_fail_times

    def update_sample_comments(self, sample_id, comments):
        self.comment_calls.append((sample_id, comments))
        if len(self.comment_calls) <= self._comment_fail_times:
            raise QBenchAPIError("429 error at /samples: ratelimit exceeded")
        return {"ok": True}

    def update_test_result(self, test_id, value):
        self.test_calls.append((test_id, value))
        return {"ok": True}


def _queue(client):
    # start_worker=False keeps the queue deterministic for tests.
    q = app.UploadQueue(client, start_worker=False)
    return q


def test_enqueue_comment_puts_comment_job_on_queue() -> None:
    q = _queue(_FakeClient())
    q.enqueue_comment(7, "hello", lab_id="010126-1")
    item = q.queue.get_nowait()
    assert item["kind"] == "comment"
    assert item["sample_id"] == 7
    assert item["comments"] == "hello"
    assert item["lab_id"] == "010126-1"


def test_process_comment_success_calls_qbench_and_broadcasts_saved() -> None:
    client = _FakeClient()
    q = _queue(client)
    events = []
    q.sse_broadcast = lambda d: events.append(d)

    q._process({"kind": "comment", "sample_id": 42, "comments": "hi",
                "lab_id": "010126-9", "attempts": 0})

    assert client.comment_calls == [(42, "hi")]
    assert any(e["type"] == "comment_saved" and e["lab_id"] == "010126-9" for e in events)


def test_process_comment_retries_on_rate_limit() -> None:
    client = _FakeClient(comment_fail_times=1)
    q = _queue(client)
    scheduled = []
    q._schedule_retry = lambda payload, delay: scheduled.append((payload, delay))

    q._process({"kind": "comment", "sample_id": 1, "comments": "x",
                "lab_id": "010126-1", "attempts": 0})

    # First attempt hit a 429 → it must schedule a retry, not drop the comment.
    assert len(scheduled) == 1
    retried_payload, _delay = scheduled[0]
    assert retried_payload["attempts"] == 1
    assert retried_payload["kind"] == "comment"


def test_process_comment_permanent_failure_broadcasts_failed() -> None:
    client = _FakeClient(comment_fail_times=99)
    q = _queue(client)
    events = []
    q.sse_broadcast = lambda d: events.append(d)

    # attempts already exhausted → no more retries, surface the failure.
    q._process({"kind": "comment", "sample_id": 1, "comments": "x",
                "lab_id": "010126-1", "attempts": 5})

    assert any(e["type"] == "comment_failed" and e["lab_id"] == "010126-1" for e in events)


def test_process_test_edit_still_works() -> None:
    client = _FakeClient()
    q = _queue(client)
    q._process({"kind": "test", "test_id": 5, "value": "Pass", "attempts": 0})
    assert client.test_calls == [(5, "Pass")]


def test_enqueue_test_edit_is_backward_compatible() -> None:
    q = _queue(_FakeClient())
    q.enqueue(11, "Fail")
    item = q.queue.get_nowait()
    assert item.get("kind", "test") == "test"
    assert item["test_id"] == 11
    assert item["value"] == "Fail"
