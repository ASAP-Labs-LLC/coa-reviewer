"""Telling "entered online" apart from "the document is missing".

Both used to render as one generic "SIF not found", so a genuinely absent
paper SIF looked identical to a customer-portal order that never had one.

Determined from live QBench data (2026-07-31, 45 orders):

    has SIF | order_request_status | count
    --------|----------------------|------
      yes   | None                 |  36
      no    | RECEIVED             |   6
      yes   | RECEIVED             |   3

Every order lacking a SIF was a portal order request (6/6), and every order
with a null request status had one (36/36). Portal orders can still carry a
SIF (3 did), so the absence of a document was never sufficient on its own —
`order_request_status` is the positive signal.
"""

from __future__ import annotations

import pytest

from app import SIF_MISSING, SIF_ONLINE_ENTRY, classify_missing_sif


def test_a_portal_order_request_is_reported_as_online_entry() -> None:
    assert classify_missing_sif({"order_request_status": "RECEIVED"}) == SIF_ONLINE_ENTRY


@pytest.mark.parametrize("status", ["SUBMITTED", "APPROVED", "PENDING", "received"])
def test_any_request_status_counts_as_a_portal_submission(status) -> None:
    """RECEIVED is the value seen in production, but the field existing at all
    means QBench recorded an order request. Hard-coding one value would
    silently mislabel the others as missing documents."""
    assert classify_missing_sif({"order_request_status": status}) == SIF_ONLINE_ENTRY


def test_no_request_status_means_the_document_is_genuinely_missing() -> None:
    """A paper order with no SIF attached is a real problem, not a shrug."""
    assert classify_missing_sif({"order_request_status": None}) == SIF_MISSING


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_request_status_is_not_evidence_of_a_portal_entry(value) -> None:
    assert classify_missing_sif({"order_request_status": value}) == SIF_MISSING


def test_an_order_we_could_not_read_is_not_claimed_to_be_online() -> None:
    """Without the order we cannot prove portal entry, and claiming it would
    reintroduce exactly the guess this replaces."""
    assert classify_missing_sif(None) == SIF_MISSING
    assert classify_missing_sif({}) == SIF_MISSING


def test_the_two_states_are_distinct() -> None:
    assert SIF_ONLINE_ENTRY != SIF_MISSING
