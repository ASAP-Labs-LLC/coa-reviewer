"""Who is online, recorded as spans without a database write per touch.

A heartbeat arrives every 60 s and most API requests also count as "here".
Writing each one would put the database on the hot path of every click, so
``touch()`` is **memory-only** — it never talks to the store. The app's own
periodic worker calls ``flush()`` (intended cadence: ``FLUSH_SECONDS``),
which is the only method that does. Everything ``flush()`` sends goes
through ``SharedStore.apply_presence`` as one batched transaction: opens,
touches and closes are collected under the tracker's lock, the lock is
released for the (possibly slow) database call, and results are applied
back under the lock again. What can be lost in a crash is at most one
flush interval's worth of a span's tail.

A gap longer than ``GAP_SECONDS`` (three missed heartbeats) ends a span at
the last time the person was seen; the next touch starts a new one. Logout
ends it at the moment of logout; the idle reaper ends it at the last touch.
A span that survives across local midnight without ever gapping is split in
two at the boundary, so "today" and "yesterday" never share one span.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional

from shared_store import SPAN_END_REASONS, SharedStore

logger = logging.getLogger("coa.presence")

GAP_SECONDS = 180.0
FLUSH_SECONDS = 30.0          # intended flush cadence for the app's worker
MAX_TRACKED_USERS = 500
MAX_CLOSING = MAX_TRACKED_USERS * 2   # retired spans awaiting their close-flush
_CAPACITY_WARN_RATE_SECONDS = 60.0


def _require_reason(reason: str) -> None:
    if reason not in SPAN_END_REASONS:
        raise ValueError(f"unknown end reason {reason!r}")


@dataclass
class _Span:
    user: str
    span_id: Optional[int]
    started: float
    last_seen: float
    dirty: bool = False
    pending_close: Optional[dict] = None


class PresenceTracker:
    def __init__(self, store: SharedStore,
                 now: Optional[Callable[[], float]] = None) -> None:
        self._store = store
        self._now = now or time.time
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()      # single-flight for flush()
        self._spans: Dict[str, _Span] = {}      # live, still "here"
        self._closing: List[_Span] = []          # retired, awaiting close-flush
        self._last_capacity_warn = 0.0

    # ── touch/end/sweep: memory-only, never block on the store ─────────────

    def touch(self, user: str) -> None:
        name = (user or "").strip()
        if not name:
            return
        key, now = name.casefold(), self._now()
        with self._lock:
            span = self._spans.get(key)
            if span is not None and now - span.last_seen > GAP_SECONDS:
                self._retire_locked(key, span, span.last_seen, "gap")
                span = None
            if span is not None:
                midnight = self._midnight_split(span, now)
                if midnight is not None:
                    # Close the old span exactly at the boundary and open
                    # the new one there too, so "today" and "yesterday"
                    # split cleanly with no gap and no overlap — then this
                    # same touch still counts, via last_seen=now below, so
                    # the first touch after midnight is never lost.
                    self._retire_locked(key, span, midnight, "midnight")
                    self._open_locked(key, name, now, started=midnight)
                    return
            if span is None:
                self._open_locked(key, name, now)
                return
            span.last_seen = now
            span.dirty = True

    def end(self, user: str, reason: str) -> None:
        _require_reason(reason)
        key = (user or "").strip().casefold()
        with self._lock:
            span = self._spans.get(key)
            if span is None:
                return
            at = self._now() if reason == "logout" else span.last_seen
            self._retire_locked(key, span, at, reason)

    def sweep(self) -> int:
        """Close spans whose owner has been silent past the gap. Bounded by
        the number of tracked users."""
        now = self._now()
        closed = 0
        with self._lock:
            for key, span in list(self._spans.items())[:MAX_TRACKED_USERS]:
                if now - span.last_seen > GAP_SECONDS:
                    self._retire_locked(key, span, span.last_seen, "timeout")
                    closed += 1
        return closed

    def online(self) -> List[str]:
        now = self._now()
        with self._lock:
            return sorted(s.user for s in self._spans.values()
                          if now - s.last_seen <= GAP_SECONDS)

    def close_all(self, reason: str) -> int:
        """Queue a close for everyone (graceful shutdown), then flush —
        blocking briefly if another flush is already in flight, so a
        shutdown doesn't skip its own cycle and lose the closes."""
        _require_reason(reason)
        with self._lock:
            for key, span in list(self._spans.items())[:MAX_TRACKED_USERS]:
                self._retire_locked(key, span, span.last_seen, reason)
        return self.flush(blocking=True)

    # ── flush: the only place that talks to the store ──────────────────────

    def flush(self, blocking: bool = False, timeout: float = 5.0) -> int:
        """Write pending opens/touches/closes in one batch. Single-flight:
        concurrent callers don't race each other into duplicate writes.

        ``blocking=False`` (the default — what a periodic worker should
        use) skips this cycle and returns 0 immediately if a flush is
        already running, logging at DEBUG; the next cycle will catch
        whatever this one would have. ``blocking=True`` (what
        ``close_all`` uses) waits up to ``timeout`` seconds instead, so a
        shutdown doesn't drop its own closes.
        """
        acquired = (self._flush_lock.acquire(timeout=timeout) if blocking
                   else self._flush_lock.acquire(blocking=False))
        if not acquired:
            logger.debug("presence: flush already in progress; skipping this cycle")
            return 0
        try:
            return self._flush_locked()
        finally:
            self._flush_lock.release()

    def _flush_locked(self) -> int:
        """The actual flush body; only ``flush()`` calls this, always
        holding ``_flush_lock``."""
        with self._lock:
            live = list(self._spans.items())[:MAX_TRACKED_USERS]
            closing = list(self._closing)[:MAX_TRACKED_USERS]
            ops: List[dict] = []
            plan: List[tuple] = []
            for key, span in live:
                if span.span_id is None:
                    ops.append({"op": "open", "user": span.user,
                               "started": span.started, "last_seen": span.last_seen})
                    plan.append(("open", key, span, span.last_seen))
                elif span.dirty:
                    ops.append({"op": "touch", "span_id": span.span_id,
                               "last_seen": span.last_seen})
                    plan.append(("touch", key, span, span.last_seen))
            for span in closing:
                if span.span_id is None:
                    ops.append({"op": "open", "user": span.user,
                               "started": span.started,
                               "last_seen": span.pending_close["at"]})
                    plan.append(("open_then_close", None, span, None))
                    ops.append({"op": "close", "span_id": "$prev",
                               "ended_at": span.pending_close["at"],
                               "reason": span.pending_close["reason"]})
                else:
                    ops.append({"op": "close", "span_id": span.span_id,
                               "ended_at": span.pending_close["at"],
                               "reason": span.pending_close["reason"]})
                plan.append(("close", None, span, None))

        if not ops:
            return 0
        results = self._store.apply_presence(ops)
        if results is None:
            logger.warning("presence: flush of %d ops failed; will retry", len(ops))
            return 0

        written = 0
        with self._lock:
            for (kind, key, span, snapshot), result in zip(plan, results):
                if kind == "open":
                    # N3: the span object is the identity, whether or not
                    # it's still in self._spans — it may have been retired
                    # (logged out, gapped, ...) while this call was in
                    # flight. Assigning the id unconditionally is what lets
                    # the next flush find and close the right row instead
                    # of opening a second, orphaning the first forever.
                    span.span_id = result
                    still_here = self._spans.get(key) is span
                    if still_here and span.last_seen <= snapshot:
                        span.dirty = False
                    written += 1
                elif kind == "open_then_close":
                    pass   # its paired "close" entry below counts the write
                elif kind == "touch":
                    still_here = self._spans.get(key) is span
                    if result:
                        if still_here and span.last_seen <= snapshot:
                            span.dirty = False
                        written += 1
                    elif still_here:
                        # closed behind our back (e.g. a restart sweep) —
                        # reopen fresh, starting now (not the stale original
                        # start time), rather than silently losing the user.
                        span.span_id = None
                        span.started = span.last_seen
                        span.dirty = True
                        logger.info("presence: %s span closed elsewhere; reopening",
                                   span.user)
                elif kind == "close":
                    written += 1
                    try:
                        self._closing.remove(span)
                    except ValueError:
                        pass
        return written

    # ── internals ───────────────────────────────────────────────────────

    def _midnight_split(self, span: "_Span", now: float) -> Optional[float]:
        """``None`` unless ``now`` has crossed into a new local calendar day
        since ``span`` started; otherwise local midnight of ``now``'s day —
        the exact instant the old span closes at and the new one opens at."""
        started_day = datetime.fromtimestamp(span.started).date()
        now_day = datetime.fromtimestamp(now).date()
        if started_day == now_day:
            return None
        return datetime(now_day.year, now_day.month, now_day.day).timestamp()

    def _open_locked(self, key: str, name: str, now: float, *,
                     started: Optional[float] = None) -> None:
        if len(self._spans) >= MAX_TRACKED_USERS:
            self._warn_capacity(name)
            return
        self._spans[key] = _Span(user=name, span_id=None,
                                 started=started if started is not None else now,
                                 last_seen=now, dirty=True)
        logger.info("presence: %s online", name)

    def _retire_locked(self, key: str, span: "_Span", at: float, reason: str) -> None:
        """Move a span from "live" to "awaiting its close-flush". Queued
        regardless of whether it ever reached the store — ``flush()`` pairs
        an open with the close for one that never did, so a visit shorter
        than one flush interval is still recorded, not lost."""
        self._spans.pop(key, None)
        span.pending_close = {"at": at, "reason": reason}
        span.dirty = False
        if len(self._closing) >= MAX_CLOSING:
            logger.warning("presence: %d spans already awaiting close-flush;"
                           " dropping the oldest", len(self._closing))
            self._closing.pop(0)
        self._closing.append(span)
        logger.info("presence: %s offline (%s, %.0f min)", span.user, reason,
                    max(0.0, at - span.started) / 60)

    def _warn_capacity(self, name: str) -> None:
        now = time.monotonic()
        if now - self._last_capacity_warn >= _CAPACITY_WARN_RATE_SECONDS:
            logger.warning("presence: %d users already tracked; not tracking %s",
                           len(self._spans), name)
            self._last_capacity_warn = now
