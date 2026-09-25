"""Shared, durable review state: verdicts, per-sample history, presence.

Why SQLite and not more JSON files: three readers need three different slices
of the same facts — the sample list wants "latest verdict per (lab_id, mode)",
the History tab wants "every change to one lab_id, newest first", and the Time
Online page wants "everything that happened on one day". Indexed tables answer
each in one bounded query; JSON would need a full scan per request.

Rules this module keeps (they are why callers can use it without try/except):

* **It never raises for I/O.** A locked, missing or corrupt database logs a
  WARNING and returns the method's safe default (``False``, ``None``, ``[]``).
  Reviewing must never fail because the history could not be written.
* **It does raise ``ValueError`` for programming errors** — an unknown mode,
  kind or outcome, or an empty lab id/user. Those are bugs visible in tests.
* **Every read is bounded** (``LIMIT`` / chunked ``IN``), every loop has a
  fixed upper bound, and there is no recursion.
* **One connection, one lock.** Writes are tiny and infrequent next to review
  work; a single serialised connection is simpler and more predictable than a
  pool, and WAL keeps readers from blocking on the file.

``ChangeLog`` (JSONL) remains the audit trail of record; this is the
queryable view of it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, TypeVar

logger = logging.getLogger("coa.shared_store")

MODES = ("info", "tests")
OUTCOMES = ("good", "bad")
EVENT_KINDS = (
    "mark", "unmark", "test_result", "sample_info", "sample_sync",
    "comments", "attachment_deleted", "listing_created", "listing_completed",
)
SPAN_END_REASONS = ("logout", "timeout", "gap", "restart")

MAX_HISTORY = 500          # rows one History tab can show
MAX_RANGE_ROWS = 5000      # rows one /activity day can use
MAX_BATCH = 900            # bound parameters per IN (SQLite limit is 999)
MAX_TEXT = 4000            # characters kept per before/after/detail value
SLOW_QUERY_MS = 50.0
REOPEN_BACKOFF_SECONDS = 30.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
    lab_id     TEXT NOT NULL,
    mode       TEXT NOT NULL,
    outcome    TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    cc_task_id INTEGER,
    sample_id  INTEGER,
    by_user    TEXT NOT NULL,
    at         REAL NOT NULL,
    PRIMARY KEY (lab_id, mode)
);
CREATE TABLE IF NOT EXISTS sample_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    lab_id  TEXT NOT NULL,
    at      REAL NOT NULL,
    user    TEXT NOT NULL,
    kind    TEXT NOT NULL,
    field   TEXT,
    before  TEXT,
    after   TEXT,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_lab_at ON sample_events(lab_id, at);
CREATE INDEX IF NOT EXISTS ix_events_at ON sample_events(at);
CREATE TABLE IF NOT EXISTS presence (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user       TEXT NOT NULL,
    started_at REAL NOT NULL,
    last_seen  REAL NOT NULL,
    ended_at   REAL,
    end_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_presence_started ON presence(started_at);
CREATE INDEX IF NOT EXISTS ix_presence_last ON presence(last_seen);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

T = TypeVar("T")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _require_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    _require(bool(text), f"{name} must be a non-empty string")
    return text


def _as_text(value: Any) -> Optional[str]:
    """Store any value as bounded text; ``None`` stays NULL ("unknown")."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text[:MAX_TEXT]


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _decode_detail(raw: Optional[str]) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


