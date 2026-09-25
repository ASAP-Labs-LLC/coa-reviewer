from datetime import date, datetime

from activity import BIN_SECONDS, build_day, day_window


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


def test_summary():
    spans = [{"user": "a", "start": ts(8), "end": ts(9), "open": True}]
    out = build_day(D, spans, [{"user": "a", "at": ts(8, 30), "kind": "mark"}], now=ts(9))
    assert out["summary"] == {"people": 1, "online_now": 1, "online_seconds": 3600,
                              "changes": 1}
