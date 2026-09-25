# Shared Marks, Sample History, Time Online, Update-on-Restart — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make verdicts shared across reviewers (with green INFO/TEST tags), record a before/after history for every change to a sample, show a Time Online day chart at `/activity`, and let Restart switch to an already-staged release.

**Architecture:** One new SQLite store (`shared_store.py`, `DATA_DIR/coa_shared.db`) owns verdicts, sample events and presence spans; `presence.py` batches heartbeat touches in memory; `activity.py` is a pure day-payload builder; `restart_update.py` is the app half of the restart→updater handshake, `deploy/updater/updater.py` the other half. `app.py` wires them into existing routes. The frontend gains tags in the sample list, a Review | History segmented control in the right panel, a ◷ header link, and a separate `/activity` page.

**Tech Stack:** Python 3 / Flask / stdlib `sqlite3`; vanilla JS + CSS (existing tokens in `static/css/app.css`); pytest.

**Spec:** `docs/superpowers/specs/2026-09-25-shared-marks-history-activity-design.md`

## Global rules for every task

- Run tests with `.venv/bin/pytest -o addopts="" -q <path>` (the repo's `pytest.ini` adds `-q`, which hides counts).
- TDD: failing test first, watch it fail, implement, watch it pass, commit.
- NASA Power-of-10, applied to Python: every loop has a fixed upper bound; no recursion; functions ≤ ~60 lines; every return value from the store is checked; inputs validated at public boundaries (`ValueError` for programming errors); no unbounded in-memory growth (cap and log).
- **Store failures never break reviewing.** Store methods return a safe default (`False` / `None` / `[]`) and log a WARNING; routes carry on.
- Loggers: `coa.shared_store`, `coa.presence`, `coa.restart`, `coa.activity`. DEBUG for traces with timings, INFO for state transitions, WARNING for degraded paths. Never log card codes or passwords.
- Never bind state to `APP_DIR` (use `DATA_DIR`) — `tests/test_consolidation.py` enforces this.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`. Never `git add` `changelog/*.jsonl` (dirty, tracked local state).

## File map

| File | Responsibility |
|---|---|
| `shared_store.py` (new) | SQLite: verdicts, sample_events, presence, meta. Bounded, thread-safe, degrades on I/O error. |
| `presence.py` (new) | In-memory presence spans, flushed to the store ≤ 1/30 s/user; gap/logout/timeout closing. |
| `activity.py` (new) | Pure function building the `/api/activity` day payload from spans + events. |
| `restart_update.py` (new) | Read `staged.json`, write/withdraw `switch-requested`. |
| `deploy/updater/updater.py` | `take_switch_request()` + `honour_switch_request()` in the run loop. |
| `app.py` | Wire store/presence into AppState, mark route, tab load, change sites, heartbeat, logout, reaper, restart, new routes. |
| `templates/index.html`, `static/js/app.js`, `static/css/app.css` | Tags, Review/History control + timeline, ◷ link, restart "Installing vX". |
| `templates/activity.html`, `static/js/activity.js`, `static/css/activity.css` (new) | Time Online page. |
| `CLAUDE.md`, `DEPLOY-SETUP.md` | Document the new state file, modules, and updater copy step. |

---

### Task 1: `shared_store.py`

**Files:** Create `shared_store.py`; Test `tests/test_shared_store.py`

- [ ] **Step 1: Write the failing tests**

```python
"""SharedStore: verdicts, history, presence — real SQLite in tmp_path."""
from __future__ import annotations

import sqlite3
import time

import pytest

from shared_store import (MAX_BATCH, MAX_HISTORY, SharedStore)


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    s = SharedStore(tmp_path / "coa_shared.db", now=clock)
    yield s
    s.close()


def test_set_and_read_verdict(store):
    assert store.set_verdict("092326-00001", "tests", "good", by="Dana P", sample_id=7)
    got = store.verdicts_for(["092326-00001"])
    v = got["092326-00001"]["tests"]
    assert v["outcome"] == "good" and v["by"] == "Dana P" and v["sample_id"] == 7


def test_verdict_is_per_mode(store):
    store.set_verdict("A", "tests", "good", by="x")
    store.set_verdict("A", "info", "bad", by="y", reason="tank wrong")
    got = store.verdicts_for(["A"])["A"]
    assert got["tests"]["outcome"] == "good"
    assert got["info"]["outcome"] == "bad" and got["info"]["reason"] == "tank wrong"


def test_set_verdict_replaces(store, clock):
    store.set_verdict("A", "tests", "bad", by="x", reason="r")
    clock.t += 10
    store.set_verdict("A", "tests", "good", by="y")
    v = store.verdicts_for(["A"])["A"]["tests"]
    assert v["outcome"] == "good" and v["by"] == "y" and v["reason"] == "" and v["at"] == clock.t


def test_clear_verdict(store):
    store.set_verdict("A", "tests", "good", by="x")
    assert store.clear_verdict("A", "tests")
    assert store.verdicts_for(["A"]) == {}


def test_verdicts_for_batches_beyond_parameter_limit(store):
    ids = [f"L{i:05d}" for i in range(MAX_BATCH * 2 + 5)]
    for i in ids[::97]:
        store.set_verdict(i, "tests", "good", by="x")
    got = store.verdicts_for(ids)
    assert set(got) == set(ids[::97])


def test_verdicts_for_empty_input(store):
    assert store.verdicts_for([]) == {}


@pytest.mark.parametrize("args", [
    ("", "tests", "good"), ("A", "nope", "good"), ("A", "tests", "meh"),
])
def test_set_verdict_rejects_bad_input(store, args):
    with pytest.raises(ValueError):
        store.set_verdict(*args, by="x")


def test_set_verdict_requires_user(store):
    with pytest.raises(ValueError):
        store.set_verdict("A", "tests", "good", by="  ")


def test_record_event_and_history_newest_first(store, clock):
    store.record_event("A", "test_result", user="Dana P", field="moisture",
                       before="11.2", after="12.0")
    clock.t += 5
    store.record_event("A", "mark", user="Ryan C", field="tests", after="good",
                       detail={"reason": ""})
    hist = store.history("A")
    assert [h["kind"] for h in hist] == ["mark", "test_result"]
    assert hist[1]["before"] == "11.2" and hist[1]["after"] == "12.0"
    assert hist[0]["detail"] == {"reason": ""}


def test_history_coerces_non_text_values(store):
    store.record_event("A", "sample_info", user="u", field="n", before=None, after=3.5)
    h = store.history("A")[0]
    assert h["before"] is None and h["after"] == "3.5"


def test_history_limit_is_clamped(store):
    for i in range(MAX_HISTORY + 20):
        store.record_event("A", "comments", user="u", after=str(i))
    assert len(store.history("A", limit=10_000)) == MAX_HISTORY
    assert len(store.history("A", limit=0)) == 1


def test_record_event_rejects_unknown_kind(store):
    with pytest.raises(ValueError):
        store.record_event("A", "made_up", user="u")


def test_events_between(store, clock):
    store.record_event("A", "mark", user="u", after="good")
    clock.t += 100
    store.record_event("B", "mark", user="v", after="bad")
    rows = store.events_between(clock.t - 50, clock.t + 1)
    assert [r["lab_id"] for r in rows] == ["B"]


def test_presence_span_lifecycle(store):
    sid = store.open_span("Dana P", 100.0)
    assert isinstance(sid, int)
    assert store.touch_span(sid, 160.0)
    assert store.close_span(sid, 170.0, "logout")
    spans = store.spans_between(0, 1000)
    assert spans == [{"id": sid, "user": "Dana P", "start": 100.0, "end": 170.0,
                      "open": False, "end_reason": "logout"}]


def test_close_open_spans_on_restart(store):
    a = store.open_span("x", 100.0)
    store.touch_span(a, 150.0)
    assert store.close_open_spans("restart") == 1
    s = store.spans_between(0, 1000)[0]
    assert s["end"] == 150.0 and s["end_reason"] == "restart" and s["open"] is False


def test_spans_between_includes_open_and_overlapping(store):
    a = store.open_span("x", 100.0)
    store.touch_span(a, 500.0)
    assert len(store.spans_between(400, 450)) == 1
    assert store.spans_between(600, 700) == []


def test_recorded_range(store):
    assert store.recorded_range() == (None, None)
    store.open_span("x", 100.0)
    store.record_event("A", "mark", user="u", after="good")
    lo, hi = store.recorded_range()
    assert lo == 100.0 and hi >= 100.0


def test_meta_flag(store):
    assert store.get_meta("k") is None
    assert store.set_meta("k", "v")
    assert store.get_meta("k") == "v"


def test_unwritable_path_degrades(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    s = SharedStore(blocker / "sub" / "db.sqlite")
    assert s.set_verdict("A", "tests", "good", by="x") is False
    assert s.verdicts_for(["A"]) is None
    assert s.history("A") == []
    assert s.open_span("x", 1.0) is None
    assert "shared store unavailable" in caplog.text


def test_survives_reopen(tmp_path):
    p = tmp_path / "db.sqlite"
    s = SharedStore(p)
    s.set_verdict("A", "tests", "good", by="x")
    s.close()
    s2 = SharedStore(p)
    assert s2.verdicts_for(["A"])["A"]["tests"]["by"] == "x"
    s2.close()


def test_concurrent_writers(store):
    import threading
    def work(n):
        for i in range(50):
            store.record_event(f"L{n}", "comments", user="u", after=str(i))
    ts = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(len(store.history(f"L{n}")) for n in range(8)) == 400


def test_performance_budget(store):
    """A mark is one upsert + one insert; a tab load one batched select."""
    ids = [f"L{i:05d}" for i in range(300)]
    t0 = time.perf_counter()
    for i in ids:
        store.set_verdict(i, "tests", "good", by="x")
        store.record_event(i, "mark", user="x", after="good")
    per_mark_ms = (time.perf_counter() - t0) * 1000 / len(ids)
    t1 = time.perf_counter()
    store.verdicts_for(ids)
    load_ms = (time.perf_counter() - t1) * 1000
    assert per_mark_ms < 25, per_mark_ms
    assert load_ms < 100, load_ms
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/pytest -o addopts="" -q tests/test_shared_store.py` → `ModuleNotFoundError: shared_store`.

- [ ] **Step 3: Implement `shared_store.py`**

```python
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
```

- [ ] **Step 4: Run tests** → all pass.
- [ ] **Step 5: Commit** — `git add shared_store.py tests/test_shared_store.py && git commit -m "Add the shared store for verdicts, sample history and presence"`

---

### Task 2: `presence.py`

**Files:** Create `presence.py`; Test `tests/test_presence.py`

- [ ] **Step 1: Failing tests**

```python
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
```

- [ ] **Step 2: Run** → fails (no module).
- [ ] **Step 3: Implement**

```python
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
```

- [ ] **Step 4: Run** → pass. **Step 5: Commit** `presence.py tests/test_presence.py` — "Track time online as batched presence spans".

---

### Task 3: `activity.py` (pure day builder)

**Files:** Create `activity.py`; Test `tests/test_activity_builder.py`

- [ ] **Step 1: Failing tests**

```python
from datetime import date, datetime

from activity import BIN_SECONDS, build_day, day_window


def ts(h, m=0):
    return datetime(2026, 9, 25, h, m).timestamp()


D = date(2026, 9, 25)


def test_day_window_is_local_midnight_to_midnight():
    lo, hi = day_window(D)
    assert lo == datetime(2026, 9, 25).timestamp()
    assert hi == datetime(2026, 9, 26).timestamp()


def test_users_spans_and_changes():
    spans = [{"user": "Dana P", "start": ts(8), "end": ts(12), "open": False},
             {"user": "dana p", "start": ts(13), "end": ts(15), "open": False}]
    events = [{"user": "Dana P", "at": ts(9, 1), "kind": "mark"},
              {"user": "Dana P", "at": ts(9, 3), "kind": "test_result"},
              {"user": "Dana P", "at": ts(14), "kind": "mark"}]
    out = build_day(D, spans, events, now=ts(20))
    assert [u["user"] for u in out["users"]] == ["Dana P"]
    u = out["users"][0]
    assert len(u["spans"]) == 2
    assert u["spans"][0]["changes"] == 2 and u["spans"][1]["changes"] == 1
    assert u["totals"]["changes"] == 3
    assert u["totals"]["online_seconds"] == 6 * 3600
    assert u["totals"]["by_kind"] == {"mark": 2, "test_result": 1}
    assert u["blocks"][0]["count"] == 2   # 9:01 and 9:03 share a block


def test_overlapping_spans_merge():
    spans = [{"user": "u", "start": ts(8), "end": ts(10), "open": False},
             {"user": "u", "start": ts(9), "end": ts(11), "open": False}]
    u = build_day(D, spans, [], now=ts(20))["users"][0]
    assert len(u["spans"]) == 1 and u["totals"]["online_seconds"] == 3 * 3600


def test_spans_clip_to_day_and_now():
    spans = [{"user": "u", "start": ts(0) - 3600, "end": ts(23), "open": True}]
    out = build_day(D, spans, [], now=ts(10))
    s = out["users"][0]["spans"][0]
    assert s["start"] == ts(0) and s["end"] == ts(10) and s["open"] is True


def test_bounds_default_and_extend():
    assert build_day(D, [], [], now=ts(20))["bounds"] == {"start_hour": 6, "end_hour": 22}
    spans = [{"user": "u", "start": ts(5, 30), "end": ts(22, 10), "open": False}]
    assert build_day(D, spans, [], now=ts(23))["bounds"] == {"start_hour": 5, "end_hour": 23}


def test_events_without_span_still_show():
    out = build_day(D, [], [{"user": "x", "at": ts(9), "kind": "mark"}], now=ts(20))
    assert out["users"][0]["blocks"][0]["count"] == 1


def test_blocks_split_on_gap():
    ev = [{"user": "x", "at": ts(9), "kind": "mark"},
          {"user": "x", "at": ts(9) + 3 * BIN_SECONDS, "kind": "mark"}]
    assert len(build_day(D, [], ev, now=ts(20))["users"][0]["blocks"]) == 2


def test_summary():
    spans = [{"user": "a", "start": ts(8), "end": ts(9), "open": True}]
    out = build_day(D, spans, [{"user": "a", "at": ts(8, 30), "kind": "mark"}], now=ts(9))
    assert out["summary"] == {"people": 1, "online_now": 1, "online_seconds": 3600,
                              "changes": 1}
```

- [ ] **Step 2: Run** → fails.
- [ ] **Step 3: Implement**

```python
"""Build the Time Online day payload from presence spans and sample events.

Pure: no I/O, no clock, so every rule is a unit test. Times are epoch seconds
in, epoch seconds out; the browser formats them in the viewer's local zone.
Days are the server's local calendar day, which is the lab's.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Tuple

BIN_SECONDS = 300          # editing blocks are built from 5-minute bins
DEFAULT_START_HOUR = 6
DEFAULT_END_HOUR = 22
MAX_USERS = 100            # columns on one day; beyond this is a data error
MAX_SPANS_PER_USER = 500


def day_window(day: date) -> Tuple[float, float]:
    start = datetime(day.year, day.month, day.day)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def _merge(spans: List[dict]) -> List[dict]:
    merged: List[dict] = []
    for s in sorted(spans, key=lambda x: x["start"])[:MAX_SPANS_PER_USER]:
        if merged and s["start"] <= merged[-1]["end"]:
            last = merged[-1]
            last["end"] = max(last["end"], s["end"])
            last["open"] = last["open"] or s["open"]
        else:
            merged.append(dict(s))
    return merged


def _blocks(times: List[float]) -> List[dict]:
    """Group change times into editing blocks; a block continues while each
    change lands within one empty bin of the previous one."""
    blocks: List[dict] = []
    for t in sorted(times):
        if blocks and t - blocks[-1]["end"] <= 2 * BIN_SECONDS:
            blocks[-1]["end"] = t
            blocks[-1]["count"] += 1
        else:
            blocks.append({"start": t, "end": t, "count": 1})
    for b in blocks:   # give single changes a visible height
        b["end"] = max(b["end"], b["start"] + BIN_SECONDS)
    return blocks


def _bounds(points: Iterable[float], lo: float) -> Dict[str, int]:
    start_h, end_h = DEFAULT_START_HOUR, DEFAULT_END_HOUR
    for p in points:
        hour = (p - lo) / 3600
        start_h = min(start_h, int(math.floor(hour)))
        end_h = max(end_h, int(math.ceil(hour)))
    return {"start_hour": max(0, start_h), "end_hour": min(24, end_h)}


def build_day(day: date, spans: List[dict], events: List[dict], *, now: float) -> dict:
    lo, hi = day_window(day)
    ceiling = min(hi, now)
    per_user: Dict[str, dict] = {}

    def slot(name: str) -> dict:
        key = name.strip().casefold()
        if key not in per_user and len(per_user) < MAX_USERS:
            per_user[key] = {"user": name.strip(), "spans": [], "events": []}
        return per_user.get(key, {"spans": [], "events": []})

    for s in spans:
        start, end = max(s["start"], lo), min(s["end"], ceiling)
        if end > start:
            slot(s["user"])["spans"].append(
                {"start": start, "end": end, "open": bool(s.get("open"))})
    for e in events:
        if lo <= e["at"] < hi:
            slot(e["user"])["events"].append(e)

    users, points = [], []
    for entry in per_user.values():
        merged = _merge(entry["spans"])
        times = [e["at"] for e in entry["events"]]
        by_kind: Dict[str, int] = {}
        for e in entry["events"]:
            by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
        for s in merged:
            s["changes"] = sum(1 for t in times if s["start"] <= t <= s["end"])
            points += [s["start"], s["end"]]
        blocks = _blocks(times)
        points += [b["start"] for b in blocks] + [b["end"] for b in blocks]
        users.append({
            "user": entry["user"], "spans": merged, "blocks": blocks,
            "first": min([s["start"] for s in merged] + times, default=hi),
            "totals": {"online_seconds": int(sum(s["end"] - s["start"] for s in merged)),
                       "changes": len(times), "by_kind": by_kind},
        })
    users.sort(key=lambda u: (u["first"], u["user"].casefold()))
    return {
        "date": day.isoformat(),
        "users": users,
        "bounds": _bounds(points, lo),
        "summary": {
            "people": len(users),
            "online_now": sum(1 for u in users if any(s["open"] for s in u["spans"])),
            "online_seconds": sum(u["totals"]["online_seconds"] for u in users),
            "changes": sum(u["totals"]["changes"] for u in users),
        },
    }
```

- [ ] **Step 4: Run** → pass. **Step 5: Commit** — "Build the Time Online day from spans and changes".

---

### Task 4: `restart_update.py` + updater half

**Files:** Create `restart_update.py`; Modify `deploy/updater/updater.py`; Tests `tests/test_restart_update.py`, append to `tests/test_updater.py`

- [ ] **Step 1: Failing tests (`tests/test_restart_update.py`)**

```python
import json

from restart_update import (MARKER_FILE, staged_update, withdraw_switch_request,
                            write_switch_request)


def staged(tmp_path, **kw):
    (tmp_path / "staged.json").write_text(json.dumps(kw), encoding="utf-8")


def test_no_staged_file(tmp_path):
    assert staged_update(tmp_path, "v3.5.0") is None


def test_healthy_newer_tag(tmp_path):
    staged(tmp_path, tag="v4.0.0", healthy=True)
    assert staged_update(tmp_path, "v3.5.0") == "v4.0.0"


def test_same_tag_is_not_an_update(tmp_path):
    staged(tmp_path, tag="V3.5.0 ", healthy=True)
    assert staged_update(tmp_path, "v3.5.0") is None


def test_unhealthy_is_ignored(tmp_path):
    staged(tmp_path, tag="v4.0.0", healthy=False)
    assert staged_update(tmp_path, "v3.5.0") is None


def test_dev_build_never_switches(tmp_path):
    staged(tmp_path, tag="v4.0.0", healthy=True)
    assert staged_update(tmp_path, "dev") is None


def test_corrupt_staged(tmp_path):
    (tmp_path / "staged.json").write_text("{nope", encoding="utf-8")
    assert staged_update(tmp_path, "v3.5.0") is None


def test_write_and_withdraw(tmp_path):
    assert write_switch_request(tmp_path, "v4.0.0", by="Dana P", now=5.0)
    doc = json.loads((tmp_path / MARKER_FILE).read_text())
    assert doc == {"tag": "v4.0.0", "by": "Dana P", "at": 5.0}
    assert withdraw_switch_request(tmp_path) is True
    assert withdraw_switch_request(tmp_path) is False
```

Append to `tests/test_updater.py` (use the file's existing import style — it imports the updater module as `updater`; check the top of the file and reuse its fixture for building an `App` if one exists, otherwise construct `updater.App(cfg, {})` with `root=tmp_path`):

```python
def test_take_switch_request_consumes_marker(tmp_path):
    (tmp_path / "switch-requested").write_text('{"tag": "v4.0.0", "by": "x"}')
    assert updater.take_switch_request(tmp_path) == {"tag": "v4.0.0", "by": "x"}
    assert not (tmp_path / "switch-requested").exists()
    assert updater.take_switch_request(tmp_path) is None


def test_take_switch_request_corrupt_is_empty_dict(tmp_path):
    (tmp_path / "switch-requested").write_text("garbage")
    assert updater.take_switch_request(tmp_path) == {}


def test_honour_switch_request_switches_to_staged(tmp_path, monkeypatch):
    app = _coa_app(tmp_path)     # helper: App with data_dir under tmp_path
    updater.write_staged(app.data_dir, tag="v4.0.0", healthy=True, notes="ok")
    (app.data_dir / "switch-requested").write_text('{"tag": "v4.0.0", "by": "x"}')
    monkeypatch.setattr(app, "current_version", lambda: "v3.5.0")
    calls = []
    monkeypatch.setattr(updater, "switch", lambda a, tag, **kw: calls.append(tag) or True)
    assert updater.honour_switch_request(app) is True
    assert calls == ["v4.0.0"]


def test_honour_switch_request_refuses_mismatch(tmp_path, monkeypatch):
    app = _coa_app(tmp_path)
    updater.write_staged(app.data_dir, tag="v4.0.1", healthy=True, notes="ok")
    (app.data_dir / "switch-requested").write_text('{"tag": "v4.0.0"}')
    monkeypatch.setattr(updater, "switch", lambda *a, **k: pytest.fail("must not switch"))
    assert updater.honour_switch_request(app) is False


def test_honour_switch_request_noop_without_marker(tmp_path, monkeypatch):
    app = _coa_app(tmp_path)
    monkeypatch.setattr(updater, "switch", lambda *a, **k: pytest.fail("must not switch"))
    assert updater.honour_switch_request(app) is False
```

(`_coa_app(tmp_path)`: define in the test file if absent — `updater.App({"name": "coa", "repo": "x/y", "root": str(tmp_path), "port": 0}, {})`, and `app.data_dir.mkdir(parents=True, exist_ok=True)`. Check `App.__init__` for required keys first.)

- [ ] **Step 2: Run** → fails.
- [ ] **Step 3: Implement `restart_update.py`**

```python
"""The app's half of "Restart installs a staged update".

The updater on ASAPSV1 stages and health-checks each new release into
``<root>\\data\\staged.json`` — the same directory the app gets as
``COA_DATA_DIR``. When someone clicks Restart and a newer, healthy release is
staged, the app writes ``switch-requested``; the updater consumes it on its
next supervision tick (≤ 20 s) and runs its normal ``switch`` (post-switch
health check and automatic rollback included). The app never touches the
junction itself: ``switch`` kills the app's whole process tree, so a helper
the app spawned would die with it.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("coa.restart")

STAGED_FILE = "staged.json"
MARKER_FILE = "switch-requested"
PICKUP_SECONDS = 60.0     # updater must consume the marker within this
SWITCH_SECONDS = 120.0    # …and stop this process within this after that


def _norm(tag: Optional[str]) -> str:
    return (tag or "").strip().lstrip("vV").casefold()


def staged_update(data_dir: Path | str, current_version: str) -> Optional[str]:
    """The staged tag if it is healthy and not what is running, else None."""
    if _norm(current_version) in ("", "dev"):
        return None
    try:
        doc = json.loads((Path(data_dir) / STAGED_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        logger.warning("could not read %s: %s", STAGED_FILE, exc)
        return None
    if not isinstance(doc, dict) or doc.get("healthy") is not True:
        return None
    tag = str(doc.get("tag") or "").strip()
    if not tag or _norm(tag) == _norm(current_version):
        return None
    return tag


def write_switch_request(data_dir: Path | str, tag: str, *, by: str, now: float) -> bool:
    path = Path(data_dir) / MARKER_FILE
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({"tag": tag, "by": by, "at": now}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("could not write %s: %s", path, exc)
        return False
    logger.info("switch to %s requested by %s", tag, by)
    return True


def marker_present(data_dir: Path | str) -> bool:
    return (Path(data_dir) / MARKER_FILE).exists()


def withdraw_switch_request(data_dir: Path | str) -> bool:
    """Remove the marker. True if it was still there (nobody consumed it)."""
    try:
        (Path(data_dir) / MARKER_FILE).unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("could not withdraw switch request: %s", exc)
        return False
    return True
```

Updater additions (place after `write_staged`, and call in the run loop right after `supervise(a)`, in its own `try/except Exception: log.exception(...)`):

```python
SWITCH_REQUEST_FILE = "switch-requested"


def take_switch_request(data_dir: Path | str) -> Optional[dict]:
    """Consume the app's restart-time switch request.

    Deleted *before* acting, so a switch that crashes the updater cannot be
    retried in a loop. ``None`` = no request; ``{}`` = unreadable request
    (acted on as "no tag", i.e. refused)."""
    path = Path(data_dir) / SWITCH_REQUEST_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return None
    try:
        path.unlink()
    except OSError as exc:
        log.warning("could not remove %s (%s); ignoring it rather than looping", path, exc)
        return None
    try:
        got = json.loads(raw)
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def honour_switch_request(app: "App") -> bool:
    """A reviewer clicked Restart while a release was staged: switch now.

    Same gate as a manual ``switch``; idleness is not required because the
    person asking is choosing to restart anyway. Anything that fails the gate
    is logged and left alone — the app restarts itself normally."""
    req = take_switch_request(app.data_dir)
    if req is None:
        return False
    tag = str(req.get("tag") or "")
    who = req.get("by") or "someone"
    if is_paused(app.data_dir):
        log.info("[%s] %s asked for %s on restart, but the app is paused", app.name, who, tag)
        return False
    ok, why = may_switch(staged=read_staged(app.data_dir), requested_tag=tag)
    if not ok:
        log.warning("[%s] restart-time switch to %r refused: %s", app.name, tag, why)
        return False
    if not differs_from(app.current_version(), tag):
        log.info("[%s] restart-time switch: already on %s", app.name, tag)
        return False
    log.warning("[%s] %s restarted the app with %s staged — switching now", app.name, who, tag)
    return switch(app, tag)
```

Run loop change (inside `for a in chosen:` after the supervise try-block):

```python
            try:
                honour_switch_request(a)
            except Exception:
                log.exception("[%s] unhandled error honouring a switch request", a.name)
```

Also add a line to the module docstring's design rules: "* **A restart may ask for a switch.** ``switch-requested`` in the data dir is the app's restart button asking for the already-staged release; it passes the same ``may_switch`` gate."

- [ ] **Step 4: Run** `tests/test_restart_update.py tests/test_updater.py` → pass.
- [ ] **Step 5: Commit** — "Let a restart ask the updater for the staged release".

---

### Task 5: Wire store + presence into `AppState`; restart path; heartbeat/logout/reaper

**Files:** Modify `app.py`; Tests `tests/test_presence_wiring.py`, `tests/test_restart_switch.py`

Read first: `AppState.__init__` (~line 1042), `_reap_idle_sessions` (~1584), `_session_cleanup_worker`, `track_activity` (~2367), `portal_logout` (~2576), `heartbeat` (~2642), `request_restart` (~1684), `_auto_restart_worker`, `/api/restart` (~2648). Look at an existing route test (e.g. `tests/test_mark_flow.py`) and copy its client/session fixture pattern.

- [ ] **Step 1: Failing tests** — cover:
  1. `state.shared` is a `SharedStore` at `DATA_DIR / "coa_shared.db"`; `state.presence` is a `PresenceTracker`.
  2. `POST /api/heartbeat` with a session → `state.presence.online()` contains the user (monkeypatch `state.shared`/`state.presence` with ones built on `tmp_path`).
  3. `POST /api/portal-logout` closes the span with `end_reason == "logout"`.
  4. `_reap_idle_sessions` closes the reaped user's span with `timeout`.
  5. `request_restart("x")` with no staged update → calls `_graceful_shutdown` path (monkeypatch `_graceful_shutdown` and `time.sleep`), returns `None`.
  6. With `staged.json` healthy `v9.9.9` in a tmp `DATA_DIR` (monkeypatch `app.DATA_DIR`, `app.APP_VERSION="v3.5.0"`): returns `"v9.9.9"`, marker exists, `_graceful_shutdown` NOT called synchronously; `_await_switch("v9.9.9", pickup=0.05, switch=0.05)` with marker still present → withdraws and calls `_graceful_shutdown("manual restart")`; with marker already consumed and process still alive past `switch` → also calls `_graceful_shutdown`.
  7. `/api/restart` JSON includes `"update": "v9.9.9"` in case 6 and `"update": null` otherwise.

- [ ] **Step 2: Run** → fail.
- [ ] **Step 3: Implement**

In imports: `from shared_store import SharedStore`, `from presence import PresenceTracker`, `import restart_update`, `import activity`.

In `AppState.__init__` after `self.change_log = ...`:

```python
        # Shared, durable review state (verdicts, per-sample history,
        # presence). DATA_DIR, never APP_DIR: it must survive release swaps.
        self.shared = SharedStore(DATA_DIR / "coa_shared.db")
        self.presence = PresenceTracker(self.shared)
        # A span still open is one the previous process never closed.
        closed = self.shared.close_open_spans("restart")
        if closed:
            logger.info("Closed %d presence span(s) left open by the last run", closed)
```

`track_activity`: after the `_last_request_time` update, for requests with a session, touch presence without a DB call per request:

```python
    uid = session.get("uid")
    if uid and request.path not in _NON_ACTIVITY_PATHS:
        us = user_sessions.get(uid)
        if us is not None:
            state.presence.touch(us.name)
```

(`dict.get` is atomic under the GIL; no lock needed for a read.)

`heartbeat()`: `state.presence.touch(get_user_state().name)` before returning.
`portal_logout()`: `state.presence.end(ustate.name, "logout")` next to the change-log line.
`_reap_idle_sessions`: in the `for us in reaped:` loop, `state.presence.end(us.name, "timeout")`.
`_session_cleanup_worker`: call `state.presence.sweep()` each cycle (wrap in `try/except Exception: logger.exception(...)`).

Restart:

```python
def request_restart(source: str) -> Optional[str]:
    """Restart because someone clicked (UI button or tray).

    If the updater has a newer release staged and healthy, ask it to switch
    (restart_update) instead of respawning the same version; a watchdog falls
    back to a normal restart if the updater never acts. Returns the tag being
    installed, or None for a plain restart."""
    logger.info("Manual restart requested via %s", source)
    tag = restart_update.staged_update(DATA_DIR, APP_VERSION)
    if tag and restart_update.write_switch_request(DATA_DIR, tag, by=source, now=time.time()):
        threading.Thread(target=_await_switch, args=(tag,), daemon=True,
                         name="await-switch").start()
        return tag

    def _go() -> None:
        time.sleep(1)
        _graceful_shutdown("manual restart")

    threading.Thread(target=_go, daemon=True).start()
    return None


def _await_switch(tag: str, pickup: float = restart_update.PICKUP_SECONDS,
                  switch: float = restart_update.SWITCH_SECONDS) -> None:
    """Give the updater `pickup` s to take the request and `switch` s more to
    stop us. If either runs out, restart the ordinary way: the person asked
    for a restart and must get one."""
    deadline = time.monotonic() + pickup
    while time.monotonic() < deadline and restart_update.marker_present(DATA_DIR):
        time.sleep(min(1.0, pickup))
    if restart_update.withdraw_switch_request(DATA_DIR):
        logger.warning("Updater did not take the switch to %s within %.0fs — "
                       "restarting normally (is updater.py up to date on this host?)",
                       tag, pickup)
        _graceful_shutdown("manual restart")
        return
    logger.info("Updater took the switch to %s; waiting to be stopped", tag)
    deadline = time.monotonic() + switch
    while time.monotonic() < deadline:
        time.sleep(min(1.0, switch))
    logger.warning("Updater accepted the switch to %s but this process is still "
                   "running after %.0fs — restarting normally", tag, switch)
    _graceful_shutdown("manual restart")
```

(Loops are bounded by their deadlines; `time.sleep` is monkeypatchable in tests.)

`/api/restart` returns `"update": tag`. The tray's restart callback already calls `request_restart`; no change needed. In `_auto_restart_worker`, replace the final `_graceful_shutdown("auto-restart")` with:

```python
        tag = restart_update.staged_update(DATA_DIR, APP_VERSION)
        if tag and restart_update.write_switch_request(DATA_DIR, tag, by="3 AM auto-restart",
                                                       now=time.time()):
            _await_switch(tag)
            continue
        _graceful_shutdown("auto-restart")
```

- [ ] **Step 4: Run** new tests + `tests/test_tray.py tests/test_healthz.py tests/test_supervisor.py` → pass.
- [ ] **Step 5: Commit** — "Record presence and let Restart install a staged release".

---

### Task 6: Shared verdicts — mark route, fan-out, tab load, tags, ledger migration

**Files:** Modify `app.py`; Test `tests/test_shared_verdicts.py`

Read first: `SampleRecord.to_dict`, `UserState.record_result`, `_reapply_verdict`, `/api/start` (mode parsing ~2757), `/api/mark`, `/api/tabs/<tab>`, `load_review_state`, `REVIEW_STATE_DIR`, and `tests/test_marks_follow_a_re_pull.py` (its fixtures build two sessions — reuse).

- [ ] **Step 1: Failing tests** — with `state.shared` monkeypatched to a tmp-path store:
  1. A marks Good in tests mode → `state.shared.verdicts_for([lab])[lab]["tests"]["by"] == A.name`; a `mark` event exists in `history(lab)` with `field == "tests"`, `after == "good"`, `before is None`.
  2. B (tests mode) holds the same lab_id on another tab → after A's mark, B's record status is `good`, B's `session_results` has the row with `reviewer == A.name`, and B's SSE queue got `sample_status`.
  3. C in **info** mode holding the same lab_id is untouched (status unchanged), but receives a `tags` SSE with `tags["tests"]["by"] == A.name`.
  4. A un-marks → verdict gone, B's record back to `ready`/`pending` and its result cleared; history has `unmark` with `before == "good"`.
  5. `GET /api/tabs/<tab>?mode=tests` for D (who pulled the sample after A's mark) returns it `status == "good"` with `tags.tests.by == A.name`, and D's export includes it.
  6. `GET /api/tabs/<tab>?mode=info` returns `tags` but applies only info verdicts.
  7. With `state.shared.verdicts_for` returning `None` (store down), `/api/tabs` returns records unchanged (the ledger still applies) and does not 500.
  8. `mode` from the mark body wins; invalid/missing falls back to `ustate.mode` (default `"tests"`); `/api/start` with `{"mode": "info"}` sets `ustate.mode == "info"`.
  9. Ledger migration: a `review_state/<acct>.json` with a fresh Good verdict on tab `Intaked` and one on `Due Out` → `migrate_ledgers_once()` writes `info` and `tests` verdicts respectively (by the account's name), sets meta `ledger_migrated`, and a second call writes nothing.
  10. Bad mark shares reason and cc_task_id; Bad produces no tag.

- [ ] **Step 2: Run** → fail.
- [ ] **Step 3: Implement**

`UserState.__init__`: `self.mode: str = "tests"`.

Helpers (near `_verdict_entry`):

```python
REVIEW_MODES = ("info", "tests")
_OUTCOME_LABEL = {"good": "Good", "bad": "Bad"}


def _note_mode(ustate: "UserState", raw: Any) -> str:
    """The mode a request speaks for; remembered on the session."""
    if raw in REVIEW_MODES:
        ustate.mode = raw
    return ustate.mode


def _tags_from(verdicts: Optional[Dict[str, dict]]) -> dict:
    """Green tags are Good verdicts only; Bad shows through status."""
    v = verdicts or {}
    def tag(mode):
        x = v.get(mode)
        return {"by": x["by"], "at": x["at"]} if x and x["outcome"] == "good" else None
    return {"info": tag("info"), "tests": tag("tests")}


def _apply_shared(ustate: "UserState", rec: SampleRecord, verdict: Optional[dict]) -> bool:
    """Make `rec` show the shared verdict for this session's mode. True if it
    changed. Never touches the preview."""
    if verdict is None:
        if rec.status not in (STATUS_GOOD, STATUS_BAD):
            return False
        rec.status = STATUS_READY if rec.preview_url else STATUS_PENDING
        rec.reason, rec.cc_task_id = "", None
        ustate.clear_result(rec.tab, rec.lab_id)
        ustate.forget_verdict(rec.tab, rec.lab_id)
        return True
    status = STATUS_GOOD if verdict["outcome"] == "good" else STATUS_BAD
    if rec.status == status and rec.reason == (verdict.get("reason") or ""):
        return False
    rec.status = status
    rec.reason = verdict.get("reason") or ""
    rec.cc_task_id = verdict.get("cc_task_id")
    ustate.record_result(rec, _OUTCOME_LABEL[verdict["outcome"]], rec.reason,
                         reviewer=verdict.get("by"))
    return True
```

`record_result` gains `reviewer: Optional[str] = None` → `"reviewer": reviewer or self.name`.

Fan-out (after `mark_sample`'s change-log call; `outcome`, `mode` known):

```python
def _share_verdict(actor: "UserState", rec: SampleRecord, outcome: str, mode: str) -> None:
    store = state.shared
    current = store.verdicts_for([rec.lab_id])
    before = ((current or {}).get(rec.lab_id, {}).get(mode) or {}).get("outcome")
    if outcome == "uncheck":
        store.clear_verdict(rec.lab_id, mode)
        shared = None
    else:
        store.set_verdict(rec.lab_id, mode, outcome, by=actor.name, reason=rec.reason,
                          cc_task_id=rec.cc_task_id, sample_id=rec.sample_id)
        shared = {"outcome": outcome, "reason": rec.reason,
                  "cc_task_id": rec.cc_task_id, "by": actor.name}
    _sample_event(rec.lab_id, "unmark" if outcome == "uncheck" else "mark",
                  user=actor.name, field=mode, before=before,
                  after=None if outcome == "uncheck" else outcome,
                  detail={"reason": rec.reason, "tab": rec.tab} if rec.reason else {"tab": rec.tab})
    with _sessions_lock:
        sessions = list(user_sessions.values())
    for us in sessions[:MAX_FANOUT_SESSIONS]:
        if us.mode != mode:
            continue
        changed = [r for (t, lid), r in list(us.records.items())
                   if lid == rec.lab_id and not (us is actor and t == rec.tab)
                   and _apply_shared(us, r, shared)]
        for r in changed:
            us.emit_sse({"type": "sample_status", "tab": r.tab, "lab_id": r.lab_id,
                         "status": r.status})
        if changed:
            us.persist()
    tags = _tags_from((store.verdicts_for([rec.lab_id]) or {}).get(rec.lab_id))
    state.broadcast_sse({"type": "tags", "lab_id": rec.lab_id, "tags": tags})
```

`MAX_FANOUT_SESSIONS = 200` constant near the session registry. `_sample_event` is defined in Task 7; for this task define it now:

```python
def _sample_event(lab_id: str, kind: str, *, user: str, **fields: Any) -> None:
    """Record one change to a sample and tell open History tabs about it.
    Never raises: history is a view, not a gate."""
    if not lab_id or not user:
        logger.debug("sample event %s skipped: lab_id=%r user=%r", kind, lab_id, user)
        return
    if state.shared.record_event(lab_id, kind, user=user, **fields):
        state.broadcast_sse({"type": "sample_event", "lab_id": lab_id, "kind": kind})
```

In `mark_sample`: `mode = _note_mode(ustate, body.get("mode"))` near the top; after the change-log call: `_share_verdict(ustate, rec, outcome, mode)`; add `"mode": mode` to the change-log record and `"tags"` to the JSON response.

`/api/start`: after mode parsing, `ustate.mode = mode`.

`get_tab`:

```python
    mode = _note_mode(ustate, request.args.get("mode"))
    records = ustate.get_tab_records(tab_name)
    shared = state.shared.verdicts_for(r.lab_id for r in records)
    if shared is not None:
        changed = [r for r in records
                   if _apply_shared(ustate, r, shared.get(r.lab_id, {}).get(mode))]
        if changed:
            ustate.persist()
    return jsonify({"tab": tab_name, "samples": [
        dict(r.to_dict(), tags=_tags_from((shared or {}).get(r.lab_id)))
        for r in records]})
```

Note on `_apply_shared(..., None)` in `get_tab`: when the store is readable and has no verdict, a locally judged record is un-judged. That is correct after migration (Step 9) — the store is the truth — and is exactly how someone else's un-mark reaches a session that was offline.

Ledger migration, called once at startup after `state` is built (module scope, after `state = AppState()`), in a background thread so boot is not delayed:

```python
def migrate_ledgers_once() -> int:
    """Carry each account's fresh 12 h verdicts into the shared store, once.

    Before v4 a mark lived only in that reviewer's review_state file; without
    this, upgrading mid-day would silently un-judge everyone's morning. Intaked
    is Info mode's tab; every other tab is Tests. INSERT-only: an existing
    shared verdict always wins."""
    store = state.shared
    if store.get_meta("ledger_migrated"):
        return 0
    moved = 0
    for path in sorted(REVIEW_STATE_DIR.glob("*.json"))[:500]:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("ledger migration: skipping %s: %s", path.name, exc)
            continue
        name = str((doc or {}).get("name") or "").strip()
        for v in ((doc or {}).get("verdicts") or [])[:5000]:
            if not name or not isinstance(v, dict) or not _verdict_is_fresh(v):
                continue
            mode = "info" if v.get("tab") == "Intaked" else "tests"
            outcome = {STATUS_GOOD: "good", STATUS_BAD: "bad"}.get(v.get("status"))
            lab_id = str(v.get("lab_id") or "")
            existing = store.verdicts_for([lab_id])
            if not outcome or not lab_id or existing is None or existing.get(lab_id, {}).get(mode):
                continue
            if store.set_verdict(lab_id, mode, outcome, by=name, reason=v.get("reason") or "",
                                 cc_task_id=v.get("cc_task_id")):
                moved += 1
    store.set_meta("ledger_migrated", datetime.now().isoformat(timespec="seconds"))
    logger.info("ledger migration: %d verdict(s) moved into the shared store", moved)
    return moved
```

Frontend (`static/js/app.js`): send `mode: currentReviewMode()` (use whatever the file already calls the active mode — grep `localStorage.getItem("reviewMode")` / `applyReviewMode`) in every `/api/mark` body; append `?mode=` to every `/api/tabs/` fetch. Leave rendering of tags to Task 9.

- [ ] **Step 4: Run** new tests + `tests/test_mark_flow.py tests/test_marks_follow_a_re_pull.py tests/test_review_survives_session_loss.py tests/test_double_check_link.py` → pass. Fix any existing test whose expectation legitimately changed (reviewer column now names the marker) — note each such change in the commit message.
- [ ] **Step 5: Commit** — "Share verdicts across reviewers, per review mode".

---

### Task 7: Sample history at every change site (with before-values)

**Files:** Modify `app.py`; Test `tests/test_sample_history.py`

Change sites (grep `state.change_log.qbench_edit(` and `state.change_log.command_center(`):

| Site | Event |
|---|---|
| `update_test` (PATCH `/api/tests/<id>`) | `test_result`, field=assay, before=old_value, after=value |
| `delete_attachment` | `attachment_deleted`, field=filename, before=filename |
| `update_comments` | `comments`, before=current comments (see below), after=new |
| `sample_info` PATCH | `sample_info` per field, before from the pre-PATCH sample read |
| `sync_sample_info` | `sample_sync` per field, before from pre-PATCH read, detail `{"source": "LabVision"}` |
| `cc_create_task` | `listing_created` per lab_id, after=initial_problem, detail `{task_id, type}` |
| `cc_complete_task` | `listing_completed` for `body.get("lab_id")` if given, after=notes, detail `{task_id}`; frontend passes `lab_id` in that POST body |
| `/api/mark` | done in Task 6 |

Before-values:
- **sample_info / sample_sync**: read the sample once before PATCH using the same QBench call the GET branch of `sample_info` uses (read that branch; reuse its helper — do not add a new API method). Build `before = {field: _display_value(current.get(field) or custom_fields.get(field))}`. Wrap in `try/except Exception` → `before = {}` and `logger.warning("history: could not read %s before edit: %s", lab_id, exc)`. **The edit proceeds regardless.**
- **comments**: `rec`'s current comments — use whatever `GET /api/comments/<lab_id>` reads (read it). If that needs a QBench call, do it with the same try/except → `None`.
- A field whose before equals after is still recorded (it documents the attempt) but with `detail={"unchanged": True}`.

- [ ] **Step 1: Failing tests** — each route above (monkeypatch `state.api_client` as existing tests do; grep an existing test of that route to copy setup) produces the history row with the right kind/field/before/after; a before-read that raises still returns 200 and records `before is None`; a store that is down still returns 200.
- [ ] **Step 2: Run** → fail. **Step 3: Implement.** **Step 4: Run** new + `tests/test_change_logging_wiring.py tests/test_cc_routes.py tests/test_sample_sync.py` → pass.
- [ ] **Step 5: Commit** — "Record every change to a sample with its before and after".

---

### Task 8: History and activity routes, `/activity` page route

**Files:** Modify `app.py`; Test `tests/test_activity_api.py`

```python
@app.route("/api/sample-history/<path:lab_id>")
@require_portal
def sample_history(lab_id: str):
    try:
        limit = int(request.args.get("limit", 200))
    except ValueError:
        return jsonify({"error": "limit must be a number"}), 400
    lab_id = lab_id.strip()
    if not lab_id:
        return jsonify({"error": "lab_id required"}), 400
    return jsonify({"lab_id": lab_id, "events": state.shared.history(lab_id, limit)})


@app.route("/api/activity")
@require_portal
def activity_day():
    raw = request.args.get("date") or date.today().isoformat()
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    if day > date.today():
        return jsonify({"error": "that day has not happened yet"}), 400
    lo, hi = activity.day_window(day)
    started = time.perf_counter()
    payload = activity.build_day(day, state.shared.spans_between(lo, hi),
                                 state.shared.events_between(lo, hi), now=time.time())
    payload["online"] = state.presence.online()
    payload["is_today"] = day == date.today()
    logger.debug("activity %s built in %.1f ms (%d users)", day,
                 (time.perf_counter() - started) * 1000, len(payload["users"]))
    return jsonify(payload)


@app.route("/api/activity/range")
@require_portal
def activity_range():
    lo, hi = state.shared.recorded_range()
    fmt = lambda t: date.fromtimestamp(t).isoformat() if t else None
    return jsonify({"first": fmt(lo), "last": fmt(hi), "today": date.today().isoformat()})


@app.route("/activity")
def activity_page():
    """Time Online. Auth is enforced by its API calls (401 → the page tells
    the viewer to sign in on the main screen), so the page itself can open in
    a new tab without a redirect dance."""
    return render_template("activity.html", app_version=APP_VERSION)
```

Tests: 401 without portal session for the three API routes; history newest-first; `limit=abc` → 400; `date=2099-01-01` → 400; `date=bad` → 400; activity payload for a seeded day has users/summary/bounds/online/is_today; range with empty store → nulls; `/activity` → 200 HTML containing `id="activity-root"` (after Task 10 creates the template — create a minimal template in this task and flesh it out in Task 10).

- [ ] Steps: failing tests → run → implement → run → commit "Serve sample history and the Time Online day".

---

### Task 9: Frontend — tags, Review | History, ◷ link, restart message

**Files:** Modify `templates/index.html`, `static/js/app.js`, `static/css/app.css`; Test `tests/test_shared_ui_frontend.py` (source-level guards, same style as `tests/test_cc_frontend.py`)

Source guards (write first):
- `index.html` has `id="activity-link"` with `href="/activity"`, `target="_blank"` and `rel="noopener"`, placed in the same container as `#restart-btn`.
- `index.html` has `#right-tab-review`, `#right-tab-history` (role="tab", aria-controls), `#review-panel-body`, `#history-panel` (role="tabpanel").
- `app.js` defines `renderTags(`, `setRightTab(`, `loadHistory(`, `renderHistory(`, handles SSE `case "tags"` and `case "sample_event"`, and every `fetch("/api/mark"` body includes `mode`.
- `app.js` never assigns un-escaped history text to `innerHTML` (assert `renderHistory` uses `escapeHtml(`).
- Restart handler reads `update` from the `/api/restart` JSON and shows `Installing`.

Implementation notes:
- **Tags**: in `renderSampleList()`'s row HTML, after the lab id: `<span class="sample-tags">${renderTags(s.tags)}</span>`. `renderTags(tags)` returns `""` if no Good tags, else up to two `<span class="tag tag-good" title="Info checked by Dana P · 9:42 AM">INFO</span>` / `TEST`. SSE `tags` handler: update `tags` on every copy of that lab_id in `state.samples[*]` and patch the row(s) in place (`.sample-item[data-lab="..."] .sample-tags`) — do not re-render the whole list.
- CSS: `.tag` 9px/600 uppercase, letter-spacing .06em, padding 1px 5px, radius 999px, `border:1px solid color-mix(in srgb, var(--good) 45%, transparent)`, `background: var(--good-soft)`, `color: var(--good)`; dark mode uses the existing dark `--good` token. `.sample-tags{margin-left:auto;display:inline-flex;gap:4px}`.
- **Right panel**: wrap the existing `.right-panel` children in `<div id="review-panel-body" role="tabpanel">`; prepend a segmented control `<div class="seg" role="tablist"><button id="right-tab-review" role="tab" aria-selected="true">Review</button><button id="right-tab-history" role="tab">History</button></div>`; append `<div id="history-panel" role="tabpanel" hidden><div id="history-list" class="history-list"></div></div>`. `setRightTab(name)` toggles `hidden`, `aria-selected`, stores `localStorage.rightPanelTab` (try/catch), and calls `loadHistory(state.currentSample?.lab_id)` when switching to history. `selectSample()` calls `loadHistory` when the history tab is showing. SSE `sample_event` for the selected lab_id reloads history (debounced 300 ms).
- `renderHistory(events)`: group by local day ("Today", "Yesterday", else `Mon, Sep 22`); each item `<li class="h-item"><span class="h-avatar">DP</span><div><p class="h-text">…</p><time title="exact">3 min ago</time></div></li>`; sentences per kind (all values through `escapeHtml`; before/after in `<code>`; `null` before renders as "—" with title "Earlier value not recorded"):
  - mark: "**{user}** marked it **{Good|Bad}** in {Info|Tests}" + reason line if present
  - unmark: "**{user}** cleared the {Info|Tests} mark" (+ "(was Good)")
  - test_result / sample_info / sample_sync: "**{user}** changed *{field}* from `before` to `after`" (+ " from LabVision" for sync)
  - comments: "**{user}** edited the comments" with a `<details>` showing before/after
  - attachment_deleted: "**{user}** deleted *{field}*"
  - listing_created: "**{user}** filed Command Center listing #{task_id}: {after}"
  - listing_completed: "**{user}** completed listing #{task_id}: {after}"
  - Empty: "No changes recorded for this sample yet. Marks and edits made from v4.0.0 on appear here." Error: "History is unavailable right now." (quiet, muted).
- CSS: timeline with a 1px `var(--border-light)` rail, 22px monochrome avatars (`var(--bg-sunken)` bg, `var(--text-muted)` text), 12.5px text, `time` 11px `var(--text-subtle)`; segmented control neutral (active = `var(--bg-card)` + `var(--e1)` on `var(--bg-sunken)` track). No accent colour.
- **◷ link**: an `<a id="activity-link" class="btn btn-icon" href="/activity" target="_blank" rel="noopener" title="Time online" aria-label="Time online">` with an inline 16px clock SVG (stroke `currentColor`), immediately before `#restart-btn`.
- **Restart**: in the existing handler (~line 4375), after `const data = await resp.json()`, if `data.update` show the existing overlay/status text as `Installing ${data.update}… this takes about a minute` and poll `/api/health` until its `version` differs from the current `#app-version` text (bounded: 90 polls × 2 s), then `location.reload()`; otherwise keep today's behaviour. If `/api/health` doesn't return `version`, add it there (it's unauthenticated; version is not sensitive — `/healthz` already exposes it).

- [ ] Steps: guards failing → implement → guards + full frontend guard files pass → commit "Show shared tags, sample history and the Time Online link".

---

### Task 10: `/activity` page

**Files:** Create `templates/activity.html`, `static/js/activity.js`, `static/css/activity.css`; Test `tests/test_activity_frontend.py`

Source guards: template links `app.css` (tokens) and `activity.css`, has `#activity-root`, theme pips, `#app-version` bottom-right; `activity.js` polls with `setInterval(…, 120000)` only when `document.visibilityState === "visible"` and `isToday`, listens to `visibilitychange`, handles 401 by rendering a sign-in message with a link to `/`, escapes all user names via a local `escapeHtml`.

Layout (desktop first, works down to 360 px):
- Top bar: "Time online" (18px/600) · date stepper `‹ Thu, Sep 25 ›` + `<input type="date" min=first max=today>` · "Updated 2:14 PM" muted · theme pips (copy the markup + `applyTheme` logic, same `localStorage.theme` key, so the two pages agree).
- Summary strip: four stat tiles — **Online now** (count + green dot if > 0), **People**, **Hours online** (1 decimal), **Changes**. Numbers 22px/600 tabular-nums, labels 11px uppercase muted.
- Chart: CSS grid. Left gutter 48px with hour labels every hour (`7 AM`), faint hour gridlines (`var(--border-light)`), a "now" line when viewing today (1px `var(--text)` at 40% + tiny label). One column per person (min 72px, max 160px, horizontal scroll inside the chart only if > fits): header = initials avatar + name + total `3h 12m`. Inside: online spans as absolutely positioned rounded bars (`var(--bg-active)`, 1px `var(--border)`), editing blocks drawn over them narrower and solid (`var(--text)` at 78% opacity in light, 85% in dark). Open span: top edge gets a green 6px dot with a soft pulse (disabled under `prefers-reduced-motion`).
- Hover/focus card (one floating element, positioned near the pointer, keyboard focus supported — bars are `tabindex="0"` with `aria-label`): name; "8:02 AM – 11:47 AM · 3h 45m"; "24 changes: 18 marks, 5 test edits, 1 listing"; for an editing block: "9:01 – 9:18 AM · 7 changes".
- Empty day: centred muted text "Nobody was signed in on this day." First-run: "Time online is recorded from v4.0.0 on."
- Errors: a quiet inline banner "Couldn't load this day — retrying" with backoff (2 min poll continues).
- Logging: `console.debug("[activity]", …)` for fetch timings behind a `DEBUG = localStorage.activityDebug === "1"` flag.

- [ ] Steps: guards failing → implement → run → commit "Add the Time Online page".

---

### Task 11: Docs, full verification, performance review, release

- [ ] Update `CLAUDE.md`: new modules in Architecture (`shared_store.py`, `presence.py`, `activity.py`, `restart_update.py`), `coa_shared.db` under Persistent state (what it holds, that it is the truth for verdicts and the 12 h ledger is now only an offline fallback, the one-time ledger migration), shared-verdict fan-out rule (`_share_verdict` + `?mode=` on `/api/tabs`), the restart→updater handshake, and new tests in the testing list. Update `DEPLOY-SETUP.md`: copy the new `deploy/updater/updater.py` to `C:\ASAPApps\updater\` once and restart the updater task.
- [ ] Full suite: `.venv/bin/pytest -o addopts="" -q` → all pass (report count).
- [ ] Smoke boot: `COA_DATA_DIR=$(mktemp -d) PORT=5599 COA_NO_TRAY=1 .venv/bin/python app.py &` then `curl -s localhost:5599/healthz`, `curl -s -o /dev/null -w '%{http_code}' localhost:5599/activity` (200), `curl -s localhost:5599/api/activity` (401); kill it. Check `coa_shared.db` was created in the temp dir.
- [ ] Commit docs; push `main`; `git tag -a v4.0.0 -m "Shared marks with INFO/TEST tags, per-sample history, Time Online, Restart installs a staged update"`; `git push origin v4.0.0`; confirm `gh run list --workflow=release.yml -L 1` succeeds and `gh release view v4.0.0` lists the zip + sha256.

---

## Revisions after critic review (supersede the task text above where they conflict)

Two critic passes (Tasks 1–3 and Task 4) changed the module APIs. Tasks 5–10
must use these, not the earlier snippets.

### Store API (shared_store.py, after hardening)

- `apply_mark(lab_id, mode, outcome, *, by, reason="", cc_task_id=None, sample_id=None, tab=None)`
  with `outcome ∈ {"good","bad","cleared"}` — ONE transaction: reads the previous
  outcome, upserts the verdict (or a **tombstone** `outcome="cleared"`), inserts
  the `mark`/`unmark` history row. Returns `{"before": prev_or_None, "verdicts": {mode: v}}`
  or `None` on failure.
- `verdicts_for(ids)` returns tombstones too (`outcome == "cleared"`). **Absent key
  = no shared record → leave the local record alone.** Only an explicit tombstone
  (or a different shared verdict) changes a session's record. So a store outage
  never un-judges anyone, and the one-time ledger migration is still useful (to
  share pre-v4 marks) but no longer load-bearing.
- `event_marks_between(start, end)` (user, at, kind only) is what `/api/activity` uses.
- Reads use a bounded reader pool; writes one locked writer; busy/locked never
  drops the connection.

### Presence API (presence.py, after hardening)

- `touch(user)` is memory-only (call it from `track_activity` and heartbeat).
- `flush()` writes pending opens/touches/closes in one transaction — call it from
  `_session_cleanup_worker` every cycle (and it is cheap when nothing is dirty).
- `end(user, reason)` / `sweep()` queue closes; `close_all("restart")` then
  `flush()` from `_graceful_shutdown` (before exit) so a clean restart loses nothing.

### Task 5 (restart) — revised

- **Drop the 3 AM hookup entirely.** `_auto_restart_worker` is unchanged. The
  updater's own `auto_switch` idle policy owns unattended deploys.
- `request_restart(source, *, by=None)` — `by` is the reviewer's name from
  `/api/restart` (`ustate.name`), `"tray"` from the tray. Single-flight: a
  module-level `_restart_lock` + `_restart_pending: Optional[str|bool]`; a
  second call while one is pending returns the pending tag (or None) and does
  nothing else.
- Flow: `tag = restart_update.staged_update(DATA_DIR, APP_VERSION)` (upgrade-only
  now). If tag: flush logs + `state.presence.close_all("restart"); state.presence.flush()`
  first (the switch kills us with taskkill /F, skipping `_graceful_shutdown`), then
  `write_switch_request(...)`. If the write returns False → `clear_switch_files`
  and normal restart.
- `_await_switch(tag, pickup=PICKUP_SECONDS, accepted_wait=45.0)`: poll every
  second (bounded) `read_switch_outcome`:
  - `refused` → log WARNING with `why`, `clear_switch_files`, normal restart now.
  - `accepted` → wait up to `accepted_wait` to be killed, then log WARNING,
    `clear_switch_files`, normal restart.
  - marker still present after `pickup` → `withdraw_switch_request`; if it
    returns True → normal restart; if False (updater claimed it in the same
    instant) → keep polling for the outcome for up to `accepted_wait`.
- At startup (`AppState.__init__`): `n = restart_update.clear_switch_files(DATA_DIR)`;
  if n: WARNING "removed N leftover switch file(s) — a new process means the
  restart already happened".
- `/api/restart` returns `update: tag|null`. Tests per the Task 4 critic list:
  second concurrent request is a no-op; write failure falls back with no marker
  left; startup clears leftovers; refused fast path; accepted-but-not-killed
  fallback.

### Task 6 (shared verdicts) — revised

- `_share_verdict` calls `state.shared.apply_mark(...)` once (outcome `"cleared"`
  for uncheck). If it returns None: log WARNING, still broadcast nothing, keep the
  local verdict (the per-account ledger keeps it) — the mark must never fail.
  Otherwise fan out using the returned verdict, then broadcast `tags` computed
  from `result["verdicts"]` merged with a fresh `verdicts_for([lab_id])` only if
  the other mode is needed (or just call `verdicts_for` once — one indexed read).
  Broadcast `sample_event` for the lab_id (the history row was written by apply_mark;
  do NOT call record_event again for marks).
- `_apply_shared(ustate, rec, verdict)`: `verdict is None` (absent) → no change.
  `outcome == "cleared"` → un-judge only if the local record is judged. good/bad →
  apply as before. `_tags_from` ignores tombstones.
- `get_tab`: as before, but absent keys never un-judge (see above).
- Migration: insert-only via `set_verdict` when `verdicts_for` shows no row at
  all for that (lab_id, mode) — a tombstone counts as a row (someone cleared it).

### Task 7 — unchanged except `_sample_event` is used for non-mark kinds only.

### Task 8 — `/api/activity` uses `event_marks_between` and passes
`online=state.presence.online()`, `is_today=...`, `truncated=` (True when either
spans or events hit `MAX_RANGE_ROWS`) into `activity.build_day` (see its new kwargs).

### Task 7b: Changes made outside COA Reviewer (added 2026-09-25 at the user's request)

Spec §3b. Files: `shared_store.py` (table `field_snapshots`, `observe()`,
`EVENT_KINDS += ("external_change",)`, optional `snapshot=(source, field, value)`
kwarg on `record_event` and `apply_mark` untouched), `app.py` (observation
points + own-edit snapshots), `static/js/app.js` (render `external_change`).
Tests: `tests/test_external_changes.py` (store-level: baseline silent, diff
records event with before/after/since, normalisation 12 vs 12.00 and whitespace,
own edit via record_event(snapshot=…) not reported later, bounded fields, store
down → no raise) and route-level (GET tests/sample-info/comments with a
monkeypatched api_client whose values changed between two reads → one
external_change row; own PATCH then GET → none; LabVision operator becomes the
actor). Comments: snapshot on UploadQueue success only (find `_process_comment`).

### Task 5 — second-pass additions (Task 4 critic, round 2)

- **N2 — never self-respawn once the updater has accepted.** After `accepted`
  (or N3's "claimed, outcome unknown"), if this process is not killed within
  `accepted_wait`, flush logs + presence and `os._exit(0)` **without** spawning
  a replacement — add `respawn: bool = True` to `_graceful_shutdown` and pass
  False. The updater's `supervise()` restarts the app within ~20 s once its
  `switching` marker is gone; a self-respawned child could survive the
  updater's port-based kill and double-bind the port (the 2026-07-31 incident).
  Also: any normal-restart fallback must not respawn while `DATA_DIR/switching`
  exists — in that case exit without respawn too.
- **N3 — marker gone, no outcome file.** If the marker has disappeared and
  `read_switch_outcome` returns None for 3 consecutive polls, treat it as
  "claimed, outcome unknown": wait up to `accepted_wait`, then the N2 exit.
- **N4 — match the outcome to this request.** Ignore any outcome whose `at`
  differs from the `at` this process wrote (a leftover from an earlier run).

### Task 6 — pending writes (store critic, round 2, N6)

Tombstones fix "a mark that never reached the store", but not "a failed write
over an existing shared verdict" (shared says good; Dana unchecks; apply_mark
fails; the next get_tab re-applies good and her uncheck silently reverts).

- Add a process-wide `PendingVerdicts` (small class in app.py or a tiny module):
  bounded dict `(lab_id, mode) → {outcome, by, reason, cc_task_id, sample_id, tab, at}`,
  `MAX_PENDING = 2000` (WARNING when full; oldest dropped with an ERROR naming it).
- `_share_verdict`: if `apply_mark` returns None → store in PendingVerdicts, log
  WARNING, the mark still succeeds for the reviewer (their local record and ledger
  hold it), fan-out uses the pending verdict.
- `get_tab` and fan-out: a pending entry for (lab_id, mode) wins over the store's
  verdict.
- `_session_cleanup_worker` retries pending entries each cycle (bounded: at most
  200 per cycle) via `apply_mark`; on success remove and broadcast `tags` +
  `sample_event`.
- Tests: failed apply_mark then get_tab does not revert; retry succeeds later and
  the history row appears; bounded size.

### Task 6 — pending writes are timestamped (store critic, final pass)

A retry must never overwrite newer work. So:
- `SharedStore.apply_mark(..., at: Optional[float] = None)` — the original mark
  time (default now). The verdict upsert is conditional:
  `ON CONFLICT(lab_id, mode) DO UPDATE SET … WHERE verdicts.at <= excluded.at`.
  If a newer verdict exists, the verdict is left alone but the history row is
  still inserted at the original `at` (with `before` = the verdict that was in
  force at that time if cheaply knowable, else the current one, and
  `detail.superseded = true`). Return value adds `"applied": bool`.
- In `_run`'s busy/keep branch: `if conn.in_transaction: conn.execute("ROLLBACK")`.
- `PendingVerdicts` entries carry `at`. In `get_tab` and fan-out a pending entry
  wins only when `pending.at > store_verdict.at` (or no store verdict).
- Any successful `apply_mark` on a (lab_id, mode) removes an older pending entry
  for it.
- Test: Dana's write fails at 10:00 → Sam marks Bad at 10:05 (succeeds) → retry
  of Dana's 10:00 uncheck → Sam's Bad survives, history shows Dana's uncheck at
  10:00 flagged superseded, and get_tab shows Bad.
