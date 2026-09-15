"""A reviewer's marks follow the samples through a re-pull.

v2.2.0 made the review survive a lost session: marks are written to
``review_state/<account>.json`` and a rebuilt session is restored from it
for 12 hours. But the same samples pulled again lost every mark, because
Start Pulling cleared the snapshot and Custom Day / Search delete their old
records before fetching. And a judged sample that came back without a
preview could never render: the window only rendered ``pending`` samples,
and Regenerate wiped the verdict.

Now each account keeps a verdict ledger keyed by (tab, lab_id). Any pull
that creates a record whose key has a fresh entry (judged within 12 hours)
gets the verdict back, with its reason, its Command Center listing and its
export row. The ledger is written into the snapshot, so it also survives a
restart. Judged samples with no preview render like pending ones and keep
their verdict through the render.

Real code throughout: routes, the real tab fetch against a fake QBench
client, real snapshot files in tmp_path. Only the render pool is faked so
nothing is rendered.
"""

from __future__ import annotations

import json
import queue
import time
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")

DAY = date(2026, 9, 10)
TAB = "Yesterday"
LABS = ["091026-40001", "091026-40002", "091026-40003"]


def _qbench_with(labs):
    api = MagicMock()
    api.fetch_samples_by_lab_id_prefix.return_value = [
        {"id": 500 + i, "lab_id": lab, "order_id": 9} for i, lab in enumerate(labs)
    ]
    api.fetch_tests_for_sample_ids.return_value = [
        {"id": 700 + i, "sample_id": 500 + i} for i in range(len(labs))
    ]
    api.fetch_all_attachments_for_sample.return_value = []
    return api


class FakePool:
    def __init__(self):
        self.futures = []

    def submit(self, fn, *args):
        fut = MagicMock()
        fut.done.return_value = False
        fut.cancel.return_value = True
        fut.args = args
        self.futures.append(fut)
        return fut

    def submitted_lab_ids(self):
        return [f.args[1] for f in self.futures]


class Lab:
    """A live session for one account plus a way to pull the day for real."""

    def __init__(self, app_module, name, pull):
        self.app = app_module
        self.name = name
        self.uid = f"test-uid-repull-{name}"
        self.ustate = app_module.UserState(self.uid, name)
        with app_module._sessions_lock:
            app_module.user_sessions[self.uid] = self.ustate
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["uid"] = self.uid
        self._pull = pull

    def pull(self, tab=TAB):
        self._pull(tab, DAY, self.ustate)

    def start(self):
        resp = self.client.post("/api/start", json={"mode": "tests"})
        assert resp.status_code == 200, resp.get_json()

    def mark(self, outcome, lab, reason="", cc_task_id=None, tab=TAB):
        body = {"tab": tab, "lab_id": lab, "outcome": outcome, "reason": reason}
        if cc_task_id is not None:
            body["cc_task_id"] = cc_task_id
        resp = self.client.post("/api/mark", json=body)
        assert resp.status_code == 200, resp.get_json()

    def tab(self, tab=TAB):
        resp = self.client.get(f"/api/tabs/{tab.replace(' ', '%20')}")
        return {s["lab_id"]: s for s in resp.get_json()["samples"]}

    def results(self):
        return {(r["tab"], r["lab_id"]): r for r in self.ustate.session_results}

    def drop_session(self):
        """What a restart or the idle reaper does: the UserState is gone."""
        with self.app._sessions_lock:
            self.app.user_sessions.pop(self.uid, None)

    def reauth(self):
        resp = self.client.post("/api/portal-reauth", json={"code": "CARD-1"})
        assert resp.status_code == 200, resp.get_json()
        # The rebuilt session is registered under a fresh uid in the cookie.
        with self.client.session_transaction() as sess:
            self.uid = sess["uid"]
        with self.app._sessions_lock:
            self.ustate = self.app.user_sessions[self.uid]
        return resp.get_json()


