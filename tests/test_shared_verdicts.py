"""Shared verdicts (v4): a mark belongs to the sample, per review mode.

A reviewer's Good/Bad/Uncheck goes into the shared store in one
transaction (``apply_mark``) and fans out to every live session in the
same mode that holds the lab id; every session gets a ``tags`` event.
Loading a tab applies the store's verdicts in one batched read.

Store failures never fail a mark: the verdict is kept locally, queued in
``pending_verdicts`` (timestamped), wins over older store verdicts, and is
retried by the cleanup worker — a late retry never overwrites newer work.
"""

from __future__ import annotations

import json
import queue
import time
from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")

LAB = "092526-50001"
OTHER = "092526-50002"
TAB = "Yesterday"
TEN = 1_790_000_000.0          # "10:00" for the timestamped cases


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Reviewer:
    """One live session with records on tabs, a client and an SSE queue."""

    def __init__(self, app_module, name, mode="tests", holds=((TAB, LAB),)):
        self.app = app_module
        self.name = name
        self.uid = f"test-uid-shared-{name}"
        self.ustate = app_module.UserState(self.uid, name)
        self.ustate.mode = mode
        for tab, lab in holds:
            self.hold(tab, lab)
        self.sse: queue.Queue = queue.Queue(maxsize=500)
        self.ustate._sse_queues.append(self.sse)
        with app_module._sessions_lock:
            app_module.user_sessions[self.uid] = self.ustate
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["uid"] = self.uid

    def hold(self, tab, lab, preview=True):
        rec = self.app.SampleRecord(lab_id=lab, tab=tab, sample_id=77, test_ids=[1])
        if preview:
            rec.preview_url = f"/pdf/{lab}"
            rec.status = self.app.STATUS_READY
        self.ustate.add_record(rec)
        return rec

    def rec(self, lab=LAB, tab=TAB):
        return self.ustate.records[(tab, lab)]

    def mark(self, outcome, lab=LAB, tab=TAB, **extra):
        body = {"tab": tab, "lab_id": lab, "outcome": outcome, **extra}
        resp = self.client.post("/api/mark", json=body)
        assert resp.status_code == 200, resp.get_json()
        return resp.get_json()

    def tab(self, tab=TAB, mode=None):
        url = f"/api/tabs/{tab.replace(' ', '%20')}"
        if mode:
            url += f"?mode={mode}"
        resp = self.client.get(url)
        assert resp.status_code == 200, resp.get_data(as_text=True)
        return {s["lab_id"]: s for s in resp.get_json()["samples"]}

    def results(self):
        return {(r["tab"], r["lab_id"]): r for r in self.ustate.session_results}

    def events(self, kind=None):
        out = []
        while True:
            try:
                ev = self.sse.get_nowait()
            except queue.Empty:
                break
            if kind is None or ev.get("type") == kind:
                out.append(ev)
        return out


@pytest.fixture
def lab(monkeypatch, tmp_path):
    import app as app_module

    monkeypatch.setattr(app_module.state, "labcore", MagicMock())
    monkeypatch.setattr(app_module, "REVIEW_STATE_DIR", tmp_path / "review_state")
    app_module.app.config["TESTING"] = True
    made = []

    def reviewer(name, **kw):
        r = Reviewer(app_module, name, **kw)
        made.append(r)
        return r

    yield app_module, reviewer

    with app_module._sessions_lock:
        for r in made:
            app_module.user_sessions.pop(r.uid, None)


def _break_writes(monkeypatch, store):
    """Mark writes fail as if the store were down (None, "unavailable")
    until the returned switch is flipped."""
    real_one, real_many = store.apply_mark, store.apply_marks
    down = {"on": True}

    def wrap(real):
        def flaky(*a, **k):
            if down["on"]:
                store._last_write_error = "unavailable"
                return None
            return real(*a, **k)
        return flaky
    monkeypatch.setattr(store, "apply_mark", wrap(real_one))
    monkeypatch.setattr(store, "apply_marks", wrap(real_many))
    return down


# ── 1–4: mark, fan-out, other mode, un-mark ─────────────────────────────

def test_mark_is_written_to_the_store_with_history(lab):
    app_module, reviewer = lab
    a = reviewer("Dana P")
    a.mark("good")
    v = app_module.state.shared.verdicts_for([LAB])[LAB]["tests"]
    assert v["by"] == "Dana P" and v["outcome"] == "good"
    ev = app_module.state.shared.history(LAB)[0]
    assert ev["kind"] == "mark" and ev["field"] == "tests"
    assert ev["after"] == "good" and ev["before"] is None


def test_mark_fans_out_to_same_mode_sessions_holding_the_lab(lab):
    app_module, reviewer = lab
    a = reviewer("Dana P")
    b = reviewer("Sam K", holds=(("Due Out", LAB),))
    a.mark("good")
    assert b.rec(tab="Due Out").status == "good"
    row = b.results()[("Due Out", LAB)]
    assert row["reviewer"] == "Dana P" and row["outcome"] == "Good"
    statuses = b.events("sample_status")
    assert {"type": "sample_status", "tab": "Due Out", "lab_id": LAB,
            "status": "good"} in statuses
    # the change is written down for B too — by the cleanup worker
    assert b.ustate.dirty is True
    app_module._persist_dirty_sessions()
    snap = json.loads(next((app_module.REVIEW_STATE_DIR).glob("sam-k.json"))
                      .read_text(encoding="utf-8"))
    assert any(r["lab_id"] == LAB and r["status"] == "good" for r in snap["records"])


