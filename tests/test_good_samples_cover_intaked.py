"""Marks made in Info mode reach the Good Samples list and the export.

Info mode reviews on the Intaked tab. Its marks were recorded like any
other (``/api/mark`` does not care which tab), but neither popup offered
Intaked and the server did not count it as link-eligible, so a reviewer who
checked sample information all morning opened Good Samples and got
"No Good samples found in the selected tabs".

Server: Intaked is a review tab like the others, in the default tab list
and among the tabs whose Good samples get a QBench link. Client: both popups
list it, the Good Samples popup checks it by default in Info mode, and each
tab's row says how many Good marks it holds so the choice is informed.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    start = APP_JS.index(f"function {name}(")
    m = re.compile(r"\n(?:async )?function ").search(APP_JS, start + 1)
    return APP_JS[start:m.start() if m else len(APP_JS)]


@pytest.fixture
def reviewed(monkeypatch):
    """A session whose reviewer marked on Intaked and on Yesterday."""
    import app as app_module
    from app import SampleRecord, UserState

    monkeypatch.setattr(app_module.state, "labcore", MagicMock())
    monkeypatch.setattr(app_module, "ARCHIVE_DIR", Path(pytest.importorskip("tempfile").mkdtemp()))

    uid = "test-uid-intaked-good"
    ustate = UserState(uid, "RC")
    for tab, lab, sid, outcome, reason in (
        ("Intaked", "091626-50001", 501, "Good", ""),
        ("Intaked", "091626-50002", 502, "Bad", "Wrong tank"),
        ("Yesterday", "091526-40001", 401, "Good", ""),
    ):
        rec = SampleRecord(lab_id=lab, tab=tab, sample_id=sid, test_ids=[1], order_id=7)
        ustate.add_record(rec)
        ustate.record_result(rec, outcome, reason)
    with app_module._sessions_lock:
        app_module.user_sessions[uid] = ustate

    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = uid
    yield client
    with app_module._sessions_lock:
        app_module.user_sessions.pop(uid, None)


# ── server ───────────────────────────────────────────────────────────────

def test_good_links_cover_intaked_when_asked(reviewed) -> None:
    resp = reviewed.post("/api/good-links", json={"tabs": ["Intaked"]})
    assert resp.status_code == 200
    links = resp.get_json()["links"]
    assert [(l["tab"], l["count"]) for l in links] == [("Intaked", 1)]
    assert "sample_ids=501" in links[0]["url"]
    assert "sample_ids=502" not in links[0]["url"], "Bad is not Good"


def test_good_links_default_tabs_include_intaked(reviewed) -> None:
    resp = reviewed.post("/api/good-links", json={})
    tabs = [l["tab"] for l in resp.get_json()["links"]]
    assert "Intaked" in tabs and "Yesterday" in tabs


def test_export_covers_intaked_rows_and_their_link(reviewed) -> None:
    resp = reviewed.post("/api/export", json={"tabs": ["Intaked"], "include_links": True})
    assert resp.status_code == 200
    data = resp.get_json()
    assert "# -- Intaked (2 reviewed) --" in data["csv"]
    assert "091626-50001" in data["csv"] and "091626-50002" in data["csv"]
    assert len(data["links"]) == 1 and "sample_ids=501" in data["links"][0]


def test_export_default_tabs_include_intaked(reviewed) -> None:
    resp = reviewed.post("/api/export", json={})
    assert "# -- Intaked" in resp.get_json()["csv"]


# ── client ───────────────────────────────────────────────────────────────

def test_the_good_samples_popup_offers_intaked() -> None:
    assert '"Intaked"' in _fn("showGoodModal") or "RESULT_TABS" in _fn("showGoodModal")
    assert re.search(r'const RESULT_TABS = \[[^\]]*"Intaked"', APP_JS)


def test_the_export_popup_offers_intaked() -> None:
    body = _fn("showExportModal")
    assert "RESULT_TABS" in body or '"Intaked"' in body


def test_the_good_samples_popup_checks_intaked_by_default_in_info_mode() -> None:
    body = _fn("showGoodModal")
    assert "currentReviewMode" in body
    assert '"Intaked"' in body


def test_each_tab_row_says_how_many_good_marks_it_holds() -> None:
    body = _fn("showGoodModal")
    assert "goodCount(" in body
    assert 'status === "good"' in _fn("goodCount")
