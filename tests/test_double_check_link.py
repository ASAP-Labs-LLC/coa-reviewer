"""A Good Samples list can be handed to a colleague as a link into this app.

Request 2026-09-17: "export in the good-to-go a link for the COA reviewer so
that you can open someone else's good-to-go samples and double check them."

Server: every Good Samples link (and every QBench link line in the export)
now comes with a double-check link of the form ``<origin>/?check=<lab ids>``.
The origin is the one the reviewer's browser is using, sent by the client,
because behind the Cloudflare tunnel the server cannot tell which hostname a
colleague will reach it by; anything that is not a bare ``http(s)://host``
is ignored in favour of the request's own host.

Opening that link logs the colleague in as themselves and loads exactly
those lab IDs into their Search tab, via ``/api/search`` with an explicit
``lab_ids`` list. Explicit, because the search box's multi-ID parser reads
``digits-digits`` as a numeric range: a comma-separated list of full lab IDs
was being dropped by its 500-span guard and found nothing. The parser now
recognises ``MMDDYY-NNNNN`` as a lab ID too, so a pasted list works in the
box as well.

Their marks are their own: Search rows are a separate review of the same
lab ID (``session_results`` is keyed by tab), and the ledger is per account.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

GOOD = ["091626-50001", "091626-50003"]
BAD = "091626-50002"


def _fn(name: str) -> str:
    start = APP_JS.index(f"function {name}(")
    m = re.compile(r"\n(?:async )?function ").search(APP_JS, start + 1)
    return APP_JS[start:m.start() if m else len(APP_JS)]


@pytest.fixture
def reviewed(monkeypatch, tmp_path):
    """A session with two Good marks and one Bad on Intaked, QBench 'up'."""
    import app as app_module
    from app import SampleRecord, UserState

    monkeypatch.setattr(app_module.state, "labcore", MagicMock())
    monkeypatch.setattr(app_module.state, "logged_in", True)
    monkeypatch.setattr(app_module, "ARCHIVE_DIR", tmp_path / "archive")
    (tmp_path / "archive").mkdir()
    monkeypatch.setattr(app_module, "REVIEW_STATE_DIR", tmp_path / "review_state")

    api = MagicMock()
    api.fetch_samples_by_lab_id.side_effect = lambda lab_id, **k: (
        [{"id": int(lab_id[-5:]), "lab_id": lab_id, "order_id": 9}] if lab_id in GOOD else []
    )
    api.fetch_samples_by_lab_id_prefix.return_value = []
    api.fetch_tests_for_sample_ids.side_effect = lambda ids, **k: [
        {"id": 700 + int(i), "sample_id": int(i)} for i in ids
    ]
    monkeypatch.setattr(app_module.state, "api_client", api)

    uid = "test-uid-double-check"
    ustate = UserState(uid, "RC")
    for lab, outcome, reason in ((GOOD[0], "Good", ""), (BAD, "Bad", "Wrong tank"), (GOOD[1], "Good", "")):
        rec = SampleRecord(lab_id=lab, tab="Intaked", sample_id=int(lab[-5:]), test_ids=[1], order_id=9)
        ustate.add_record(rec)
        ustate.record_result(rec, outcome, reason)
    with app_module._sessions_lock:
        app_module.user_sessions[uid] = ustate

    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = uid
    yield client, ustate, api
    with app_module._sessions_lock:
        app_module.user_sessions.pop(uid, None)


# ── the link that goes out ───────────────────────────────────────────────

def test_good_links_carry_a_double_check_link_for_the_good_lab_ids(reviewed) -> None:
    client, _, _ = reviewed
    resp = client.post("/api/good-links", json={"tabs": ["Intaked"], "origin": "https://coa.asaplabs.net"})
    link = resp.get_json()["links"][0]
    assert link["lab_ids"] == GOOD, "Good only, in review order"
    assert link["review_url"] == "https://coa.asaplabs.net/?check=091626-50001,091626-50003"


def test_the_double_check_link_uses_the_request_host_when_no_origin_is_sent(reviewed) -> None:
    client, _, _ = reviewed
    link = client.post("/api/good-links", json={"tabs": ["Intaked"]}).get_json()["links"][0]
    assert link["review_url"].startswith("http://localhost/?check=")


@pytest.mark.parametrize("bad", [
    "javascript:alert(1)",
    "https://coa.asaplabs.net/somewhere",
    "coa.asaplabs.net",
    "https://evil.example/?x=",
])
def test_an_origin_that_is_not_a_bare_host_is_ignored(reviewed, bad) -> None:
    client, _, _ = reviewed
    link = client.post("/api/good-links", json={"tabs": ["Intaked"], "origin": bad}).get_json()["links"][0]
    assert link["review_url"].startswith("http://localhost/?check="), link["review_url"]


def test_the_export_writes_the_double_check_link_next_to_the_qbench_one(reviewed) -> None:
    client, _, _ = reviewed
    resp = client.post("/api/export", json={
        "tabs": ["Intaked"], "include_links": True, "origin": "https://coa.asaplabs.net",
    })
    data = resp.get_json()
    assert "# QBench link: " in data["csv"]
    assert "# COA Reviewer double-check link: https://coa.asaplabs.net/?check=091626-50001,091626-50003" in data["csv"]
    assert data["review_links"] == ["https://coa.asaplabs.net/?check=091626-50001,091626-50003"]


def test_no_links_means_no_double_check_link_either(reviewed) -> None:
    client, _, _ = reviewed
    data = client.post("/api/export", json={"tabs": ["Intaked"], "include_links": False}).get_json()
    assert "double-check" not in data["csv"]
    assert data["review_links"] == []


# ── the link that comes in ───────────────────────────────────────────────

def test_search_loads_an_explicit_list_of_lab_ids_exactly(reviewed) -> None:
    client, ustate, api = reviewed
    resp = client.post("/api/search", json={"lab_ids": GOOD})
    assert resp.status_code == 200, resp.get_json()
    looked_up = [c.args[0] for c in api.fetch_samples_by_lab_id.call_args_list]
    assert looked_up == GOOD, "one exact lookup per lab ID, no ranges, no prefix search"
    assert sorted(s["lab_id"] for s in resp.get_json()["samples"]) == sorted(GOOD)
    assert all(k[0] == "Search" for k in ustate.records if k[1] in GOOD and k[0] != "Intaked")


def test_an_explicit_list_replaces_the_previous_search(reviewed) -> None:
    from app import SampleRecord
    client, ustate, _ = reviewed
    ustate.add_record(SampleRecord(lab_id="080126-11111", tab="Search", sample_id=11111, test_ids=[1]))
    client.post("/api/search", json={"lab_ids": GOOD})
    assert ("Search", "080126-11111") not in ustate.records


def test_an_explicit_list_ignores_blanks_and_duplicates(reviewed) -> None:
    client, _, api = reviewed
    client.post("/api/search", json={"lab_ids": [GOOD[0], "", "  ", GOOD[0], GOOD[1]]})
    assert [c.args[0] for c in api.fetch_samples_by_lab_id.call_args_list] == GOOD


def test_search_still_needs_something_to_search_for(reviewed) -> None:
    client, _, _ = reviewed
    assert client.post("/api/search", json={"lab_ids": []}).status_code == 400
    assert client.post("/api/search", json={}).status_code == 400


def test_the_search_box_reads_full_lab_ids_as_ids_not_ranges() -> None:
    """`091626-50001` is MMDDYY-NNNNN, not "50001 through 91626"."""
    import app as app_module
    assert app_module._parse_search_query("091626-50001,091626-50003") == ["091626-50001", "091626-50003"]
    assert app_module._parse_search_query("091626-50001") == ["091626-50001"]


def test_the_search_box_still_expands_numeric_ranges() -> None:
    import app as app_module
    assert app_module._parse_search_query("32217-32220") == ["32217", "32218", "32219", "32220"]
    # Six digits that are not a date are still a range, not a lab ID.
    assert app_module._parse_search_query("100000-100002") == ["100000", "100001", "100002"]


# ── the client ───────────────────────────────────────────────────────────

def test_the_good_samples_popup_shows_and_copies_the_double_check_link() -> None:
    body = _fn("handleOpenGoodLinks")
    assert "origin: location.origin" in body, "the server needs the hostname the browser is on"
    assert "review_url" in body
    assert "copy-review-link" in body


def test_the_export_popup_lists_the_double_check_links() -> None:
    body = _fn("handleExport")
    assert "origin: location.origin" in body
    assert "review_links" in body


def test_the_app_reads_the_check_parameter_once_and_removes_it_from_the_url() -> None:
    body = _fn("readDoubleCheckParam")
    assert "URLSearchParams(location.search)" in body
    assert '"check"' in body
    assert "history.replaceState" in body, "a refresh must not re-run the search"


def test_the_pending_list_is_loaded_into_search_once_the_app_is_up() -> None:
    body = _fn("runPendingDoubleCheck")
    assert '"/api/search"' in body
    assert "lab_ids" in body
    assert 'switchTab("Search")' in body
    assert "setTimeout(runPendingDoubleCheck" in body, "QBench may still be logging in; try again"
    assert "initDoubleCheckLink" in _fn("initDoubleCheckLink") and "MutationObserver" in _fn("initDoubleCheckLink")
    assert "initDoubleCheckLink();" in APP_JS
