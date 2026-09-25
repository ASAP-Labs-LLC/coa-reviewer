"""Who is online, recorded as spans without a database write per request.

A heartbeat arrives every 60 s and most API requests also count as "here".
Writing each one would put the database on the hot path of every click, so
spans live in memory and are flushed at most every ``FLUSH_SECONDS`` per
person. What can be lost in a crash is at most that much of a span's tail,
and ``SharedStore.close_open_spans`` closes it at the last flushed heartbeat.

A gap longer than ``GAP_SECONDS`` (three missed heartbeats) ends a span at the
last time the person was seen; the next touch starts a new one. Logout ends
it at the moment of logout; the idle reaper ends it at the last touch.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from shared_store import SharedStore

logger = logging.getLogger("coa.presence")

GAP_SECONDS = 180.0
FLUSH_SECONDS = 30.0
MAX_TRACKED_USERS = 500


@dataclass
class _Span:
    user: str
    span_id: Optional[int]
    started: float
    last_seen: float
    flushed: float


class PresenceTracker:
    def __init__(self, store: SharedStore,
                 now: Optional[Callable[[], float]] = None) -> None:
        self._store = store
        self._now = now or time.time
        self._lock = threading.Lock()
        self._spans: Dict[str, _Span] = {}

    def touch(self, user: str) -> None:
        name = (user or "").strip()
        if not name:
            return
        key, now = name.casefold(), self._now()
        with self._lock:
            span = self._spans.get(key)
            if span is not None and now - span.last_seen > GAP_SECONDS:
                self._close_locked(key, span, span.last_seen, "gap")
                span = None
            if span is None:
                self._open_locked(key, name, now)
                return
            span.last_seen = now
            if span.span_id is None:          # store was down when it opened
                span.span_id = self._store.open_span(span.user, span.started)
            if span.span_id is not None and now - span.flushed >= FLUSH_SECONDS:
                if self._store.touch_span(span.span_id, now):
                    span.flushed = now

    def end(self, user: str, reason: str) -> None:
        key = (user or "").strip().casefold()
        with self._lock:
            span = self._spans.get(key)
            if span is None:
                return
            at = self._now() if reason == "logout" else span.last_seen
            self._close_locked(key, span, at, reason)

    def sweep(self) -> int:
        """Close spans whose owner has been silent past the gap. Bounded by
        the number of tracked users."""
        now = self._now()
        closed = 0
        with self._lock:
            for key, span in list(self._spans.items())[:MAX_TRACKED_USERS]:
                if now - span.last_seen > GAP_SECONDS:
                    self._close_locked(key, span, span.last_seen, "timeout")
                    closed += 1
        return closed

    def online(self) -> List[str]:
        now = self._now()
        with self._lock:
            return sorted(s.user for s in self._spans.values()
                          if now - s.last_seen <= GAP_SECONDS)

    def _open_locked(self, key: str, name: str, now: float) -> None:
        if len(self._spans) >= MAX_TRACKED_USERS:
            logger.warning("presence: %d users already tracked; not tracking %s",
                           len(self._spans), name)
            return
        span_id = self._store.open_span(name, now)
        self._spans[key] = _Span(name, span_id, now, now, now)
        logger.info("presence: %s online (span %s)", name, span_id)

    def _close_locked(self, key: str, span: _Span, at: float, reason: str) -> None:
        self._spans.pop(key, None)
        if span.span_id is not None:
            self._store.close_span(span.span_id, at, reason)
        logger.info("presence: %s offline (%s, %.0f min)", span.user, reason,
                    max(0.0, at - span.started) / 60)
