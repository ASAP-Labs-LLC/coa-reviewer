"""A loopback stand-in for QBench's report-preview endpoints.

This exists so a benchmark run can drive the **real** ``COASession``. Every
run before it swapped in ``bench.fakes.FakeCoaSession``, whose
``generate_preview`` returns an f-string instantly — which meant three things
that dominate a real pull had never been measured at all:

* ``COASession._session_lock`` is held across the preview POST **and** across
  every poll GET (app.py:739-745, 785-790). One lock, one ``requests.Session``,
  every worker. The sleeps between polls are outside it, so the lock's cost is
  exactly ``post_latency + n_polls * poll_latency`` per preview, serialised
  across the whole pool. With the endpoints faked away that cost was zero.
* ``_preview_poll_delays`` (app.py:260-275) yields 0.75, 1.0, 1.5, 2.0, 2.5
  then 3.0 repeating, and the loop *sleeps first*. So every preview has a hard
  ~0.75 s floor no matter how fast the server answers, and a render that
  finishes at t=4 s is not observed until t=5.25 s.
* whether the pull finishes inside ``SESSION_TIMEOUT_SECONDS``.

Three endpoints, matching what ``generate_preview`` actually calls:

1. ``POST /report/preview`` → ``{"id": <int>}``. Form-encoded; ``sample_id``
   and ``report_config_id`` are required, because a harness that accepted an
   empty body would let a broken request look healthy.
2. ``GET /report/preview/get?id=<n>`` → ``{"render_status": "PENDING"}`` until
   that preview's render duration has elapsed, then ``"SUCCESSFUL"``.
3. ``GET /report/preview?id=<n>`` → the PDF bytes. This is the URL
   ``generate_preview`` returns (app.py:815) and the one ``cache_pdf`` fetches
   afterwards, so it serves the *same* PyMuPDF document ``bench/synth.py``
   builds — masthead colour marker included, or paint detection in
   ``bench/browser.py`` would stop finding the COA on screen.

Threaded on purpose: the contention under measurement is app.py's single
session lock. A single-threaded fixture server would add contention of its own
and the run would be measuring the harness.

Binds 127.0.0.1 only. Nothing here talks to the network.

**Latency is the parameter, and it is deterministic.** ``jitter_factor`` is
seeded on ``(seed, kind, key)`` rather than drawn from a shared RNG, so the
latency a given sample sees does not depend on which worker thread got to it
first. Two runs with the same seed see the same latencies; without that, two
runs cannot be compared.
"""

from __future__ import annotations

import json
import random
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .synth import SyntheticLab


# ── deterministic jitter ─────────────────────────────────────────────────

def jitter_factor(seed: int, kind: str, key) -> float:
    """A deviate in [-1, 1], fixed by ``(seed, kind, key)`` alone.

    Not drawn from a shared generator: request order across a thread pool is
    not reproducible, so a shared RNG would hand a different latency to the
    same sample on every run and quietly make two runs incomparable.
    """
    return random.Random(f"{seed}|{kind}|{key}").uniform(-1.0, 1.0)


def jittered(base: float, jitter: float, *, seed: int, kind: str, key) -> float:
    """``base`` scaled by ±``jitter`` (a fraction), deterministically."""
    if not jitter:
        return base
    return base * (1.0 + jitter * jitter_factor(seed, kind, key))


