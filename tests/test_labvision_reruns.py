"""The Lab Vision pane shows a test's re-runs and add-ons, not just its cell.

Request 2026-09-18: "the re-runs in labvision are not showing up in the
labvision view."

LabCore keeps re-run and add-on flags in their own table (`test_reruns`:
lab_id, test_name, run_kind, work_date, created_by, reason) and serves them
from `/api/reruns`; the per-result history behind a re-run is served per
(lab_id, test_name) from `/api/results`. Neither is part of `/api/sample`,
whose `tests` list is one row per test with the current cell only, and that
list was all the pane ever asked for. Measured live 2026-09-18: 198 flags
(193 add-ons, 5 re-runs), none of them visible here.

Now `/api/sync-preview` also asks LabCore for the sample's flags and, for
each flagged test, its result history, and merges both onto the matching
test row. Both calls are best-effort: a LabCore hiccup costs the flags, not
the pane. An add-on flagged on a test the sample does not list yet appears
as a row with no result, because that is exactly the outstanding work a
reviewer should see.

Client tests run against a real stub HTTP server, like the rest of the
LabCore client tests; the route tests use a MagicMock LabCore.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")

from labcore_client import LabCoreClient, LabCoreUnavailable  # noqa: E402
from tests.test_labcore_client import _StubLabCore  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
APP_CSS = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")

LAB = "081126-37689"
KF = "ASTM D6304 - Water, by Karl Fischer"
FLAG = {"lab_id": LAB, "test_name": KF, "run_kind": "rerun", "work_date": "2026-08-12",
        "created_at": "2026-08-12 10:58:18", "created_by": "kejuan", "reason": "",
        "sample_id": "VB-Kempton", "customer_name": "Pearce Services", "result": "277.96"}
OTHER_FLAG = {**FLAG, "lab_id": "081126-37684", "test_name": "FBP", "created_by": "cody"}
HISTORY = [
    {"result_id": 314435, "value": "164.76", "created_at": "2026-08-12 10:58:18",
     "updated_at": "2026-08-12 12:09:04", "operator": "kejuan", "source": "LabStation", "is_final": 0},
    {"result_id": 314174, "value": "277.96", "created_at": "2026-08-12 09:49:32",
     "updated_at": "2026-08-13 08:28:20", "operator": "kejuan", "source": "backfill", "is_final": 0},
]


@pytest.fixture
def stub():
    servers = []

    def _make(routes=None):
        s = _StubLabCore(routes)
        servers.append(s)
        return s

    yield _make
    for s in servers:
        s.close()


# ── the client ───────────────────────────────────────────────────────────

def test_reruns_asks_for_the_flags_and_keeps_only_this_sample(stub) -> None:
    server = stub({"GET /api/reruns": {"reruns": [OTHER_FLAG, FLAG]}})
    flags = LabCoreClient(base_url=f"http://127.0.0.1:{server.port}").reruns(LAB)
    assert flags == [{"test_name": KF, "kind": "rerun", "work_date": "2026-08-12",
                      "by": "kejuan", "reason": ""}]


def test_reruns_narrows_the_request_to_days_since_the_sample_came_in(stub) -> None:
    """The flag table is lab-wide and grows forever; a re-run cannot predate
    the sample's intake, which its lab ID encodes."""
    server = stub({"GET /api/reruns": {"reruns": []}})
    LabCoreClient(base_url=f"http://127.0.0.1:{server.port}").reruns(LAB)
    assert server.last()["query"].get("start") == ["2026-08-11"]


def test_reruns_with_an_odd_lab_id_asks_for_every_day(stub) -> None:
    server = stub({"GET /api/reruns": {"reruns": []}})
    LabCoreClient(base_url=f"http://127.0.0.1:{server.port}").reruns("Rehab")
    assert "start" not in server.last()["query"]


def test_result_history_asks_for_one_pair_oldest_first(stub) -> None:
    server = stub({"GET /api/results": {"results": HISTORY}})
    rows = LabCoreClient(base_url=f"http://127.0.0.1:{server.port}").result_history(LAB, KF)
    assert server.last()["query"] == {"lab_id": [LAB], "test_name": [KF]}
    assert [r["value"] for r in rows] == ["277.96", "164.76"], "in the order they were measured"
    assert rows[0] == {"value": "277.96", "at": "2026-08-12 09:49:32", "by": "kejuan", "source": "backfill"}


def test_a_labcore_error_on_the_flags_raises_like_any_other_read(stub) -> None:
    server = stub({})  # 404 on everything
    with pytest.raises(LabCoreUnavailable):
        LabCoreClient(base_url=f"http://127.0.0.1:{server.port}").reruns(LAB)


# ── the merge ────────────────────────────────────────────────────────────

def test_flags_and_history_land_on_the_matching_test() -> None:
    import app as app_module
    raw = [{"test": KF, "result": "277.96", "operator": "kejuan"},
           {"test": "Flash Point", "result": "71.0", "operator": ""}]
    flags = [{"test_name": KF, "kind": "rerun", "work_date": "2026-08-12", "by": "kejuan", "reason": ""}]
    hist = {KF: [{"value": "277.96", "at": "a", "by": "kejuan", "source": "backfill"},
                 {"value": "164.76", "at": "b", "by": "kejuan", "source": "LabStation"}]}
    out = app_module.lab_vision_tests(raw, flags=flags, histories=hist)
    assert out[0]["reruns"] == flags
    assert out[0]["history"] == hist[KF]
    assert out[1]["reruns"] == [] and out[1]["history"] == []


