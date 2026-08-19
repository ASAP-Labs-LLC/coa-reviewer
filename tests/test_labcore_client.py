"""Tests for the LabCore / Command Center HTTP client.

Runs against a real stub HTTP server on a real socket rather than mocking
``requests``. Mocking the transport here would test our own mock: the things
worth pinning down are the exact wire shapes LabCore expects — the
``/api/queue/write`` envelope, the ``cc_create_task`` conflict response, the
required ``completion_notes`` — and a mock would happily accept a wrong one.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from labcore_client import LabCoreClient, LabCoreUnavailable
from tests.conftest import free_port


class _StubLabCore:
    """Minimal stand-in for LabCore's HTTP API.

    ``routes`` maps "METHOD /path" to either a dict (sent as JSON 200) or a
    (status, dict) tuple. Every request is recorded on ``.requests``.
    """

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):  # silence stderr noise
                pass

            def _respond(self, method):
                parsed = urlparse(self.path)
                body = None
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    body = json.loads(self.rfile.read(length))
                outer.requests.append({
                    "method": method,
                    "path": parsed.path,
                    "query": parse_qs(parsed.query),
                    "body": body,
                })
                entry = outer.routes.get(f"{method} {parsed.path}")
                if entry is None:
                    self.send_error(404)
                    return
                status, payload = entry if isinstance(entry, tuple) else (200, entry)
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._respond("GET")

            def do_POST(self):
                self._respond("POST")

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def last(self):
        return self.requests[-1]


@pytest.fixture
def stub():
    servers = []

    def _make(routes=None):
        s = _StubLabCore(routes)
        servers.append(s)
        return s

    yield _make
    for s in servers:
        s.close()


def _client(stub_server):
    return LabCoreClient(base_url=f"http://127.0.0.1:{stub_server.port}")


# ── base url ─────────────────────────────────────────────────────────────
#
# LabCore is reached at https://labvision.asaplabs.net (behind Cloudflare on
# 443), not on a LAN host:port. The client therefore takes a whole URL —
# scheme included — rather than composing http://{host}:{port}, which cannot
# express HTTPS and would have produced a wrong URL for every real call.

def test_accepts_an_https_base_url() -> None:
    client = LabCoreClient(base_url="https://labvision.asaplabs.net")
    assert client.base_url == "https://labvision.asaplabs.net"


def test_strips_a_trailing_slash_so_paths_do_not_double_up() -> None:
    """Otherwise every request goes to //api/... which the proxy 404s."""
    client = LabCoreClient(base_url="https://labvision.asaplabs.net/")
    assert client.base_url == "https://labvision.asaplabs.net"


def test_still_supports_an_explicit_host_and_port() -> None:
    """A LAN LabCore on 8080 has to keep working — e.g. a local instance
    during development, or a fallback if the tunnel is down."""
    assert LabCoreClient(base_url="http://127.0.0.1:8080").base_url == \
        "http://127.0.0.1:8080"


def test_assumes_https_when_the_scheme_is_omitted() -> None:
    """A bare hostname in config must not silently become a plaintext call to
    a service that only answers on 443."""
    assert LabCoreClient(base_url="labvision.asaplabs.net").base_url == \
        "https://labvision.asaplabs.net"


# ── create_task ──────────────────────────────────────────────────────────

def test_create_task_posts_the_queue_write_envelope(stub) -> None:
    """LabCore's gateway expects {operation, params, source} — not a bare
    listing body. Getting this shape wrong is a silent 400."""
    srv = stub({"POST /api/queue/write": {"ok": True, "task_id": 12}})
    result = _client(srv).create_task({
        "type": "double_check",
        "initial_problem": "Potency reads low",
        "sample_ids": [{"lab_id": "073126-41552"}],
    })

    assert result == {"ok": True, "task_id": 12}
    sent = srv.last()
    assert sent["path"] == "/api/queue/write"
    assert sent["body"]["operation"] == "cc_create_task"
    assert sent["body"]["source"] == "COAReviewer"
    assert sent["body"]["params"]["initial_problem"] == "Potency reads low"
    assert sent["body"]["params"]["sample_ids"] == [{"lab_id": "073126-41552"}]


def test_create_task_sends_an_op_id_so_retries_cannot_duplicate(stub) -> None:
    """LabCore short-circuits a repeated op_id to the recorded result. Without
    one, a retried request creates a second listing for the same sample."""
    srv = stub({"POST /api/queue/write": {"ok": True, "task_id": 1}})
    _client(srv).create_task({"initial_problem": "x"})
    assert srv.last()["body"].get("op_id"), "create_task must send an op_id"


def test_create_task_uses_the_caller_supplied_op_id_when_given(stub) -> None:
    srv = stub({"POST /api/queue/write": {"ok": True, "task_id": 1}})
    _client(srv).create_task({"initial_problem": "x"}, op_id="fixed-123")
    assert srv.last()["body"]["op_id"] == "fixed-123"


