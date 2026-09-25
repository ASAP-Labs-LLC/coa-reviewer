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

import hashlib
import json
import logging
import queue
import re
import sqlite3
import threading
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, TypeVar

logger = logging.getLogger("coa.shared_store")

MODES = ("info", "tests")
OUTCOMES = ("good", "bad")                       # what a caller may *set*
MARK_OUTCOMES = ("good", "bad", "cleared")        # what apply_mark accepts
EVENT_KINDS = (
    "mark", "unmark", "test_result", "sample_info", "sample_sync",
    "comments", "attachment_deleted", "listing_created", "listing_completed",
    "external_change",
)
# Where an observed value came from (field_snapshots.source).
SOURCES = ("qbench_test", "qbench_info", "qbench_comments", "labvision_test")
# Sources compared numerically (12 == 12.00). Test results are measurements
# whose written precision matters, so they compare as exact normalised text.
NUMERIC_SOURCES = ("qbench_info",)
EXTERNAL_ACTOR = "Outside COA Reviewer"
# What observe()/observe_many() return when asked not to wait for a busy writer.
WRITER_BUSY = "writer-busy"
SPAN_END_REASONS = ("logout", "timeout", "gap", "restart", "midnight")

MAX_HISTORY = 500          # rows one History tab can show
MAX_RANGE_ROWS = 5000      # rows one /activity day can use; len==limit means truncated
MAX_BATCH = 900            # bound parameters per IN (SQLite limit is 999)
MAX_TEXT = 4000            # characters kept per before/after/detail value
MAX_VERDICTS_INPUT = 10_000  # lab_ids accepted by one verdicts_for() call
MAX_MARKS_PER_BATCH = 500    # marks written by one apply_marks() transaction
MAX_EVENTS_PER_BATCH = 500   # history rows written by one record_events() transaction
MAX_OBSERVE_FIELDS = 500     # fields compared by one observe() call
MAX_FIELD_NAME = 200         # characters kept of a snapshot's field name
MAX_NUMERIC_TEXT = 64        # longer strings are never compared as numbers
SNAPSHOT_REFRESH_SECONDS = 3600.0  # unchanged fields refresh seen_at at most hourly
MAX_OBSERVE_ITEMS = 2000     # (lab_id, source) items one observe_many() call takes
MAX_OBSERVE_READ_ROWS = 200_000  # snapshot rows one observe_many() read may return
CHANGED_AT_SLACK_SECONDS = 60.0  # clock skew allowed around [since, detected_at]
MAX_PRUNE_ROWS = 10_000      # snapshot rows one prune_snapshots() call may delete
_WHEN_POLICIES = ("always", "if_judged", "if_absent")
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
CREATE TABLE IF NOT EXISTS field_snapshots (
    lab_id  TEXT NOT NULL,
    source  TEXT NOT NULL,
    field   TEXT NOT NULL,
    value   TEXT,
    seen_at REAL NOT NULL,
    digest  TEXT,
    PRIMARY KEY (lab_id, source, field)
);
CREATE INDEX IF NOT EXISTS ix_snapshots_seen ON field_snapshots(seen_at);
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

# apply_mark's upsert: a mark only replaces a verdict that is not newer than
# it, so a late retry of a failed write can never overwrite newer work.
_UPSERT_VERDICT_IF_NEWER_SQL = _UPSERT_VERDICT_SQL + " WHERE verdicts.at <= excluded.at"

_UPSERT_SNAPSHOT_SQL = (
    "INSERT INTO field_snapshots (lab_id, source, field, value, digest, seen_at)"
    " VALUES (?,?,?,?,?,?) ON CONFLICT(lab_id, source, field) DO UPDATE SET"
    " value=excluded.value, digest=excluded.digest, seen_at=excluded.seen_at"
)
_INSERT_EVENT_SQL = (
    "INSERT INTO sample_events (lab_id, at, user, kind, field,"
    " before, after, detail) VALUES (?,?,?,?,?,?,?,?)"
)

T = TypeVar("T")

# ── sqlite error classification ─────────────────────────────────────────────
# BUSY/LOCKED is contention, not damage: keep the connection, no backoff.
# CONSTRAINT/MISUSE/RANGE/a plain SQLITE_ERROR are a bad query, not file
# damage or contention either — also keep the connection, no backoff ("keep").
# IOERR/CANTOPEN and "no such table" mean the connection is no good any more.
# CORRUPT/NOTADB additionally means the *file* is no good — quarantine it.
#
# Primary result codes (low byte of ``sqlite_errorcode``, Python >= 3.11);
# see https://www.sqlite.org/rescode.html. Checked first because it's exact;
# the message-text fallback below is for older interpreters or odd drivers.
_SQLITE_ERROR = 1
_SQLITE_BUSY = 5
_SQLITE_LOCKED = 6
_SQLITE_IOERR = 10
_SQLITE_CORRUPT = 11
_SQLITE_CANTOPEN = 14
_SQLITE_CONSTRAINT = 19
_SQLITE_MISMATCH = 20
_SQLITE_MISUSE = 21
_SQLITE_RANGE = 25
_SQLITE_NOTADB = 26

_BUSY_CODES = {_SQLITE_BUSY, _SQLITE_LOCKED}
_CORRUPT_CODES = {_SQLITE_CORRUPT, _SQLITE_NOTADB}
_DROP_CODES = {_SQLITE_IOERR, _SQLITE_CANTOPEN}
_KEEP_CODES = {_SQLITE_ERROR, _SQLITE_CONSTRAINT, _SQLITE_MISMATCH, _SQLITE_MISUSE,
              _SQLITE_RANGE}