def _nap(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


# ── the server ───────────────────────────────────────────────────────────

class _Preview:
    __slots__ = ("id", "sample_id", "lab_id", "ready_at", "render_s", "polls")

    def __init__(self, pid: int, sample_id: int, lab_id: str, render_s: float) -> None:
        self.id = pid
        self.sample_id = sample_id
        self.lab_id = lab_id
        self.render_s = render_s
        self.ready_at = time.monotonic() + render_s
        self.polls = 0


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer that does not shout about half-closed sockets.

    app.py probes the preview URL with ``stream=True`` and closes the response
    without reading the body (app.py:1734-1736), which resets the connection.
    socketserver's default ``handle_error`` prints a full traceback for that —
    two per sample, straight into the run's stderr, burying anything that
    actually went wrong.
    """

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError,
                            ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Quiet: N x (1 POST + several polls + 2 PDF fetches) would bury the run's
    # own output in access-log noise.
    def log_message(self, *args) -> None:      # noqa: D102
        pass

    # ── plumbing ─────────────────────────────────────────────────────────

    @property
    def fake(self) -> "QBenchFakeServer":
        return self.server.fake            # type: ignore[attr-defined]

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # app.py probes the preview URL with stream=True and closes it
            # immediately (app.py:1734-1736); a half-read body is normal.
            pass

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(json.dumps(payload).encode("utf-8"), "application/json", status)

    # ── endpoints ────────────────────────────────────────────────────────

    def do_POST(self) -> None:             # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path != "/report/preview":
            self._send(b"not found", "text/plain", 404)
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
        sample_id = (form.get("sample_id") or [""])[0]
        report_config_id = (form.get("report_config_id") or [""])[0]
        if not sample_id or not report_config_id:
            self._json({"error": "sample_id and report_config_id are required"}, 400)
            return

        fake = self.fake
        _nap(fake.post_delay_for(sample_id))
        try:
            preview = fake.create_preview(int(sample_id))
        except (KeyError, ValueError):
            self._json({"error": f"unknown sample {sample_id!r}"}, 404)
            return
        self._json({"id": preview.id})

    def do_GET(self) -> None:              # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        fake = self.fake

        if parsed.path == "/health":
            self._send(b"ok", "text/plain")
            return

        if parsed.path == "/report/preview/get":
            preview = fake.lookup((query.get("id") or [""])[0])
            if preview is None:
                self._json({"error": "unknown preview"}, 404)
                return
            _nap(fake.poll_delay_for(preview))
            done = time.monotonic() >= preview.ready_at
            self._json({"render_status": "SUCCESSFUL" if done else "PENDING",
                        "render_error": ""})
            return

        if parsed.path == "/report/preview":
            preview = fake.lookup((query.get("id") or [""])[0])
            if preview is None:
                self._send(b"unknown preview", "text/plain", 404)
                return
            fake.count("pdf")
            self._send(fake.pdf_for(preview), "application/pdf")
            return

        self._send(b"not found", "text/plain", 404)


class QBenchFakeServer:
    """QBench's preview endpoints, on loopback, with configurable latency.

    ``lab`` may be attached after construction: the parent process needs this
    server's port to start the app process, but only learns which lab-id
    prefix to build from that process's reply.
    """

    def __init__(
        self,
        lab: Optional[SyntheticLab] = None,
        *,
        post_ms: float = 300.0,
        poll_ms: float = 150.0,
        render_s: float = 4.0,
        jitter: float = 0.25,
        seed: int = 1,
        host: str = "127.0.0.1",
    ) -> None:
        self.post_ms = float(post_ms)
        self.poll_ms = float(poll_ms)
        self.render_s = float(render_s)
        self.jitter = float(jitter)
        self.seed = int(seed)
        self.host = host

        self.hits: dict = {}
        self._previews: dict[int, _Preview] = {}
        self._next_id = 1
        self._lock = threading.Lock()

        self._server = _QuietThreadingHTTPServer((host, 0), _Handler)
        self._server.fake = self          # type: ignore[attr-defined]
        self.port: int = self._server.server_address[1]
        self._thread: Optional[threading.Thread] = None
        self._lab: Optional[SyntheticLab] = None
        self._lab_by_sample: dict[int, str] = {}
        if lab is not None:
            self.lab = lab

    # ── lab ──────────────────────────────────────────────────────────────

    @property
    def lab(self) -> Optional[SyntheticLab]:
        return self._lab

    @lab.setter
    def lab(self, value: SyntheticLab) -> None:
        self._lab = value
        self._lab_by_sample = {int(s["id"]): s["lab_id"] for s in value.samples}

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    # ── what the handler asks for ────────────────────────────────────────

    def count(self, key: str) -> None:
        with self._lock:
            self.hits[key] = self.hits.get(key, 0) + 1

    def post_delay_for(self, sample_id) -> float:
        return jittered(self.post_ms / 1000.0, self.jitter,
                        seed=self.seed, kind="post", key=sample_id)

    def poll_delay_for(self, preview: _Preview) -> float:
        with self._lock:
            preview.polls += 1
            n = preview.polls
        return jittered(self.poll_ms / 1000.0, self.jitter,
                        seed=self.seed, kind="poll", key=f"{preview.id}:{n}")

    def create_preview(self, sample_id: int) -> _Preview:
        lab_id = self._lab_by_sample[int(sample_id)] if self._lab_by_sample else str(sample_id)
        render_s = max(0.0, jittered(self.render_s, self.jitter, seed=self.seed,
                                     kind="render", key=sample_id))
        with self._lock:
            pid = self._next_id
            self._next_id += 1
            preview = _Preview(pid, int(sample_id), lab_id, render_s)
            self._previews[pid] = preview
            self.hits["post"] = self.hits.get("post", 0) + 1
        return preview

    def lookup(self, raw_id) -> Optional[_Preview]:
        try:
            pid = int(raw_id)
        except (TypeError, ValueError):
            return None
        with self._lock:
            return self._previews.get(pid)

    def pdf_for(self, preview: _Preview) -> bytes:
        if self._lab is None:
            raise RuntimeError("no lab attached to the fake QBench server")
        return self._lab.coa_pdf(preview.lab_id)

    # ── the run's own tally ──────────────────────────────────────────────

    @property
    def previews_created(self) -> int:
        """How many previews the app actually asked QBench to render.

        Not the same as N: app.py retries a preview up to three times
        (app.py:1717), so a run whose count exceeds N had failures in it.
        """
        with self._lock:
            return len(self._previews)

    @property
    def polls_served(self) -> int:
        with self._lock:
            return sum(p.polls for p in self._previews.values())

    def stats(self) -> dict:
        return {
            "previews_created": self.previews_created,
            "polls_served": self.polls_served,
            "pdf_fetches": self.hits.get("pdf", 0),
            "post_ms": self.post_ms,
            "poll_ms": self.poll_ms,
            "render_s": self.render_s,
            "jitter": self.jitter,
            "seed": self.seed,
        }

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> "QBenchFakeServer":
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="qbench-fake", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self) -> "QBenchFakeServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