class SharedStore:
    """Thread-safe access to ``coa_shared.db``. See the module docstring."""

    def __init__(self, path: Path | str,
                 now: Optional[Callable[[], float]] = None) -> None:
        self._path = Path(path)
        self._now = now or time.time
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._failed_at: Optional[float] = None

    @property
    def path(self) -> Path:
        return self._path

    # ── connection management ────────────────────────────────────────────

    def _connection(self) -> Optional[sqlite3.Connection]:
        """Open lazily; after a failure, retry at most every 30 s. Caller
        holds ``_lock``."""
        if self._conn is not None:
            return self._conn
        if (self._failed_at is not None
                and time.monotonic() - self._failed_at < REOPEN_BACKOFF_SECONDS):
            return None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._path), timeout=5.0,
                                   check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(_SCHEMA)
        except (sqlite3.Error, OSError) as exc:
            self._failed_at = time.monotonic()
            logger.warning("shared store unavailable at %s: %s (retry in %.0fs)",
                           self._path, exc, REOPEN_BACKOFF_SECONDS)
            return None
        self._conn = conn
        self._failed_at = None
        logger.info("shared store opened at %s", self._path)
        return conn

    def _drop_connection(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
        self._conn = None
        self._failed_at = time.monotonic()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
            self._conn = None

    def _run(self, op: str, fn: Callable[[sqlite3.Connection], T], default: T) -> T:
        """Run ``fn`` under the lock; any sqlite error → WARNING + default."""
        started = time.perf_counter()
        with self._lock:
            conn = self._connection()
            if conn is None:
                return default
            try:
                result = fn(conn)
            except sqlite3.OperationalError as exc:
                # Disk I/O, locked past busy_timeout, file vanished: reopen later.
                logger.warning("shared store %s failed: %s", op, exc)
                self._drop_connection()
                return default
            except sqlite3.Error as exc:
                logger.warning("shared store %s failed: %s", op, exc)
                return default
        elapsed_ms = (time.perf_counter() - started) * 1000
        if elapsed_ms > SLOW_QUERY_MS:
            logger.info("shared store %s slow: %.1f ms", op, elapsed_ms)
        else:
            logger.debug("shared store %s %.1f ms", op, elapsed_ms)
        return result

    # ── verdicts ─────────────────────────────────────────────────────────

    def set_verdict(self, lab_id: str, mode: str, outcome: str, *, by: str,
                    reason: str = "", cc_task_id: Any = None,
                    sample_id: Any = None) -> bool:
        lab_id = _require_text(lab_id, "lab_id")
        _require(mode in MODES, f"unknown mode {mode!r}")
        _require(outcome in OUTCOMES, f"unknown outcome {outcome!r}")
        by = _require_text(by, "by")
        row = (lab_id, mode, outcome, (reason or "")[:MAX_TEXT],
               _as_int(cc_task_id), _as_int(sample_id), by, self._now())

        def op(c: sqlite3.Connection) -> bool:
            c.execute(
                "INSERT INTO verdicts (lab_id, mode, outcome, reason, cc_task_id,"
                " sample_id, by_user, at) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(lab_id, mode) DO UPDATE SET outcome=excluded.outcome,"
                " reason=excluded.reason, cc_task_id=excluded.cc_task_id,"
                " sample_id=excluded.sample_id, by_user=excluded.by_user,"
                " at=excluded.at", row)
            return True
        return self._run("set_verdict", op, False)

    def clear_verdict(self, lab_id: str, mode: str) -> bool:
        lab_id = _require_text(lab_id, "lab_id")
        _require(mode in MODES, f"unknown mode {mode!r}")

        def op(c: sqlite3.Connection) -> bool:
            c.execute("DELETE FROM verdicts WHERE lab_id=? AND mode=?", (lab_id, mode))
            return True
        return self._run("clear_verdict", op, False)

    def verdicts_for(self, lab_ids: Iterable[str]) -> Optional[Dict[str, Dict[str, dict]]]:
        """``{lab_id: {mode: verdict}}`` for those that have one.

        ``None`` means the store could not be read — distinct from ``{}``
        ("nobody has judged these"), because a caller must not un-judge a
        sample just because the disk hiccupped.
        """
        ids = sorted({str(x) for x in lab_ids if x})
        out: Dict[str, Dict[str, dict]] = {}
        for start in range(0, len(ids), MAX_BATCH):
            chunk = ids[start:start + MAX_BATCH]
            marks = ",".join("?" * len(chunk))
            rows = self._run(
                "verdicts_for",
                lambda c, chunk=chunk, marks=marks: c.execute(
                    f"SELECT * FROM verdicts WHERE lab_id IN ({marks})", chunk).fetchall(),
                None)
            if rows is None:
                return None
            for r in rows:
                out.setdefault(r["lab_id"], {})[r["mode"]] = {
                    "outcome": r["outcome"], "reason": r["reason"],
                    "cc_task_id": r["cc_task_id"], "sample_id": r["sample_id"],
                    "by": r["by_user"], "at": r["at"],
                }
        return out

    # ── sample history ───────────────────────────────────────────────────

    def record_event(self, lab_id: str, kind: str, *, user: str,
                     field: Optional[str] = None, before: Any = None,
                     after: Any = None, detail: Any = None) -> bool:
        lab_id = _require_text(lab_id, "lab_id")
        _require(kind in EVENT_KINDS, f"unknown event kind {kind!r}")
        user = _require_text(user, "user")
        row = (lab_id, self._now(), user, kind, _as_text(field), _as_text(before),
               _as_text(after), None if detail is None else _as_text(detail))

        def op(c: sqlite3.Connection) -> bool:
            c.execute("INSERT INTO sample_events (lab_id, at, user, kind, field,"
                      " before, after, detail) VALUES (?,?,?,?,?,?,?,?)", row)
            return True
        return self._run("record_event", op, False)

    def history(self, lab_id: str, limit: int = 200) -> List[dict]:
        lab_id = _require_text(lab_id, "lab_id")
        limit = max(1, min(int(limit), MAX_HISTORY))

        def op(c: sqlite3.Connection) -> List[dict]:
            rows = c.execute(
                "SELECT * FROM sample_events WHERE lab_id=?"
                " ORDER BY at DESC, id DESC LIMIT ?", (lab_id, limit)).fetchall()
            return [self._event_dict(r) for r in rows]
        return self._run("history", op, [])

    def events_between(self, start: float, end: float,
                       limit: int = MAX_RANGE_ROWS) -> List[dict]:
        limit = max(1, min(int(limit), MAX_RANGE_ROWS))

        def op(c: sqlite3.Connection) -> List[dict]:
            rows = c.execute(
                "SELECT * FROM sample_events WHERE at >= ? AND at < ?"
                " ORDER BY at, id LIMIT ?", (float(start), float(end), limit)).fetchall()
            return [self._event_dict(r) for r in rows]
        return self._run("events_between", op, [])

    @staticmethod
    def _event_dict(r: sqlite3.Row) -> dict:
        return {"id": r["id"], "lab_id": r["lab_id"], "at": r["at"],
                "user": r["user"], "kind": r["kind"], "field": r["field"],
                "before": r["before"], "after": r["after"],
                "detail": _decode_detail(r["detail"])}

    # ── presence ─────────────────────────────────────────────────────────

    def open_span(self, user: str, at: float) -> Optional[int]:
        user = _require_text(user, "user")

        def op(c: sqlite3.Connection) -> Optional[int]:
            cur = c.execute("INSERT INTO presence (user, started_at, last_seen)"
                            " VALUES (?,?,?)", (user, float(at), float(at)))
            return int(cur.lastrowid)
        return self._run("open_span", op, None)

    def touch_span(self, span_id: int, last_seen: float) -> bool:
        def op(c: sqlite3.Connection) -> bool:
            c.execute("UPDATE presence SET last_seen=? WHERE id=? AND ended_at IS NULL",
                      (float(last_seen), int(span_id)))
            return True
        return self._run("touch_span", op, False)

    def close_span(self, span_id: int, ended_at: float, reason: str) -> bool:
        _require(reason in SPAN_END_REASONS, f"unknown end reason {reason!r}")

        def op(c: sqlite3.Connection) -> bool:
            c.execute("UPDATE presence SET last_seen=MAX(last_seen, ?), ended_at=?,"
                      " end_reason=? WHERE id=? AND ended_at IS NULL",
                      (float(ended_at), float(ended_at), reason, int(span_id)))
            return True
        return self._run("close_span", op, False)

    def close_open_spans(self, reason: str) -> int:
        """Close every span left open (a process that died mid-span) at its
        last heartbeat. Returns how many were closed."""
        _require(reason in SPAN_END_REASONS, f"unknown end reason {reason!r}")

        def op(c: sqlite3.Connection) -> int:
            cur = c.execute("UPDATE presence SET ended_at=last_seen, end_reason=?"
                            " WHERE ended_at IS NULL", (reason,))
            return int(cur.rowcount or 0)
        return self._run("close_open_spans", op, 0)

    def spans_between(self, start: float, end: float,
                      limit: int = MAX_RANGE_ROWS) -> List[dict]:
        limit = max(1, min(int(limit), MAX_RANGE_ROWS))

        def op(c: sqlite3.Connection) -> List[dict]:
            rows = c.execute(
                "SELECT * FROM presence WHERE started_at < ?"
                " AND COALESCE(ended_at, last_seen) >= ?"
                " ORDER BY started_at, id LIMIT ?",
                (float(end), float(start), limit)).fetchall()
            return [{"id": r["id"], "user": r["user"], "start": r["started_at"],
                     "end": r["ended_at"] if r["ended_at"] is not None else r["last_seen"],
                     "open": r["ended_at"] is None, "end_reason": r["end_reason"]}
                    for r in rows]
        return self._run("spans_between", op, [])

    def recorded_range(self) -> Tuple[Optional[float], Optional[float]]:
        """Earliest and latest instant anything was recorded, for the day picker."""
        def op(c: sqlite3.Connection) -> Tuple[Optional[float], Optional[float]]:
            p = c.execute("SELECT MIN(started_at), MAX(last_seen) FROM presence").fetchone()
            e = c.execute("SELECT MIN(at), MAX(at) FROM sample_events").fetchone()
            lows = [x for x in (p[0], e[0]) if x is not None]
            highs = [x for x in (p[1], e[1]) if x is not None]
            return (min(lows) if lows else None, max(highs) if highs else None)
        return self._run("recorded_range", op, (None, None))

    # ── meta ─────────────────────────────────────────────────────────────

    def get_meta(self, key: str) -> Optional[str]:
        def op(c: sqlite3.Connection) -> Optional[str]:
            r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return r[0] if r else None
        return self._run("get_meta", op, None)

    def set_meta(self, key: str, value: str) -> bool:
        def op(c: sqlite3.Connection) -> bool:
            c.execute("INSERT INTO meta (key, value) VALUES (?, ?)"
                      " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, str(value)))
            return True
        return self._run("set_meta", op, False)