def test_an_add_on_the_sample_does_not_list_yet_shows_as_outstanding() -> None:
    import app as app_module
    raw = [{"test": "Flash Point", "result": "71.0", "operator": ""}]
    flags = [{"test_name": "Sulfur", "kind": "addon", "work_date": "2026-09-09", "by": "tyler",
              "reason": "Re-added via previous order"}]
    out = app_module.lab_vision_tests(raw, flags=flags)
    assert [t["test"] for t in out] == ["Flash Point", "Sulfur"]
    assert out[1]["result"] == "" and out[1]["reruns"] == flags


def test_the_merge_tolerates_junk_from_labcore() -> None:
    """A MagicMock LabCore in other tests, or a half-broken response, must
    not take the pane down."""
    import app as app_module
    raw = [{"test": KF, "result": "1", "operator": ""}]
    out = app_module.lab_vision_tests(raw, flags=MagicMock(), histories=MagicMock())
    assert out == [{"test": KF, "result": "1", "operator": "", "reruns": [], "history": []}]
    assert app_module.lab_vision_tests(raw)[0]["reruns"] == []


# ── the route ────────────────────────────────────────────────────────────

@pytest.fixture
def pane(monkeypatch, tmp_path):
    import app as app_module
    from app import SampleRecord, UserState

    lc = MagicMock()
    lc.sample_data.return_value = {
        "lab_id": LAB, "customer_name": "Pearce Services",
        "tests": [{"test": KF, "result": "277.96", "operator": "kejuan"},
                  {"test": "Flash Point", "result": "71.0", "operator": ""}],
    }
    lc.reruns.return_value = [{"test_name": KF, "kind": "rerun", "work_date": "2026-08-12",
                               "by": "kejuan", "reason": ""}]
    lc.result_history.return_value = [
        {"value": "277.96", "at": "2026-08-12 09:49:32", "by": "kejuan", "source": "backfill"},
        {"value": "164.76", "at": "2026-08-12 10:58:18", "by": "kejuan", "source": "LabStation"},
    ]
    monkeypatch.setattr(app_module.state, "labcore", lc)
    monkeypatch.setattr(app_module.state, "logged_in", True)
    api = MagicMock()
    api.fetch_sample.return_value = {"id": 37689, "lab_id": LAB}
    monkeypatch.setattr(app_module.state, "api_client", api)

    uid = "test-uid-lv-reruns"
    ustate = UserState(uid, "RC")
    ustate.add_record(SampleRecord(lab_id=LAB, tab="Yesterday", sample_id=37689, test_ids=[9]))
    with app_module._sessions_lock:
        app_module.user_sessions[uid] = ustate
    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = uid
    yield client, lc
    with app_module._sessions_lock:
        app_module.user_sessions.pop(uid, None)


def test_the_pane_gets_the_flags_and_the_history(pane) -> None:
    client, lc = pane
    tests = {t["test"]: t for t in client.get(f"/api/sync-preview/{LAB}").get_json()["tests"]}
    assert tests[KF]["reruns"][0]["kind"] == "rerun"
    assert [h["value"] for h in tests[KF]["history"]] == ["277.96", "164.76"]
    assert tests["Flash Point"]["reruns"] == [] and tests["Flash Point"]["history"] == []
    lc.reruns.assert_called_once_with(LAB)
    lc.result_history.assert_called_once_with(LAB, KF), "history only for flagged tests"


def test_labcore_failing_on_the_flags_costs_the_flags_not_the_pane(pane) -> None:
    client, lc = pane
    lc.reruns.side_effect = LabCoreUnavailable("down")
    resp = client.get(f"/api/sync-preview/{LAB}")
    assert resp.status_code == 200
    tests = {t["test"]: t for t in resp.get_json()["tests"]}
    assert tests[KF]["result"] == "277.96" and tests[KF]["reruns"] == []


def test_labcore_failing_on_the_history_keeps_the_flag(pane) -> None:
    client, lc = pane
    lc.result_history.side_effect = LabCoreUnavailable("down")
    tests = {t["test"]: t for t in client.get(f"/api/sync-preview/{LAB}").get_json()["tests"]}
    assert tests[KF]["reruns"][0]["kind"] == "rerun"
    assert tests[KF]["history"] == []


# ── the client renders it ────────────────────────────────────────────────

def _fn(name: str) -> str:
    start = APP_JS.index(f"function {name}(")
    m = re.compile(r"\n(?:async )?function ").search(APP_JS, start + 1)
    return APP_JS[start:m.start() if m else len(APP_JS)]


def test_the_pane_marks_a_flagged_test_and_says_what_and_when() -> None:
    body = _fn("renderLabVisionTests")
    assert "t.reruns" in body
    assert "lv-test-row--flagged" in body
    assert "work_date" in body
    assert "Re-run" in body and "Add-on" in body, "the kind is spelled out, not a code"
    assert ".lv-test-flags" in APP_CSS and ".lv-test-row--flagged" in APP_CSS


def test_the_pane_lists_the_earlier_results_behind_a_re_run() -> None:
    body = _fn("renderLabVisionTests")
    assert "t.history" in body
    assert "lv-test-history" in body
