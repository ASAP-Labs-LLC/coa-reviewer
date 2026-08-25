"""Percentiles, INP, and a sampler for another process's RSS.

Kept free of Playwright and of ``app`` so it can be unit-tested in the
normal (fast) pytest run.
"""

from __future__ import annotations

import subprocess
import threading
import time
from typing import Any, Iterable, Mapping, Optional, Sequence


# ── percentiles ──────────────────────────────────────────────────────────

def percentile(values: Sequence[float], p: float) -> Optional[float]:
    """The ``p``-th percentile with linear interpolation between ranks.

    Same definition as numpy's default (``method="linear"``), chosen over
    nearest-rank because at N=20 a single outlier is 5% of the sample and
    nearest-rank would round it all the way up to the maximum.

    Returns None for an empty input rather than raising: a run that failed
    to measure anything must still be able to write its result file.
    """
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (p / 100.0) * (len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def summarise(values: Sequence[float]) -> dict:
    """The block every timing series is reported as."""
    return {
        "n": len(values),
        "min": min(values) if values else None,
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "max": max(values) if values else None,
    }


# ── INP ──────────────────────────────────────────────────────────────────

def interaction_durations(entries: Iterable[Mapping[str, Any]]) -> list[float]:
    """Collapse PerformanceEventTiming entries into per-interaction durations.

    One tap produces pointerdown/pointerup/click (and a keypress produces
    keydown/keyup); they share an ``interactionId`` and are ONE interaction
    whose duration is the largest of the group. ``interactionId == 0`` means
    the event is not part of an interaction at all (mousemove, mouseover) and
    is dropped — those routinely carry the largest durations and would
    otherwise dominate the metric.

    Returned newest-first is not meaningful, so the list is sorted descending
    by duration; callers that want them in order should not use this.
    """
    worst: dict[int, float] = {}
    for e in entries:
        iid = int(e.get("interactionId") or 0)
        if not iid:
            continue
        d = float(e.get("duration") or 0.0)
        if d > worst.get(iid, -1.0):
            worst[iid] = d
    return sorted(worst.values(), reverse=True)


def inp(entries: Iterable[Mapping[str, Any]]) -> Optional[float]:
    """Interaction to Next Paint, as web-vitals defines it.

    The high percentile with one discarded outlier per 50 interactions:
    index ``floor(n / 50)`` of the descending list. Below 50 interactions
    that is index 0 — the worst interaction — which is the documented
    behaviour, not an approximation.
    """
    durations = interaction_durations(entries)
    if not durations:
        return None
    return durations[min(len(durations) - 1, len(durations) // 50)]


# ── server RSS ───────────────────────────────────────────────────────────

def read_rss_mb(pid: int) -> Optional[float]:
    """Resident set size of ``pid`` in MB, via ``ps``.

    ``ps`` rather than psutil deliberately: psutil is not installed, and
    adding it to requirements.txt would put a benchmark-only dependency into
    every release venv.
    """
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    raw = out.stdout.strip()
    if not raw:
        return None
    try:
        return int(raw.split()[0]) / 1024.0   # ps reports KB on macOS + Linux
    except (ValueError, IndexError):
        return None


class RssSampler:
    """Poll a process's RSS on a background thread and keep the peak."""

    def __init__(self, pid: int, interval: float = 0.25) -> None:
        self.pid = pid
        self.interval = interval
        self.peak_mb: Optional[float] = None
        self.samples = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _run(self) -> None:
        while not self._stop.is_set():
            mb = read_rss_mb(self.pid)
            if mb is not None:
                self.samples += 1
                if self.peak_mb is None or mb > self.peak_mb:
                    self.peak_mb = mb
            self._stop.wait(self.interval)

    def start(self) -> "RssSampler":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> Optional[float]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        return self.peak_mb

    def __enter__(self) -> "RssSampler":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
