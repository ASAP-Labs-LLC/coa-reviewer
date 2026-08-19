"""Append-only audit log of everything reviewers change.

Written to the network share so the record outlives any one machine, and so
the lab can answer "who changed this result, and when" without a database.

Shape
-----
* **One file per category.** "Who edited this result" and "who signed this COA
  off" are different questions asked by different people; keeping them apart
  makes each one a single grep instead of a filter.
* **Month-partitioned** (``reviews-2026-07.jsonl``). A share is a bad place
  for an unbounded file, and "what happened in July" becomes one file rather
  than a date range.
* **JSON Lines.** Appendable without rewriting, greppable by eye, and a
  truncated tail costs one record rather than the whole file.

The one hard rule: **logging must never break reviewing.** A dropped share
mount, a full disk, or a permissions problem degrades to a missing log line —
never to a failed mark. Every public method swallows its own errors.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)

#: The only categories that may be written. Constrained on purpose — a typo'd
#: category would silently create an orphan file nobody ever reads.
CATEGORIES = (
    "reviews",          # good / bad / uncheck decisions
    "command_center",   # listings created + completed from here
    "qbench_edits",     # test results, sample-info fields, comments, attachments
    "sessions",         # login / logout, and by which card or account
)

#: Fields the log owns. A caller cannot overwrite them — otherwise a record
#: could misattribute a change or carry a forged timestamp.
_RESERVED = ("ts", "category", "event")


class ChangeLog:
    """Thread-safe writer for the per-category audit logs."""

    def __init__(self, directory: Path | str,
                 now: Callable[[], datetime] | None = None) -> None:
        self._dir = Path(directory)
        self._now = now or datetime.now
        self._lock = threading.Lock()

    @property
    def directory(self) -> Path:
        return self._dir

    def path_for(self, category: str, when: datetime | None = None) -> Path:
        when = when or self._now()
        return self._dir / f"{category}-{when:%Y-%m}.jsonl"

    def record(self, category: str, event: str, /, **fields: Any) -> None:
        """Append one record. Never raises for I/O or serialisation problems.

        ``category`` and ``event`` are positional-only so a caller passing
        ``category=...`` as a field lands in ``fields`` (where ``_RESERVED``
        strips it) instead of colliding with the parameter.

        ``ValueError`` for an unknown category is deliberately *not* caught:
        that is a programming error visible in tests, not a runtime condition.
        """
        if category not in CATEGORIES:
            raise ValueError(
                f"Unknown change-log category {category!r}. "
                f"Known: {', '.join(CATEGORIES)}"
            )

        now = self._now()
        # Caller fields first, then the reserved ones, so the log's own values
        # always win over anything passed in.
        record: Dict[str, Any] = {k: v for k, v in fields.items()
                                  if k not in _RESERVED}
        record.update({
            "ts": now.isoformat(timespec="seconds"),
            "category": category,
            "event": event,
        })

        try:
            # default=str keeps an odd value (a Path, a model object) from
            # costing the whole record.
            line = json.dumps(record, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            logger.warning("change_log: could not serialise a %s/%s record",
                           category, event)
            return

        path = self.path_for(category, now)
        try:
            # One lock for all categories: writes are tiny and infrequent
            # relative to review work, and a single lock removes any chance of
            # two threads interleaving inside one line.
            with self._lock:
                self._dir.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError as exc:
            # Share unmounted, disk full, read-only: log locally and move on.
            logger.warning("change_log: could not write to %s: %s", path, exc)

    # ── convenience wrappers, one per category ───────────────────────────

    def review(self, event: str, **fields: Any) -> None:
        self.record("reviews", event, **fields)

    def command_center(self, event: str, **fields: Any) -> None:
        self.record("command_center", event, **fields)

    def qbench_edit(self, event: str, **fields: Any) -> None:
        self.record("qbench_edits", event, **fields)

    def session(self, event: str, **fields: Any) -> None:
        self.record("sessions", event, **fields)