@pytest.fixture
def lab(monkeypatch, tmp_path):
    import app as app_module

    labcore = MagicMock()
    labcore.authenticate_card.return_value = "RC"
    labcore.authenticate_user.return_value = "RC"
    monkeypatch.setattr(app_module.state, "labcore", labcore)
    monkeypatch.setattr(app_module.state, "api_client", _qbench_with(LABS))
    monkeypatch.setattr(app_module.state, "logged_in", True)
    monkeypatch.setattr(app_module.state, "coa_session", object())
    monkeypatch.setattr(app_module, "REVIEW_STATE_DIR", tmp_path / "review_state")
    monkeypatch.setattr(app_module, "PREVIEW_POOL", FakePool())
    app_module.app.config["TESTING"] = True

    # Start Pulling spawns the fetches on threads; the test pulls explicitly
    # and synchronously with the real function instead.
    real_fetch = app_module.fetch_samples_for_tab
    monkeypatch.setattr(app_module, "fetch_samples_for_tab", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "fetch_re_review_samples", lambda *a, **k: None)

    lab = Lab(app_module, "RC", real_fetch)
    yield lab

    with app_module._sessions_lock:
        for k in [k for k, v in app_module.user_sessions.items()
                  if k.startswith("test-uid-repull-") or v.name in ("RC", "JD")]:
            app_module.user_sessions.pop(k, None)


def _snapshot(tmp_path) -> dict:
    path = next((tmp_path / "review_state").glob("*.json"))
    return json.loads(path.read_text(encoding="utf-8"))


# ── the marks come back ──────────────────────────────────────────────────

def test_start_pulling_again_brings_the_marks_back_with_the_samples(lab) -> None:
    lab.pull()
    lab.mark("good", LABS[0])
    lab.mark("bad", LABS[1], reason="Wrong tank", cc_task_id=55)

    lab.start()
    assert lab.tab() == {}, "Start Pulling still begins from an empty list"
    lab.pull()

    samples = lab.tab()
    assert samples[LABS[0]]["status"] == "good"
    assert samples[LABS[1]]["status"] == "bad"
    assert samples[LABS[1]]["reason"] == "Wrong tank"
    assert samples[LABS[1]]["cc_task_id"] == 55
    assert samples[LABS[2]]["status"] == "pending"


def test_a_re_pulled_mark_reports_no_preview_until_it_renders(lab) -> None:
    """Claiming a preview that is not there puts a 404 in the viewer."""
    lab.pull()
    lab.mark("good", LABS[0])
    lab.start()
    lab.pull()
    assert lab.tab()[LABS[0]]["has_preview"] is False


def test_re_pulled_marks_are_back_in_the_export(lab) -> None:
    lab.pull()
    lab.mark("good", LABS[0])
    lab.mark("bad", LABS[1], reason="Wrong tank")
    first_date = lab.results()[(TAB, LABS[0])]["date"]

    lab.start()
    assert lab.results() == {}, "the results follow the list, then come back with it"
    lab.pull()

    rows = lab.results()
    assert rows[(TAB, LABS[0])]["outcome"] == "Good"
    assert rows[(TAB, LABS[0])]["date"] == first_date, "the review date is when it was judged"
    assert rows[(TAB, LABS[0])]["reviewer"] == "RC"
    assert rows[(TAB, LABS[1])]["outcome"] == "Bad"
    assert rows[(TAB, LABS[1])]["reason"] == "Wrong tank"
    assert (TAB, LABS[2]) not in rows


def test_an_unchecked_mark_stays_unchecked_after_a_re_pull(lab) -> None:
    lab.pull()
    lab.mark("good", LABS[0])
    lab.mark("uncheck", LABS[0])
    lab.start()
    lab.pull()
    assert lab.tab()[LABS[0]]["status"] == "pending"
    assert (TAB, LABS[0]) not in lab.results()


