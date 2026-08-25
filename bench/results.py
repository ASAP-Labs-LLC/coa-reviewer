"""What a benchmark run writes down.

One JSON file per run at ``bench/results/run-N<count>.json``, plus a
one-line summary for a human. Percentiles are computed here rather than left
to the reader: a file that stores only the raw list makes everyone who opens
it recompute the metric the run exists to report.
"""

from __future__ import annotations

import json
import os
import platform
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import metrics

RESULTS_DIR = Path(__file__).resolve().parent / "results"


@dataclass
class RunParams:
    """Everything that has to match for two runs to be comparable."""
    samples: int
    seed: int
    coa_bytes: int
    sif_bytes: int
    cpu_throttle: float
    cores: int
    rss_ceiling_mb: int
    js_heap_mb: int
    preview_workers: int
    samples_per_order: int
    # Resolved at run time, recorded so a rerun on another box is legible.
    cpu_count: int = 0
    machine: str = ""
    chromium: str = ""

    # ── the preview path ─────────────────────────────────────────────────
    # "real" drives app.py's own COASession against bench/qbench_fake.py;
    # "fake" is the older bench.fakes.FakeCoaSession, kept so earlier results
    # stay reproducible. The four latency knobs below are what decides whether
    # a pull lands inside SESSION_TIMEOUT_SECONDS, so they are recorded even
    # in "fake" mode (where they are inert) rather than omitted — a missing
    # key and a zero are not the same claim.
    preview_mode: str = "real"
    qbench_post_ms: float = 300.0
    qbench_poll_ms: float = 150.0
    qbench_render_s: float = 4.0
    qbench_jitter: float = 0.25
    # 0 = every sample. Caps the post-pull interaction passes only; it does
    # not touch the pull the verdict is about.
    max_interactions: int = 0


