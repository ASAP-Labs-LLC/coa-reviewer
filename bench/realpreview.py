"""Wiring the benchmark onto the **real** ``COASession``, and measuring what
that session's lock costs — without editing app.py.

Four seams, all verified against app.py, none of which need the file changed:

* ``QBENCH_BASE`` (app.py:153) is read as a module global *inside*
  ``generate_preview``, so ``setattr(app, "QBENCH_BASE", ...)`` redirects
  every preview call to loopback.
* ``COASession.__init__`` (app.py:628) makes no network call — it only builds
  a ``requests.Session`` and two locks — so the real class can be constructed
  directly.
* ``generate_preview`` returns ``None`` immediately unless ``csrf_token`` or
  ``playwright_cookies`` is truthy (app.py:713-714). Setting ``csrf_token``
  is what gets past that guard.
* ``login()`` drives Playwright against real QBench and must never run. It is
  replaced with a no-op on the instance, exactly as
  ``tests/test_coa_session.py:83-99`` already does. ``relogin()`` is left
  real, so the app's retry path (and its 30-second throttle) still runs.

``_session_lock`` is an ordinary instance attribute, so swapping in a wrapper
that keeps the same context-manager contract measures contention from the
outside. That is the whole reason a benchmark can report a lock-wait total
here without a line of app.py changing.
"""

from __future__ import annotations

import threading
import time
from typing import Optional


class InstrumentedLock:
    """A ``threading.Lock`` that remembers how long callers waited for it.

    ``wait_total_s`` is the number the benchmark reports: the sum, across every
    worker thread, of time spent blocked at the door. Under a 4-worker pool
    driving one serialised session, that total is the direct cost of app.py
    holding one lock across every preview HTTP call.

    ``hold_total_s`` is its counterpart — how long the lock was actually held.
    A run where ``hold_total_s`` approaches the wall clock is one where the
    lock, not the pool, is the throughput limit.
    """

    #: A wait shorter than this is scheduler noise, not contention.
    CONTENTION_FLOOR_S = 0.001

    def __init__(self, lock: Optional[threading.Lock] = None) -> None:
        self._lock = lock if lock is not None else threading.Lock()
        self._stats_lock = threading.Lock()
        self._local = threading.local()
        self.wait_total_s = 0.0
        self.hold_total_s = 0.0
        self.wait_max_s = 0.0
        self.acquisitions = 0
        self.contended = 0

    def acquire(self, *args, **kwargs) -> bool:
        t0 = time.perf_counter()
        got = self._lock.acquire(*args, **kwargs)
        t1 = time.perf_counter()
        if got:
            waited = t1 - t0
            self._local.acquired_at = t1
            with self._stats_lock:
                self.acquisitions += 1
                self.wait_total_s += waited
                if waited > self.wait_max_s:
                    self.wait_max_s = waited
                if waited >= self.CONTENTION_FLOOR_S:
                    self.contended += 1
        return got

    def release(self) -> None:
        held_from = getattr(self._local, "acquired_at", None)
        if held_from is not None:
            self._local.acquired_at = None
            with self._stats_lock:
                self.hold_total_s += time.perf_counter() - held_from
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def __enter__(self) -> "InstrumentedLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> bool:
        self.release()
        return False

    def stats(self) -> dict:
        with self._stats_lock:
            return {
                "wait_total_s": round(self.wait_total_s, 4),
                "hold_total_s": round(self.hold_total_s, 4),
                "wait_max_s": round(self.wait_max_s, 4),
                "acquisitions": self.acquisitions,
                "contended": self.contended,
            }


def install_real_coa_session(
    app_module,
    qbench_base: str,
    *,
    username: str = "bench",
    password: str = "bench",
    report_config_id: Optional[str] = None,
):
    """Point ``app``'s preview path at ``qbench_base`` and give it a real session.

    Returns the ``COASession`` — the genuine class, not a double — with its
    ``_session_lock`` replaced by an :class:`InstrumentedLock` so the run can
    report contention.
    """
    base = qbench_base.rstrip("/")
    # Read as a module global inside generate_preview, so this redirects the
    # POST, every poll, and the success URL in one assignment.
    setattr(app_module, "QBENCH_BASE", base)

    session = app_module.COASession(
        username, password,
        report_config_id or app_module.REPORT_CONFIG_ID,
    )
    # Past the guard at app.py:713-714 without a Playwright login ever running.
    session.csrf_token = "bench"
    session.login = lambda headless=True: None      # noqa: E731  (see docstring)
    session._session_lock = InstrumentedLock(session._session_lock)

    app_module.state.coa_session = session
    app_module.state.logged_in = True
    return session


def lock_stats(session) -> dict:
    """The lock numbers off an installed session, or empty if uninstrumented."""
    lock = getattr(session, "_session_lock", None)
    if isinstance(lock, InstrumentedLock):
        return lock.stats()
    return {}