def test_actor_other_tab_with_same_lab_is_updated_too(lab):
    app_module, reviewer = lab
    a = reviewer("Dana P", holds=((TAB, LAB), ("Search", LAB)))
    a.mark("good")
    assert a.rec(tab="Search").status == "good"


def test_other_mode_session_gets_tags_but_no_verdict(lab):
    app_module, reviewer = lab
    a = reviewer("Dana P")
    c = reviewer("Cy L", mode="info")
    before = c.rec().status
    a.mark("good")
    assert c.rec().status == before
    assert (TAB, LAB) not in c.results()
    tags = c.events("tags")
    assert tags and tags[-1]["lab_id"] == LAB
    assert tags[-1]["tags"]["tests"]["by"] == "Dana P"
    assert tags[-1]["tags"]["info"] is None


def test_unmark_clears_shared_verdict_and_fans_out(lab):
    app_module, reviewer = lab
    a = reviewer("Dana P")
    b = reviewer("Sam K")
    a.mark("good")
    assert b.rec().status == "good"
    a.mark("uncheck")
    v = app_module.state.shared.verdicts_for([LAB])[LAB]["tests"]
    assert v["outcome"] == "cleared"
    assert b.rec().status == "ready"           # had a preview: READY, never PENDING
    assert (TAB, LAB) not in b.results()
    ev = app_module.state.shared.history(LAB)[0]
    assert ev["kind"] == "unmark" and ev["before"] == "good" and ev["after"] is None


def test_unmark_without_preview_goes_back_to_pending(lab):
    app_module, reviewer = lab
    a = reviewer("Dana P")
    b = reviewer("Sam K", holds=())
    b.hold(TAB, LAB, preview=False)
    a.mark("good")
    a.mark("uncheck")
    assert b.rec().status == "pending"


def test_sample_event_is_broadcast_for_a_mark(lab):
    app_module, reviewer = lab
    a = reviewer("Dana P")
    c = reviewer("Cy L", mode="info", holds=())
    a.mark("good")
    assert {"type": "sample_event", "lab_id": LAB, "kind": "mark"} in c.events("sample_event")


# ── 5–7: tab load ────────────────────────────────────────────────────────

def test_tab_load_applies_shared_verdict_to_a_later_pull(lab):
    app_module, reviewer = lab
    a = reviewer("Dana P")
    a.mark("good")
    d = reviewer("Dee R", holds=())
    d.hold(TAB, LAB)
    samples = d.tab(mode="tests")
    assert samples[LAB]["status"] == "good"
    assert samples[LAB]["tags"]["tests"]["by"] == "Dana P"
    row = d.results()[(TAB, LAB)]
    assert row["reviewer"] == "Dana P" and row["outcome"] == "Good"


def test_tab_load_in_info_mode_shows_tags_but_applies_only_info(lab):
    app_module, reviewer = lab
    a = reviewer("Dana P")
    a.mark("good")
    d = reviewer("Dee R", mode="info", holds=())
    d.hold(TAB, LAB)
    samples = d.tab(mode="info")
    assert samples[LAB]["status"] == "ready"
    assert samples[LAB]["tags"]["tests"]["by"] == "Dana P"
    assert samples[LAB]["tags"]["info"] is None


def test_tab_load_with_store_unreadable_leaves_records_alone(lab, monkeypatch):
    app_module, reviewer = lab
    d = reviewer("Dee R")
    d.rec().status = "good"
    d.ustate.record_result(d.rec(), "Good")
    monkeypatch.setattr(app_module.state.shared, "verdicts_for", lambda ids: None)
    samples = d.tab(mode="tests")
    assert samples[LAB]["status"] == "good"
    assert samples[LAB]["tags"] == {"info": None, "tests": None}


def test_tab_load_absent_shared_verdict_never_unjudges(lab):
    app_module, reviewer = lab
    d = reviewer("Dee R")
    d.mark("good")
    other = reviewer("Oli", holds=())
    other.hold(TAB, OTHER)
    other.rec(OTHER).status = "bad"
    other.rec(OTHER).reason = "local only"
    assert other.tab(mode="tests")[OTHER]["status"] == "bad"


def test_tab_load_tombstone_unjudges(lab):
    app_module, reviewer = lab
    d = reviewer("Dee R")
    d.rec().status = "good"
    d.ustate.record_result(d.rec(), "Good")
    app_module.state.shared.apply_mark(LAB, "tests", "cleared", by="Sam K")
    samples = d.tab(mode="tests")
    assert samples[LAB]["status"] == "ready"
    assert (TAB, LAB) not in d.results()


# ── 8: mode ──────────────────────────────────────────────────────────────

def test_mark_body_mode_wins_and_is_remembered(lab):
    app_module, reviewer = lab
    a = reviewer("Dana P")
    a.mark("good", mode="info")
    assert a.ustate.mode == "info"
    got = app_module.state.shared.verdicts_for([LAB])[LAB]
    assert "info" in got and "tests" not in got