def test_marking_again_replaces_the_remembered_verdict(lab) -> None:
    lab.pull()
    lab.mark("bad", LABS[0], reason="Wrong tank")
    lab.mark("good", LABS[0])
    lab.start()
    lab.pull()
    s = lab.tab()[LABS[0]]
    assert s["status"] == "good"
    assert s["reason"] == ""
    assert lab.results()[(TAB, LABS[0])]["outcome"] == "Good"


# ── the window and the account ───────────────────────────────────────────

def _pull_at(lab, when: float) -> None:
    """Pull with the app's clock moved to `when` (the routes keep real time,
    so the portal session does not expire along with the marks)."""
    import app as app_module
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(app_module.time, "time", lambda: when)
        lab.pull()


def test_a_mark_older_than_twelve_hours_is_left_behind(lab) -> None:
    lab.pull()
    lab.mark("good", LABS[0])
    judged = time.time()
    lab.start()
    _pull_at(lab, judged + 12 * 3600 + 1)
    assert lab.tab()[LABS[0]]["status"] == "pending"
    assert (TAB, LABS[0]) not in lab.results()


def test_a_mark_just_inside_twelve_hours_still_comes_back(lab) -> None:
    lab.pull()
    lab.mark("good", LABS[0])
    judged = time.time()
    lab.start()
    _pull_at(lab, judged + 12 * 3600 - 60)
    assert lab.tab()[LABS[0]]["status"] == "good"


def test_marks_belong_to_the_account_that_made_them(lab) -> None:
    import app as app_module
    lab.pull()
    lab.mark("good", LABS[0])

    other = Lab(app_module, "JD", lab._pull)
    other.pull()
    assert other.tab()[LABS[0]]["status"] == "pending"
    assert other.results() == {}


def test_a_mark_on_one_tab_does_not_mark_the_same_sample_on_another(lab) -> None:
    """Yesterday and Due Out are two reviews of the same lab ID."""
    lab.pull(TAB)
    lab.mark("good", LABS[0])
    lab.pull("Due Out")
    assert lab.tab("Due Out")[LABS[0]]["status"] == "pending"


# ── the ledger survives a restart ────────────────────────────────────────

def test_the_marks_survive_a_restart_and_a_re_pull(lab) -> None:
    lab.pull()
    lab.mark("good", LABS[0])
    lab.mark("bad", LABS[1], reason="Wrong tank", cc_task_id=55)

    lab.drop_session()
    lab.reauth()
    lab.start()
    lab.pull()

    samples = lab.tab()
    assert samples[LABS[0]]["status"] == "good"
    assert samples[LABS[1]]["status"] == "bad"
    assert samples[LABS[1]]["cc_task_id"] == 55


def test_the_marks_survive_a_restart_after_start_pulling(lab) -> None:
    """Start Pulling writes an empty list; the ledger must be in that file."""
    lab.pull()
    lab.mark("good", LABS[0])
    lab.start()

    lab.drop_session()
    lab.reauth()
    lab.pull()
    assert lab.tab()[LABS[0]]["status"] == "good"


def test_the_snapshot_carries_only_fresh_verdicts(lab, tmp_path, monkeypatch) -> None:
    import app as app_module
    lab.pull()
    lab.mark("good", LABS[0])
    judged = time.time()
    lab.mark("good", LABS[1])
    entry = lab.ustate.verdicts[(TAB, LABS[0])]
    entry["judged_at"] = judged - 12 * 3600 - 1
    lab.ustate.persist()

    doc = _snapshot(tmp_path)
    kept = {(v["tab"], v["lab_id"]) for v in doc["verdicts"]}
    assert kept == {(TAB, LABS[1])}


