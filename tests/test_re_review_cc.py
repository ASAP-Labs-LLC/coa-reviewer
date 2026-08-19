"""Re-review is fed by Command Center instead of the Double Check sheet.

The mapping from CC listings to Re-review rows is pulled out as a pure
function so it can be tested without QBench: the surrounding loop resolves
each lab_id to sample/test ids over the network, which is not what's
interesting here.

Per the design decision, Re-review shows **all** open ``double_check``
listings regardless of which program filed them — so listings raised in
LabVision also reach the reviewer.
"""

from __future__ import annotations

import pytest

from app import cc_tasks_to_re_review_entries


def _task(**kw):
    base = {
        "id": 1,
        "type": "double_check",
        "status": "open",
        "initial_problem": "problem",
        "customer": "Acme",
        "department": "",
        "samples": [{"lab_id": "073126-41552"}],
        "latest_update": "",
        "latest_update_by": "",
    }
    base.update(kw)
    return base


def test_keeps_double_check_listings() -> None:
    entries = cc_tasks_to_re_review_entries([_task()])
    assert [e["lab_id"] for e in entries] == ["073126-41552"]


def test_ignores_listing_types_that_are_not_double_check() -> None:
    """Maintenance and customer-clarification listings are real Command Center
    work, but they are not COA re-reviews."""
    tasks = [
        _task(id=1, type="maintenance", samples=[{"lab_id": "A"}]),
        _task(id=2, type="customer_clarification", samples=[{"lab_id": "B"}]),
        _task(id=3, type="other", samples=[{"lab_id": "C"}]),
        _task(id=4, type="double_check", samples=[{"lab_id": "D"}]),
    ]
    assert [e["lab_id"] for e in cc_tasks_to_re_review_entries(tasks)] == ["D"]


def test_expands_a_multi_sample_listing_into_one_entry_per_sample() -> None:
    """A single listing can cover a whole batch; each sample still needs its
    own COA reviewed."""
    task = _task(samples=[
        {"lab_id": "073126-41552"},
        {"lab_id": "073126-41553"},
        {"lab_id": "073126-41554"},
    ])
    entries = cc_tasks_to_re_review_entries([task])

    assert [e["lab_id"] for e in entries] == [
        "073126-41552", "073126-41553", "073126-41554",
    ]
    assert all(e["task"]["id"] == 1 for e in entries)


def test_a_sample_on_two_listings_appears_once() -> None:
    """LabCore dedups on create, but force_create and consolidation can still
    put one sample on two active listings. The tab must not show it twice."""
    tasks = [
        _task(id=9, samples=[{"lab_id": "073126-41552"}]),
        _task(id=4, samples=[{"lab_id": "073126-41552"}]),
    ]
    entries = cc_tasks_to_re_review_entries(tasks)

    assert len(entries) == 1
    assert entries[0]["task"]["id"] == 9, "should keep the first (newest) listing"


def test_skips_listings_with_no_samples_attached() -> None:
    """A listing with no lab_id has no COA to pull up."""
    assert cc_tasks_to_re_review_entries([_task(samples=[])]) == []


def test_skips_blank_lab_ids() -> None:
    task = _task(samples=[{"lab_id": ""}, {"lab_id": "   "}, {"lab_id": "OK"}])
    assert [e["lab_id"] for e in cc_tasks_to_re_review_entries([task])] == ["OK"]


def test_excludes_completed_listings() -> None:
    """The caller asks for view=active, but a listing completed between the
    read and the render must not resurface as work to do."""
    tasks = [
        _task(id=1, status="completed", samples=[{"lab_id": "DONE"}]),
        _task(id=2, status="urgent", samples=[{"lab_id": "LIVE"}]),
    ]
    assert [e["lab_id"] for e in cc_tasks_to_re_review_entries(tasks)] == ["LIVE"]


def test_carries_the_listing_details_the_reviewer_needs() -> None:
    """The panel shows why the sample is back, and where it stands."""
    task = _task(
        id=12, status="urgent", initial_problem="Potency reads low",
        customer="Acme", department="Lab",
        latest_update="Re-ran, awaiting confirmation", latest_update_by="JD",
    )
    entry = cc_tasks_to_re_review_entries([task])[0]

    assert entry["task"]["id"] == 12
    assert entry["task"]["status"] == "urgent"
    assert entry["task"]["initial_problem"] == "Potency reads low"
    assert entry["task"]["customer"] == "Acme"
    assert entry["task"]["department"] == "Lab"
    assert entry["task"]["latest_update"] == "Re-ran, awaiting confirmation"
    assert entry["task"]["latest_update_by"] == "JD"


def test_tolerates_malformed_listings() -> None:
    """LabCore's payload is not a contract we control; a missing samples key
    or a non-dict entry must not take down the whole tab."""
    tasks = [
        {"id": 1, "type": "double_check", "status": "open"},          # no samples
        {"id": 2, "type": "double_check", "status": "open", "samples": None},
        _task(id=3, samples=[{"lab_id": "GOOD"}]),
    ]
    assert [e["lab_id"] for e in cc_tasks_to_re_review_entries(tasks)] == ["GOOD"]


def test_handles_an_empty_board() -> None:
    assert cc_tasks_to_re_review_entries([]) == []
