"""Shared, durable review state: verdicts, per-sample history, presence.

Why SQLite and not more JSON files: three readers need three different slices
of the same facts — the sample list wants "latest verdict per (lab_id, mode)",
the History tab wants "every change to one lab_id, newest first", and the Time
Online page wants "everything that happened on one day". Indexed tables answer
each in one bounded query; JSON would need a full scan per request.

Rules this module keeps (they are why callers can use it without try/except):

* **It never raises for I/O.** A locked, missing or corrupt database logs a
  WARNING (or ERROR, for corruption) and returns the method's safe default
  (``False``, ``None``, ``[]``). Reviewing must never fail because the
  history could not be written.
* **It does raise ``ValueError`` for programming errors** — an unknown mode,
  kind or outcome, or an empty lab id/user. Those are bugs visible in tests.
* **Every read is bounded** (``LIMIT`` / chunked ``IN``), every loop has a
  fixed upper bound, and there is no recursion.
* **One writer, a small pool of readers.** Writes are tiny and infrequent
  next to review work, so a single serialised writer connection is simpler
  and more predictable than a pool. Reads are far more frequent (every tab
  load) and must never queue behind a write, so they use their own
  read-only connections (``READER_POOL_SIZE`` of them). WAL is what makes
  this safe: one writer and many readers never block each other.
* **Contention is not corruption.** ``SQLITE_BUSY``/``SQLITE_LOCKED`` (someone
  else mid-write) just means "try again later" — the connection is kept and
  there is no backoff. A real I/O problem or a corrupt file is different: the
  connection is dropped, reopening backs off exponentially, and a corrupt
  file is quarantined (renamed aside) so a fresh, empty one can take its
  place rather than every call failing forever.

``ChangeLog`` (JSONL) remains the audit trail of record; this is the
queryable view of it.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, TypeVar

logger = logging.getLogger("coa.shared_store")

MODES = ("info", "tests")
OUTCOMES = ("good", "bad")                       # what a caller may *set*
MARK_OUTCOMES = ("good", "bad", "cleared")        # what apply_mark accepts
EVENT_KINDS = (
    "mark", "unmark", "test_result", "sample_info", "sample_sync",
    "comments", "attachment_deleted", "listing_created", "listing_completed",
)
SPAN_END_REASONS = ("logout", "timeout", "gap", "restart")

MAX_HISTORY = 500          # rows one History tab can show
MAX_RANGE_ROWS = 5000      # rows one /activity day can use; len==limit means truncated
MAX_BATCH = 900            # bound parameters per IN (SQLite limit is 999)
MAX_TEXT = 4000            # characters kept per before/after/detail value
MAX_VERDICTS_INPUT = 10_000  # lab_ids accepted by one verdicts_for() call
MAX_SPAN_SECONDS = 86400 + 600  # a span can't outlive one calendar day + slack

SLOW_QUERY_MS = 50.0
BUSY_TIMEOUT_MS = 1000
CONNECT_TIMEOUT_SECONDS = 1.0
READER_POOL_SIZE = 3
READER_ACQUIRE_TIMEOUT_SECONDS = 2.0
BACKOFF_START_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0
SKIP_LOG_INTERVAL_SECONDS = 10.0

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

_UPSERT_VERDICT_SQL = (
    "INSERT INTO verdicts (lab_id, mode, outcome, reason, cc_task_id,"
    " sample_id, by_user, at) VALUES (?,?,?,?,?,?,?,?)"
    " ON CONFLICT(lab_id, mode) DO UPDATE SET outcome=excluded.outcome,"
    " reason=excluded.reason, cc_task_id=excluded.cc_task_id,"
    " sample_id=excluded.sample_id, by_user=excluded.by_user,"
    " at=excluded.at"
)

T = TypeVar("T")

# ── sqlite error classification ─────────────────────────────────────────────
# BUSY/LOCKED is contention, not damage: keep the connection, no backoff.
# IOERR/CANTOPEN and "no such table" mean the connection is no good any more.
# CORRUPT/NOTADB additionally means the *file* is no good — quarantine it.

_BUSY_NAMES = {"SQLITE_BUSY", "SQLITE_LOCKED"}
_CORRUPT_NAMES = {"SQLITE_CORRUPT", "SQLITE_NOTADB"}
_DROP_NAMES = {"SQLITE_IOERR", "SQLITE_CANTOPEN"}


def _classify(exc: sqlite3.Error) -> str:
    """Return ``"busy"``, ``"corrupt"`` or ``"drop"`` for how to react."""
    name = getattr(exc, "sqlite_errorname", None)
    if name:
        base = name.split("_ERROR")[0] if "_ERROR" in name else name
        if base in _BUSY_NAMES:
            return "busy"
        if base in _CORRUPT_NAMES:
            return "corrupt"
        if base in _DROP_NAMES:
            return "drop"
    text = str(exc).lower()
    if "no such table" in text:
        return "drop"
    if "locked" in text or "busy" in text:
        return "busy"
    if "malformed" in text or "not a database" in text or "corrupt" in text:
        return "corrupt"
    return "drop"   # unrecognised: be conservative and reopen


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


def _encode_detail(value: Any) -> Optional[str]:
    """Encode ``detail`` for storage, keeping the JSON valid even when a
    field is huge: string values inside a dict are pre-truncated so the
    overall document (usually) still fits; if it still doesn't, store a
    small sentinel instead of a half-written value."""
    if value is None:
        return None
    if isinstance(value, dict):
        safe = {k: (v[:MAX_TEXT // 4] if isinstance(v, str) else v)
                for k, v in value.items()}
    else:
        safe = value
    text = value if isinstance(value, str) else json.dumps(safe, default=str)
    if len(text) > MAX_TEXT:
        return json.dumps({"truncated": True})
    return text[:MAX_TEXT]


def _decode_detail(raw: Optional[str]) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _op_label(op: str, key: Optional[str]) -> str:
    return f"{op}[{key}]" if key else op


class _ReaderPool:
    """A small, bounded pool of read-only connections so reads never queue
    behind the single writer lock. Opened lazily; each holds
    ``PRAGMA query_only=1`` so a bug here can't accidentally write.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._pool: "queue.Queue[sqlite3.Connection]" = queue.Queue(maxsize=READER_POOL_SIZE)
        self._created = 0
        self._create_lock = threading.Lock()

    def _open(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), timeout=CONNECT_TIMEOUT_SECONDS,
                               check_same_thread=False, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            conn.executescript(_SCHEMA)   # idempotent: guarantees tables exist
            conn.execute("PRAGMA query_only=1")
        except sqlite3.Error:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            raise
        return conn

    def acquire(self) -> Optional[sqlite3.Connection]:
        try:
            return self._pool.get_nowait()
        except queue.Empty:
            pass
        with self._create_lock:
            if self._created < READER_POOL_SIZE:
                try:
                    conn = self._open()
                except (sqlite3.Error, OSError) as exc:
                    logger.warning("shared store unavailable at %s: %s", self._path, exc)
                    return None
                self._created += 1
                return conn
        try:
            return self._pool.get(timeout=READER_ACQUIRE_TIMEOUT_SECONDS)
        except queue.Empty:
            logger.warning("shared store reader pool exhausted at %s", self._path)
            return None

    def release(self, conn: sqlite3.Connection, healthy: bool) -> None:
        if healthy:
            try:
                self._pool.put_nowait(conn)
                return
            except queue.Full:  # pragma: no cover - pool sized to _created
                pass
        try:
            conn.close()
        except sqlite3.Error:
            pass
        with self._create_lock:
            self._created = max(0, self._created - 1)

    def close(self) -> None:
        with self._create_lock:
            while True:
                try:
                    conn = self._pool.get_nowait()
                except queue.Empty:
                    break
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._created = 0


