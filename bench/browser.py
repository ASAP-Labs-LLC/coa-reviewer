"""Driving the real UI in a real headless Chromium, and timing what a
reviewer actually waits for.

**Which Chromium.** ``channel="chromium"`` is not optional. Playwright's
plain ``headless=True`` launches ``chrome-headless-shell``, which ships no
PDF plugin at all: a ``<iframe src="...pdf">`` there stays on ``about:blank``
for ever and every COA timing would be a timeout. Verified on this box
before anything else was built.

**What "the COA is open" means.** The COA lives in an out-of-process PDFium
plugin, and nothing about it is visible to script in the page:

* the iframe's ``load`` event fires ~30 ms in, when the *plugin shell*
  commits — it fired at 30 ms even against a server that deliberately
  stalled one second in the middle of the body, so it is not a signal about
  the document at all;
* the Resource Timing entry for the iframe navigation reports
  ``responseEnd == responseStart`` and a 777-byte ``transferSize``, because
  the load is handed off to the plugin;
* the plugin's own frames appear before the bytes finish arriving.

So the only honest signal is pixels. Each COA is printed with a full-width
masthead band in one of six well-separated colours, and "open" is the first
screencast frame in which that colour fills the viewer pane. Measured
against the deliberately-stalled server, this signal reported 1066 ms where
the iframe ``load`` event reported 28 ms.

**What that costs.** Frames arrive only when the compositor produces one, so
every timing is quantised to the screencast cadence (~16-60 ms under CPU
throttling) and is an upper bound within one frame interval. That is
recorded in the results file rather than hidden.
"""

from __future__ import annotations

import base64
import io
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from PIL import Image

from .synth import MARKERS

PDF_EXTENSION = "chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai"

# One evaluation answering "where has the pull got to, and is the app still
# listening?". The second half is not decoration: static/js/app.js closes the
# EventSource inside `triggerTimeout()` after INACTIVITY_MS (10 minutes, the
# same value as app.py's SESSION_TIMEOUT_SECONDS), and SSE is the *only*
# channel by which a preview reports "ready". So a pull that runs past 600s
# with nobody touching the keyboard does not merely miss a target — the rows
# still pending at that moment can never be seen to land, no matter how long
# the harness waits. Without this check a run in that state is
# indistinguishable from a slow one, and both report as a bare timeout.
PULL_STATE_JS = """
() => {
  const rows = [...document.querySelectorAll('#sample-list .sample-item')];
  const busy = s => s === 'pending' || s === 'loading';
  const ov = document.getElementById('timeout-overlay');
  return {
    total: rows.length,
    ready: rows.filter(r => r.dataset.status === 'ready').length,
    terminal: rows.filter(r => !busy(r.dataset.status)).length,
    overlay: !!ov && !ov.classList.contains('hidden'),
  };
}
"""

# How far a pixel may sit from a marker's RGB and still count as that marker.
# The closest pair of markers is ~140 apart, so 55 leaves a wide margin for
# JPEG artefacts on the band's edges.
COLOUR_TOLERANCE = 55
# Fraction of the viewer pane that must carry the marker colour. The band is
# the top ~28% of page one, and the page fills the pane's width under
# `#view=FitH`, so a rendered COA scores ~0.15-0.30. Anything below 0.02 is
# "not this COA".
MATCH_ON = 0.05
MATCH_OFF = 0.01

# Installed before any document runs. Buffered so interactions that happen
# before the observer is attached are still counted; 16 ms is the smallest
# threshold the spec allows.
INP_INIT_SCRIPT = """
(() => {
  // Tally SSE event types. The app sheds queued events when a browser falls
  // behind (app.py:1116, queue maxsize 200) and tells it to `resync`, which
  // reloads every tab and rebuilds the sample list from scratch. That is a
  // real load-dependent behaviour and needs counting, not just surviving.
  try {
    const RealES = window.EventSource;
    window.__benchSse = Object.create(null);
    const Wrapped = function (url, opts) {
      const es = new RealES(url, opts);
      es.addEventListener('message', (ev) => {
        try {
          const t = JSON.parse(ev.data).type || '?';
          window.__benchSse[t] = (window.__benchSse[t] || 0) + 1;
        } catch (e) { /* keepalives are not JSON */ }
      });
      return es;
    };
    Wrapped.prototype = RealES.prototype;
    window.EventSource = Wrapped;
  } catch (err) { /* not the main frame */ }
})();
(() => {
  try {
    if (window.__benchEvents) return;
    window.__benchEvents = [];
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        window.__benchEvents.push({
          interactionId: e.interactionId, duration: e.duration,
          name: e.name, startTime: e.startTime,
        });
      }
    }).observe({ type: 'event', buffered: true, durationThreshold: 16 });
  } catch (err) { /* PDF plugin frames have no PerformanceObserver */ }
})();
"""