def test_create_task_returns_a_conflict_response_untouched(stub) -> None:
    """The route layer needs the existing listings to offer add/create/cancel,
    so the client must not swallow or reshape a conflict."""
    conflict = {
        "conflict": True,
        "existing_tasks": [
            {"id": 7, "type": "double_check", "status": "open",
             "initial_problem": "already flagged", "customer": "Acme"},
        ],
    }
    srv = stub({"POST /api/queue/write": conflict})
    assert _client(srv).create_task({"initial_problem": "again"}) == conflict


def test_create_task_raises_when_labcore_is_unreachable() -> None:
    """Flagging a bad sample must fail loudly — never look like it worked."""
    client = LabCoreClient(base_url=f"http://127.0.0.1:{free_port()}")
    with pytest.raises(LabCoreUnavailable):
        client.create_task({"initial_problem": "x"})


# ── complete_task ────────────────────────────────────────────────────────

def test_complete_task_sends_task_id_notes_and_author(stub) -> None:
    """LabCore rejects a completion with empty completion_notes."""
    srv = stub({"POST /api/queue/write": {"ok": True}})
    _client(srv).complete_task(7, notes="Re-ran, result confirmed", completed_by="RC")

    params = srv.last()["body"]["params"]
    assert srv.last()["body"]["operation"] == "cc_complete_task"
    assert params["task_id"] == 7
    assert params["completion_notes"] == "Re-ran, result confirmed"
    assert params["completed_by"] == "RC"


def test_complete_task_rejects_blank_notes_before_the_network(stub) -> None:
    """No point round-tripping a request LabCore will refuse."""
    srv = stub({"POST /api/queue/write": {"ok": True}})
    with pytest.raises(ValueError):
        _client(srv).complete_task(7, notes="   ", completed_by="RC")
    assert srv.requests == []


# ── active_tasks ─────────────────────────────────────────────────────────

def test_active_tasks_requests_the_active_view(stub) -> None:
    srv = stub({"GET /api/cc/tasks": [{"id": 1, "type": "double_check"}]})
    tasks = _client(srv).active_tasks()

    assert tasks == [{"id": 1, "type": "double_check"}]
    assert srv.last()["query"]["view"] == ["active"]


def test_active_tasks_raises_when_labcore_is_unreachable() -> None:
    client = LabCoreClient(base_url=f"http://127.0.0.1:{free_port()}")
    with pytest.raises(LabCoreUnavailable):
        client.active_tasks()


# ── check_duplicate ──────────────────────────────────────────────────────

def test_check_duplicate_passes_every_lab_id(stub) -> None:
    srv = stub({"GET /api/cc/tasks/check-duplicate":
                {"conflict": True, "existing_tasks": [{"id": 3}]}})
    result = _client(srv).check_duplicate(["073126-41552", "073126-41553"])

    assert result["conflict"] is True
    assert srv.last()["query"]["lab_id"] == ["073126-41552", "073126-41553"]


def test_check_duplicate_short_circuits_on_no_lab_ids(stub) -> None:
    srv = stub({})
    assert _client(srv).check_duplicate([]) == {"conflict": False, "existing_tasks": []}
    assert srv.requests == []


# ── sample_info ──────────────────────────────────────────────────────────

def test_sample_info_returns_the_matching_sample(stub) -> None:
    srv = stub({"GET /api/cc/samples/search": [
        {"lab_id": "073126-41552", "customer_name": "Acme", "fuel_type": "Diesel"},
    ]})
    info = _client(srv).sample_info("073126-41552")

    assert info["customer_name"] == "Acme"
    assert info["fuel_type"] == "Diesel"
    assert srv.last()["query"]["q"] == ["073126-41552"]


def test_sample_info_prefers_an_exact_lab_id_match(stub) -> None:
    """A LIKE search on 41552 also returns 415520; autofilling the wrong
    customer onto a listing would be worse than autofilling nothing."""
    srv = stub({"GET /api/cc/samples/search": [
        {"lab_id": "073126-415520", "customer_name": "Wrong", "fuel_type": "X"},
        {"lab_id": "073126-41552", "customer_name": "Right", "fuel_type": "Diesel"},
    ]})
    assert _client(srv).sample_info("073126-41552")["customer_name"] == "Right"


def test_sample_info_returns_empty_when_labcore_does_not_know_the_sample(stub) -> None:
    srv = stub({"GET /api/cc/samples/search": []})
    assert _client(srv).sample_info("073126-99999") == {}


# ── customers ────────────────────────────────────────────────────────────

def test_customers_returns_the_name_list(stub) -> None:
    srv = stub({"GET /api/cc/customers": ["Acme", "Globex"]})
    assert _client(srv).customers() == ["Acme", "Globex"]


# ── is_available ─────────────────────────────────────────────────────────

def test_is_available_true_when_labcore_answers(stub) -> None:
    srv = stub({"GET /api/queue/status": {"depth": 0}})
    assert _client(srv).is_available() is True


def test_is_available_false_when_nothing_is_there() -> None:
    """A probe must report, never raise — it drives a UI banner."""
    assert LabCoreClient(base_url=f"http://127.0.0.1:{free_port()}").is_available() is False
