"""Build the Time Online day payload from presence spans and sample events.

Pure: no I/O, no clock of its own — every rule is a unit test. Times are
epoch seconds in, epoch seconds out; the browser formats them in the
viewer's local zone. Days are the server's local calendar day, which is the
lab's.

``online`` and ``is_today`` exist because "is this person online *right
now*" is not something a day's spans can answer on their own: a span left
``open`` on a past day (the process died mid-span and got force-closed
later) is not evidence anyone is online *now* — only the live presence list
is, and only for today's page. Hour bounds are computed from each point's
actual wall-clock hour (``datetime.fromtimestamp(...).hour``), not from
elapsed seconds since local midnight, because a DST fall-back day is 25
real hours long and elapsed-seconds math would push the end hour past 24.
"""

from __future__ import annotations

import bisect
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Sequence, Tuple

BIN_SECONDS = 300          # editing blocks are built from 5-minute bins
DEFAULT_START_HOUR = 6
DEFAULT_END_HOUR = 22
MAX_USERS = 100            # columns on one day; beyond this is a data error
MAX_SPANS_PER_USER = 500


def day_window(day: date) -> Tuple[float, float]:
    start = datetime(day.year, day.month, day.day)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def _merge(spans: List[dict]) -> Tuple[List[dict], bool]:
    """Merge overlapping spans (already-bounded input, capped further
    here). Returns ``(merged, truncated)``."""
    ordered = sorted(spans, key=lambda x: x["start"])
    truncated = len(ordered) > MAX_SPANS_PER_USER
    merged: List[dict] = []
    for s in ordered[:MAX_SPANS_PER_USER]:
        if merged and s["start"] <= merged[-1]["end"]:
            last = merged[-1]
            last["end"] = max(last["end"], s["end"])
            last["open"] = last["open"] or s["open"]
        else:
            merged.append(dict(s))
    return merged, truncated


def _blocks(times: List[float]) -> List[dict]:
    """Group change times into editing blocks; a block continues while each
    change lands within one empty bin of the previous one. ``times`` must
    already be sorted."""
    blocks: List[dict] = []
    for t in times:
        if blocks and t - blocks[-1]["end"] <= 2 * BIN_SECONDS:
            blocks[-1]["end"] = t
            blocks[-1]["count"] += 1
        else:
            blocks.append({"start": t, "end": t, "count": 1})
    for b in blocks:   # give single changes a visible height
        b["end"] = max(b["end"], b["start"] + BIN_SECONDS)
    return blocks


def _bounds(points: Iterable[float], hi: float) -> Dict[str, int]:
    """Hour bounds from each point's actual local wall-clock time, so a DST
    transition day (23 or 25 real hours) never produces an ``end_hour``
    outside 0..24.

    A point clipped to the day's end lands exactly on ``hi`` — local
    midnight of the *next* day, wall-clock hour 0 — which would otherwise
    read as the *start* of a day rather than the end of this one; it's
    special-cased to hour 24 instead of being converted normally.
    """
    start_h, end_h = DEFAULT_START_HOUR, DEFAULT_END_HOUR
    for p in points:
        if p >= hi:
            end_h = 24
            continue
        dt = datetime.fromtimestamp(p)
        hour = dt.hour + dt.minute / 60
        floor_h = int(hour)                                  # floor: always <= hour
        ceil_h = floor_h if hour == floor_h else floor_h + 1  # ceil
        start_h = min(start_h, floor_h)
        end_h = max(end_h, ceil_h)
    return {"start_hour": max(0, start_h), "end_hour": min(24, end_h)}


def build_day(day: date, spans: List[dict], events: List[dict], *, now: float,
             online: Sequence[str] = (), is_today: bool = False,
             truncated: bool = False) -> dict:
    lo, hi = day_window(day)
    ceiling = min(hi, now)
    per_user: Dict[str, dict] = {}
    was_truncated = bool(truncated)

    def slot(name: str) -> dict:
        nonlocal was_truncated
        key = name.strip().casefold()
        if key not in per_user:
            if len(per_user) >= MAX_USERS:
                was_truncated = True
                return {"spans": [], "events": []}   # dropped; not tracked
            per_user[key] = {"user": name.strip(), "spans": [], "events": []}
        return per_user[key]

    for s in spans:
        start, end = max(s["start"], lo), min(s["end"], ceiling)
        if end > start:
            slot(s["user"])["spans"].append(
                {"start": start, "end": end, "open": bool(s.get("open"))})
    for e in events:
        if lo <= e["at"] < hi:
            slot(e["user"])["events"].append(e)

    online_keys = {u.strip().casefold() for u in online if u and u.strip()}
    users, points = [], []
    for entry in per_user.values():
        merged, span_truncated = _merge(entry["spans"])
        if span_truncated:
            was_truncated = True
        times = sorted(e["at"] for e in entry["events"])
        by_kind: Dict[str, int] = {}
        for e in entry["events"]:
            by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
        for s in merged:
            lo_i = bisect.bisect_left(times, s["start"])
            hi_i = bisect.bisect_right(times, s["end"])
            s["changes"] = hi_i - lo_i
            points += [s["start"], s["end"]]
        blocks = _blocks(times)
        points += [b["start"] for b in blocks] + [b["end"] for b in blocks]
        users.append({
            "user": entry["user"], "spans": merged, "blocks": blocks,
            "first": min([s["start"] for s in merged] + times, default=hi),
            "totals": {"online_seconds": int(sum(s["end"] - s["start"] for s in merged)),
                       "changes": len(times), "by_kind": by_kind},
        })
    users.sort(key=lambda u: (u["first"], u["user"].casefold()))

    online_now = (sum(1 for u in users if u["user"].strip().casefold() in online_keys)
                 if is_today else 0)
    return {
        "date": day.isoformat(),
        "users": users,
        "bounds": _bounds(points, hi),
        "truncated": was_truncated,
        "summary": {
            "people": len(users),
            "online_now": online_now,
            "online_seconds": sum(u["totals"]["online_seconds"] for u in users),
            "changes": sum(u["totals"]["changes"] for u in users),
        },
    }