@dataclass
class PullOutcome:
    """How the initial pull ended.

    Returned rather than raised. A configuration that cannot finish is the
    most informative point in a sweep, and an exception here means the run
    writes no result file at all — the measurement destroying its own
    evidence.
    """
    completed: bool
    elapsed_s: float
    ready: int
    total: int
    frontend_timed_out: bool = False
    reason: str = ""


@dataclass
class _Frame:
    arrived: float
    stamp: float
    data: str
    scores: dict = field(default_factory=dict)


class PaintWatcher:
    """Turns CDP screencast frames into 'when did this COA appear' answers."""

    def __init__(self, cdp, page, pane_selector: str = "#pdf-viewer") -> None:
        self.cdp = cdp
        self.page = page
        self.pane_selector = pane_selector
        self._frames: list[_Frame] = []
        self._region: Optional[tuple] = None
        self._viewport = page.viewport_size
        self._clock_offset: Optional[float] = None
        self.notes: list[str] = []
        self.frames_seen = 0

    # ── plumbing ─────────────────────────────────────────────────────────

    def _on_frame(self, ev: dict) -> None:
        # Deliberately cheap: this runs on Playwright's event-dispatch
        # thread, and decoding here would stall every other call.
        self._frames.append(_Frame(
            arrived=time.time(),
            stamp=float((ev.get("metadata") or {}).get("timestamp") or 0.0),
            data=ev["data"],
        ))
        self.frames_seen += 1
        try:
            self.cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})
        except Exception:
            pass

    def start(self, quality: int = 60, max_width: int = 640, max_height: int = 480) -> None:
        self.cdp.on("Page.screencastFrame", self._on_frame)
        self.cdp.send("Page.startScreencast", {
            "format": "jpeg", "quality": quality,
            "maxWidth": max_width, "maxHeight": max_height, "everyNthFrame": 1,
        })

    def stop(self) -> None:
        try:
            self.cdp.send("Page.stopScreencast")
        except Exception:
            pass

    def locate_pane(self) -> None:
        """Cache the viewer pane's box so frames can be cropped to it."""
        box = self.page.locator(self.pane_selector).bounding_box()
        if not box:
            raise RuntimeError(f"{self.pane_selector} has no box — is the COA pane hidden?")
        self._pane_box = box
        self._region = None   # recomputed against the first frame's size

    # ── scoring ──────────────────────────────────────────────────────────

    def _region_for(self, w: int, h: int) -> tuple:
        if self._region is not None:
            return self._region
        vp = self._viewport or {"width": w, "height": h}
        sx, sy = w / vp["width"], h / vp["height"]
        b = self._pane_box
        # A few pixels of inset keeps the pane's own border out of the count.
        x0 = max(0, int(b["x"] * sx) + 2)
        y0 = max(0, int(b["y"] * sy) + 2)
        x1 = min(w, int((b["x"] + b["width"]) * sx) - 2)
        y1 = min(h, int((b["y"] + b["height"]) * sy) - 2)
        if x1 - x0 < 8 or y1 - y0 < 8:
            x0, y0, x1, y1 = 0, 0, w, h
            self.notes.append("viewer pane too small in the screencast frame; "
                              "paint detection fell back to the whole viewport")
        self._region = (x0, y0, x1, y1)
        return self._region

    def _score(self, frame: _Frame, marker: int) -> float:
        cached = frame.scores.get(marker)
        if cached is not None:
            return cached
        img = Image.open(io.BytesIO(base64.b64decode(frame.data))).convert("RGB")
        w, h = img.size
        crop = img.crop(self._region_for(w, h)).resize((32, 32))
        raw = crop.tobytes()
        tr, tg, tb = (int(round(c * 255)) for c in MARKERS[marker])
        tol2 = COLOUR_TOLERANCE * COLOUR_TOLERANCE
        hits = 0
        for i in range(0, len(raw), 3):
            dr = raw[i] - tr
            dg = raw[i + 1] - tg
            db = raw[i + 2] - tb
            if dr * dr + dg * dg + db * db <= tol2:
                hits += 1
        score = hits / (len(raw) / 3)
        frame.scores[marker] = score
        return score

    def _frame_time(self, frame: _Frame) -> float:
        """Frame times on the same clock as ``time.time()``.

        ``metadata.timestamp`` is the capture time and is preferred; it is
        checked once against the arrival time and abandoned if the two clocks
        turn out not to agree.
        """
        if self._clock_offset is None:
            if frame.stamp and abs(frame.stamp - frame.arrived) < 5.0:
                self._clock_offset = 0.0
            else:
                self._clock_offset = float("nan")
                self.notes.append(
                    "screencast metadata timestamps did not agree with the wall "
                    "clock; paint times use frame-arrival time instead, which "
                    "adds the CDP delivery latency")
        if self._clock_offset == 0.0 and frame.stamp:
            return frame.stamp
        return frame.arrived

    # ── the measurement ──────────────────────────────────────────────────

    def wait_for_paint(self, marker: int, t0: float, timeout: float = 20.0) -> Optional[float]:
        """Milliseconds from ``t0`` until the COA with ``marker`` is on screen.

        Requires the pane to be seen *blank* (or at least not showing this
        marker) at or after ``t0`` before it will accept a match. The app
        routes every src change through ``about:blank`` first, so this
        transition always exists — and without it a COA that happened to
        share the previous one's colour would report an instantaneous switch.

        Returns None if the COA never appeared inside ``timeout``.
        """
        deadline = time.time() + timeout
        idx = 0
        seen_blank = False
        while True:
            while idx < len(self._frames):
                frame = self._frames[idx]
                idx += 1
                ft = self._frame_time(frame)
                if ft < t0:
                    continue
                score = self._score(frame, marker)
                if not seen_blank:
                    if score <= MATCH_OFF:
                        seen_blank = True
                    continue
                if score >= MATCH_ON:
                    return (ft - t0) * 1000.0
            if time.time() > deadline:
                return None
            # Lets Playwright's dispatcher deliver more frames.
            self.page.wait_for_timeout(8)

    def trim(self, keep_after: float) -> None:
        """Drop frames older than ``keep_after`` so a 250-sample run does not
        accumulate every frame it ever saw."""
        self._frames = [f for f in self._frames if self._frame_time(f) >= keep_after]