@dataclass
class RunResult:
    # app.py:164-165. Duplicated rather than imported: results.py is unit-
    # tested without importing app (which builds an AppState at module scope).
    # tests/test_bench_qbench_fake.py asserts these still match app.py, so a
    # change there fails the suite instead of silently moving the bar.
    SESSION_TIMEOUT_S = 600
    SESSION_CLEANUP_S = 720

    samples: int
    params: RunParams
    served_count: int = 0
    coa_open_ms: list[float] = field(default_factory=list)
    switch_ms: list[float] = field(default_factory=list)
    inp_ms: Optional[float] = None
    interaction_ms: list[float] = field(default_factory=list)
    peak_rss_mb: Optional[float] = None
    rss_samples: int = 0
    # Time from clicking Start to every sample reaching "ready".
    preview_all_ready_s: Optional[float] = None
    # Set only when the pull DID NOT finish: how long the harness waited
    # before giving up, and how many samples had reached "ready" by then.
    # Recorded because the configuration that fails to complete is the most
    # informative point in a sweep, and the first version of this harness
    # threw it away by raising instead of writing a file.
    pull_gave_up_after_s: Optional[float] = None
    ready_at_giveup: int = 0
    frontend_timed_out: bool = False
    # The bar that pull is judged against. Reported back by the app process
    # from app.py's own constants, so the thresholds cannot go stale here.
    session_timeout_s: float = SESSION_TIMEOUT_S
    session_cleanup_s: float = SESSION_CLEANUP_S
    # How many previews QBench was actually asked to render. Not necessarily
    # N: app.py retries a failed preview up to three times (app.py:1717).
    preview_count: int = 0
    # COASession._session_lock, measured from outside via a wrapper (see
    # bench/realpreview.py). None in fully-faked mode, where there is no lock.
    lock_wait_total_s: Optional[float] = None
    lock_hold_total_s: Optional[float] = None
    lock_wait_max_s: Optional[float] = None
    lock_acquisitions: int = 0
    lock_contended: int = 0
    sse_events: dict = field(default_factory=dict)
    coa_open_failures: int = 0
    switch_failures: int = 0
    notes: list[str] = field(default_factory=list)
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    # ── derived ──────────────────────────────────────────────────────────

    @property
    def ok(self) -> bool:
        """A run is only usable if the app actually served the N asked for.

        A sample with no tests is dropped silently by app.py, so a short
        count is the failure this flag exists to make un-missable.
        """
        return self.served_count == self.samples

    @property
    def completed(self) -> bool:
        """Did every sample reach a terminal status before the harness gave up?"""
        return self.preview_all_ready_s is not None

    def _passes(self, threshold: float) -> Optional[bool]:
        """Whether the pull landed inside ``threshold`` seconds.

        Three answers, and the third one matters. A pull that finished is a
        straight comparison. A pull that was abandoned *after* the threshold
        had already passed is a FAIL — it demonstrably did not land in time,
        and calling that "unknown" would let the worst configuration in a
        sweep report as neither pass nor fail. A pull abandoned *before* the
        threshold says nothing about it, and is None: an unknown that
        defaulted either way would be a fabricated result.
        """
        if self.preview_all_ready_s is not None:
            return self.preview_all_ready_s <= threshold
        if self.pull_gave_up_after_s is not None and self.pull_gave_up_after_s > threshold:
            return False
        return None

    @property
    def pass_session_timeout(self) -> Optional[bool]:
        return self._passes(self.session_timeout_s)

    @property
    def pass_session_cleanup(self) -> Optional[bool]:
        """The hard failure: past this the server drops an idle session."""
        return self._passes(self.session_cleanup_s)

    @property
    def verdict(self) -> str:
        if self.pass_session_timeout is None:
            return "n/a"
        return "PASS" if self.pass_session_timeout else "FAIL"

    @property
    def rss_within_ceiling(self) -> Optional[bool]:
        if self.peak_rss_mb is None:
            return None
        return self.peak_rss_mb <= self.params.rss_ceiling_mb

    def to_dict(self) -> dict:
        return {
            "samples": self.samples,
            "served_count": self.served_count,
            "ok": self.ok,
            "started_at": self.started_at,
            "host": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                # The real core count, as opposed to params.cpu_count, which is
                # what app.py was made to believe so its pool size is pinned.
                "cpus": os.cpu_count(),
            },
            "params": asdict(self.params),
            "coa_open": metrics.summarise(self.coa_open_ms),
            "switch": metrics.summarise(self.switch_ms),
            "inp_ms": self.inp_ms,
            "interactions": metrics.summarise(self.interaction_ms),
            "peak_rss_mb": self.peak_rss_mb,
            "rss_ceiling_mb": self.params.rss_ceiling_mb,
            "rss_within_ceiling": self.rss_within_ceiling,
            "rss_samples": self.rss_samples,
            "preview_all_ready_s": self.preview_all_ready_s,
            "preview_count": self.preview_count,
            "verdict": {
                "preview_all_ready_s": self.preview_all_ready_s,
                "completed": self.completed,
                "gave_up_after_s": self.pull_gave_up_after_s,
                "ready_at_giveup": self.ready_at_giveup,
                "frontend_timed_out": self.frontend_timed_out,
                "session_timeout_s": self.session_timeout_s,
                "session_cleanup_s": self.session_cleanup_s,
                "pass_session_timeout": self.pass_session_timeout,
                "pass_session_cleanup": self.pass_session_cleanup,
                "verdict": self.verdict,
            },
            "lock": {
                "measured": self.lock_wait_total_s is not None,
                "wait_total_s": self.lock_wait_total_s,
                "hold_total_s": self.lock_hold_total_s,
                "wait_max_s": self.lock_wait_max_s,
                "acquisitions": self.lock_acquisitions,
                "contended": self.lock_contended,
            },
            "sse_events": self.sse_events,
            "failures": {
                "coa_open": self.coa_open_failures,
                "switch": self.switch_failures,
            },
            "raw": {
                "coa_open_ms": self.coa_open_ms,
                "switch_ms": self.switch_ms,
                "interaction_ms": self.interaction_ms,
            },
            "notes": self.notes,
        }

    def summary_line(self) -> str:
        def f(v: Optional[float]) -> str:
            return "n/a" if v is None else f"{v:.0f}"
        d = self.to_dict()
        return (
            f"N={self.samples} served={self.served_count} "
            f"coa_open_p95={f(d['coa_open']['p95'])}ms "
            f"switch_p95={f(d['switch']['p95'])}ms "
            f"INP={f(self.inp_ms)}ms "
            f"peak_rss={f(self.peak_rss_mb)}MB/{self.params.rss_ceiling_mb}MB "
            f"workers={self.params.preview_workers} "
            f"pull={f(self.preview_all_ready_s) if self.completed else 'GAVE-UP@' + f(self.pull_gave_up_after_s)}s"
            f"/{self.session_timeout_s:.0f}s {self.verdict} "
            f"{'OK' if self.ok else 'SHORT-COUNT'}"
        )

    def write(self, target: Optional[Path] = None) -> Path:
        """Write the run to ``target``.

        A path ending in ``.json`` is the file to write; anything else is a
        directory to write ``run-N{samples}.json`` into. Without the first
        form, every run of a given size overwrote the previous one — which is
        how a comparison this session was destroyed by the harness that
        produced it. Passing a filename also used to create a *directory* of
        that name, since only the directory form existed.
        """
        target = Path(target) if target is not None else RESULTS_DIR
        if target.suffix == ".json":
            target.parent.mkdir(parents=True, exist_ok=True)
            path = target
        else:
            target.mkdir(parents=True, exist_ok=True)
            path = target / f"run-N{self.samples}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return path
