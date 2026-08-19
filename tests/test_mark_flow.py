"""Marking, un-marking, and bulk regeneration.

Covers the behavior changes that come with Command Center replacing the
Double Check sheet:

* flagging Bad no longer writes a spreadsheet row — the listing is created
  through /api/cc/tasks and its id is recorded against the sample;
* marking Good no longer silently closes anything (the reviewer is asked);
* a sample can be un-marked, and that has to actually affect the exports;
* every unmarked sample on a tab can be re-rendered in one go.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")


@pytest.fixture
def marking(monkeypatch):
    """(client, ustate) with three records on two tabs."""
    import app as app_module
    from app import SampleRecord, UserState

    monkeypatch.setattr(app_module.state, "labcore", MagicMock())

    uid = "test-uid-mark"
    ustate = UserState(uid, "RC")
    for tab, lab in (("Yesterday", "073126-41552"),
                     ("Yesterday", "073126-41553"),
                     ("Due Out", "073126-41554")):
        ustate.add_record(SampleRecord(lab_id=lab, tab=tab, sample_id=1, test_ids=[9]))
    with app_module._sessions_lock:
        app_module.user_sessions[uid] = ustate

    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = uid

    yield client, ustate

    with app_module._sessions_lock:
        app_module.user_sessions.pop(uid, None)


def _mark(client, outcome, lab="073126-41552", tab="Yesterday", **extra):
    return client.post("/api/mark", json={
        "tab": tab, "lab_id": lab, "outcome": outcome, **extra,
    })


# ── flagging Bad ─────────────────────────────────────────────────────────

def test_marking_bad_sets_status_and_reason(marking) -> None:
    client, ustate = marking
    resp = _mark(client, "bad", reason="Potency reads low")

    assert resp.status_code == 200
    rec = ustate.records[("Yesterday", "073126-41552")]
    assert rec.status == "bad"
    assert rec.reason == "Potency reads low"


def test_marking_bad_no_longer_writes_to_the_spreadsheet(marking) -> None:
    """Command Center replaces the Double Check sheet outright. A stray write
    here would keep the old logbook half-alive and diverging."""
    import app as app_module
    client, _ = marking
    _mark(client, "bad", reason="Potency reads low")

    # There is no sheets client left to call at all.
    assert not hasattr(app_module.state, "sheets")


def test_marking_bad_records_the_listing_it_was_filed_under(marking) -> None:
    """The frontend creates the listing first, then marks. Keeping the id lets
    the sample link back to its listing without another lookup."""
    client, ustate = marking
    _mark(client, "bad", reason="x", cc_task_id=42)

    assert ustate.records[("Yesterday", "073126-41552")].cc_task_id == 42


def test_marking_bad_still_requires_a_reason(marking) -> None:
    client, ustate = marking
    resp = _mark(client, "bad", reason="   ")

    assert resp.status_code == 400
    assert ustate.records[("Yesterday", "073126-41552")].status != "bad"


# ── marking Good ─────────────────────────────────────────────────────────

def test_marking_good_sets_status(marking) -> None:
    client, ustate = marking
    assert _mark(client, "good").status_code == 200
    assert ustate.records[("Yesterday", "073126-41552")].status == "good"


def test_marking_good_does_not_silently_close_anything(marking) -> None:
    """The old code auto-completed the sheet row under hardcoded initials
    ("CR"). Closing a listing is now an explicit choice the reviewer makes in
    the complete / continue / back-out prompt."""
    import app as app_module
    client, _ = marking
    _mark(client, "good")

    assert app_module.state.labcore.complete_task.call_count == 0


# ── session results / exports ────────────────────────────────────────────

def test_a_marked_sample_appears_in_the_session_results(marking) -> None:
    client, ustate = marking
    _mark(client, "bad", reason="Potency reads low")

    assert len(ustate.session_results) == 1
    assert ustate.session_results[0]["lab_id"] == "073126-41552"
    assert ustate.session_results[0]["outcome"] == "Bad"


def test_re_marking_a_sample_replaces_its_result_rather_than_adding_one(marking) -> None:
    """session_results was append-only, so changing your mind about a sample
    exported it twice with contradictory outcomes."""
    client, ustate = marking
    _mark(client, "bad", reason="Potency reads low")
    _mark(client, "good")

    rows = [r for r in ustate.session_results if r["lab_id"] == "073126-41552"]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "Good"
    assert rows[0]["reason"] == ""


def test_the_same_lab_id_on_two_tabs_is_tracked_separately(marking) -> None:
    """Search and Yesterday can both hold one sample; they are distinct rows."""
    import app as app_module
    from app import SampleRecord
    _, ustate = marking
    ustate.add_record(SampleRecord(lab_id="073126-41552", tab="Search",
                                   sample_id=1, test_ids=[9]))
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = ustate.uid

    _mark(client, "good")
    _mark(client, "bad", tab="Search", reason="different call")

    assert len(ustate.session_results) == 2


# ── un-marking ───────────────────────────────────────────────────────────

def test_unmarking_returns_a_rendered_sample_to_ready_not_pending(marking) -> None:
    """Un-marking must not look like the COA was thrown away.

    The frontend derives `has_preview` from status
    (`ready`/`good`/`bad` -> true, see updateSampleStatus in app.js), so
    setting `pending` here made an already-rendered sample display as if it
    had never rendered — the preview visibly vanished even though the PDF was
    still cached server-side. `ready` means "rendered, awaiting review", which
    is exactly what an un-marked sample is.
    """
    client, ustate = marking
    rec = ustate.records[("Yesterday", "073126-41552")]
    rec.preview_url = "https://qbench/preview/abc"

    _mark(client, "bad", reason="Potency reads low")
    resp = _mark(client, "uncheck")

    assert resp.status_code == 200
    assert rec.status == "ready"
    assert rec.reason == ""


def test_unmarking_keeps_the_cached_pdf(marking) -> None:
    """Nothing about un-marking requires re-rendering; the reviewer just
    changed their mind about the verdict."""
    client, ustate = marking
    rec = ustate.records[("Yesterday", "073126-41552")]
    rec.preview_url = "https://qbench/preview/abc"
    ustate.pdf_cache["073126-41552"] = b"%PDF-cached"

    _mark(client, "good")
    _mark(client, "uncheck")

    assert ustate.pdf_cache.get("073126-41552") == b"%PDF-cached"
    assert rec.preview_url == "https://qbench/preview/abc"


def test_unmarking_a_sample_that_never_rendered_stays_pending(marking) -> None:
    """With no preview there is nothing to return to — `ready` would promise
    a COA the pane cannot show."""
    client, ustate = marking
    rec = ustate.records[("Yesterday", "073126-41552")]
    rec.preview_url = None

    _mark(client, "bad", reason="x")
    _mark(client, "uncheck")

    assert rec.status == "pending"


def test_unmarking_removes_the_sample_from_the_exports(marking) -> None:
    """This is the point of the button: un-marking has to affect Export CSV
    and Good Samples, not just the colour in the list."""
    client, ustate = marking
    _mark(client, "good")
    _mark(client, "bad", lab="073126-41553", reason="x")
    assert len(ustate.session_results) == 2

    _mark(client, "uncheck")

    assert [r["lab_id"] for r in ustate.session_results] == ["073126-41553"]


def test_unmarking_an_unmarked_sample_is_harmless(marking) -> None:
    client, ustate = marking
    assert _mark(client, "uncheck").status_code == 200
    assert ustate.session_results == []


def test_unmarking_clears_the_recorded_listing_id(marking) -> None:
    client, ustate = marking
    _mark(client, "bad", reason="x", cc_task_id=42)
    _mark(client, "uncheck")

    assert ustate.records[("Yesterday", "073126-41552")].cc_task_id is None


def test_marking_an_unknown_sample_is_a_404(marking) -> None:
    client, _ = marking
    assert _mark(client, "uncheck", lab="nope").status_code == 404


# ── regenerate pending ───────────────────────────────────────────────────

def test_regenerate_pending_resets_only_the_unmarked_samples(marking) -> None:
    """Re-rendering a sample already judged would throw away the reviewer's
    work-in-progress view of it for no reason."""
    client, ustate = marking
    _mark(client, "good")
    ustate.records[("Yesterday", "073126-41553")].preview_url = "http://old"

    resp = client.post("/api/regenerate-pending", json={"tab": "Yesterday"})

    assert resp.status_code == 200
    assert resp.get_json()["count"] == 1
    assert ustate.records[("Yesterday", "073126-41552")].status == "good"
    assert ustate.records[("Yesterday", "073126-41553")].preview_url is None


def test_regenerate_pending_includes_samples_that_errored(marking) -> None:
    """Expired Amazon links and cache misses land as `error` — those are
    exactly the ones worth retrying in bulk."""
    client, ustate = marking
    ustate.records[("Yesterday", "073126-41552")].status = "error"
    ustate.records[("Yesterday", "073126-41553")].status = "ready"

    assert client.post("/api/regenerate-pending",
                       json={"tab": "Yesterday"}).get_json()["count"] == 2


def test_regenerate_pending_leaves_other_tabs_alone(marking) -> None:
    """Only the tab the reviewer is looking at."""
    client, ustate = marking
    ustate.records[("Due Out", "073126-41554")].preview_url = "http://keep"

    client.post("/api/regenerate-pending", json={"tab": "Yesterday"})

    assert ustate.records[("Due Out", "073126-41554")].preview_url == "http://keep"


def test_regenerate_pending_clears_cached_pdfs_for_the_reset_samples(marking) -> None:
    client, ustate = marking
    ustate.pdf_cache["073126-41552"] = b"stale"

    client.post("/api/regenerate-pending", json={"tab": "Yesterday"})

    assert "073126-41552" not in ustate.pdf_cache


def test_regenerate_pending_on_an_unknown_tab_reports_nothing_to_do(marking) -> None:
    client, _ = marking
    resp = client.post("/api/regenerate-pending", json={"tab": "Nope"})

    assert resp.status_code == 200
    assert resp.get_json()["count"] == 0


# ── regenerate an explicit selection ─────────────────────────────────────

def test_regenerate_selected_resets_exactly_the_listed_samples(marking) -> None:
    client, ustate = marking
    for lab in ("073126-41552", "073126-41553"):
        ustate.records[("Yesterday", lab)].preview_url = "http://old"

    resp = client.post("/api/regenerate-selected", json={
        "tab": "Yesterday", "lab_ids": ["073126-41552"]})

    assert resp.get_json()["count"] == 1
    assert ustate.records[("Yesterday", "073126-41552")].preview_url is None
    assert ustate.records[("Yesterday", "073126-41553")].preview_url == "http://old"


def test_regenerate_selected_honours_an_explicitly_picked_marked_sample(marking) -> None:
    """Different from Regenerate Pending on purpose. Pending skips judged
    samples because it is a blanket sweep; picking one by hand is a direct
    instruction, so it is obeyed."""
    client, ustate = marking
    _mark(client, "good")
    ustate.records[("Yesterday", "073126-41552")].preview_url = "http://old"

    resp = client.post("/api/regenerate-selected", json={
        "tab": "Yesterday", "lab_ids": ["073126-41552"]})

    assert resp.get_json()["count"] == 1
    assert ustate.records[("Yesterday", "073126-41552")].preview_url is None


def test_regenerate_selected_ignores_lab_ids_that_are_not_on_the_tab(marking) -> None:
    client, ustate = marking
    resp = client.post("/api/regenerate-selected", json={
        "tab": "Yesterday", "lab_ids": ["073126-41552", "nope", "073126-41554"]})

    # 41554 is on Due Out, "nope" does not exist: only 41552 counts.
    assert resp.get_json()["count"] == 1


def test_regenerate_selected_with_an_empty_selection_does_nothing(marking) -> None:
    client, _ = marking
    resp = client.post("/api/regenerate-selected", json={"tab": "Yesterday", "lab_ids": []})
    assert resp.status_code == 200
    assert resp.get_json()["count"] == 0