# ══════════════════════════════════════════════════════════════════════════
# Driving the app
# ══════════════════════════════════════════════════════════════════════════

class BenchDriver:
    """Boots the app the way a reviewer does, then measures the two actions.

    The boot sequence is the fast one, verified against this app:

    1. ``POST /api/portal-card-login`` through the browser context's own
       request object, so the session cookie is already set when the page
       first loads. One call, and it never touches ``/api/login`` (which
       drives a real Playwright session against real QBench).
    2. Click ``button.review-pill[data-mode="tests"]``. The picker is not
       cosmetic — ``chooseReviewMode()`` awaits a promise that only a click
       resolves, so the app does not start without it.
    3. Click ``#start-btn``.
    """

    def __init__(self, page, context, cdp, base_url: str, lab, *, timeout_s: float = 900.0):
        self.page = page
        self.context = context
        self.cdp = cdp
        self.base_url = base_url.rstrip("/")
        self.lab = lab
        self.timeout_s = timeout_s
        self.watcher = PaintWatcher(cdp, page)
        self.pdf_requests = 0
        page.on("request", self._count_request)

    def _count_request(self, request) -> None:
        if "/api/pdf/" in request.url:
            self.pdf_requests += 1

    # ── boot ─────────────────────────────────────────────────────────────

    def login(self, card_code: str = "BENCH-CARD") -> None:
        resp = self.context.request.post(
            f"{self.base_url}/api/portal-card-login",
            data={"code": card_code},
        )
        if not resp.ok:
            raise RuntimeError(f"card login failed: {resp.status} {resp.text()[:200]}")

    def open_app(self, expected: int) -> float:
        """Load the page, choose Tests, Start, and wait for N ready samples.

        Returns the seconds from clicking Start to the last preview landing.
        """
        self.page.goto(self.base_url + "/", wait_until="domcontentloaded")
        self.page.wait_for_selector('button.review-pill[data-mode="tests"]', timeout=30_000)
        self.page.click('button.review-pill[data-mode="tests"]')
        self.page.wait_for_selector("#start-btn", state="visible", timeout=30_000)

        started = time.time()
        self.page.click("#start-btn")

        # The count line is the app's own answer to "how many samples are on
        # this tab", which is exactly the number a dropped sample would dent.
        self.page.wait_for_function(
            "n => document.querySelectorAll('#sample-list .sample-item').length === n",
            arg=expected, timeout=int(self.timeout_s * 1000),
        )

        # Then wait for every preview to finish. `pending`/`loading` are the
        # only non-terminal states. Polled rather than left to
        # wait_for_function so the loop can also notice the app locking the
        # reviewer out — see PULL_STATE_JS.
        deadline = started + self.timeout_s
        state = {"total": expected, "ready": 0, "terminal": 0, "overlay": False}
        while True:
            state = self.page.evaluate(PULL_STATE_JS)
            elapsed = time.time() - started
            if state["total"] > 0 and state["terminal"] >= state["total"]:
                return PullOutcome(True, elapsed, state["ready"], state["total"])
            if state["overlay"]:
                return PullOutcome(
                    False, elapsed, state["ready"], state["total"],
                    frontend_timed_out=True,
                    reason="the frontend's inactivity overlay appeared mid-pull; "
                           "triggerTimeout() closed the EventSource, so no further "
                           "preview can ever be reported ready to this browser")
            if time.time() >= deadline:
                return PullOutcome(
                    False, elapsed, state["ready"], state["total"],
                    reason=f"gave up after {self.timeout_s:.0f}s with "
                           f"{state['total'] - state['terminal']} sample(s) still "
                           f"pending or loading")
            self.page.wait_for_timeout(500)

    def served_count(self) -> int:
        """What ``#sample-count`` says, parsed. The run's own tripwire."""
        text = self.page.locator("#sample-count").inner_text()
        digits = "".join(c for c in text.split()[0] if c.isdigit())
        return int(digits or 0)

    def ready_count(self) -> int:
        return self.page.locator('#sample-list .sample-item[data-status="ready"]').count()

    # ── measurement ──────────────────────────────────────────────────────

    def _marker(self, index: int) -> int:
        return self.lab.marker_index(self.lab.samples[index]["lab_id"])

    def _row(self, index: int):
        lab_id = self.lab.samples[index]["lab_id"]
        return self.page.locator(f'#sample-list .sample-item[data-lab="{lab_id}"]')

    def select(self, index: int, timeout: float = 25.0) -> Optional[float]:
        """Click row ``index`` and time the COA into the viewer pane.

        Retried once, because the list is not stable under load: an SSE
        overflow makes the client call ``restoreAllTabs()``, which rebuilds
        every row, and a row located a moment earlier is then detached.
        Retrying keeps that from ending the run — the ``sse.resync`` tally in
        the results is where it gets reported instead.
        """
        for attempt in (0, 1):
            try:
                row = self._row(index)
                row.scroll_into_view_if_needed()
                self.watcher.trim(time.time() - 0.5)
                t0 = time.time()
                row.click()
                break
            except Exception:
                if attempt:
                    raise
                self.page.wait_for_timeout(500)
        return self.watcher.wait_for_paint(self._marker(index), t0, timeout)

    def arrow_down(self, index: int, timeout: float = 25.0) -> Optional[float]:
        """Press ArrowDown and time the *next* COA into the viewer pane.

        ``index`` is the row being moved **to**.
        """
        self.watcher.trim(time.time() - 0.5)
        t0 = time.time()
        self.page.keyboard.press("ArrowDown")
        return self.watcher.wait_for_paint(self._marker(index), t0, timeout)

    def clear_http_cache(self) -> None:
        """Both passes must be cold.

        ``/api/pdf/<lab_id>`` is served ``private, max-age=3600, immutable``
        (deliberately — see tests/test_pdf_delivery_chrome.py), so the second
        pass over the same samples would otherwise be served from the
        browser's disk cache and would not measure the app at all.
        """
        self.cdp.send("Network.clearBrowserCache")

    def reset_events(self) -> None:
        self.page.evaluate("() => { window.__benchEvents = []; }")

    def sse_counts(self) -> dict:
        try:
            return self.page.evaluate("() => Object.assign({}, window.__benchSse || {})")
        except Exception:
            return {}

    def settle(self, seconds: float = 4.0) -> None:
        """Let a debounced resync fire before anything is measured.

        ``handleSSE`` schedules ``restoreAllTabs()`` 1.5 s after an overflow,
        so a pull that ends in a shed queue rebuilds the list *after* the
        last preview lands.
        """
        self.page.wait_for_timeout(int(seconds * 1000))

    def collect_events(self) -> list[dict]:
        try:
            return self.page.evaluate("() => window.__benchEvents || []")
        except Exception:
            return []
