"""Previews render in a window around the sample being looked at, not for
the whole day up front.

A pull used to submit every sample on every tab to PREVIEW_POOL the moment
the list arrived. Renders serialise on the one COASession, so a 100-sample
day queued 100 Playwright renders, 100 PDF downloads and 100 SIF fetches
before the reviewer had clicked anything — and the sample they actually
wanted might be 80th in that queue. Big days felt like a different app.

Now nothing renders until the reviewer is looking at a tab, and then only
PREVIEW_WINDOW (20) samples forward from the one they selected. Moving the
selection slides the window; renders queued for samples that fell out of it
and have not started are cancelled, so a jump down the list is answered
promptly instead of behind twenty stale renders. Whatever the day's size,
the server is only ever working on twenty samples.

The pool is faked so submissions can be counted and cancelled without
running anything. Real code otherwise: routes, records, window arithmetic.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")


class FakeFuture:
    def __init__(self, fn, args):
        self.fn, self.args = fn, args
        self.started = False
        self.cancelled = False

    def cancel(self) -> bool:
        if self.started:
            return False
        self.cancelled = True
        return True

    def done(self) -> bool:
        return self.cancelled


class FakePool:
    """Records what would have run; never runs it."""

    def __init__(self):
        self.futures: list = []

    def submit(self, fn, *args):
        fut = FakeFuture(fn, args)
        self.futures.append(fut)
        return fut

    def submitted_lab_ids(self, tab=None):
        return [f.args[1] for f in self.futures
                if not f.cancelled and (tab is None or f.args[0] == tab)]

    def live(self):
        return [f for f in self.futures if not f.cancelled]


TAB = "Custom Day"
IDS = [f"090926-{40001 + i:05d}" for i in range(50)]


@pytest.fixture
def windowed(monkeypatch, tmp_path):
    """(client, ustate, pool): 50 pending samples on Custom Day, QBench 'up'."""
    import app as app_module
    from app import SampleRecord, UserState

    pool = FakePool()
    monkeypatch.setattr(app_module, "PREVIEW_POOL", pool)
    # Truthy so the "is QBench up" gates pass; it has no generate_preview, so
    # a render that does run fails fast on the first attempt.
    monkeypatch.setattr(app_module.state, "coa_session", object())
    monkeypatch.setattr(app_module.state, "logged_in", True)
    monkeypatch.setattr(app_module.state, "labcore", MagicMock())
    # Never the real client: a render that gets as far as the attachment
    # fetch must not send a request to QBench from the test suite.
    api = MagicMock()
    api.fetch_all_attachments_for_sample.return_value = []
    monkeypatch.setattr(app_module.state, "api_client", api)
    monkeypatch.setattr(app_module, "REVIEW_STATE_DIR", tmp_path / "review_state")

    uid = "test-uid-window"
    ustate = UserState(uid, "RC")
    for i, lab in enumerate(IDS):
        ustate.add_record(SampleRecord(lab_id=lab, tab=TAB, sample_id=40001 + i,
                                       test_ids=[1], order_id=7))
    with app_module._sessions_lock:
        app_module.user_sessions[uid] = ustate

    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = uid

    yield client, ustate, pool

    with app_module._sessions_lock:
        app_module.user_sessions.pop(uid, None)


def _focus(client, lab_id=None, tab=TAB):
    body = {"tab": tab}
    if lab_id is not None:
        body["lab_id"] = lab_id
    return client.post("/api/focus", json=body)


# ── nothing renders until someone is looking ─────────────────────────────

def test_a_pulled_tab_renders_nothing_until_it_is_looked_at(windowed, monkeypatch) -> None:
    import app as app_module
    client, ustate, pool = windowed

    api = MagicMock()
    api.fetch_samples_by_lab_id_prefix.return_value = [
        {"id": 500 + i, "lab_id": f"091026-{50001 + i:05d}", "order_id": 9} for i in range(30)
    ]
    api.fetch_tests_for_sample_ids.return_value = [
        {"id": 700 + i, "sample_id": 500 + i} for i in range(30)
    ]
    monkeypatch.setattr(app_module.state, "api_client", api)

    app_module.fetch_samples_for_tab("Yesterday", date(2026, 9, 10), ustate)

    assert len(ustate.get_tab_records("Yesterday")) == 30
    assert pool.submitted_lab_ids() == [], (
        "the pull fanned every sample out to the preview pool; the window is "
        "supposed to decide what renders")
    assert all(r.status == "pending" for r in ustate.get_tab_records("Yesterday"))


def test_a_search_renders_nothing_until_it_is_looked_at(windowed, monkeypatch) -> None:
    import app as app_module
    client, ustate, pool = windowed

    api = MagicMock()
    api.fetch_samples_by_lab_id.return_value = [
        {"id": 900 + i, "lab_id": f"091026-{60001 + i:05d}", "order_id": 9} for i in range(25)
    ]
    api.fetch_tests_for_sample_ids.return_value = [
        {"id": 800 + i, "sample_id": 900 + i} for i in range(25)
    ]
    monkeypatch.setattr(app_module.state, "api_client", api)

    resp = client.post("/api/search", json={"query": "091026"})

    assert resp.status_code == 200
    assert len(resp.get_json()["samples"]) == 25
    assert pool.submitted_lab_ids() == []


# ── the window ───────────────────────────────────────────────────────────

def test_the_window_is_twenty_samples_by_default() -> None:
    import app as app_module
    assert app_module.PREVIEW_WINDOW == 20


def test_focus_renders_a_window_forward_from_the_selected_sample(windowed) -> None:
    client, ustate, pool = windowed

    resp = _focus(client, IDS[9])

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["queued"] == 20
    assert pool.submitted_lab_ids() == IDS[9:29], "window must start AT the selection"


def test_focus_without_a_selection_starts_at_the_top(windowed) -> None:
    client, ustate, pool = windowed
    _focus(client)
    assert pool.submitted_lab_ids() == IDS[:20]


def test_focus_on_an_unknown_sample_starts_at_the_top(windowed) -> None:
    client, ustate, pool = windowed
    _focus(client, "090926-99999")
    assert pool.submitted_lab_ids() == IDS[:20]


def test_the_window_is_shorter_at_the_end_of_the_list(windowed) -> None:
    client, ustate, pool = windowed
    _focus(client, IDS[45])
    assert pool.submitted_lab_ids() == IDS[45:50]


def test_focus_skips_samples_that_are_already_rendered_or_judged(windowed) -> None:
    """Twenty positions, not twenty renders: a sample that already has a
    verdict or a preview costs nothing and is left alone."""
    client, ustate, pool = windowed
    ustate.records[(TAB, IDS[0])].status = "good"
    ustate.records[(TAB, IDS[1])].status = "ready"
    ustate.records[(TAB, IDS[1])].preview_url = "http://x"
    ustate.records[(TAB, IDS[2])].status = "loading"

    data = _focus(client).get_json()

    assert pool.submitted_lab_ids() == IDS[3:20]
    assert data["queued"] == 17


def test_refocusing_does_not_queue_a_sample_twice(windowed) -> None:
    """Every arrow-down refocuses; the pool must not fill with duplicates."""
    client, ustate, pool = windowed
    _focus(client, IDS[0])
    _focus(client, IDS[0])
    moved = _focus(client, IDS[1]).get_json()

    # Every submission ever made: no lab_id twice, and moving one row down
    # costs exactly the one sample that entered the window.
    assert [f.args[1] for f in pool.futures] == IDS[:21]
    assert moved["queued"] == 1
    # The sample that fell off the front had not started, so it was dropped.
    assert pool.submitted_lab_ids() == IDS[1:21]


def test_moving_the_window_cancels_renders_that_have_not_started(windowed) -> None:
    """A jump down the list must not wait behind twenty renders the reviewer
    is no longer near."""
    client, ustate, pool = windowed
    _focus(client, IDS[0])
    pool.futures[0].started = True          # one is mid-render; it finishes

    data = _focus(client, IDS[30]).get_json()

    assert data["queued"] == 20
    live = pool.submitted_lab_ids()
    assert live == [IDS[0]] + IDS[30:50], live
    assert all(ustate.records[(TAB, lab)].status == "pending" for lab in IDS[1:20]), (
        "a cancelled render must leave the sample pending, not stuck")


def test_looking_at_another_tab_cancels_the_old_tabs_queue(windowed) -> None:
    """Only the page being looked at."""
    import app as app_module
    from app import SampleRecord
    client, ustate, pool = windowed
    other = [f"091026-{70001 + i:05d}" for i in range(5)]
    for i, lab in enumerate(other):
        ustate.add_record(SampleRecord(lab_id=lab, tab="Yesterday", sample_id=70001 + i,
                                       test_ids=[1], order_id=8))
    _focus(client, IDS[0])

    _focus(client, other[0], tab="Yesterday")

    assert pool.submitted_lab_ids() == other


def test_focus_is_a_no_op_before_qbench_is_logged_in(windowed, monkeypatch) -> None:
    import app as app_module
    client, ustate, pool = windowed
    monkeypatch.setattr(app_module.state, "coa_session", None)

    data = _focus(client).get_json()

    assert data["ok"] is True
    assert data["queued"] == 0
    assert pool.submitted_lab_ids() == []


def test_focus_needs_a_tab(windowed) -> None:
    client, _, _ = windowed
    assert client.post("/api/focus", json={}).status_code == 400


def test_a_render_drops_its_queue_entry_when_it_starts(windowed) -> None:
    """generate_preview_for_sample takes over from the queue; the bookkeeping
    must not keep claiming the sample is waiting."""
    import app as app_module
    client, ustate, pool = windowed
    _focus(client, IDS[0])
    assert (TAB, IDS[0]) in ustate.preview_futures

    # coa_session is a bare object() here, so the render bails immediately
    # after taking over the record — enough to prove the hand-off.
    app_module.generate_preview_for_sample(TAB, IDS[0], ustate)

    assert (TAB, IDS[0]) not in ustate.preview_futures


# ── bulk regenerate goes through the window too ──────────────────────────

def test_regenerate_pending_resets_everything_but_renders_only_the_window(windowed) -> None:
    """The button exists for expired preview links. On a big day it used to
    queue every unjudged sample at once — the same herd a pull used to be."""
    client, ustate, pool = windowed
    for lab in IDS:
        rec = ustate.records[(TAB, lab)]
        rec.status = "ready"
        rec.preview_url = "http://old/" + lab
        ustate.pdf_cache[lab] = b"stale"
    ustate.records[(TAB, IDS[0])].status = "good"

    resp = client.post("/api/regenerate-pending", json={"tab": TAB, "lab_id": IDS[5]})

    data = resp.get_json()
    assert data["count"] == 49, "every unjudged sample is reset"
    assert pool.submitted_lab_ids() == IDS[5:25]
    assert ustate.records[(TAB, IDS[40])].preview_url is None
    assert ustate.records[(TAB, IDS[40])].status == "pending", (
        "a reset sample outside the window must not sit on 'loading' forever")
    assert IDS[40] not in ustate.pdf_cache
    assert ustate.records[(TAB, IDS[0])].status == "good"


def test_regenerate_pending_without_a_selection_renders_from_the_top(windowed) -> None:
    client, ustate, pool = windowed
    client.post("/api/regenerate-pending", json={"tab": TAB})
    assert pool.submitted_lab_ids() == IDS[:20]


# ── the queue follows the records ────────────────────────────────────────

def test_start_pulling_cancels_every_queued_render(windowed, monkeypatch) -> None:
    import app as app_module
    client, ustate, pool = windowed
    _focus(client, IDS[0])
    monkeypatch.setattr(app_module, "fetch_samples_for_tab", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "fetch_re_review_samples", lambda *a, **k: None)

    assert client.post("/api/start", json={"mode": "tests"}).status_code == 200

    assert pool.submitted_lab_ids() == []
    assert ustate.preview_futures == {}


def test_regenerating_one_sample_replaces_its_queued_render(windowed) -> None:
    """An explicit regenerate on a sample already waiting in the window must
    not render it twice."""
    client, ustate, pool = windowed
    _focus(client, IDS[0])
    queued = [f for f in pool.futures if f.args[1] == IDS[3]]
    assert len(queued) == 1

    client.post("/api/regenerate", json={"tab": TAB, "lab_id": IDS[3]})

    assert queued[0].cancelled
    assert pool.submitted_lab_ids().count(IDS[3]) == 1
