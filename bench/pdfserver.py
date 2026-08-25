"""A loopback HTTP server that hands out the run's COA and SIF PDFs.

This is the seam that keeps real HTTP and real bytes inside the
measurement. ``COASession.generate_preview`` is faked to return a URL on
this server, and the attachment dicts point their ``url`` here too, which
between them satisfies every ``_requests.get`` call site in app.py —
``_sif_download``, ``cache_pdf``, ``get_pdf`` and ``download_pdf`` — with no
monkeypatching of ``requests`` at all.

Binds 127.0.0.1 only. Nothing here talks to the network.
"""

from __future__ import annotations

import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .synth import SyntheticLab


def _handler_for(counter: dict):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        # Quiet: one line per request would be N x 6 lines of noise.
        def log_message(self, *args) -> None:  # noqa: D102
            pass

        def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path
            lab = getattr(self.server, "lab", None)
            if lab is None:
                self._send(b"no lab attached yet", "text/plain", 503)
                return
            try:
                if path.startswith("/coa/") and path.endswith(".pdf"):
                    lab_id = path[len("/coa/"):-len(".pdf")]
                    counter["coa"] = counter.get("coa", 0) + 1
                    self._send(lab.coa_pdf(lab_id), "application/pdf")
                    return
                if path.startswith("/sif/") and path.endswith(".pdf"):
                    order_id = int(path[len("/sif/"):-len(".pdf")])
                    counter["sif"] = counter.get("sif", 0) + 1
                    self._send(lab.sif_pdf(order_id), "application/pdf")
                    return
                if path == "/health":
                    self._send(b"ok", "text/plain")
                    return
            except (KeyError, ValueError):
                self._send(b"unknown document", "text/plain", 404)
                return
            self._send(b"not found", "text/plain", 404)

    return Handler


class PdfFixtureServer:
    """Serves ``/coa/<lab_id>.pdf`` and ``/sif/<order_id>.pdf``."""

    def __init__(self, lab: Optional[SyntheticLab] = None, host: str = "127.0.0.1") -> None:
        self.hits: dict = {}
        self._server = ThreadingHTTPServer((host, 0), _handler_for(self.hits))
        # Settable after construction: the parent needs this server's PORT to
        # start the app process, but only learns the lab-id prefix (and so
        # which lab to build) from that process's reply.
        self._server.lab = lab
        self._server.daemon_threads = True
        self.port: int = self._server.server_address[1]
        self.host = host
        self._thread: Optional[threading.Thread] = None

    @property
    def lab(self) -> Optional[SyntheticLab]:
        return self._server.lab

    @lab.setter
    def lab(self, value: SyntheticLab) -> None:
        self._server.lab = value

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> "PdfFixtureServer":
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="pdf-fixture", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self) -> "PdfFixtureServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