def test_invalid_or_missing_mode_falls_back_to_session_mode(lab):
    app_module, reviewer = lab
    a = reviewer("Dana P", mode="info")
    a.mark("good", mode="bogus")
    assert a.ustate.mode == "info"
    assert "info" in app_module.state.shared.verdicts_for([LAB])[LAB]


def test_default_session_mode_is_tests():
    import app as app_module
    assert app_module.UserState("u", "N").mode == "tests"


def test_tab_query_mode_sets_session_mode(lab):
    app_module, reviewer = lab
    d = reviewer("Dee R")
    d.tab(mode="info")
    assert d.ustate.mode == "info"
    d.tab(mode="nonsense")
    assert d.ustate.mode == "info"


def test_start_sets_session_mode(lab, monkeypatch):
    app_module, reviewer = lab
    monkeypatch.setattr(app_module.state, "logged_in", True)
    monkeypatch.setattr(app_module, "fetch_samples_for_tab", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "fetch_re_review_samples", lambda *a, **k: None)
    d = reviewer("Dee R", holds=())
    resp = d.client.post("/api/start", json={"mode": "info"})
    assert resp.status_code == 200
    assert d.ustate.mode == "info"


def test_change_log_record_names_the_mode(lab, monkeypatch):
    app_module, reviewer = lab
    seen = []
    monkeypatch.setattr(app_module.state.change_log, "review",
                        lambda event, **f: seen.append((event, f)))
    a = reviewer("Dana P")
    a.mark("good", mode="info")
    assert seen[-1][1]["mode"] == "info"


# ── 9: ledger migration ──────────────────────────────────────────────────

def _ledger(dirpath, name, verdicts):
    dirpath.mkdir(parents=True, exist_ok=True)
    slug = name.lower().replace(" ", "-")
    (dirpath / f"{slug}.json").write_text(json.dumps({
        "version": 2, "name": name, "saved_at": time.time(),
        "records": [], "session_results": [], "verdicts": verdicts,
    }), encoding="utf-8")


def _entry(tab, lab_id, status, judged_at=None, reason=""):
    return {"tab": tab, "lab_id": lab_id, "status": status, "reason": reason,
            "cc_task_id": None, "judged_at": judged_at or time.time() - 60,
            "date": "2026-09-25"}


def test_ledger_migration_moves_only_tabs_whose_mode_is_certain(lab):
    """Intaked is only ever Info and Re-review only Tests; a Yesterday or
    Due Out mark could have been either, so it stays in its ledger."""
    app_module, _ = lab
    _ledger(app_module.REVIEW_STATE_DIR, "Dana P", [
        _entry("Intaked", LAB, "good"),
        _entry("Re-review", OTHER, "bad", reason="Low"),
        _entry("Due Out", "092526-50003", "good"),
        _entry("Yesterday", "092526-50004", "good"),
        _entry("Intaked", "092526-50005", "good", judged_at=time.time() - 13 * 3600),
    ])
    assert app_module.migrate_ledgers_once() == 2
    got = app_module.state.shared.verdicts_for(
        [LAB, OTHER, "092526-50003", "092526-50004", "092526-50005"])
    assert got[LAB]["info"]["by"] == "Dana P" and got[LAB]["info"]["outcome"] == "good"
    assert got[OTHER]["tests"]["outcome"] == "bad" and got[OTHER]["tests"]["reason"] == "Low"
    assert set(got) == {LAB, OTHER}
    assert app_module.state.shared.history(LAB)[0]["detail"]["migrated"] is True
    assert app_module.state.shared.get_meta("ledger_migrated")
    assert app_module.migrate_ledgers_once() == 0


def test_ledger_migration_keeps_the_original_mark_time(lab):
    app_module, _ = lab
    judged = time.time() - 3600
    _ledger(app_module.REVIEW_STATE_DIR, "Dana P", [_entry("Intaked", LAB, "good", judged_at=judged)])
    app_module.migrate_ledgers_once()
    assert app_module.state.shared.verdicts_for([LAB])[LAB]["info"]["at"] == judged


def test_ledger_migration_never_overwrites_an_existing_row(lab):
    app_module, _ = lab
    app_module.state.shared.apply_mark(LAB, "tests", "cleared", by="Sam K")
    _ledger(app_module.REVIEW_STATE_DIR, "Dana P", [_entry("Re-review", LAB, "good")])
    assert app_module.migrate_ledgers_once() == 0
    assert app_module.state.shared.verdicts_for([LAB])[LAB]["tests"]["outcome"] == "cleared"


def test_ledger_migration_waits_when_the_store_cannot_be_written(lab, monkeypatch):
    app_module, _ = lab
    _ledger(app_module.REVIEW_STATE_DIR, "Dana P", [_entry("Re-review", LAB, "good")])
    monkeypatch.setattr(app_module.state.shared, "apply_marks", lambda items: None)
    assert app_module.migrate_ledgers_once() == 0
    assert app_module.state.shared.get_meta("ledger_migrated") is None


