"""Replacing the Search tab must release the PDFs it was holding.

`pdf_cache` is keyed by lab_id and holds whole rendered COAs, so an entry that
outlives its record is unreachable memory for the life of the session — the
reviewer can no longer navigate to that sample, but its bytes stay resident.

Custom Day already gets this right: it pops each lab_id as it deletes the
record. Search deleted its records and left the cache untouched, so every
repeated search permanently orphaned another set of COAs. A reviewer who
searches repeatedly is the one who accumulates them fastest.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")

OLD = "073126-41552"
NEW = "073126-49999"


@pytest.fixture
def searcher(monkeypatch):
    """(client, ustate) with a stale Search result already cached."""
    import app as app_module
    from app import SampleRecord, UserState

    monkeypatch.setattr(app_module.state, "labcore", MagicMock())
    monkeypatch.setattr(app_module.state, "logged_in", True)

    api = MagicMock()
    api.fetch_samples_by_lab_id.return_value = []
    api.fetch_samples_by_lab_id_prefix.return_value = []
    api.fetch_tests_for_sample_ids.return_value = []
    monkeypatch.setattr(app_module.state, "api_client", api)

    uid = "test-uid-search-leak"
    ustate = UserState(uid, "RC")
    ustate.add_record(
        SampleRecord(lab_id=OLD, tab="Search", sample_id=1, test_ids=[9])
    )
    ustate.pdf_cache[OLD] = b"%PDF-old-search-hit"
    with app_module._sessions_lock:
        app_module.user_sessions[uid] = ustate

    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = uid

    yield client, ustate

    with app_module._sessions_lock:
        app_module.user_sessions.pop(uid, None)


def test_a_new_search_releases_the_previous_results_pdfs(searcher) -> None:
    client, ustate = searcher

    client.post("/api/search", json={"query": NEW})

    assert ("Search", OLD) not in ustate.records, "the stale record survived"
    assert OLD not in ustate.pdf_cache, (
        "the COA bytes for a discarded search result are still held; nothing "
        "can reach them and nothing will ever free them"
    )


def test_other_tabs_keep_their_cached_pdfs(searcher) -> None:
    """Only the Search tab is being replaced — don't evict everyone else."""
    import app as app_module
    from app import SampleRecord

    client, ustate = searcher
    keep = "073126-40001"
    ustate.add_record(
        SampleRecord(lab_id=keep, tab="Yesterday", sample_id=2, test_ids=[7])
    )
    ustate.pdf_cache[keep] = b"%PDF-yesterday"

    client.post("/api/search", json={"query": NEW})

    assert ("Yesterday", keep) in ustate.records
    assert ustate.pdf_cache.get(keep) == b"%PDF-yesterday"
