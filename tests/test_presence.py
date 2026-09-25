from __future__ import annotations

import pytest

from presence import FLUSH_SECONDS, GAP_SECONDS, PresenceTracker
from shared_store import SharedStore


class Clock:
    def __init__(self, t=10_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    store = SharedStore(tmp_path / "db", now=clock)
    yield PresenceTracker(store, now=clock), store, clock
    store.close()


def test_first_touch_opens_a_span(env):
    tr, store, clock = env
    tr.touch("Dana P")
    spans = store.spans_between(0, 1e9)
    assert len(spans) == 1 and spans[0]["open"] and spans[0]["user"] == "Dana P"
    assert tr.online() == ["Dana P"]


def test_touches_are_batched(env, monkeypatch):
    tr, store, clock = env
    tr.touch("Dana P")
    calls = []
    monkeypatch.setattr(store, "touch_span", lambda *a: calls.append(a) or True)
    for _ in range(10):
        clock.t += 2
        tr.touch("Dana P")
    assert calls == []            # 20 s < FLUSH_SECONDS
    clock.t += FLUSH_SECONDS
    tr.touch("Dana P")
    assert len(calls) == 1


def test_case_insensitive_identity(env):
    tr, store, clock = env
    tr.touch("Dana P")
    tr.touch("dana p")
    assert len(store.spans_between(0, 1e9)) == 1


def test_gap_closes_and_reopens(env):
    tr, store, clock = env
    tr.touch("u")
    start = clock.t
    clock.t += 60
    tr.touch("u")
    clock.t += GAP_SECONDS + 1
    tr.touch("u")
    spans = store.spans_between(0, 1e9)
    assert len(spans) == 2
    assert spans[0]["end"] == start + 60 and spans[0]["end_reason"] == "gap"
    assert spans[1]["open"]


def test_logout_closes_now(env):
    tr, store, clock = env
    tr.touch("u")
    clock.t += 40
    tr.end("u", "logout")
    s = store.spans_between(0, 1e9)[0]
    assert s["end"] == clock.t and s["end_reason"] == "logout"
    assert tr.online() == []


def test_timeout_closes_at_last_seen(env):
    tr, store, clock = env
    tr.touch("u")
    seen = clock.t
    clock.t += 700
    tr.end("u", "timeout")
    assert store.spans_between(0, 1e9)[0]["end"] == seen


def test_sweep_closes_idle(env):
    tr, store, clock = env
    tr.touch("a")
    clock.t += GAP_SECONDS + 5
    tr.touch("b")
    assert tr.sweep() == 1
    assert tr.online() == ["b"]


def test_blank_user_ignored(env):
    tr, store, clock = env
    tr.touch("  ")
    assert store.spans_between(0, 1e9) == []


def test_store_down_does_not_raise(tmp_path):
    blocker = tmp_path / "f"
    blocker.write_text("x")
    tr = PresenceTracker(SharedStore(blocker / "x" / "db"))
    tr.touch("u")
    tr.end("u", "logout")
    assert tr.sweep() == 0
