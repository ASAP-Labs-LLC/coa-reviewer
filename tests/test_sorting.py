"""Tests for numeric lab_id ordering.

Feature (requested 2026-06-24): lab IDs must ALWAYS list numerically smallest
→ largest in every tab. They were never sorted (QBench API order). The fix
sorts in UserState.get_tab_records so it's enforced in one place for all tabs.
lab_id format is MMDDYY-NNNNN; within a tab the date prefix is constant, so
the meaningful key is the trailing sample number.
"""

from __future__ import annotations

import pytest

pytest.importorskip("flask")

import app  # noqa: E402


def test_lab_sort_key_orders_by_numeric_tail() -> None:
    ids = ["061226-35280", "061226-35266", "061226-35243", "061226-35300"]
    assert sorted(ids, key=app._lab_sort_key) == [
        "061226-35243", "061226-35266", "061226-35280", "061226-35300",
    ]


def test_lab_sort_key_is_numeric_not_lexicographic() -> None:
    # String sort would put "...-100" before "...-99"; numeric must not.
    ids = ["061226-100", "061226-99", "061226-9"]
    assert sorted(ids, key=app._lab_sort_key) == [
        "061226-9", "061226-99", "061226-100",
    ]


def test_lab_sort_key_handles_malformed_id_without_crashing() -> None:
    ids = ["061226-35266", "weird-id", "061226-35200"]
    out = sorted(ids, key=app._lab_sort_key)
    # Numeric ones still ordered; the malformed one is pushed to the end.
    assert out[:2] == ["061226-35200", "061226-35266"]
    assert out[-1] == "weird-id"


def test_get_tab_records_returns_sorted_smallest_to_largest() -> None:
    us = app.UserState("uid-1", "Tester")
    for lab in ["062226-35279", "062226-35280", "062226-35266", "062226-35243"]:
        us.add_record(app.SampleRecord(lab_id=lab, tab="Yesterday"))
    # A record in another tab must not leak in.
    us.add_record(app.SampleRecord(lab_id="062226-10000", tab="Due Out"))

    labs = [r.lab_id for r in us.get_tab_records("Yesterday")]
    assert labs == ["062226-35243", "062226-35266", "062226-35279", "062226-35280"]