def _classify(exc: sqlite3.Error) -> str:
    """Return ``"busy"``, ``"keep"``, ``"corrupt"`` or ``"drop"`` for how to
    react. ``"no such table"`` always drops regardless of code — it means
    the schema is missing, which a plain SQLITE_ERROR code doesn't
    distinguish from a one-off bad query."""
    text = str(exc).lower()
    if "no such table" in text:
        return "drop"
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        primary = code & 0xFF
        if primary in _BUSY_CODES:
            return "busy"
        if primary in _CORRUPT_CODES:
            return "corrupt"
        if primary in _DROP_CODES:
            return "drop"
        if primary in _KEEP_CODES:
            return "keep"
    if "locked" in text or "busy" in text:
        return "busy"
    if "malformed" in text or "not a database" in text or "corrupt" in text:
        return "corrupt"
    if "constraint" in text or "misuse" in text or "range" in text:
        return "keep"
    return "drop"   # unrecognised: be conservative and reopen


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _require_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    _require(bool(text), f"{name} must be a non-empty string")
    return text


def _require_float(value: Any, name: str) -> float:
    """Coerce to ``float`` or raise ``ValueError`` — used to validate a
    read's numeric args *before* acquiring a reader, so a bad argument
    never ties up (and never even touches) the pool."""
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number, got {value!r}") from exc


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


# Plain decimals only: no exponent, no "nan"/"inf", no "1_000" — Python's
# float() accepts all of those, and "1e3" silently equalling "1000" would hide
# a real edit to a text field.
_DECIMAL_RE = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)")


def _full_text(value: Any) -> str:
    """The whole value as text (not bounded — see ``_digest``)."""
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, default=str)


def _comparable(value: Any, numeric: bool = True) -> Tuple[str, str]:
    """The comparison key for an observed value (never what is stored).

    Stripped, internal whitespace collapsed; ``None`` and blank are the same.
    With ``numeric``, a plain decimal (≤ ``MAX_NUMERIC_TEXT`` chars) compares
    as an exact ``Decimal``, so ``12`` == ``12.00`` but ``0.1`` !=
    ``0.10000000000000000001``. Without it the text must match exactly."""
    text = " ".join(_full_text(value).split())
    if numeric and len(text) <= MAX_NUMERIC_TEXT and _DECIMAL_RE.fullmatch(text):
        try:
            number = Decimal(text)
        except InvalidOperation:   # pragma: no cover - the regex rules it out
            return ("s", text)
        return ("n", "0" if number == 0 else str(number.normalize()))
    return ("s", text)


def same_value(a: Any, b: Any, numeric: bool = True) -> bool:
    """Whether two observed values are the same after normalisation."""
    return _comparable(a, numeric) == _comparable(b, numeric)


def _digest(value: Any, source: str) -> str:
    """sha256 of the whole normalised value. The ``value`` column keeps only
    ``MAX_TEXT`` characters for display; comparing digests means a change
    past that point is still seen."""
    kind, canon = _comparable(value, source in NUMERIC_SOURCES)
    return hashlib.sha256(f"{kind}:{canon}".encode("utf-8")).hexdigest()


def _snapshot_row(lab_id: str, source: str, field: str, value: Any,
                  seen_at: float) -> tuple:
    return (lab_id, source, field, _as_text(value), _digest(value, source), seen_at)


def _check_snapshot(snap: Any) -> Tuple[str, str, Any]:
    """Validate a ``(source, field, value)`` snapshot."""
    _require(isinstance(snap, (tuple, list)) and len(snap) == 3,
             f"snapshot must be (source, field, value), got {snap!r}")
    source, field, value = snap
    _require(source in SOURCES, f"unknown snapshot source {source!r}")
    return source, _require_text(field, "snapshot field")[:MAX_FIELD_NAME], value


def _snapshot_list(snapshot: Any) -> List[Tuple[str, str, Any]]:
    """``None``, one ``(source, field, value)`` or a list of them."""
    if snapshot is None:
        return []
    if isinstance(snapshot, tuple):
        return [_check_snapshot(snapshot)]
    _require(isinstance(snapshot, list) and len(snapshot) <= MAX_OBSERVE_FIELDS,
             "snapshot must be a tuple or a bounded list of tuples")
    return [_check_snapshot(s) for s in snapshot]


def _parse_when(value: Any) -> Optional[float]:
    """An epoch number or an ISO-ish timestamp (naive = local time) as
    epoch seconds; ``None`` if it cannot be read."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value or "").strip()
    if not text or len(text) > 64:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _ensure_snapshot_digest(conn: sqlite3.Connection) -> None:
    """A database created before ``digest`` existed gets the column (NULL
    digests are computed from ``value`` when compared)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(field_snapshots)").fetchall()}
    if "digest" in cols:
        return
    try:
        conn.execute("ALTER TABLE field_snapshots ADD COLUMN digest TEXT")
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():   # another connection won
            raise


def _op_label(op: str, key: Optional[str]) -> str:
    return f"{op}[{key}]" if key else op


def _validate_presence_span_id(raw: Any, prev_kind: Optional[str]) -> None:
    if raw == "$prev":
        _require(prev_kind == "open",
                 "'$prev' span_id must immediately follow an 'open' op in the batch")
        return
    _require(isinstance(raw, int) and not isinstance(raw, bool),
             f"span_id must be an int or '$prev', got {raw!r}")


def _validate_presence_batch(batch: List[dict]) -> None:
    """Validate every op in an ``apply_presence`` batch before any write
    begins (see that method's docstring for why). Raises ``ValueError`` on
    the first problem found."""
    prev_kind: Optional[str] = None
    for item in batch:
        if not isinstance(item, dict):
            raise ValueError(f"presence op must be a dict, got {item!r}")
        kind = item.get("op")
        try:
            if kind == "open":
                _require_text(item.get("user"), "user")
                float(item["started"])
                float(item["last_seen"])
            elif kind == "touch":
                _validate_presence_span_id(item.get("span_id"), prev_kind)
                float(item["last_seen"])
            elif kind == "close":
                _validate_presence_span_id(item.get("span_id"), prev_kind)
                _require(item.get("reason") in SPAN_END_REASONS,
                         f"unknown end reason {item.get('reason')!r}")
                float(item["ended_at"])
            else:
                raise ValueError(f"unknown presence op {kind!r}")
        except KeyError as exc:
            raise ValueError(f"presence op {item!r} missing field {exc}") from exc
        except TypeError as exc:
            raise ValueError(f"presence op {item!r} has an invalid field: {exc}") from exc
        prev_kind = kind


