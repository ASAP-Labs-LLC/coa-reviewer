import os
import sys
import time as time_mod
from datetime import date, datetime

import pytest

from activity import BIN_SECONDS, MAX_SPANS_PER_USER, MAX_USERS, build_day, day_window


def ts(h, m=0):
    return datetime(2026, 9, 25, h, m).timestamp()


D = date(2026, 9, 25)


def test_day_window_is_local_midnight_to_midnight():
    lo, hi = day_window(D)
    assert lo == datetime(2026, 9, 25).timestamp()
    assert hi == datetime(2026, 9, 26).timestamp()


def test_users_spans_and_changes():
    spans = [{"user": "Dana P", "start": ts(8), "end": ts(12), "open": False},
             {"user": "dana p", "start": ts(13), "end": ts(15), "open": False}]
    events = [{"user": "Dana P", "at": ts(9, 1), "kind": "mark"},
              {"user": "Dana P", "at": ts(9, 3), "kind": "test_result"},
              {"user": "Dana P", "at": ts(14), "kind": "mark"}]
    out = build_day(D, spans, events, now=ts(20))
    assert [u["user"] for u in out["users"]] == ["Dana P"]
    u = out["users"][0]
    assert len(u["spans"]) == 2
    assert u["spans"][0]["changes"] == 2 and u["spans"][1]["changes"] == 1
    assert u["totals"]["changes"] == 3
    assert u["totals"]["online_seconds"] == 6 * 3600
    assert u["totals"]["by_kind"] == {"mark": 2, "test_result": 1}
    assert u["blocks"][0]["count"] == 2   # 9:01 and 9:03 share a block


def test_overlapping_spans_merge():
    spans = [{"user": "u", "start": ts(8), "end": ts(10), "open": False},
             {"user": "u", "start": ts(9), "end": ts(11), "open": False}]
    u = build_day(D, spans, [], now=ts(20))["users"][0]
    assert len(u["spans"]) == 1 and u["totals"]["online_seconds"] == 3 * 3600


def test_spans_clip_to_day_and_now():
    spans = [{"user": "u", "start": ts(0) - 3600, "end": ts(23), "open": True}]
    out = build_day(D, spans, [], now=ts(10))
    s = out["users"][0]["spans"][0]
    assert s["start"] == ts(0) and s["end"] == ts(10) and s["open"] is True


def test_bounds_default_and_extend():
    assert build_day(D, [], [], now=ts(20))["bounds"] == {"start_hour": 6, "end_hour": 22}
    spans = [{"user": "u", "start": ts(5, 30), "end": ts(22, 10), "open": False}]
    assert build_day(D, spans, [], now=ts(23))["bounds"] == {"start_hour": 5, "end_hour": 23}


def test_events_without_span_still_show():
    out = build_day(D, [], [{"user": "x", "at": ts(9), "kind": "mark"}], now=ts(20))
    assert out["users"][0]["blocks"][0]["count"] == 1


def test_blocks_split_on_gap():
    ev = [{"user": "x", "at": ts(9), "kind": "mark"},
          {"user": "x", "at": ts(9) + 3 * BIN_SECONDS, "kind": "mark"}]
    assert len(build_day(D, [], ev, now=ts(20))["users"][0]["blocks"]) == 2


# ── online_now: authoritative live list, gated by is_today ────────────────

def test_summary_online_now_comes_from_the_online_list_when_today():
    spans = [{"user": "a", "start": ts(8), "end": ts(9), "open": True}]
    out = build_day(D, spans, [{"user": "a", "at": ts(8, 30), "kind": "mark"}],
                    now=ts(9), online=["A"], is_today=True)
    assert out["summary"] == {"people": 1, "online_now": 1, "online_seconds": 3600,
                              "changes": 1}


def test_online_now_ignores_open_spans_on_a_past_day():
    """A span left "open" on a day that isn't today (e.g. the process died
    mid-span and it was force-closed later) must not count as "online now"
    just because the row says open — that was the old, buggy heuristic."""
    spans = [{"user": "a", "start": ts(8), "end": ts(9), "open": True}]
    out = build_day(D, spans, [], now=ts(9), online=["a"], is_today=False)
    assert out["summary"]["online_now"] == 0


def test_online_now_requires_presence_in_the_online_list():
    spans = [{"user": "a", "start": ts(8), "end": ts(9), "open": True}]
    out = build_day(D, spans, [], now=ts(9), online=["someone else"], is_today=True)
    assert out["summary"]["online_now"] == 0


def test_online_now_default_is_zero():
    out = build_day(D, [], [], now=ts(9))
    assert out["summary"]["online_now"] == 0


# ── truncation flags ────────────────────────────────────────────────────

def test_truncated_flag_defaults_false():
    assert build_day(D, [], [], now=ts(9))["truncated"] is False


def test_truncated_when_users_exceed_max_users():
    spans = [{"user": f"u{i}", "start": ts(8), "end": ts(9), "open": False}
             for i in range(MAX_USERS + 5)]
    out = build_day(D, spans, [], now=ts(9))
    assert out["truncated"] is True
    assert len(out["users"]) == MAX_USERS


def test_truncated_when_one_users_spans_exceed_max_spans():
    spans = [{"user": "u", "start": ts(0) + i, "end": ts(0) + i + 0.5, "open": False}
             for i in range(MAX_SPANS_PER_USER + 5)]
    out = build_day(D, spans, [], now=ts(20))
    assert out["truncated"] is True


def test_truncated_kwarg_ors_in_from_caller():
    out = build_day(D, [], [], now=ts(9), truncated=True)
    assert out["truncated"] is True


# ── DST: bounds must use wall-clock hour, not elapsed seconds ─────────────

@pytest.mark.skipif(sys.platform.startswith("win"), reason="time.tzset is POSIX-only")
def test_dst_fall_back_day_bounds_use_wall_clock_hour():
    """America/Los_Angeles, 2026-11-01 is a 25-hour fall-back day. A point
    25.5 elapsed hours into the day is wall-clock ~01:30, not hour 25 —
    bounds computed from elapsed seconds would blow past 24."""
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time_mod.tzset()
    try:
        day = date(2026, 11, 1)
        lo, hi = day_window(day)
        assert hi - lo == 25 * 3600   # confirms this really is the fall-back day
        late_point = lo + 25.5 * 3600
        spans = [{"user": "u", "start": lo + 3600, "end": late_point, "open": False}]
        out = build_day(day, spans, [], now=hi)
        assert out["bounds"]["end_hour"] <= 24
        assert out["bounds"]["start_hour"] >= 0
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time_mod.tzset()
