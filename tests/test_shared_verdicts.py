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
    """apply_mark fails (returns None) until the returned switch is flipped."""
    real = store.apply_mark
    down = {"on": True}

    def flaky(*a, **k):
        return None if down["on"] else real(*a, **k)
    monkeypatch.setattr(store, "apply_mark", flaky)
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
    # the change is written down for B too
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


def test_ledger_migration_moves_fresh_verdicts_once(lab):
    app_module, _ = lab
    _ledger(app_module.REVIEW_STATE_DIR, "Dana P", [
        _entry("Intaked", LAB, "good"),
        _entry("Due Out", OTHER, "bad", reason="Low"),
        _entry("Due Out", "092526-50003", "good", judged_at=time.time() - 13 * 3600),
    ])
    assert app_module.migrate_ledgers_once() == 2
    got = app_module.state.shared.verdicts_for([LAB, OTHER, "092526-50003"])
    assert got[LAB]["info"]["by"] == "Dana P" and got[LAB]["info"]["outcome"] == "good"
    assert got[OTHER]["tests"]["outcome"] == "bad" and got[OTHER]["tests"]["reason"] == "Low"
    assert "092526-50003" not in got                      # stale: not carried
    assert app_module.state.shared.get_meta("ledger_migrated")
    assert app_module.migrate_ledgers_once() == 0


def test_ledger_migration_never_overwrites_an_existing_row(lab):
    app_module, _ = lab
    app_module.state.shared.apply_mark(LAB, "tests", "cleared", by="Sam K")
    _ledger(app_module.REVIEW_STATE_DIR, "Dana P", [_entry("Due Out", LAB, "good")])
    assert app_module.migrate_ledgers_once() == 0
    assert app_module.state.shared.verdicts_for([LAB])[LAB]["tests"]["outcome"] == "cleared"


def test_ledger_migration_waits_when_store_unreadable(lab, monkeypatch):
    app_module, _ = lab
    _ledger(app_module.REVIEW_STATE_DIR, "Dana P", [_entry("Due Out", LAB, "good")])
    monkeypatch.setattr(app_module.state.shared, "verdicts_for", lambda ids: None)
    assert app_module.migrate_ledgers_once() == 0
    assert app_module.state.shared.get_meta("ledger_migrated") is None


def test_ledger_migration_is_not_run_at_import():
    import app as app_module
    import inspect
    src = inspect.getsource(app_module)
    main = src[src.index('if __name__ == "__main__":'):]
    assert "_migrate_ledgers_safely" in main
    assert main.index("_wait_for_port") < main.index("_migrate_ledgers_safely")
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
    monkeypatch.setattr(app_module.state.shared, "apply_mark",
                        lambda *a, **k: calls.append(a) or {"before": None, "applied": True,
                                                            "verdicts": {}})
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