class SharedStore:
    """Thread-safe access to ``coa_shared.db``. See the module docstring."""

    def __init__(self, path: Path | str,
                 now: Optional[Callable[[], float]] = None) -> None:
        self._path = Path(path)
        self._now = now or time.time
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._backoff = BACKOFF_START_SECONDS
        self._backoff_until: Optional[float] = None
        self._last_skip_log = 0.0
        self._readers = _ReaderPool(self._path)

    @property
    def path(self) -> Path:
        return self._path

    # ── connection management (writer) ─────────────────────────────────────

    def _open_raw(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), timeout=CONNECT_TIMEOUT_SECONDS,
                               check_same_thread=False, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                logger.warning("shared store at %s could not enable WAL (mode=%s)",
                               self._path, mode)
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
        except sqlite3.Error:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            raise
        return conn

    def _connection(self) -> Optional[sqlite3.Connection]:
        """Open lazily; after a failure, retry with exponential backoff
        (1, 2, 4, ... 30s, reset on success). Caller holds ``_lock``. A
        corrupt file is quarantined and one fresh open is retried
        immediately (bounded: at most two attempts)."""
        if self._conn is not None:
            return self._conn
        if (self._backoff_until is not None
                and time.monotonic() < self._backoff_until):
            self._log_skip()
            return None
        for attempt in range(2):   # one retry after quarantining a corrupt file
            try:
                conn = self._open_raw()
            except sqlite3.Error as exc:
                if _classify(exc) == "corrupt":
                    self._quarantine_files()
                    continue
                self._fail_open(exc)
                return None
            except OSError as exc:
                self._fail_open(exc)
                return None
            self._conn = conn
            self._backoff = BACKOFF_START_SECONDS
            self._backoff_until = None
            logger.info("shared store opened at %s", self._path)
            return conn
        self._fail_open("still unavailable after quarantining a corrupt file")
        return None

    def _fail_open(self, exc: Any) -> None:
        logger.warning("shared store unavailable at %s: %s (retry in %.0fs)",
                       self._path, exc, self._backoff)
        self._backoff_until = time.monotonic() + self._backoff
        self._backoff = min(self._backoff * 2, BACKOFF_MAX_SECONDS)

    def _log_skip(self) -> None:
        now = time.monotonic()
        if now - self._last_skip_log >= SKIP_LOG_INTERVAL_SECONDS:
            logger.debug("shared store %s: skipped, backoff active", self._path)
            self._last_skip_log = now

    def _quarantine_files(self) -> None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        logger.error("shared store at %s is corrupt; quarantining (tag %s)",
                    self._path, ts)
        for suffix in ("", "-wal", "-shm"):
            src = Path(f"{self._path}{suffix}")
            if not src.exists():
                continue
            dest = Path(f"{self._path}.corrupt-{ts}{suffix}")
            try:
                src.rename(dest)
            except OSError as exc:
                logger.warning("shared store: could not quarantine %s: %s", src, exc)

    def _drop_connection(self, *, backoff: bool) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
        self._conn = None
        if backoff:
            self._backoff_until = time.monotonic() + self._backoff
            self._backoff = min(self._backoff * 2, BACKOFF_MAX_SECONDS)
        else:
            self._backoff = BACKOFF_START_SECONDS
            self._backoff_until = None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
            self._conn = None
        self._readers.close()

    # ── run a write under the writer lock ───────────────────────────────────

    def _run(self, op: str, fn: Callable[[sqlite3.Connection], T], default: T,
             key: Optional[str] = None) -> T:
        """Run ``fn`` under the lock; any sqlite error → WARNING/ERROR +
        default. BUSY/LOCKED keeps the connection and does not back off;
        other errors drop it; a corrupt file is quarantined."""
        started = time.perf_counter()
        label = _op_label(op, key)
        with self._lock:
            conn = self._connection()
            if conn is None:
                return default
            try:
                result = fn(conn)
            except sqlite3.Error as exc:
                kind = _classify(exc)
                if kind == "busy":
                    logger.warning("shared store %s failed (busy): %s", label, exc)
                    return default
                if kind == "corrupt":
                    self._quarantine_files()
                    self._drop_connection(backoff=False)
                    return default
                logger.warning("shared store %s failed: %s", label, exc)
                self._drop_connection(backoff=True)
                return default
        elapsed_ms = (time.perf_counter() - started) * 1000
        if elapsed_ms > SLOW_QUERY_MS:
            logger.info("shared store %s slow: %.1f ms", label, elapsed_ms)
        else:
            logger.debug("shared store %s %.1f ms", label, elapsed_ms)
        return result

    # ── run a read against the reader pool ──────────────────────────────────

    def _run_read(self, op: str, fn: Callable[[sqlite3.Connection], T], default: T,
                  key: Optional[str] = None) -> T:
        started = time.perf_counter()
        label = _op_label(op, key)
        conn = self._readers.acquire()
        if conn is None:
            return default
        try:
            result = fn(conn)
        except sqlite3.Error as exc:
            kind = _classify(exc)
            healthy = kind == "busy"
            logger.warning("shared store %s failed%s: %s", label,
                           " (busy)" if kind == "busy" else "", exc)
            self._readers.release(conn, healthy)
            return default
        self._readers.release(conn, True)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if elapsed_ms > SLOW_QUERY_MS:
            logger.info("shared store %s slow: %.1f ms", label, elapsed_ms)
        else:
            logger.debug("shared store %s %.1f ms", label, elapsed_ms)
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
            c.execute(_UPSERT_VERDICT_SQL, row)
            return True
        return self._run("set_verdict", op, False, key=lab_id)

    def clear_verdict(self, lab_id: str, mode: str, *, by: str) -> bool:
        """Tombstone a verdict (outcome becomes ``"cleared"``) rather than
        deleting the row, so ``verdicts_for`` can distinguish "someone
        un-marked this" from "nobody has judged this"."""
        lab_id = _require_text(lab_id, "lab_id")
        _require(mode in MODES, f"unknown mode {mode!r}")
        by = _require_text(by, "by")
        row = (lab_id, mode, "cleared", "", None, None, by, self._now())

        def op(c: sqlite3.Connection) -> bool:
            c.execute(_UPSERT_VERDICT_SQL, row)
            return True
        return self._run("clear_verdict", op, False, key=lab_id)

    def verdicts_for(self, lab_ids: Iterable[str]) -> Optional[Dict[str, Dict[str, dict]]]:
        """``{lab_id: {mode: verdict}}`` for those that have one, including
        tombstoned ("cleared") ones.

        ``None`` means the store could not be read — distinct from ``{}``
        ("nobody has judged these"), because a caller must not un-judge a
        sample just because the disk hiccupped.
        """
        raw = [str(x).strip() for x in lab_ids if x and str(x).strip()]
        ids = sorted(set(raw))
        if len(ids) > MAX_VERDICTS_INPUT:
            logger.warning("verdicts_for: input capped from %d to %d ids",
                           len(ids), MAX_VERDICTS_INPUT)
            ids = ids[:MAX_VERDICTS_INPUT]
        out: Dict[str, Dict[str, dict]] = {}
        for start in range(0, len(ids), MAX_BATCH):
            chunk = ids[start:start + MAX_BATCH]
            marks = ",".join("?" * len(chunk))
            rows = self._run_read(
                "verdicts_for",
                lambda c, chunk=chunk, marks=marks: c.execute(
                    f"SELECT * FROM verdicts WHERE lab_id IN ({marks})", chunk).fetchall(),
                None, key=f"{len(chunk)} ids")
            if rows is None:
                return None
            for r in rows:
                out.setdefault(r["lab_id"], {})[r["mode"]] = {
                    "outcome": r["outcome"], "reason": r["reason"],
                    "cc_task_id": r["cc_task_id"], "sample_id": r["sample_id"],
                    "by": r["by_user"], "at": r["at"],
                }
        return out

    def apply_mark(self, lab_id: str, mode: str, outcome: str, *, by: str,
                   reason: str = "", cc_task_id: Any = None,
                   sample_id: Any = None, tab: Optional[str] = None) -> Optional[dict]:
        """Set (or tombstone) a verdict and record the history event for it
        as one atomic transaction: either both happen or neither does.

        Returns ``{"before": <previous outcome or None>, "verdicts": {mode:
        verdict, ...}}`` for this lab_id, or ``None`` if the write failed —
        callers must not assume the mark took effect without checking.
        """
        lab_id = _require_text(lab_id, "lab_id")
        _require(mode in MODES, f"unknown mode {mode!r}")
        _require(outcome in MARK_OUTCOMES, f"unknown outcome {outcome!r}")
        by = _require_text(by, "by")
        reason = (reason or "")[:MAX_TEXT]
        now = self._now()
        cc = _as_int(cc_task_id)
        sid = _as_int(sample_id)
        stored_reason = "" if outcome == "cleared" else reason

        def op(c: sqlite3.Connection) -> dict:
            c.execute("BEGIN IMMEDIATE")
            try:
                prev_row = c.execute(
                    "SELECT outcome FROM verdicts WHERE lab_id=? AND mode=?",
                    (lab_id, mode)).fetchone()
                prev_outcome = (prev_row["outcome"]
                               if prev_row and prev_row["outcome"] != "cleared" else None)
                c.execute(_UPSERT_VERDICT_SQL,
                         (lab_id, mode, outcome, stored_reason, cc, sid, by, now))
                detail: Dict[str, Any] = {}
                if tab is not None:
                    detail["tab"] = tab
                if reason:
                    detail["reason"] = reason
                kind = "unmark" if outcome == "cleared" else "mark"
                after_text = None if outcome == "cleared" else outcome
                c.execute(
                    "INSERT INTO sample_events (lab_id, at, user, kind, field,"
                    " before, after, detail) VALUES (?,?,?,?,?,?,?,?)",
                    (lab_id, now, by, kind, mode, prev_outcome, after_text,
                     _encode_detail(detail) if detail else None))
                rows = c.execute("SELECT * FROM verdicts WHERE lab_id=?",
                                 (lab_id,)).fetchall()
                verdicts = {r["mode"]: {
                    "outcome": r["outcome"], "reason": r["reason"],
                    "cc_task_id": r["cc_task_id"], "sample_id": r["sample_id"],
                    "by": r["by_user"], "at": r["at"],
                } for r in rows}
                c.execute("COMMIT")
            except sqlite3.Error:
                try:
                    c.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            return {"before": prev_outcome, "verdicts": verdicts}
        return self._run("apply_mark", op, None, key=lab_id)

    # ── sample history ───────────────────────────────────────────────────

    def record_event(self, lab_id: str, kind: str, *, user: str,
                     field: Optional[str] = None, before: Any = None,
                     after: Any = None, detail: Any = None) -> bool:
        lab_id = _require_text(lab_id, "lab_id")
        _require(kind in EVENT_KINDS, f"unknown event kind {kind!r}")
        user = _require_text(user, "user")
        row = (lab_id, self._now(), user, kind, _as_text(field), _as_text(before),
               _as_text(after), _encode_detail(detail))

        def op(c: sqlite3.Connection) -> bool:
            c.execute("INSERT INTO sample_events (lab_id, at, user, kind, field,"
                      " before, after, detail) VALUES (?,?,?,?,?,?,?,?)", row)
            return True
        return self._run("record_event", op, False, key=lab_id)

    def history(self, lab_id: str, limit: int = 200) -> List[dict]:
        lab_id = _require_text(lab_id, "lab_id")
        limit = max(1, min(int(limit), MAX_HISTORY))

        def op(c: sqlite3.Connection) -> List[dict]:
            rows = c.execute(
                "SELECT * FROM sample_events WHERE lab_id=?"
                " ORDER BY at DESC, id DESC LIMIT ?", (lab_id, limit)).fetchall()
            return [self._event_dict(r) for r in rows]
        return self._run_read("history", op, [], key=lab_id)

    def events_between(self, start: float, end: float,
                       limit: int = MAX_RANGE_ROWS) -> List[dict]:
        limit = max(1, min(int(limit), MAX_RANGE_ROWS))

        def op(c: sqlite3.Connection) -> List[dict]:
            rows = c.execute(
                "SELECT * FROM sample_events WHERE at >= ? AND at < ?"
                " ORDER BY at, id LIMIT ?", (float(start), float(end), limit)).fetchall()
            return [self._event_dict(r) for r in rows]
        rows = self._run_read("events_between", op, [])
        if len(rows) == limit:
            logger.warning("events_between: result truncated at %d rows (MAX_RANGE_ROWS)",
                           limit)
        return rows

    def event_marks_between(self, start: float, end: float,
                            limit: int = MAX_RANGE_ROWS) -> List[dict]:
        """Like ``events_between`` but only ``user``/``at``/``kind`` — the
        slim shape the Time Online day builder needs, without paying to
        decode every ``detail`` JSON blob for a day with a lot of activity."""
        limit = max(1, min(int(limit), MAX_RANGE_ROWS))

        def op(c: sqlite3.Connection) -> List[dict]:
            rows = c.execute(
                "SELECT user, at, kind FROM sample_events WHERE at >= ? AND at < ?"
                " ORDER BY at, id LIMIT ?", (float(start), float(end), limit)).fetchall()
            return [{"user": r["user"], "at": r["at"], "kind": r["kind"]} for r in rows]
        rows = self._run_read("event_marks_between", op, [])
        if len(rows) == limit:
            logger.warning("event_marks_between: result truncated at %d rows"
                           " (MAX_RANGE_ROWS)", limit)
        return rows

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
        return self._run("open_span", op, None, key=user)

    def touch_span(self, span_id: int, last_seen: float) -> bool:
        def op(c: sqlite3.Connection) -> bool:
            cur = c.execute("UPDATE presence SET last_seen=? WHERE id=? AND ended_at IS NULL",
                            (float(last_seen), int(span_id)))
            return cur.rowcount == 1
        return self._run("touch_span", op, False, key=str(span_id))

    def close_span(self, span_id: int, ended_at: float, reason: str) -> bool:
        _require(reason in SPAN_END_REASONS, f"unknown end reason {reason!r}")

        def op(c: sqlite3.Connection) -> bool:
            cur = c.execute(
                "UPDATE presence SET last_seen=MAX(last_seen, ?),"
                " ended_at=MAX(last_seen, ?), end_reason=?"
                " WHERE id=? AND ended_at IS NULL",
                (float(ended_at), float(ended_at), reason, int(span_id)))
            return cur.rowcount == 1
        return self._run("close_span", op, False, key=str(span_id))

    def close_open_spans(self, reason: str) -> int:
        """Close every span left open (a process that died mid-span) at its
        last heartbeat. Returns how many were closed."""
        _require(reason in SPAN_END_REASONS, f"unknown end reason {reason!r}")

        def op(c: sqlite3.Connection) -> int:
            cur = c.execute("UPDATE presence SET ended_at=last_seen, end_reason=?"
                            " WHERE ended_at IS NULL", (reason,))
            return int(cur.rowcount or 0)
        return self._run("close_open_spans", op, 0, key=reason)

    def apply_presence(self, ops: List[dict]) -> Optional[List[Any]]:
        """Apply a batch of presence mutations in one transaction, for
        ``PresenceTracker.flush()``. Each item is
        ``{"op": "open", "user":..., "started":..., "last_seen":...}``,
        ``{"op": "touch", "span_id":..., "last_seen":...}`` or
        ``{"op": "close", "span_id":..., "ended_at":..., "reason":...}``.
        A ``"close"`` (or ``"touch"``) may use ``"span_id": "$prev"`` to mean
        "the id the immediately preceding op in this same batch produced" —
        how a span that opened and closed before ever reaching the store
        (nobody flushed in between) gets both halves written atomically in
        one flush instead of being silently dropped.

        Returns a list of results parallel to ``ops`` (the new span id for
        ``"open"``, a bool for ``"touch"``/``"close"``), or ``None`` if the
        whole batch failed — nothing in it was applied, so the caller
        should treat every item as still pending.
        """
        batch = list(ops)[:MAX_HISTORY * 3]   # bounded; callers cap far below this

        def op(c: sqlite3.Connection) -> List[Any]:
            c.execute("BEGIN IMMEDIATE")
            results: List[Any] = []
            try:
                for item in batch:
                    kind = item.get("op")
                    if kind == "open":
                        cur = c.execute(
                            "INSERT INTO presence (user, started_at, last_seen)"
                            " VALUES (?,?,?)",
                            (_require_text(item["user"], "user"),
                             float(item["started"]), float(item["last_seen"])))
                        results.append(int(cur.lastrowid))
                    elif kind == "touch":
                        span_id = self._resolve_span_id(item["span_id"], results)
                        cur = c.execute(
                            "UPDATE presence SET last_seen=? WHERE id=? AND ended_at IS NULL",
                            (float(item["last_seen"]), span_id))
                        results.append(cur.rowcount == 1)
                    elif kind == "close":
                        reason = item["reason"]
                        _require(reason in SPAN_END_REASONS,
                                f"unknown end reason {reason!r}")
                        span_id = self._resolve_span_id(item["span_id"], results)
                        cur = c.execute(
                            "UPDATE presence SET last_seen=MAX(last_seen, ?),"
                            " ended_at=MAX(last_seen, ?), end_reason=?"
                            " WHERE id=? AND ended_at IS NULL",
                            (float(item["ended_at"]), float(item["ended_at"]), reason,
                             span_id))
                        results.append(cur.rowcount == 1)
                    else:
                        raise ValueError(f"unknown presence op {kind!r}")
                c.execute("COMMIT")
            except sqlite3.Error:
                try:
                    c.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            return results
        return self._run("apply_presence", op, None, key=f"{len(batch)} ops")

    @staticmethod
    def _resolve_span_id(raw: Any, results: List[Any]) -> int:
        if raw == "$prev":
            _require(bool(results) and isinstance(results[-1], int),
                     "'$prev' span_id with no preceding open in this batch")
            return int(results[-1])
        return int(raw)

    def spans_between(self, start: float, end: float,
                      limit: int = MAX_RANGE_ROWS) -> List[dict]:
        limit = max(1, min(int(limit), MAX_RANGE_ROWS))

        def op(c: sqlite3.Connection) -> List[dict]:
            rows = c.execute(
                "SELECT * FROM presence WHERE started_at >= ? AND started_at < ?"
                " AND COALESCE(ended_at, last_seen) >= ?"
                " ORDER BY started_at, id LIMIT ?",
                (float(start) - MAX_SPAN_SECONDS, float(end), float(start),
                 limit)).fetchall()
            return [{"id": r["id"], "user": r["user"], "start": r["started_at"],
                     "end": r["ended_at"] if r["ended_at"] is not None else r["last_seen"],
                     "open": r["ended_at"] is None, "end_reason": r["end_reason"]}
                    for r in rows]
        rows = self._run_read("spans_between", op, [])
        if len(rows) == limit:
            logger.warning("spans_between: result truncated at %d rows (MAX_RANGE_ROWS)",
                           limit)
        return rows

    def recorded_range(self) -> Tuple[Optional[float], Optional[float]]:
        """Earliest and latest instant anything was recorded, for the day picker."""
        def op(c: sqlite3.Connection) -> Tuple[Optional[float], Optional[float]]:
            lo_p = c.execute("SELECT MIN(started_at) FROM presence").fetchone()[0]
            hi_p = c.execute("SELECT MAX(last_seen) FROM presence").fetchone()[0]
            lo_e = c.execute("SELECT MIN(at) FROM sample_events").fetchone()[0]
            hi_e = c.execute("SELECT MAX(at) FROM sample_events").fetchone()[0]
            lows = [x for x in (lo_p, lo_e) if x is not None]
            highs = [x for x in (hi_p, hi_e) if x is not None]
            return (min(lows) if lows else None, max(highs) if highs else None)
        return self._run_read("recorded_range", op, (None, None))

    # ── meta ─────────────────────────────────────────────────────────────

    def get_meta(self, key: str) -> Optional[str]:
        def op(c: sqlite3.Connection) -> Optional[str]:
            r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return r[0] if r else None
        return self._run("get_meta", op, None, key=key)

    def set_meta(self, key: str, value: str) -> bool:
        def op(c: sqlite3.Connection) -> bool:
            c.execute("INSERT INTO meta (key, value) VALUES (?, ?)"
                      " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, str(value)))
            return True
        return self._run("set_meta", op, False, key=key)