def test_a_snapshot_written_before_the_ledger_still_re_applies(lab, tmp_path) -> None:
    """What is on the lab server today is a version-1 snapshot: judged
    records, no `verdicts`. Upgrading must not cost those marks."""
    import app as app_module
    saved_at = time.time() - 600
    doc = {
        "version": 1,
        "name": "RC",
        "saved_at": saved_at,
        "records": [
            {"tab": TAB, "lab_id": LABS[0], "sample_id": 500, "test_ids": [700],
             "order_id": 9, "status": "good", "reason": "", "cc_task_id": None,
             "cc_task": None, "info": {}},
            {"tab": TAB, "lab_id": LABS[1], "sample_id": 501, "test_ids": [701],
             "order_id": 9, "status": "pending", "reason": "", "cc_task_id": None,
             "cc_task": None, "info": {}},
        ],
        "session_results": [
            {"lab_id": LABS[0], "sample_id": 500, "tab": TAB, "outcome": "Good",
             "reason": "", "reviewer": "RC", "date": "2026-09-10"},
        ],
    }
    path = app_module._review_state_path("RC")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")

    lab.drop_session()
    lab.reauth()
    lab.start()
    lab.pull()

    samples = lab.tab()
    assert samples[LABS[0]]["status"] == "good"
    assert samples[LABS[1]]["status"] == "pending"
    assert lab.results()[(TAB, LABS[0])]["date"] == "2026-09-10"


# ── a judged sample still gets its COA ───────────────────────────────────

def test_the_window_renders_a_re_pulled_judged_sample(lab) -> None:
    import app as app_module
    lab.pull()
    lab.mark("good", LABS[0])
    lab.start()
    lab.pull()

    resp = lab.client.post("/api/focus", json={"tab": TAB, "lab_id": LABS[0]})
    assert resp.status_code == 200
    assert LABS[0] in app_module.PREVIEW_POOL.submitted_lab_ids()


def test_a_judged_sample_that_already_has_a_coa_is_not_re_rendered(lab) -> None:
    import app as app_module
    lab.pull()
    lab.ustate.records[(TAB, LABS[0])].preview_url = "http://preview/x"
    lab.ustate.records[(TAB, LABS[0])].status = "ready"
    lab.mark("good", LABS[0])

    lab.client.post("/api/focus", json={"tab": TAB, "lab_id": LABS[0]})
    assert LABS[0] not in app_module.PREVIEW_POOL.submitted_lab_ids()


def _render_stub(monkeypatch, app_module, url="http://preview/rendered.pdf", fail=False):
    session = MagicMock()
    if fail:
        session.generate_preview.side_effect = RuntimeError("QBench said no")
    else:
        session.generate_preview.return_value = url
    session._session.get.return_value = MagicMock(url=url)
    monkeypatch.setattr(app_module.state, "coa_session", session)
    monkeypatch.setattr(app_module.IO_POOL, "submit", lambda *a, **k: None)
    return session


def _events(ustate):
    q: "queue.Queue" = queue.Queue()
    ustate._sse_queues.append(q)
    return q


def test_rendering_a_judged_sample_keeps_its_verdict(lab, monkeypatch) -> None:
    import app as app_module
    lab.pull()
    lab.mark("bad", LABS[0], reason="Wrong tank")
    lab.start()
    lab.pull()
    _render_stub(monkeypatch, app_module)
    events = _events(lab.ustate)

    app_module.generate_preview_for_sample(TAB, LABS[0], lab.ustate)

    rec = lab.ustate.records[(TAB, LABS[0])]
    assert rec.status == "bad"
    assert rec.reason == "Wrong tank"
    assert rec.preview_url == "http://preview/rendered.pdf"
    statuses = []
    while not events.empty():
        ev = events.get_nowait()
        if ev.get("type") == "sample_status":
            statuses.append(ev)
    assert all(ev["status"] != "loading" for ev in statuses), "a verdict never shows as loading"
    assert statuses[-1]["status"] == "bad"
    assert statuses[-1]["has_preview"] is True


