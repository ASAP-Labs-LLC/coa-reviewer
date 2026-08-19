"""Tests for bounded preview-generation concurrency.

Root cause (diagnosed 2026-06-24): preview generation spawned one OS thread
per sample (hundreds at once across tabs), all contending on the QBench rate
limiter and the single COASession lock — fueling the 429 storm. The fix routes
preview work through a bounded ThreadPoolExecutor.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("flask")

import app  # noqa: E402


def test_preview_workers_is_bounded_and_sane() -> None:
    n = app._preview_workers()
    assert 4 <= n <= 8


def test_preview_pool_is_a_bounded_executor() -> None:
    assert isinstance(app.PREVIEW_POOL, ThreadPoolExecutor)
    assert app.PREVIEW_POOL._max_workers == app._preview_workers()


def test_preview_pool_never_runs_more_than_max_workers_concurrently() -> None:
    pool = app.PREVIEW_POOL
    maxw = pool._max_workers

    lock = threading.Lock()
    counts = {"cur": 0, "max": 0}
    started = threading.Semaphore(0)
    release = threading.Event()

    def task():
        with lock:
            counts["cur"] += 1
            counts["max"] = max(counts["max"], counts["cur"])
        started.release()
        release.wait(5)
        with lock:
            counts["cur"] -= 1

    futures = [pool.submit(task) for _ in range(maxw * 3)]
    # Wait (with timeout, no fixed sleep) until the pool is saturated.
    for _ in range(maxw):
        assert started.acquire(timeout=5)
    with lock:
        # Exactly maxw tasks are running; the rest are queued, not running.
        saturated = counts["cur"]

    release.set()
    for f in futures:
        f.result(timeout=5)

    assert saturated == maxw
    assert counts["max"] <= maxw