def test_ledger_migration_is_not_run_at_import():
    import app as app_module
    import inspect
    src = inspect.getsource(app_module)
    main = src[src.index('if __name__ == "__main__":'):]
    assert "_migrate_ledgers_safely" in main and "pending_verdicts.load()" in main
    assert main.index("_wait_for_port") < main.index("_migrate_ledgers_safely")
    assert main.index("_wait_for_port") < main.index("pending_verdicts.load()")
    assert "migrate_ledgers_once()" in inspect.getsource(app_module._migrate_ledgers_safely)
    assert app_module.state.shared.get_meta("ledger_migrated") is None


# ── 10: Bad ──────────────────────────────────────────────────────────────

def test_bad_shares_reason_and_listing_and_makes_no_tag(lab):
    app_module, reviewer = lab
    a = reviewer("Dana P")
    b = reviewer("Sam K")
    resp = a.mark("bad", reason="Wrong tank", cc_task_id=55)
    v = app_module.state.shared.verdicts_for([LAB])[LAB]["tests"]
    assert v["reason"] == "Wrong tank" and v["cc_task_id"] == 55
    assert b.rec().status == "bad" and b.rec().reason == "Wrong tank"
    assert b.rec().cc_task_id == 55
    assert resp["tags"]["tests"] is None
    assert b.tab(mode="tests")[LAB]["tags"]["tests"] is None


# ── store failures and pending writes ────────────────────────────────────

def test_store_down_during_mark_still_marks_locally(lab, monkeypatch):
    app_module, reviewer = lab
    store = app_module.state.shared
    _break_writes(monkeypatch, store)
    monkeypatch.setattr(store, "verdicts_for", lambda ids: None)
    a = reviewer("Dana P")
    a.mark("good")
    assert a.rec().status == "good"
    assert a.results()[(TAB, LAB)]["outcome"] == "Good"
    assert a.tab(mode="tests")[LAB]["status"] == "good"
    assert len(app_module.pending_verdicts) == 1


def test_failed_uncheck_is_not_reverted_by_a_tab_load(lab, monkeypatch):
    """Shared says Good; Dana unchecks; the write fails. Her next tab load
    must not put the Good back."""
    app_module, reviewer = lab
    store = app_module.state.shared
    store.apply_mark(LAB, "tests", "good", by="Ann", at=time.time() - 600)
    d = reviewer("Dana P")
    assert d.tab(mode="tests")[LAB]["status"] == "good"
    _break_writes(monkeypatch, store)
    d.mark("uncheck")
    assert d.tab(mode="tests")[LAB]["status"] == "ready"
    assert d.tab(mode="tests")[LAB]["tags"]["tests"] is None   # pending wins in tags too


def test_pending_write_fans_out_to_other_sessions(lab, monkeypatch):
    app_module, reviewer = lab
    _break_writes(monkeypatch, app_module.state.shared)
    a = reviewer("Dana P")
    b = reviewer("Sam K")
    a.mark("good")
    assert b.rec().status == "good"
    assert b.results()[(TAB, LAB)]["reviewer"] == "Dana P"


def test_pending_write_is_retried_and_history_appears(lab, monkeypatch):
    app_module, reviewer = lab
    store = app_module.state.shared
    down = _break_writes(monkeypatch, store)
    a = reviewer("Dana P")
    c = reviewer("Cy L", mode="info", holds=())
    a.mark("good")
    assert store.history(LAB) == []
    app_module._retry_pending_verdicts()            # still down: kept
    assert len(app_module.pending_verdicts) == 1
    down["on"] = False
    c.events()
    app_module._retry_pending_verdicts()
    assert len(app_module.pending_verdicts) == 0
    assert store.verdicts_for([LAB])[LAB]["tests"]["by"] == "Dana P"
    hist = store.history(LAB)
    assert hist[0]["kind"] == "mark" and hist[0]["user"] == "Dana P"
    kinds = {e["type"] for e in c.events()}
    assert {"tags", "sample_event"} <= kinds


def test_cleanup_cycle_retries_pending(lab, monkeypatch):
    app_module, reviewer = lab
    down = _break_writes(monkeypatch, app_module.state.shared)
    a = reviewer("Dana P")
    a.mark("good")
    down["on"] = False
    a.ustate.last_active = time.time()
    app_module._session_cleanup_cycle(time.time())
    assert len(app_module.pending_verdicts) == 0


def test_pending_verdicts_are_bounded(caplog):
    import app as app_module
    pv = app_module.PendingVerdicts(cap=3)
    for i in range(5):
        pv.put(f"L{i}", "tests", {"outcome": "good", "by": "x", "at": float(i)})
    assert len(pv) == 3
    assert pv.get("L0", "tests") is None and pv.get("L4", "tests") is not None
    assert any("L0" in r.getMessage() and r.levelname == "ERROR" for r in caplog.records)


def test_pending_verdicts_keep_the_newer_entry():
    import app as app_module
    pv = app_module.PendingVerdicts(cap=10)
    pv.put("L", "tests", {"outcome": "good", "by": "x", "at": 20.0})
    pv.put("L", "tests", {"outcome": "bad", "by": "y", "at": 10.0})
    assert pv.get("L", "tests")["outcome"] == "good"