def test_a_failed_render_does_not_cost_the_verdict(lab, monkeypatch) -> None:
    import app as app_module
    lab.pull()
    lab.mark("good", LABS[0])
    lab.start()
    lab.pull()
    _render_stub(monkeypatch, app_module, fail=True)

    app_module.generate_preview_for_sample(TAB, LABS[0], lab.ustate)

    rec = lab.ustate.records[(TAB, LABS[0])]
    assert rec.status == "good"
    assert rec.preview_url is None


def test_a_judged_sample_whose_render_failed_is_not_retried_on_every_focus(lab, monkeypatch) -> None:
    """A pending sample that fails goes to `error` and waits for Regenerate.
    A judged one keeps its verdict instead, so without a memory of the
    failure every window pass would queue the same 30-second render again."""
    import app as app_module
    lab.pull()
    lab.mark("good", LABS[0])
    lab.start()
    lab.pull()
    _render_stub(monkeypatch, app_module, fail=True)
    app_module.generate_preview_for_sample(TAB, LABS[0], lab.ustate)

    pool = FakePool()
    monkeypatch.setattr(app_module, "PREVIEW_POOL", pool)
    lab.client.post("/api/focus", json={"tab": TAB, "lab_id": LABS[0]})
    assert LABS[0] not in pool.submitted_lab_ids()
    assert LABS[2] in pool.submitted_lab_ids(), "the rest of the window still renders"


def test_regenerate_gives_a_failed_judged_render_another_go(lab, monkeypatch) -> None:
    import app as app_module
    lab.pull()
    lab.mark("good", LABS[0])
    lab.start()
    lab.pull()
    _render_stub(monkeypatch, app_module, fail=True)
    app_module.generate_preview_for_sample(TAB, LABS[0], lab.ustate)

    pool = FakePool()
    monkeypatch.setattr(app_module, "PREVIEW_POOL", pool)
    resp = lab.client.post("/api/regenerate", json={"tab": TAB, "lab_id": LABS[0]})
    assert resp.status_code == 200
    assert pool.submitted_lab_ids() == [LABS[0]]


def test_a_pending_sample_still_renders_the_old_way(lab, monkeypatch) -> None:
    import app as app_module
    lab.pull()
    _render_stub(monkeypatch, app_module)
    events = _events(lab.ustate)

    app_module.generate_preview_for_sample(TAB, LABS[2], lab.ustate)

    rec = lab.ustate.records[(TAB, LABS[2])]
    assert rec.status == "ready"
    seen = []
    while not events.empty():
        ev = events.get_nowait()
        if ev.get("type") == "sample_status":
            seen.append(ev["status"])
    assert seen == ["loading", "ready"]


# ── regenerate is a new document ─────────────────────────────────────────

def test_regenerate_un_judges_the_sample_everywhere(lab) -> None:
    """The list already showed a regenerated sample as unjudged; the export
    row and the remembered verdict must agree with it."""
    lab.pull()
    lab.mark("good", LABS[0])

    resp = lab.client.post("/api/regenerate", json={"tab": TAB, "lab_id": LABS[0]})
    assert resp.status_code == 200
    assert (TAB, LABS[0]) not in lab.results()

    lab.start()
    lab.pull()
    assert lab.tab()[LABS[0]]["status"] == "pending"


# ── the client loads the COA when the render says it is there ────────────

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    body = APP_JS[APP_JS.index(f"function {name}"):]
    return body[:body.index("\nfunction ", 1)]


def test_the_client_passes_the_render_flag_through() -> None:
    assert "updateSampleStatus(data.tab, data.lab_id, data.status, data.has_preview)" in APP_JS


def test_the_client_loads_the_coa_when_the_render_says_it_is_there() -> None:
    """A judged sample's render ends in `good`/`bad`, not `ready`, so the old
    `status === "ready"` trigger never fired for it."""
    body = _fn("updateSampleStatus")
    assert "hasPreview" in body
    assert "hasPreview === true" in body
