"""Build the Time Online day payload from presence spans and sample events.

Pure: no I/O, no clock, so every rule is a unit test. Times are epoch seconds
in, epoch seconds out; the browser formats them in the viewer's local zone.
Days are the server's local calendar day, which is the lab's.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Tuple

BIN_SECONDS = 300          # editing blocks are built from 5-minute bins
DEFAULT_START_HOUR = 6
DEFAULT_END_HOUR = 22
MAX_USERS = 100            # columns on one day; beyond this is a data error
MAX_SPANS_PER_USER = 500


def day_window(day: date) -> Tuple[float, float]:
    start = datetime(day.year, day.month, day.day)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def _merge(spans: List[dict]) -> List[dict]:
    merged: List[dict] = []
    for s in sorted(spans, key=lambda x: x["start"])[:MAX_SPANS_PER_USER]:
        if merged and s["start"] <= merged[-1]["end"]:
            last = merged[-1]
            last["end"] = max(last["end"], s["end"])
            last["open"] = last["open"] or s["open"]
        else:
            merged.append(dict(s))
    return merged


def _blocks(times: List[float]) -> List[dict]:
    """Group change times into editing blocks; a block continues while each
    change lands within one empty bin of the previous one."""
    blocks: List[dict] = []
    for t in sorted(times):
        if blocks and t - blocks[-1]["end"] <= 2 * BIN_SECONDS:
            blocks[-1]["end"] = t
            blocks[-1]["count"] += 1
        else:
            blocks.append({"start": t, "end": t, "count": 1})
    for b in blocks:   # give single changes a visible height
        b["end"] = max(b["end"], b["start"] + BIN_SECONDS)
    return blocks


def _bounds(points: Iterable[float], lo: float) -> Dict[str, int]:
    start_h, end_h = DEFAULT_START_HOUR, DEFAULT_END_HOUR
    for p in points:
        hour = (p - lo) / 3600
        start_h = min(start_h, int(math.floor(hour)))
        end_h = max(end_h, int(math.ceil(hour)))
    return {"start_hour": max(0, start_h), "end_hour": min(24, end_h)}


def build_day(day: date, spans: List[dict], events: List[dict], *, now: float) -> dict:
    lo, hi = day_window(day)
    ceiling = min(hi, now)
    per_user: Dict[str, dict] = {}

    def slot(name: str) -> dict:
        key = name.strip().casefold()
        if key not in per_user and len(per_user) < MAX_USERS:
            per_user[key] = {"user": name.strip(), "spans": [], "events": []}
        return per_user.get(key, {"spans": [], "events": []})

    for s in spans:
        start, end = max(s["start"], lo), min(s["end"], ceiling)
        if end > start:
            slot(s["user"])["spans"].append(
                {"start": start, "end": end, "open": bool(s.get("open"))})
    for e in events:
        if lo <= e["at"] < hi:
            slot(e["user"])["events"].append(e)

    users, points = [], []
    for entry in per_user.values():
        merged = _merge(entry["spans"])
        times = [e["at"] for e in entry["events"]]
        by_kind: Dict[str, int] = {}
        for e in entry["events"]:
            by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
        for s in merged:
            s["changes"] = sum(1 for t in times if s["start"] <= t <= s["end"])
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
    return {
        "date": day.isoformat(),
        "users": users,
        "bounds": _bounds(points, lo),
        "summary": {
            "people": len(users),
            "online_now": sum(1 for u in users if any(s["open"] for s in u["spans"])),
            "online_seconds": sum(u["totals"]["online_seconds"] for u in users),
            "changes": sum(u["totals"]["changes"] for u in users),
        },
    }
