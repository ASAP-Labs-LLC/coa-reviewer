"""What happens to a live update stream when the browser falls behind.

SSE is the only push channel this app has: `markGood()` deliberately does not
set a sample's status itself, it waits for the `sample_status` event. So a
stream that stops delivering does not degrade the UI, it freezes it — samples
sit on "loading" forever and marking looks like it did nothing.

Each sample emits roughly five events during a pull, and `/api/start` fans out
across several tabs at once, so a large day can push well over a thousand
events through a 200-slot queue. A reviewer's browser only has to stall
briefly during that burst to fill it.

The original behaviour on a full queue was to drop the queue out of
`_sse_queues` permanently. Nothing ever put it back, while the generator on
the other end went on yielding keepalives — so the connection looked perfectly
healthy from the browser, `onerror` never fired, and EventSource never
reconnected. The UI went silently and permanently deaf.

Backpressure must instead stay recoverable: keep the subscriber attached, shed
the stale events rather than the listener, and tell the client it missed
something so it can resynchronise.
"""

from __future__ import annotations

import queue

import pytest

pytest.importorskip("flask")


@pytest.fixture
def ustate():
    from app import UserState
    return UserState("test-uid-sse", "RC")


def _drain(q: queue.Queue) -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def test_a_slow_subscriber_is_not_silently_unsubscribed(ustate) -> None:
    """Overflowing must not detach the listener — that is the deaf-UI bug."""
    q: queue.Queue = queue.Queue(maxsize=4)
    ustate._sse_queues.append(q)

    for i in range(50):
        ustate.emit_sse({"type": "sample_status", "n": i})

    assert q in ustate._sse_queues, (
        "the queue was dropped from _sse_queues and nothing re-adds it, so "
        "this browser never receives another event"
    )


def test_the_newest_events_survive_an_overflow(ustate) -> None:
    """Shed stale events, not current ones.

    A dropped `loading` is harmless; a dropped terminal status leaves a sample
    stuck. Keeping the tail is what makes the stream self-correcting.
    """
    q: queue.Queue = queue.Queue(maxsize=4)
    ustate._sse_queues.append(q)

    for i in range(50):
        ustate.emit_sse({"type": "sample_status", "n": i})

    delivered = _drain(q)
    numbered = [d["n"] for d in delivered if d.get("type") == "sample_status"]
    assert numbered, "nothing was delivered at all"
    assert 49 in numbered, f"the most recent event was lost: {numbered}"


def test_the_client_is_told_it_missed_events(ustate) -> None:
    """A gap the browser does not know about is indistinguishable from no gap."""
    q: queue.Queue = queue.Queue(maxsize=4)
    ustate._sse_queues.append(q)

    for i in range(50):
        ustate.emit_sse({"type": "sample_status", "n": i})

    delivered = _drain(q)
    assert any(d.get("type") == "resync" for d in delivered), (
        "events were dropped with no resync signal, so the UI keeps showing "
        "stale state and never knows to reload"
    )


def test_a_keeping_up_subscriber_gets_everything_untouched(ustate) -> None:
    """No resync noise and no loss on the normal path."""
    q: queue.Queue = queue.Queue(maxsize=200)
    ustate._sse_queues.append(q)

    for i in range(50):
        ustate.emit_sse({"type": "sample_status", "n": i})

    delivered = _drain(q)
    assert [d["n"] for d in delivered] == list(range(50))
    assert not any(d.get("type") == "resync" for d in delivered)


def test_emitting_to_nobody_is_harmless(ustate) -> None:
    ustate.emit_sse({"type": "status", "message": "no subscribers"})
