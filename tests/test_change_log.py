"""Audit log of every change reviewers make, on the network share.

One file per category so "who changed this result" and "who signed this COA
off" are separate greps, month-partitioned so a file never grows unbounded on
a share, JSON Lines so it appends safely and stays greppable.

The hard requirement is that logging can never break reviewing: a full disk,
a dropped share mount, or a permissions problem must degrade to "no log
entry", never to a failed mark.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime

import pytest

from change_log import CATEGORIES, ChangeLog


@pytest.fixture
def log(tmp_path):
    return ChangeLog(tmp_path / "changelog")


def _read(directory, category, when="2026-07"):
    path = directory / f"{category}-{when}.jsonl"
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ── basic writing ────────────────────────────────────────────────────────

def test_writes_a_record_to_its_category_file(tmp_path) -> None:
    log = ChangeLog(tmp_path / "cl", now=lambda: datetime(2026, 7, 31, 14, 5))
    log.record("reviews", "mark", user="RC", lab_id="073126-41552", outcome="Bad")

    rows = _read(tmp_path / "cl", "reviews")
    assert len(rows) == 1
    assert rows[0]["event"] == "mark"
    assert rows[0]["user"] == "RC"
    assert rows[0]["lab_id"] == "073126-41552"
    assert rows[0]["outcome"] == "Bad"


def test_every_record_is_timestamped_and_labelled(tmp_path) -> None:
    log = ChangeLog(tmp_path / "cl", now=lambda: datetime(2026, 7, 31, 14, 5, 9))
    log.record("reviews", "mark", user="RC")

    row = _read(tmp_path / "cl", "reviews")[0]
    assert row["ts"] == "2026-07-31T14:05:09"
    assert row["category"] == "reviews"


def test_creates_the_directory_if_it_is_missing(tmp_path) -> None:
    target = tmp_path / "deep" / "changelog"
    ChangeLog(target).record("reviews", "mark", user="RC")
    assert target.is_dir()


def test_appends_rather_than_overwriting(tmp_path) -> None:
    log = ChangeLog(tmp_path / "cl", now=lambda: datetime(2026, 7, 31))
    for i in range(3):
        log.record("reviews", "mark", user="RC", lab_id=f"lab{i}")

    rows = _read(tmp_path / "cl", "reviews")
    assert [r["lab_id"] for r in rows] == ["lab0", "lab1", "lab2"]


# ── separation ───────────────────────────────────────────────────────────

def test_categories_go_to_separate_files(tmp_path) -> None:
    """The point of splitting them: "who edited this result" is a different
    question from "who signed this COA off"."""
    log = ChangeLog(tmp_path / "cl", now=lambda: datetime(2026, 7, 31))
    log.record("reviews", "mark", user="RC")
    log.record("qbench_edits", "test_edit", user="RC")
    log.record("command_center", "listing_created", user="RC")
    log.record("sessions", "login", user="RC")

    for cat in ("reviews", "qbench_edits", "command_center", "sessions"):
        assert len(_read(tmp_path / "cl", cat)) == 1


def test_files_are_partitioned_by_month(tmp_path) -> None:
    """Keeps any one file small on a network share, and makes "what happened
    in July" a single file rather than a date filter."""
    d = tmp_path / "cl"
    ChangeLog(d, now=lambda: datetime(2026, 7, 31)).record("reviews", "mark", user="RC")
    ChangeLog(d, now=lambda: datetime(2026, 8, 1)).record("reviews", "mark", user="RC")

    assert (d / "reviews-2026-07.jsonl").exists()
    assert (d / "reviews-2026-08.jsonl").exists()


def test_rejects_an_unknown_category(tmp_path) -> None:
    """A typo'd category would silently create an orphan file nobody reads."""
    with pytest.raises(ValueError):
        ChangeLog(tmp_path / "cl").record("reviewz", "mark", user="RC")


def test_the_declared_categories_are_the_four_we_wire_up() -> None:
    assert set(CATEGORIES) == {"reviews", "command_center", "qbench_edits", "sessions"}


# ── robustness ───────────────────────────────────────────────────────────

def test_a_broken_destination_never_breaks_the_caller(tmp_path) -> None:
    """A dropped share mount must cost a log line, not a reviewer's mark."""
    blocker = tmp_path / "cl"
    blocker.write_text("I am a file, not a directory")   # mkdir will fail

    log = ChangeLog(blocker)
    log.record("reviews", "mark", user="RC")   # must not raise


def test_values_that_are_not_json_serialisable_do_not_raise(tmp_path) -> None:
    """Callers pass whatever they have; an odd value must not lose the record
    or crash the request."""
    log = ChangeLog(tmp_path / "cl", now=lambda: datetime(2026, 7, 31))
    log.record("reviews", "mark", user="RC", weird=object())

    rows = _read(tmp_path / "cl", "reviews")
    assert len(rows) == 1
    assert rows[0]["user"] == "RC"


def test_concurrent_writers_produce_intact_lines(tmp_path) -> None:
    """Several reviewers act at once on one server; interleaved writes must
    not corrupt a line (a half-written record makes the whole file suspect)."""
    log = ChangeLog(tmp_path / "cl", now=lambda: datetime(2026, 7, 31))

    def spam(n):
        for i in range(40):
            log.record("reviews", "mark", user=f"U{n}", lab_id=f"{n}-{i}",
                       note="x" * 200)

    threads = [threading.Thread(target=spam, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    rows = _read(tmp_path / "cl", "reviews")     # parses => every line intact
    assert len(rows) == 240


def test_reserved_fields_cannot_be_clobbered_by_a_caller(tmp_path) -> None:
    """A caller passing user=... twice, or its own ts, must not produce a
    record that misattributes the change."""
    log = ChangeLog(tmp_path / "cl", now=lambda: datetime(2026, 7, 31, 12, 0, 0))
    log.record("reviews", "mark", user="RC", ts="1999-01-01T00:00:00",
               category="sessions")

    row = _read(tmp_path / "cl", "reviews")[0]
    assert row["ts"] == "2026-07-31T12:00:00"
    assert row["category"] == "reviews"