def test_retry_is_bounded_per_cycle(lab, monkeypatch):
    app_module, _ = lab
    calls = []
    monkeypatch.setattr(app_module.state.shared, "apply_marks",
                        lambda items: calls.append(items) or [
                            {"before": None, "applied": True, "skipped": False,
                             "verdicts": {}}])
    for i in range(app_module.MAX_PENDING_RETRY_PER_CYCLE + 50):
        app_module.pending_verdicts.put(f"L{i}", "tests",
                                        {"outcome": "good", "by": "x", "at": float(i),
                                         "reason": "", "cc_task_id": None,
                                         "sample_id": None, "tab": TAB})
    app_module._retry_pending_verdicts()
    assert len(calls) == app_module.MAX_PENDING_RETRY_PER_CYCLE
    assert len(app_module.pending_verdicts) == 50


def test_late_retry_never_overwrites_newer_work(lab, monkeypatch):
    """Dana's 10:00 uncheck fails → Sam marks Bad at 10:05 → the retry of
    Dana's uncheck lands after it: Sam's Bad stays, and history shows Dana's
    uncheck at 10:00, flagged superseded."""
    app_module, reviewer = lab
    clock = Clock(TEN - 3600)
    monkeypatch.setattr(app_module, "_mark_clock", clock)
    store = app_module.state.shared
    store.apply_mark(LAB, "tests", "good", by="Ann", at=TEN - 3600)
    dana = reviewer("Dana P")
    sam = reviewer("Sam K")
    assert dana.tab(mode="tests")[LAB]["status"] == "good"

    down = _break_writes(monkeypatch, store)
    clock.t = TEN
    dana.mark("uncheck")
    assert dana.tab(mode="tests")[LAB]["status"] == "ready"

    down["on"] = False
    clock.t = TEN + 300
    sam.mark("bad", reason="Wrong tank")
    assert dana.rec().status == "bad"

    app_module._retry_pending_verdicts()
    assert len(app_module.pending_verdicts) == 0
    v = store.verdicts_for([LAB])[LAB]["tests"]
    assert v["outcome"] == "bad" and v["by"] == "Sam K"
    dana_rows = [h for h in store.history(LAB) if h["user"] == "Dana P"]
    assert len(dana_rows) == 1
    assert dana_rows[0]["kind"] == "unmark" and dana_rows[0]["at"] == TEN
    assert dana_rows[0]["detail"]["superseded"] is True
    assert dana.tab(mode="tests")[LAB]["status"] == "bad"


# ── regenerate is a new document, for everyone ───────────────────────────

def test_regenerate_clears_the_shared_verdict(lab, monkeypatch):
    app_module, reviewer = lab
    monkeypatch.setattr(app_module, "PREVIEW_POOL", MagicMock())
    a = reviewer("Dana P")
    b = reviewer("Sam K")
    a.mark("good")
    assert b.rec().status == "good"
    resp = a.client.post("/api/regenerate", json={"tab": TAB, "lab_id": LAB})
    assert resp.status_code == 200
    assert app_module.state.shared.verdicts_for([LAB])[LAB]["tests"]["outcome"] == "cleared"
    assert b.rec().status == "ready"
    # the tab load must not bring the old verdict back
    assert a.tab(mode="tests")[LAB]["status"] == "loading"


# ══ critic round: render, locking, actor, persistence, modes, retry ══════

def _clocked(monkeypatch, app_module, t=TEN):
    clock = Clock(t)
    monkeypatch.setattr(app_module, "_mark_clock", clock)
    return clock


class _Session:
    """A fake QBench web session whose render runs `during` mid-render."""

    def __init__(self, during=None, url="https://example.invalid/coa.pdf"):
        self.during = during
        self.url = url
        self._session = MagicMock()
        self._session.get.return_value = MagicMock(url=url)

    def generate_preview(self, **kw):
        if self.during:
            self.during()
        return self.url


def _render(app_module, monkeypatch, reviewer, during, url="https://example.invalid/coa.pdf"):
    monkeypatch.setattr(app_module.state, "coa_session", _Session(during, url))
    api = MagicMock()
    api.fetch_all_attachments_for_sample.return_value = []
    monkeypatch.setattr(app_module.state, "api_client", api)
    monkeypatch.setattr(app_module, "IO_POOL", MagicMock())
    app_module.generate_preview_for_sample(TAB, LAB, reviewer.ustate)


# ── 1: a render finishing never reverts a verdict that landed meanwhile ──

def test_fan_out_good_landing_mid_render_stays_good(lab, monkeypatch):
    app_module, reviewer = lab
    a = reviewer("Dana P")
    b = reviewer("Sam K", holds=())
    b.hold(TAB, LAB, preview=False)
    _render(app_module, monkeypatch, b, lambda: a.mark("good"))
    assert b.rec().status == "good"
    assert b.rec().preview_url
    last = b.events("sample_status")[-1]
    assert last["status"] == "good" and last.get("has_preview") is True


def test_uncheck_landing_mid_render_of_a_judged_record_stays_unjudged(lab, monkeypatch):
    app_module, reviewer = lab
    clock = _clocked(monkeypatch, app_module)
    a = reviewer("Dana P")
    b = reviewer("Sam K", holds=())
    b.hold(TAB, LAB, preview=False)
    a.mark("good")
    assert b.rec().status == "good"

    def uncheck():
        clock.t += 1
        a.mark("uncheck")
    _render(app_module, monkeypatch, b, uncheck)
    assert b.rec().status == "ready"
    assert (TAB, LAB) not in b.results()


