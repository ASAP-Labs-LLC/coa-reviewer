"""`/api/sif/<lab_id>` serves the SIF from whichever record has it.

Bug report 2026-09-17: the SIF pane showed Chrome's pretty-printed
``{"error": "SIF not available"}`` for a sample whose order does carry a
SIF. The browser only asks for the SIF when the sample on screen reports
``found``, so the server *had* the document; it just answered 404.

The route resolves the record by lab_id alone and took the first match in
insertion order. The same lab_id sits on two tabs more often than not: a
Due Out sample with an open double_check listing is also on Re-review, and
a searched lab_id is usually on a day tab too. Until v2.2.0 every sample on
every tab rendered up front, so every copy ended up with the SIF bytes and
the first match was as good as any. Previews now render on demand, so the
copy nobody looked at has no SIF, and if it was inserted first the SIF on
the copy being looked at was unreachable. `/api/pdf` never had this problem
because it skips copies without a preview.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")

LAB = "091626-41210"
SIF_BYTES = b"%PDF-1.4\n% a stand-in for the order's SIF\n%%EOF\n"


@pytest.fixture
def session(monkeypatch):
    import app as app_module
    from app import UserState

    monkeypatch.setattr(app_module.state, "labcore", MagicMock())
    uid = "test-uid-sif-copies"
    ustate = UserState(uid, "RC")
    with app_module._sessions_lock:
        app_module.user_sessions[uid] = ustate
    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = uid
    yield client, ustate
    with app_module._sessions_lock:
        app_module.user_sessions.pop(uid, None)


def _record(tab, *, with_sif: bool):
    from app import SampleRecord
    rec = SampleRecord(lab_id=LAB, tab=tab, sample_id=41210, test_ids=[1], order_id=7310)
    if with_sif:
        rec.sif_pdf_bytes = SIF_BYTES
        rec.sif_page = None  # whole document; keeps PyMuPDF out of this test
        rec.sif_total_pages = 1
        rec.sif_status = "found"
    return rec


def test_the_sif_is_served_when_an_unrendered_copy_was_inserted_first(session) -> None:
    """Due Out was pulled first and never looked at; the reviewer is on
    Re-review, where the SIF was fetched."""
    client, ustate = session
    ustate.add_record(_record("Due Out", with_sif=False))
    ustate.add_record(_record("Re-review", with_sif=True))

    resp = client.get(f"/api/sif/{LAB}")

    assert resp.status_code == 200, resp.get_json()
    assert resp.mimetype == "application/pdf"
    assert resp.data == SIF_BYTES


def test_the_sif_is_served_when_the_copy_with_it_was_inserted_first(session) -> None:
    client, ustate = session
    ustate.add_record(_record("Yesterday", with_sif=True))
    ustate.add_record(_record("Search", with_sif=False))
    resp = client.get(f"/api/sif/{LAB}")
    assert resp.status_code == 200
    assert resp.data == SIF_BYTES


def test_no_copy_has_the_sif_is_still_a_404(session) -> None:
    client, ustate = session
    ustate.add_record(_record("Due Out", with_sif=False))
    ustate.add_record(_record("Re-review", with_sif=False))
    resp = client.get(f"/api/sif/{LAB}")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "SIF not available"}


def test_an_unknown_lab_id_is_a_404(session) -> None:
    client, _ = session
    assert client.get("/api/sif/000000-00000").status_code == 404