class _ReaderPool:
    """A small, bounded pool of read-only connections so reads never queue
    behind the single writer lock. Opened lazily; each holds
    ``PRAGMA query_only=1`` so a bug here can't accidentally write.

    Every connection is tagged (by identity, since ``sqlite3.Connection``
    can't carry extra attributes) with the pool's *generation* at the time
    it was opened. ``invalidate_idle()`` — called before a corrupt file is
    renamed away — bumps the generation and closes every currently-idle
    connection; one still checked out by another thread at that moment is
    closed instead of recycled the next time it's released, so nothing
    keeps a handle open on the file that's about to move.
    """

    def __init__(self, path: Path, backoff_active: Callable[[], bool]) -> None:
        self._path = path
        self._backoff_active = backoff_active
        self._pool: "queue.Queue[sqlite3.Connection]" = queue.Queue(maxsize=READER_POOL_SIZE)
        self._created = 0
        self._create_lock = threading.Lock()
        self._generation = 0
        self._gen_of: Dict[int, int] = {}   # id(conn) -> generation it was opened in

    def _open(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), timeout=CONNECT_TIMEOUT_SECONDS,
                               check_same_thread=False, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            conn.executescript(_SCHEMA)   # idempotent: guarantees tables exist
            _ensure_snapshot_digest(conn)
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
                if self._backoff_active():
                    # the writer can't open this file right now either;
                    # don't pile on with more failing connect attempts.
                    logger.debug("shared store reader %s: writer backoff active,"
                                " not opening", self._path)
                    return None
                try:
                    conn = self._open()
                except (sqlite3.Error, OSError) as exc:
                    logger.warning("shared store unavailable at %s: %s", self._path, exc)
                    return None
                self._created += 1
                self._gen_of[id(conn)] = self._generation
                return conn
        try:
            return self._pool.get(timeout=READER_ACQUIRE_TIMEOUT_SECONDS)
        except queue.Empty:
            logger.warning("shared store reader pool exhausted at %s", self._path)
            return None

    def release(self, conn: sqlite3.Connection, healthy: bool) -> None:
        stale = self._gen_of.get(id(conn)) != self._generation
        if healthy and not stale:
            try:
                self._pool.put_nowait(conn)
                return
            except queue.Full:  # pragma: no cover - pool sized to _created
                pass
        self._close_one(conn)

    def _close_one(self, conn: sqlite3.Connection) -> None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        self._gen_of.pop(id(conn), None)
        with self._create_lock:
            self._created = max(0, self._created - 1)

    def invalidate_idle(self) -> None:
        """Bump the generation and close every idle (pooled) connection.
        A connection checked out right now finishes its read normally but
        is closed rather than recycled when ``release()`` sees its stale
        generation."""
        with self._create_lock:
            self._generation += 1
        while True:
            try:
                conn = self._pool.get_nowait()
            except queue.Empty:
                break
            self._close_one(conn)

    def close(self) -> None:
        self.invalidate_idle()
        with self._create_lock:
            self._created = 0
        self._gen_of.clear()


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
        self._quarantine_disabled = False
        self._readers = _ReaderPool(self._path, backoff_active=self._writer_backing_off)
        # Why the most recent write failed: None (it succeeded), "unavailable"
        # (no connection / backing off), "busy", "keep" (a bad query — the
        # store itself is fine), "corrupt" or "io". Process-wide and racy
        # across threads by nature; a hint for retry policy, not a contract.
        self._last_write_error: Optional[str] = None

    @property
    def last_write_error(self) -> Optional[str]:
        return self._last_write_error

    def _writer_backing_off(self) -> bool:
        return (self._backoff_until is not None
                and time.monotonic() < self._backoff_until)

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
            _ensure_snapshot_digest(conn)
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
        immediately (bounded: at most two attempts) — unless quarantine
        has already failed once this process, in which case it degrades
        to the normal backoff instead of retrying a rename that will just
        fail again."""
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
                if _classify(exc) == "corrupt" and self._quarantine_files():
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

    def _quarantine_files(self) -> bool:
        """Rename the corrupt db file (and ``-wal``/``-shm``) aside so a
        fresh one can take its place on the next open.

        Order matters: our own writer handle is dropped *before* this is
        called (see callers), and readers' idle handles are dropped here
        via ``invalidate_idle()`` — on Windows a rename fails (WinError 32)
        while any handle is still open on the file, so every handle we
        control has to be gone first.

        Returns ``False`` if the main file could not be renamed. After
        that this process stops retrying quarantine (``_quarantine_disabled``)
        and simply degrades to "unavailable" with the normal exponential
        backoff instead of hammering a rename that will keep failing.
        """
        if self._quarantine_disabled:
            return False
        self._readers.invalidate_idle()
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        logger.error("shared store at %s is corrupt; quarantining (tag %s)",
                    self._path, ts)
        main_ok = True
        for suffix in ("", "-wal", "-shm"):
            src = Path(f"{self._path}{suffix}")
            if not src.exists():
                continue
            dest = Path(f"{self._path}.corrupt-{ts}{suffix}")
            try:
                src.rename(dest)
            except OSError as exc:
                logger.warning("shared store: could not quarantine %s: %s", src, exc)
                if suffix == "":
                    main_ok = False
        if not main_ok:
            self._quarantine_disabled = True
            logger.error("shared store at %s: quarantine failed; giving up on"
                        " automatic recovery for this process (falling back to"
                        " the normal unavailable/backoff path instead of"
                        " retrying the rename)", self._path)
        return main_ok

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
             key: Optional[str] = None, *, wait: bool = True, busy: Any = None) -> T:
        """Run ``fn`` under the writer lock; any sqlite error → WARNING/ERROR +
        default. With ``wait=False`` a writer already in use by another
        thread returns ``busy`` at once instead of queueing behind it (a
        request thread must never block on history).

        BUSY/LOCKED/a bad-query error (CONSTRAINT/MISUSE/RANGE/a plain
        SQLITE_ERROR) keeps the connection and does not back off; an I/O
        error drops it; a corrupt file is quarantined (writer dropped first,
        so our own handle can't block the rename)."""
        started = time.perf_counter()
        label = _op_label(op, key)
        if not self._lock.acquire(blocking=wait):
            logger.debug("shared store %s: writer busy, not waiting", label)
            return busy
        try:
            ok, result = self._run_locked(label, fn, default)
        finally:
            self._lock.release()
        if not ok:
            return result
        elapsed_ms = (time.perf_counter() - started) * 1000
        if elapsed_ms > SLOW_QUERY_MS:
            logger.info("shared store %s slow: %.1f ms", label, elapsed_ms)
        else:
            logger.debug("shared store %s %.1f ms", label, elapsed_ms)
        return result

    def _run_locked(self, label: str, fn: Callable[[sqlite3.Connection], T],
                    default: T) -> Tuple[bool, T]:
        """The body of ``_run``; caller holds ``_lock``. ``(ok, result)``."""
        conn = self._connection()
        if conn is None:
            self._last_write_error = "unavailable"
            return False, default
        try:
            result = fn(conn)
        except sqlite3.Error as exc:
            kind = _classify(exc)
            self._last_write_error = kind if kind in ("busy", "keep", "corrupt") else "io"
            if kind in ("busy", "keep"):
                logger.warning("shared store %s failed (%s): %s", label, kind, exc)
                # The connection is kept, so it must not be left inside
                # a transaction ``fn`` opened: every later write would
                # silently vanish into it.
                try:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                except sqlite3.Error as rb_exc:
                    logger.warning("shared store %s rollback failed: %s", label, rb_exc)
                return False, default
            if kind == "corrupt":
                self._drop_connection(backoff=False)   # our handle first
                if self._quarantine_files():
                    self._backoff = BACKOFF_START_SECONDS
                    self._backoff_until = None
                else:
                    self._fail_open(exc)
                return False, default
            logger.warning("shared store %s failed: %s", label, exc)
            self._drop_connection(backoff=True)
            return False, default
        self._last_write_error = None
        return True, result

    # ── run a read against the reader pool ──────────────────────────────────

    def _run_read(self, op: str, fn: Callable[[sqlite3.Connection], T], default: T,
                  key: Optional[str] = None) -> T:
        """Run ``fn`` against a pooled reader. The reader is *always*
        released — via ``finally`` — even if ``fn`` raises something that
        isn't a ``sqlite3.Error`` (a programming bug): three such leaks
        used to exhaust the pool, after which every read would wait out
        ``READER_ACQUIRE_TIMEOUT_SECONDS`` and return the default forever.
        ``healthy`` defaults to ``False`` (close, don't recycle) and is
        only set ``True`` on success or contention (``busy``/``keep``)."""
        started = time.perf_counter()
        label = _op_label(op, key)
        conn = self._readers.acquire()
        if conn is None:
            return default
        healthy = False
        succeeded = False
        result = default
        try:
            result = fn(conn)
            healthy = True
            succeeded = True
        except sqlite3.Error as exc:
            kind = _classify(exc)
            healthy = kind in ("busy", "keep")
            logger.warning("shared store %s failed%s: %s", label,
                           f" ({kind})" if kind in ("busy", "keep") else "", exc)
        finally:
            self._readers.release(conn, healthy)
        if not succeeded:
            return default
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
                   sample_id: Any = None, tab: Optional[str] = None,
                   at: Optional[float] = None,
                   extra_detail: Optional[Dict[str, Any]] = None) -> Optional[dict]:
        """Set (or tombstone) a verdict and record the history event for it
        as one atomic transaction: either both happen or neither does.

        ``at`` is when the mark was made (default now). A retried write
        passes its original time: if a newer verdict already exists it is
        left alone, but the history row is still written at ``at`` with
        ``detail.superseded = true``. ``extra_detail`` (e.g. ``{"cause":
        "regenerate"}``) is merged into the history row's detail.

        Returns ``{"before": <previous outcome or None>, "applied": bool,
        "skipped": False, "verdicts": {mode: verdict, ...}}`` for this
        lab_id, or ``None`` if the write failed — callers must not assume
        the mark took effect without checking.
        """
        item = self._prepare_mark({
            "lab_id": lab_id, "mode": mode, "outcome": outcome, "by": by,
            "reason": reason, "cc_task_id": cc_task_id, "sample_id": sample_id,
            "tab": tab, "at": at, "extra_detail": extra_detail})
        out = self._run("apply_mark", lambda c: self._marks_tx(c, [item]), None,
                        key=item["row"][0])
        return out[0] if out is not None else None

    def apply_marks(self, items: List[dict]) -> Optional[List[dict]]:
        """Several marks in ONE transaction (at most ``MAX_MARKS_PER_BATCH``),
        each with apply_mark's semantics plus an optional ``when``:

        - ``"always"`` (default) — as apply_mark;
        - ``"if_judged"`` — only if the current row is good/bad (a clear
          that has nothing to clear writes nothing, not even history);
        - ``"if_absent"`` — only if there is no row at all (a tombstone
          counts as a row): insert-only, for the ledger migration.

        Every item is validated before anything is written. Returns one
        result per item (``skipped`` True for a ``when`` that did not hold),
        or ``None`` if the write failed (then nothing was written)."""
        _require(isinstance(items, list), "items must be a list")
        _require(len(items) <= MAX_MARKS_PER_BATCH,
                 f"at most {MAX_MARKS_PER_BATCH} marks per batch, got {len(items)}")
        prepared = [self._prepare_mark(it) for it in items]
        if not prepared:
            return []
        return self._run("apply_marks", lambda c: self._marks_tx(c, prepared), None,
                         key=f"{len(prepared)} marks")

    def _prepare_mark(self, it: dict) -> dict:
        """Validate one mark and build its verdict row and history detail."""
        _require(isinstance(it, dict), "a mark must be a dict")
        lab_id = _require_text(it.get("lab_id"), "lab_id")
        mode, outcome = it.get("mode"), it.get("outcome")
        _require(mode in MODES, f"unknown mode {mode!r}")
        _require(outcome in MARK_OUTCOMES, f"unknown outcome {outcome!r}")
        by = _require_text(it.get("by"), "by")
        when_policy = it.get("when") or "always"
        _require(when_policy in _WHEN_POLICIES, f"unknown when {when_policy!r}")
        at = it.get("at")
        when = self._now() if at is None else _require_float(at, "at")
        reason = (it.get("reason") or "")[:MAX_TEXT]
        extra = it.get("extra_detail")
        _require(extra is None or isinstance(extra, dict), "extra_detail must be a dict")
        row = (lab_id, mode, outcome, "" if outcome == "cleared" else reason,
               _as_int(it.get("cc_task_id")), _as_int(it.get("sample_id")), by, when)
        detail: Dict[str, Any] = {}
        if it.get("tab") is not None:
            detail["tab"] = it.get("tab")
        if reason:
            detail["reason"] = reason
        detail.update(extra or {})
        return {"row": row, "detail": detail, "when": when_policy}

    def _marks_tx(self, c: sqlite3.Connection, prepared: List[dict]) -> List[dict]:
        c.execute("BEGIN IMMEDIATE")
        try:
            results = [self._apply_mark_tx(c, p["row"], dict(p["detail"]), p["when"])
                       for p in prepared]
            c.execute("COMMIT")
        except BaseException:
            # Not just sqlite3.Error: a ValueError from a bug in this block
            # must not leave the writer sitting mid-transaction — every
            # later write would silently vanish into it.
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return results

    @staticmethod
    def _verdict_rows(c: sqlite3.Connection, lab_id: str) -> Dict[str, dict]:
        rows = c.execute("SELECT * FROM verdicts WHERE lab_id=?", (lab_id,)).fetchall()
        return {r["mode"]: {
            "outcome": r["outcome"], "reason": r["reason"],
            "cc_task_id": r["cc_task_id"], "sample_id": r["sample_id"],
            "by": r["by_user"], "at": r["at"],
        } for r in rows}

    @classmethod
    def _apply_mark_tx(cls, c: sqlite3.Connection, row: tuple, detail: Dict[str, Any],
                       when_policy: str = "always") -> dict:
        """One mark inside a transaction the caller owns."""
        lab_id, mode, outcome, by, when = row[0], row[1], row[2], row[6], row[7]
        prev_row = c.execute("SELECT outcome, at FROM verdicts WHERE lab_id=? AND mode=?",
                             (lab_id, mode)).fetchone()
        judged = prev_row is not None and prev_row["outcome"] in OUTCOMES
        if (when_policy == "if_judged" and not judged) or \
                (when_policy == "if_absent" and prev_row is not None):
            return {"before": prev_row["outcome"] if judged else None, "applied": False,
                    "skipped": True, "verdicts": cls._verdict_rows(c, lab_id)}
        applied = prev_row is None or prev_row["at"] <= when
        if applied:
            prev_outcome = prev_row["outcome"] if judged else None
            c.execute(_UPSERT_VERDICT_IF_NEWER_SQL, row)
        else:
            # Superseded: "before" is what was in force at ``when`` — the
            # last mark/unmark at or before it (one indexed lookup).
            last = c.execute(
                "SELECT after FROM sample_events WHERE lab_id=? AND field=?"
                " AND kind IN ('mark','unmark') AND at<=?"
                " ORDER BY at DESC, id DESC LIMIT 1", (lab_id, mode, when)).fetchone()
            prev_outcome = last["after"] if last else None
            detail["superseded"] = True
        c.execute(
            "INSERT INTO sample_events (lab_id, at, user, kind, field,"
            " before, after, detail) VALUES (?,?,?,?,?,?,?,?)",
            (lab_id, when, by, "unmark" if outcome == "cleared" else "mark", mode,
             prev_outcome, None if outcome == "cleared" else outcome,
             _encode_detail(detail) if detail else None))
        return {"before": prev_outcome, "applied": applied, "skipped": False,
                "verdicts": cls._verdict_rows(c, lab_id)}

    # ── sample history ───────────────────────────────────────────────────

    def record_event(self, lab_id: str, kind: str, *, user: str,
                     field: Optional[str] = None, before: Any = None,
                     after: Any = None, detail: Any = None,
                     snapshot: Any = None) -> bool:
        """One history row. ``snapshot`` — ``(source, field, value)`` or a
        list of them — records what COA Reviewer just wrote, in the same
        transaction, so the next read of that field does not report this
        edit as a change made outside COA Reviewer."""
        return self.record_events(lab_id, [{
            "kind": kind, "user": user, "field": field, "before": before,
            "after": after, "detail": detail, "snapshot": snapshot}])

    def record_events(self, lab_id: str, events: List[dict]) -> bool:
        """Several history rows (and their snapshots) for one lab id in ONE
        transaction — a Sample Info save of 20 fields is one write, not 20.
        Everything is validated before anything is written."""
        lab_id = _require_text(lab_id, "lab_id")
        _require(isinstance(events, list), "events must be a list")
        _require(len(events) <= MAX_EVENTS_PER_BATCH,
                 f"at most {MAX_EVENTS_PER_BATCH} events per batch, got {len(events)}")
        now = self._now()
        rows: List[tuple] = []
        snaps: List[tuple] = []
        for ev in events:
            _require(isinstance(ev, dict), "an event must be a dict")
            kind = ev.get("kind")
            _require(kind in EVENT_KINDS, f"unknown event kind {kind!r}")
            rows.append((lab_id, now, _require_text(ev.get("user"), "user"), kind,
                         _as_text(ev.get("field")), _as_text(ev.get("before")),
                         _as_text(ev.get("after")), _encode_detail(ev.get("detail"))))
            snaps.extend(_snapshot_row(lab_id, src, fld, val, now)
                         for src, fld, val in _snapshot_list(ev.get("snapshot")))
        if not rows:
            return True

        def op(c: sqlite3.Connection) -> bool:
            self._in_tx(c, lambda: (c.executemany(_INSERT_EVENT_SQL, rows),
                                    c.executemany(_UPSERT_SNAPSHOT_SQL, snaps)))
            return True
        return self._run("record_events", op, False, key=f"{lab_id} x{len(rows)}")

    @staticmethod
    def _in_tx(c: sqlite3.Connection, body: Callable[[], T]) -> T:
        """Run ``body`` inside BEGIN IMMEDIATE … COMMIT, rolling back on any
        failure (not just sqlite3.Error) so the writer is never left
        mid-transaction."""
        c.execute("BEGIN IMMEDIATE")
        try:
            out = body()
            c.execute("COMMIT")
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return out

    # ── changes made outside COA Reviewer (field snapshots) ──────────────

    def snapshots(self, lab_id: str, source: str) -> Optional[Dict[str, Optional[str]]]:
        """``{field: value}`` last seen or written for (lab_id, source);
        ``None`` if the store could not be read."""
        lab_id = _require_text(lab_id, "lab_id")
        _require(source in SOURCES, f"unknown source {source!r}")

        def op(c: sqlite3.Connection) -> Dict[str, Optional[str]]:
            rows = c.execute("SELECT field, value FROM field_snapshots WHERE lab_id=?"
                             " AND source=? LIMIT ?",
                             (lab_id, source, MAX_OBSERVE_FIELDS * 2)).fetchall()
            return {r["field"]: r["value"] for r in rows}
        return self._run_read("snapshots", op, None, key=lab_id)

    def update_snapshots(self, lab_id: str, source: str, values: Dict[str, Any], *,
                         seen_at: Optional[float] = None) -> bool:
        """Record values COA Reviewer itself wrote, with no history row."""
        lab_id = _require_text(lab_id, "lab_id")
        _require(isinstance(values, dict), "values must be a dict")
        when = self._now() if seen_at is None else _require_float(seen_at, "seen_at")
        rows = [_snapshot_row(lab_id, *_check_snapshot((source, f, v)), when)
                for f, v in list(values.items())[:MAX_OBSERVE_FIELDS]]

        def op(c: sqlite3.Connection) -> bool:
            self._in_tx(c, lambda: c.executemany(_UPSERT_SNAPSHOT_SQL, rows))
            return True
        return self._run("update_snapshots", op, False, key=lab_id)

    def prune_snapshots(self, *, before: float, limit: int = MAX_PRUNE_ROWS) -> Optional[int]:
        """Delete up to ``limit`` snapshots last seen before ``before`` (a
        sample nobody has looked at for months). A later read of one simply
        re-baselines it. Returns how many went, or ``None`` on failure."""
        before = _require_float(before, "before")
        _require(isinstance(limit, int) and 0 < limit <= MAX_PRUNE_ROWS,
                 f"limit must be 1..{MAX_PRUNE_ROWS}, got {limit!r}")

        def op(c: sqlite3.Connection) -> int:
            cur = c.execute("DELETE FROM field_snapshots WHERE rowid IN (SELECT rowid FROM"
                            " field_snapshots WHERE seen_at < ? LIMIT ?)", (before, limit))
            return int(cur.rowcount or 0)
        n = self._run("prune_snapshots", op, None, key=f"<{before:.0f}")
        if n:
            logger.info("pruned %d field snapshot(s) last seen before %.0f", n, before)
        return n

    def observe(self, lab_id: str, source: str, values: Dict[str, Any], *,
                seen_at: Optional[float] = None, actor_hint: Optional[str] = None,
                changed_at: Any = None, field_meta: Optional[Dict[str, dict]] = None,
                wait: bool = True) -> Any:
        """Compare what a read saw with the snapshot; record every field that
        changed without COA Reviewer as an ``external_change``.

        ``seen_at`` is when the read *started* (default now): a snapshot
        confirmed after that is newer knowledge than the read, so the field
        is skipped — neither recorded nor overwritten. A field never seen is
        stored silently as the baseline. ``field_meta`` is ``{field:
        {"label", "actor", "changed_at"}}``: ``label`` is the text the
        history shows for a key like ``test:9``; ``actor``/``changed_at``
        override the call-wide ``actor_hint``/``changed_at``. A
        ``changed_at`` outside [previous sighting, now] is dropped.

        Returns the number of changes recorded, ``None`` on failure, or
        ``WRITER_BUSY`` when ``wait`` is False and the writer was in use."""
        out = self.observe_many([{
            "lab_id": lab_id, "source": source, "values": values, "seen_at": seen_at,
            "actor_hint": actor_hint, "changed_at": changed_at,
            "field_meta": field_meta}], wait=wait)
        if out is None or out == WRITER_BUSY:
            return out
        return out.get(str(lab_id).strip(), 0)

    def observe_many(self, items: List[dict], *, wait: bool = True) -> Any:
        """``observe`` for many (lab_id, source) items at once: ONE reader
        acquisition (one SELECT per source per ≤ ``MAX_BATCH`` lab ids) and,
        only if something is new, different or stale, one writer transaction
        per ≤ ``MAX_BATCH`` items. The writer re-checks inside its
        transaction, so a concurrent own edit or observer is never counted.

        Returns ``{lab_id: changes}`` for lab ids with changes (``{}`` when
        nothing changed), ``None`` on failure, or ``WRITER_BUSY``."""
        started = time.perf_counter()
        _require(isinstance(items, list), "items must be a list")
        _require(len(items) <= MAX_OBSERVE_ITEMS,
                 f"at most {MAX_OBSERVE_ITEMS} items per call, got {len(items)}")
        prepared = [p for p in (self._prepare_observe(it) for it in items) if p["fields"]]
        if not prepared:
            return {}
        known = self._run_read("observe", lambda c: self._read_snapshots(c, prepared), None,
                               key=f"{len(prepared)} item(s)")
        if known is None:
            return None
        todo = [(p, names) for p in prepared
                for names in [self._observe_todo(p, known.get((p["lab_id"], p["source"]), {}))]
                if names]
        if not todo:
            logger.debug("observe: %d item(s) unchanged, %.1f ms", len(prepared),
                         (time.perf_counter() - started) * 1000)
            return {}
        counts = self._observe_write(todo, wait)
        if counts is None or counts == WRITER_BUSY:
            return counts
        for lab_id, n in counts.items():
            logger.info("external changes detected: %s n=%d", lab_id, n)
        logger.debug("observe: %d item(s), %d written, %d changed lab id(s), %.1f ms",
                     len(prepared), len(todo), len(counts),
                     (time.perf_counter() - started) * 1000)
        return counts

    def _prepare_observe(self, it: dict) -> dict:
        _require(isinstance(it, dict), "an observe item must be a dict")
        lab_id = _require_text(it.get("lab_id"), "lab_id")
        source = it.get("source")
        _require(source in SOURCES, f"unknown source {source!r}")
        seen_at = it.get("seen_at")
        meta = it.get("field_meta") or {}
        _require(isinstance(meta, dict), "field_meta must be a dict")
        return {"lab_id": lab_id, "source": source,
                "fields": self._observed_fields(lab_id, source, it.get("values")),
                "seen_at": self._now() if seen_at is None else _require_float(seen_at, "seen_at"),
                "actor": it.get("actor_hint"), "changed_at": it.get("changed_at"),
                "meta": meta}

    @staticmethod
    def _observed_fields(lab_id: str, source: str, values: Any) -> Dict[str, Any]:
        _require(isinstance(values, dict), "values must be a dict")
        items = list(values.items())
        if len(items) > MAX_OBSERVE_FIELDS:
            logger.warning("observe %s/%s: fields capped from %d to %d", lab_id, source,
                           len(items), MAX_OBSERVE_FIELDS)
            items = items[:MAX_OBSERVE_FIELDS]
        out: Dict[str, Any] = {}
        for field, value in items:
            name = str(field or "").strip()[:MAX_FIELD_NAME]
            if name:
                out[name] = value
        return out

    @staticmethod
    def _read_snapshots(c: sqlite3.Connection,
                        prepared: List[dict]) -> Dict[Tuple[str, str], Dict[str, sqlite3.Row]]:
        by_source: Dict[str, List[str]] = {}
        for p in prepared:
            by_source.setdefault(p["source"], []).append(p["lab_id"])
        out: Dict[Tuple[str, str], Dict[str, sqlite3.Row]] = {}
        for source, lab_ids in by_source.items():
            ids = sorted(set(lab_ids))
            for start in range(0, len(ids), MAX_BATCH):
                chunk = ids[start:start + MAX_BATCH]
                rows = c.execute(
                    f"SELECT lab_id, field, value, digest, seen_at FROM field_snapshots"
                    f" WHERE source=? AND lab_id IN ({','.join('?' * len(chunk))}) LIMIT ?",
                    [source, *chunk, MAX_OBSERVE_READ_ROWS]).fetchall()
                if len(rows) == MAX_OBSERVE_READ_ROWS:
                    logger.warning("observe: snapshot read capped at %d rows; the writer"
                                   " re-checks the rest", MAX_OBSERVE_READ_ROWS)
                for r in rows:
                    out.setdefault((r["lab_id"], source), {})[r["field"]] = r
        return out

    @staticmethod
    def _stored_digest(row: sqlite3.Row, source: str) -> str:
        return row["digest"] or _digest(row["value"], source)

    def _observe_todo(self, p: dict, known: Dict[str, sqlite3.Row]) -> List[str]:
        """Fields that need the writer: new, different, or stale — never one
        whose snapshot is newer than the read."""
        todo = []
        for name, value in p["fields"].items():
            row = known.get(name)
            if row is None:
                todo.append(name)
            elif float(row["seen_at"]) > p["seen_at"]:
                logger.debug("observe %s/%s/%s: read is older than its snapshot, skipped",
                             p["lab_id"], p["source"], name)
            elif (self._stored_digest(row, p["source"]) != _digest(value, p["source"])
                    or p["seen_at"] - float(row["seen_at"]) >= SNAPSHOT_REFRESH_SECONDS):
                todo.append(name)
        return todo

    def _observe_write(self, todo: List[Tuple[dict, List[str]]], wait: bool) -> Any:
        counts: Dict[str, int] = {}
        for start in range(0, len(todo), MAX_BATCH):
            chunk = todo[start:start + MAX_BATCH]
            got = self._run("observe", lambda c, chunk=chunk: self._in_tx(
                c, lambda: self._observe_tx(c, chunk)), None,
                key=f"{len(chunk)} item(s)", wait=wait, busy=WRITER_BUSY)
            if got is None or got == WRITER_BUSY:
                return got
            for lab_id, n in got.items():
                counts[lab_id] = counts.get(lab_id, 0) + n
        return counts

    def _observe_tx(self, c: sqlite3.Connection,
                    chunk: List[Tuple[dict, List[str]]]) -> Dict[str, int]:
        detected_at = self._now()
        counts: Dict[str, int] = {}
        for p, names in chunk:
            known = self._snapshot_rows(c, p["lab_id"], p["source"], names)
            for name in names:
                if self._observe_field_tx(c, p, name, known.get(name), detected_at):
                    counts[p["lab_id"]] = counts.get(p["lab_id"], 0) + 1
        return counts

    def _observe_field_tx(self, c: sqlite3.Connection, p: dict, name: str,
                          row: Optional[sqlite3.Row], detected_at: float) -> bool:
        """One field inside the writer's transaction; True if it changed."""
        lab_id, source, seen_at = p["lab_id"], p["source"], p["seen_at"]
        value = p["fields"][name]
        if row is not None and float(row["seen_at"]) > seen_at:
            logger.debug("observe %s/%s/%s: read is older than its snapshot, skipped",
                         lab_id, source, name)
            return False
        digest = _digest(value, source)
        if row is not None and self._stored_digest(row, source) == digest:
            if seen_at - float(row["seen_at"]) >= SNAPSHOT_REFRESH_SECONDS or not row["digest"]:
                c.execute("UPDATE field_snapshots SET seen_at=?, digest=? WHERE lab_id=?"
                          " AND source=? AND field=?", (seen_at, digest, lab_id, source, name))
            return False
        if row is not None:
            c.execute(_INSERT_EVENT_SQL, self._external_row(p, name, row, value, detected_at))
        c.execute(_UPSERT_SNAPSHOT_SQL, _snapshot_row(lab_id, source, name, value, seen_at))
        return row is not None

    @staticmethod
    def _snapshot_rows(c: sqlite3.Connection, lab_id: str, source: str,
                       names: List[str]) -> Dict[str, sqlite3.Row]:
        """Snapshot rows for ``names`` (≤ MAX_OBSERVE_FIELDS < MAX_BATCH)."""
        marks = ",".join("?" * len(names))
        rows = c.execute(f"SELECT field, value, digest, seen_at FROM field_snapshots WHERE"
                         f" lab_id=? AND source=? AND field IN ({marks})",
                         [lab_id, source, *names]).fetchall()
        return {r["field"]: r for r in rows}

    @staticmethod
    def _external_row(p: dict, name: str, row: sqlite3.Row, value: Any,
                      detected_at: float) -> tuple:
        meta = p["meta"].get(name) or {}
        actor = str(meta.get("actor") or p["actor"] or "").strip() or EXTERNAL_ACTOR
        since = float(row["seen_at"])
        detail: Dict[str, Any] = {"source": p["source"], "key": name, "since": since,
                                  "detected_at": detected_at}
        changed_at = meta.get("changed_at") or p["changed_at"]
        if changed_at:
            when = _parse_when(changed_at)
            if (when is not None and since - CHANGED_AT_SLACK_SECONDS <= when
                    <= detected_at + CHANGED_AT_SLACK_SECONDS):
                detail["changed_at"] = changed_at
            else:
                logger.debug("observe %s/%s: changed_at %r outside [%s, %s], dropped",
                             p["lab_id"], name, changed_at, since, detected_at)
        label = str(meta.get("label") or name)[:MAX_FIELD_NAME]
        return (p["lab_id"], detected_at, actor, "external_change", label,
                row["value"], _as_text(value), _encode_detail(detail))

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
        start = _require_float(start, "start")
        end = _require_float(end, "end")
        limit = max(1, min(int(limit), MAX_RANGE_ROWS))

        def op(c: sqlite3.Connection) -> List[dict]:
            rows = c.execute(
                "SELECT * FROM sample_events WHERE at >= ? AND at < ?"
                " ORDER BY at, id LIMIT ?", (start, end, limit)).fetchall()
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
        decode every ``detail`` JSON blob for a day with a lot of activity.
        ``external_change`` rows are left out: their ``user`` is a LabVision
        operator or "Outside COA Reviewer", not someone using the app, and
        they must not take a column or use up the row limit."""
        start = _require_float(start, "start")
        end = _require_float(end, "end")
        limit = max(1, min(int(limit), MAX_RANGE_ROWS))

        def op(c: sqlite3.Connection) -> List[dict]:
            rows = c.execute(
                "SELECT user, at, kind FROM sample_events WHERE at >= ? AND at < ?"
                " AND kind != 'external_change'"
                " ORDER BY at, id LIMIT ?", (start, end, limit)).fetchall()
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

        The whole batch is validated *before* ``BEGIN`` — a malformed item
        raises ``ValueError`` having touched nothing, rather than raising
        from partway through the transaction (which, if not every failure
        path rolled back correctly, could leave the writer connection
        sitting mid-transaction: later writes would then silently queue
        into a transaction nobody ever commits).

        Returns a list of results parallel to ``ops`` (the new span id for
        ``"open"``, a bool for ``"touch"``/``"close"``), or ``None`` if the
        whole batch failed — nothing in it was applied, so the caller
        should treat every item as still pending.
        """
        batch = list(ops)[:MAX_HISTORY * 3]   # bounded; callers cap far below this
        _validate_presence_batch(batch)

        def op(c: sqlite3.Connection) -> List[Any]:
            c.execute("BEGIN IMMEDIATE")
            results: List[Any] = []
            try:
                for item in batch:
                    kind = item["op"]
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
                    else:   # "close" — validated to be one of the three above
                        reason = item["reason"]
                        span_id = self._resolve_span_id(item["span_id"], results)
                        cur = c.execute(
                            "UPDATE presence SET last_seen=MAX(last_seen, ?),"
                            " ended_at=MAX(last_seen, ?), end_reason=?"
                            " WHERE id=? AND ended_at IS NULL",
                            (float(item["ended_at"]), float(item["ended_at"]), reason,
                             span_id))
                        results.append(cur.rowcount == 1)
                c.execute("COMMIT")
            except BaseException:
                # Not just sqlite3.Error — any failure here (even one this
                # pre-validation pass didn't anticipate) must roll back so
                # the connection isn't left mid-transaction.
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
            prev = results[-1] if results else None
            _require(isinstance(prev, int) and not isinstance(prev, bool),
                     "'$prev' span_id must reference a preceding 'open' in this batch")
            return prev
        _require(isinstance(raw, int) and not isinstance(raw, bool),
                 f"span_id must be an int, got {raw!r}")
        return raw

    def spans_between(self, start: float, end: float,
                      limit: int = MAX_RANGE_ROWS) -> List[dict]:
        start = _require_float(start, "start")
        end = _require_float(end, "end")
        limit = max(1, min(int(limit), MAX_RANGE_ROWS))

        def op(c: sqlite3.Connection) -> List[dict]:
            rows = c.execute(
                "SELECT * FROM presence WHERE started_at >= ? AND started_at < ?"
                " AND COALESCE(ended_at, last_seen) >= ?"
                " ORDER BY started_at, id LIMIT ?",
                (start - MAX_SPAN_SECONDS, end, start, limit)).fetchall()
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