def test_failed_render_keeps_a_verdict_that_landed_mid_render(lab, monkeypatch):
    app_module, reviewer = lab
    a = reviewer("Dana P")
    b = reviewer("Sam K", holds=())
    b.hold(TAB, LAB, preview=False)
    _render(app_module, monkeypatch, b, lambda: a.mark("good"), url=None)
    assert b.rec().status == "good"
    assert b.rec().render_failed is True


# ── 2: locking and ordering ──────────────────────────────────────────────

def test_concurrent_record_and_clear_result_lose_no_rows():
    import sys
    import threading
    import app as app_module
    us = app_module.UserState("u-conc", "Dana P")
    keep = [app_module.SampleRecord(lab_id=f"K{i}", tab=TAB) for i in range(300)]
    churn = [app_module.SampleRecord(lab_id=f"C{i}", tab=TAB) for i in range(300)]
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        def add():
            for r in keep:
                us.record_result(r, "Good")

        def churner():
            for r in churn:
                us.record_result(r, "Good")
                us.clear_result(r.tab, r.lab_id)
        threads = [threading.Thread(target=add), threading.Thread(target=churner),
                   threading.Thread(target=churner)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(old)
    got = {r["lab_id"] for r in us.session_results}
    assert {r.lab_id for r in keep} <= got
    assert not any(x.startswith("C") for x in got)


def test_stale_tab_load_never_overwrites_a_fresher_fan_out(lab, monkeypatch):
    app_module, reviewer = lab
    clock = _clocked(monkeypatch, app_module)
    a = reviewer("Dana P")
    b = reviewer("Sam K")
    a.mark("good")
    stale = app_module.state.shared.verdicts_for([LAB])      # good @ TEN
    clock.t += 5
    a.mark("uncheck")
    assert b.rec().status == "ready"
    monkeypatch.setattr(app_module.state.shared, "verdicts_for", lambda ids: stale)
    assert b.tab(mode="tests")[LAB]["status"] == "ready"


# ── 3: the actor's own record follows a newer verdict ────────────────────

def test_actor_gets_the_winning_verdict_when_its_mark_is_superseded(lab, monkeypatch):
    app_module, reviewer = lab
    _clocked(monkeypatch, app_module)
    app_module.state.shared.apply_mark(LAB, "tests", "good", by="Sam K", at=TEN + 600)
    a = reviewer("Dana P")
    resp = a.mark("bad", reason="Low")
    assert resp["status"] == "good"
    assert a.rec().status == "good" and a.rec().reason == ""
    assert resp["tags"]["tests"]["by"] == "Sam K"
    assert a.events("sample_status")[-1]["status"] == "good"
    assert a.results()[(TAB, LAB)]["reviewer"] == "Sam K"


# ── 4: persistence of fanned-out changes ─────────────────────────────────

def _count_persists(monkeypatch, r):
    calls = []
    real = r.ustate.persist
    monkeypatch.setattr(r.ustate, "persist", lambda: (calls.append(1), real()))
    return calls


def test_fan_out_marks_sessions_dirty_and_the_worker_writes_them(lab, monkeypatch):
    app_module, reviewer = lab
    a = reviewer("Dana P")
    b = reviewer("Sam K")
    calls = _count_persists(monkeypatch, b)
    a.mark("good")
    assert calls == [] and b.ustate.dirty is True
    b.ustate.last_active = time.time()
    app_module._session_cleanup_cycle(time.time())
    assert calls == [1] and b.ustate.dirty is False


def test_fan_out_of_a_pending_write_persists_at_once(lab, monkeypatch):
    app_module, reviewer = lab
    _break_writes(monkeypatch, app_module.state.shared)
    a = reviewer("Dana P")
    b = reviewer("Sam K")
    calls = _count_persists(monkeypatch, b)
    a.mark("good")
    assert calls == [1]


def test_regenerate_selected_shares_its_unmarks_in_one_batch(lab, monkeypatch):
    app_module, reviewer = lab
    monkeypatch.setattr(app_module, "PREVIEW_POOL", MagicMock())
    labs = [LAB, OTHER, "092526-50003"]
    a = reviewer("Dana P", holds=[(TAB, x) for x in labs])
    b = reviewer("Sam K", holds=[(TAB, x) for x in labs])
    for x in labs:
        a.mark("good", lab=x)
    store = app_module.state.shared
    batches, singles = [], []
    real_many = store.apply_marks
    monkeypatch.setattr(store, "apply_marks", lambda items: (batches.append(len(items)),
                                                             real_many(items))[1])
    monkeypatch.setattr(store, "apply_mark", lambda *a_, **k: singles.append(1))
    calls = _count_persists(monkeypatch, b)
    resp = a.client.post("/api/regenerate-selected", json={"tab": TAB, "lab_ids": labs})
    assert resp.status_code == 200
    assert len(batches) == 1 and singles == []
    assert calls == []
    assert all(b.rec(x).status == "ready" for x in labs)
    ev = store.history(LAB)[0]
    assert ev["kind"] == "unmark" and ev["detail"]["cause"] == "regenerate"


def test_regenerate_clears_both_modes(lab, monkeypatch):
    app_module, reviewer = lab
    monkeypatch.setattr(app_module, "PREVIEW_POOL", MagicMock())
    store = app_module.state.shared
    store.apply_mark(LAB, "info", "good", by="Cy L", at=time.time() - 60)
    a = reviewer("Dana P")
    a.mark("good")
    a.client.post("/api/regenerate", json={"tab": TAB, "lab_id": LAB})
    got = store.verdicts_for([LAB])[LAB]
    assert got["tests"]["outcome"] == "cleared" and got["info"]["outcome"] == "cleared"


def test_regenerate_of_a_never_judged_sample_writes_no_history(lab, monkeypatch):
    app_module, reviewer = lab
    monkeypatch.setattr(app_module, "PREVIEW_POOL", MagicMock())
    a = reviewer("Dana P")
    a.client.post("/api/regenerate", json={"tab": TAB, "lab_id": LAB})
    assert app_module.state.shared.history(LAB) == []


# ── 5: one session speaks for one mode ───────────────────────────────────

def test_switching_mode_hides_the_other_modes_verdicts_and_export(lab):
    app_module, reviewer = lab
    a = reviewer("Dana P")
    a.mark("good")
    samples = a.tab(mode="info")
    assert samples[LAB]["status"] == "ready"
    links = a.client.post("/api/good-links", json={"tabs": [TAB]}).get_json()["links"]
    assert links == []
    assert a.client.post("/api/export", json={"tabs": [TAB]}).status_code == 400

    samples = a.tab(mode="tests")
    assert samples[LAB]["status"] == "good"
    links = a.client.post("/api/good-links", json={"tabs": [TAB]}).get_json()["links"]
    assert links and links[0]["lab_ids"] == [LAB]


def test_switch_back_brings_the_mark_back_from_the_ledger_when_the_store_is_down(lab, monkeypatch):
    app_module, reviewer = lab
    a = reviewer("Dana P")
    a.mark("good")
    monkeypatch.setattr(app_module.state.shared, "verdicts_for", lambda ids: None)
    assert a.tab(mode="info")[LAB]["status"] == "ready"
    assert a.tab(mode="tests")[LAB]["status"] == "good"


def test_an_info_verdict_does_not_overwrite_a_tests_row(lab):
    app_module, reviewer = lab
    a = reviewer("Dana P")
    a.mark("bad", reason="Low")
    a.tab(mode="info")
    a.mark("good", mode="info")
    rows = [r for r in a.ustate.session_results if r["lab_id"] == LAB]
    assert sorted((r["mode"], r["outcome"]) for r in rows) == [("info", "Good"), ("tests", "Bad")]


def test_the_ledger_only_brings_back_marks_of_the_session_mode(lab):
    import app as app_module
    us = app_module.UserState("u-ledger", "Dana P")
    us.mode = "info"
    us.verdicts[(TAB, LAB)] = {"tab": TAB, "lab_id": LAB, "status": "good", "reason": "",
                               "cc_task_id": None, "judged_at": time.time(),
                               "date": "2026-09-25", "mode": "tests"}
    rec = app_module.SampleRecord(lab_id=LAB, tab=TAB)
    us.add_record(rec)
    assert rec.status == "pending"


def test_restore_keeps_the_mode(lab):
    app_module, reviewer = lab
    a = reviewer("Dana P")
    a.tab(mode="info")
    a.mark("good", mode="info")
    doc = app_module.load_review_state("Dana P")
    assert doc["mode"] == "info"
    us = app_module.UserState("u-restored", "Dana P")
    us.hydrate(doc)
    assert us.mode == "info"
    assert us.records[(TAB, LAB)].status == "good"


# ── 7: retries — one bad entry never blocks the queue ────────────────────

def _pend(app_module, lab_id, at, **kw):
    entry = {"outcome": "good", "by": "x", "at": at, "reason": "", "cc_task_id": None,
             "sample_id": None, "tab": TAB}
    entry.update(kw)
    app_module.pending_verdicts.put(lab_id, "tests", entry)


def test_a_bad_entry_is_skipped_and_the_rest_retried(lab, monkeypatch, caplog):
    app_module, _ = lab
    store = app_module.state.shared
    real = store.apply_marks

    def picky(items):
        if items[0]["lab_id"] == "BAD":
            store._last_write_error = "keep"
            return None
        return real(items)
    monkeypatch.setattr(store, "apply_marks", picky)
    _pend(app_module, "BAD", 1.0)
    _pend(app_module, "GOOD", 2.0)
    app_module._retry_pending_verdicts()
    assert app_module.pending_verdicts.get("GOOD", "tests") is None
    assert app_module.pending_verdicts.get("BAD", "tests")["attempts"] == 1
    for _ in range(app_module.MAX_PENDING_ATTEMPTS):
        app_module._retry_pending_verdicts()
    assert app_module.pending_verdicts.get("BAD", "tests") is None
    assert any("BAD" in r.getMessage() and r.levelname == "ERROR" for r in caplog.records)


def test_store_down_stops_the_retry_cycle(lab, monkeypatch):
    app_module, _ = lab
    store = app_module.state.shared
    calls = []

    def down(items):
        calls.append(1)
        store._last_write_error = "busy"
        return None
    monkeypatch.setattr(store, "apply_marks", down)
    _pend(app_module, "A", 1.0)
    _pend(app_module, "B", 2.0)
    app_module._retry_pending_verdicts()
    assert calls == [1]
    assert app_module.pending_verdicts.get("A", "tests").get("attempts", 0) == 0


def test_an_invalid_entry_is_dropped_at_once(lab, caplog):
    app_module, _ = lab
    _pend(app_module, "A", 1.0, outcome="meh")
    app_module._retry_pending_verdicts()
    assert len(app_module.pending_verdicts) == 0
    assert any(r.levelname == "ERROR" and "A" in r.getMessage() for r in caplog.records)


# ── 8: pending writes survive a restart ──────────────────────────────────

def test_pending_verdicts_save_and_load(tmp_path):
    import app as app_module
    path = tmp_path / "pending_verdicts.json"
    pv = app_module.PendingVerdicts(path=path)
    pv.put(LAB, "tests", {"outcome": "good", "by": "Dana P", "at": 5.0, "tab": TAB})
    assert not path.exists()                    # coalesced: nothing until save()
    assert pv.save() is True
    assert pv.save() is False                   # clean: no second write
    again = app_module.PendingVerdicts(path=path)
    assert again.load() == 1
    assert again.get(LAB, "tests")["by"] == "Dana P"
    pv.remove_if_same((LAB, "tests"), 5.0)
    pv.save()
    fresh = app_module.PendingVerdicts(path=path)
    assert fresh.load() == 0


def test_pending_verdicts_load_is_bounded_and_skips_junk(tmp_path):
    import app as app_module
    path = tmp_path / "pending_verdicts.json"
    rows = [{"lab_id": f"L{i}", "mode": "tests", "outcome": "good", "by": "x", "at": float(i)}
            for i in range(10)]
    rows.append({"lab_id": "J", "mode": "nope", "outcome": "good", "by": "x", "at": 1.0})
    rows.append("junk")
    path.write_text(json.dumps(rows), encoding="utf-8")
    pv = app_module.PendingVerdicts(cap=5, path=path)
    assert pv.load() == 5
    assert pv.get("J", "nope") is None


def test_pending_verdicts_load_survives_a_corrupt_file(tmp_path):
    import app as app_module
    path = tmp_path / "pending_verdicts.json"
    path.write_text("{not json", encoding="utf-8")
    assert app_module.PendingVerdicts(path=path).load() == 0


def test_cleanup_cycle_and_exit_save_pending(lab, monkeypatch, tmp_path):
    app_module, _ = lab
    pv = app_module.PendingVerdicts(path=tmp_path / "p.json")
    monkeypatch.setattr(app_module, "pending_verdicts", pv)
    monkeypatch.setattr(app_module.state.shared, "apply_marks", lambda items: None)
    _pend(app_module, "A", 1.0)
    app_module._session_cleanup_cycle(time.time())
    assert (tmp_path / "p.json").exists()
    _pend(app_module, "B", 2.0)
    app_module._save_state_for_exit()
    assert "B" in (tmp_path / "p.json").read_text(encoding="utf-8")


# ── minors ───────────────────────────────────────────────────────────────

def test_failed_write_with_unreadable_store_reports_no_tags(lab, monkeypatch):
    app_module, reviewer = lab
    _break_writes(monkeypatch, app_module.state.shared)
    monkeypatch.setattr(app_module.state.shared, "verdicts_for", lambda ids: None)
    a = reviewer("Dana P")
    assert a.mark("good")["tags"] is None


def test_a_repeat_good_by_someone_else_renames_the_export_reviewer(lab, monkeypatch):
    app_module, reviewer = lab
    clock = _clocked(monkeypatch, app_module)
    a = reviewer("Dana P")
    b = reviewer("Sam K")
    c = reviewer("Cy L", holds=())
    c.hold(TAB, LAB)
    a.mark("good")
    assert c.results()[(TAB, LAB)]["reviewer"] == "Dana P"
    clock.t += 5
    b.mark("good")
    assert c.results()[(TAB, LAB)]["reviewer"] == "Sam K"


def test_a_mid_session_mode_switch_reloads_every_tab_in_the_browser():
    from pathlib import Path
    import re
    src = (Path(__file__).resolve().parent.parent / "static" / "js" / "app.js").read_text(
        encoding="utf-8")
    body = src[src.index("function applyReviewMode"):]
    body = body[:body.index("\n}\n")]
    assert re.search(r"if \(switched[^)]*\)[^{]*\{\s*restoreAllTabs\(\)", body)


def test_a_tombstone_in_one_mode_keeps_the_other_modes_ledger_entry(lab):
    import app as app_module
    us = app_module.UserState("u-ledger2", "Dana P")
    rec = app_module.SampleRecord(lab_id=LAB, tab=TAB)
    rec.preview_url = "/pdf"
    us.add_record(rec)
    us.verdicts[(TAB, LAB)] = {"tab": TAB, "lab_id": LAB, "status": "good", "reason": "",
                               "cc_task_id": None, "judged_at": time.time(),
                               "date": "2026-09-25", "mode": "info"}
    rec.status, rec.verdict_mode = "good", "tests"
    app_module._apply_shared(us, rec, {"outcome": "cleared", "at": time.time() + 1}, "tests")
    assert rec.status == "ready"
    assert (TAB, LAB) in us.verdicts
