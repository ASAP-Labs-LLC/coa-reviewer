"""End-to-end: Flask routes → real LabCoreClient → stub LabCore over HTTP.

The route tests mock the client and the client tests stub the server; neither
can catch a mismatch *between* those layers — a route calling a client method
with the wrong argument shape still passes both. These join them, with only
LabCore itself replaced.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("flask")


class _StubLabCore:
    """Enough of LabCore to answer the Command Center calls, recording each."""

    def __init__(self):
        self.requests = []
        self.tasks = [
            {
                "id": 7, "type": "double_check", "status": "open",
                "initial_problem": "Potency reads low", "customer": "Acme",
                "department": "Lab", "created_by": "JD",
                "source_program": "LabVision", "date_created": "2026-07-30 09:00",
                "latest_update": "Re-ran", "latest_update_by": "JD",
                "samples": [{"lab_id": "073126-41552", "customer_name": "Acme",
                             "fuel_type": "Diesel"}],
            },
            {
                # Carries a sample on purpose: a maintenance listing with no
                # samples would be filtered out for the wrong reason, and the
                # type filter could rot undetected.
                "id": 8, "type": "maintenance", "status": "open",
                "initial_problem": "Calibrate GC",
                "samples": [{"lab_id": "073126-40000", "customer_name": "Acme",
                             "fuel_type": "Diesel"}],
            },
        ]
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def _json(self, payload, status=200):
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                p = urlparse(self.path)
                qs = parse_qs(p.query)
                outer.requests.append(("GET", p.path, qs, None))
                if p.path == "/api/queue/status":
                    return self._json({"depth": 0})
                if p.path == "/api/cc/tasks":
                    return self._json(outer.tasks)
                if p.path == "/api/cc/tasks/check-duplicate":
                    labs = qs.get("lab_id", [])
                    hits = [
                        {"id": t["id"], "type": t["type"], "status": t["status"],
                         "initial_problem": t["initial_problem"],
                         "customer": t.get("customer", "")}
                        for t in outer.tasks
                        if any(s["lab_id"] in labs for s in t.get("samples", []))
                    ]
                    return self._json({"conflict": bool(hits), "existing_tasks": hits})
                if p.path == "/api/cc/samples/search":
                    q = qs.get("q", [""])[0]
                    return self._json([
                        s for t in outer.tasks for s in t.get("samples", [])
                        if q in s["lab_id"]
                    ])
                if p.path == "/api/cc/customers":
                    return self._json(["Acme", "Globex"])
                self.send_error(404)

            def do_POST(self):
                p = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                outer.requests.append(("POST", p.path, {}, body))
                if p.path != "/api/queue/write":
                    return self.send_error(404)
                op, params = body.get("operation"), body.get("params", {})
                if op == "cc_create_task":
                    labs = [s.get("lab_id") for s in params.get("sample_ids", [])]
                    clash = [
                        {"id": t["id"], "type": t["type"], "status": t["status"],
                         "initial_problem": t["initial_problem"],
                         "customer": t.get("customer", "")}
                        for t in outer.tasks
                        if any(s["lab_id"] in labs for s in t.get("samples", []))
                    ]
                    if clash and not params.get("force_create"):
                        return self._json({"conflict": True, "existing_tasks": clash})
                    new_id = max(t["id"] for t in outer.tasks) + 1
                    outer.tasks.append({**params, "id": new_id,
                                        "samples": params.get("sample_ids", [])})
                    return self._json({"ok": True, "task_id": new_id})
                if op == "cc_complete_task":
                    if not str(params.get("completion_notes", "")).strip():
                        return self._json({"error": "Completion notes are required."})
                    for t in outer.tasks:
                        if t["id"] == params.get("task_id"):
                            t["status"] = "completed"
                    return self._json({"ok": True})
                return self._json({"error": f"unknown op {op}"})

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3)


@pytest.fixture
def wired(monkeypatch):
    """(client, stub_labcore, ustate) — real LabCoreClient against the stub."""
    import app as app_module
    from app import UserState
    from labcore_client import LabCoreClient

    stub = _StubLabCore()
    monkeypatch.setattr(app_module.state, "labcore",
                        LabCoreClient(base_url=f"http://127.0.0.1:{stub.port}"))

    uid = "test-uid-e2e"
    ustate = UserState(uid, "RC")
    with app_module._sessions_lock:
        app_module.user_sessions[uid] = ustate

    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = uid

    yield client, stub, ustate

    with app_module._sessions_lock:
        app_module.user_sessions.pop(uid, None)
    stub.close()


def test_config_reports_a_reachable_labcore(wired) -> None:
    client, _, _ = wired
    assert client.get("/api/cc/config").get_json()["available"] is True


def test_lookup_returns_autofill_and_the_existing_listing(wired) -> None:
    client, _, _ = wired
    data = client.get("/api/cc/lookup/073126-41552").get_json()

    assert data["customer_name"] == "Acme"
    assert data["fuel_type"] == "Diesel"
    assert data["conflict"] is True
    assert data["existing_tasks"][0]["id"] == 7


def test_creating_a_listing_for_a_fresh_sample_succeeds(wired) -> None:
    client, stub, _ = wired
    resp = client.post("/api/cc/tasks", json={
        "initial_problem": "Water content high",
        "sample_ids": [{"lab_id": "073126-99999"}],
        "status": "urgent", "department": "Lab",
    })

    data = resp.get_json()
    assert data["ok"] is True
    created = [t for t in stub.tasks if t["id"] == data["task_id"]][0]
    assert created["initial_problem"] == "Water content high"
    assert created["status"] == "urgent"
    assert created["department"] == "Lab"
    assert created["created_by"] == "RC"
    assert created["source_program"] == "COAReviewer"


def test_a_duplicate_sample_conflicts_then_force_create_wins(wired) -> None:
    """The exact add/create-anyway path the conflict modal drives."""
    client, stub, _ = wired
    body = {"initial_problem": "Second opinion",
            "sample_ids": [{"lab_id": "073126-41552"}]}

    first = client.post("/api/cc/tasks", json=body).get_json()
    assert first["conflict"] is True
    assert first["existing_tasks"][0]["id"] == 7

    second = client.post("/api/cc/tasks",
                         json={**body, "force_create": True}).get_json()
    assert second["ok"] is True


def test_completing_a_listing_closes_it_on_the_board(wired) -> None:
    client, stub, _ = wired
    resp = client.post("/api/cc/tasks/7/complete",
                       json={"notes": "Re-ran, original result confirmed"})

    assert resp.get_json()["ok"] is True
    assert [t for t in stub.tasks if t["id"] == 7][0]["status"] == "completed"


def test_blank_completion_notes_never_reach_labcore(wired) -> None:
    client, stub, _ = wired
    before = len(stub.requests)

    assert client.post("/api/cc/tasks/7/complete", json={"notes": ""}).status_code == 400
    assert len(stub.requests) == before, "should not have called LabCore at all"


def test_re_review_pulls_the_double_check_listing_only(wired) -> None:
    """The maintenance listing on the same board must not enter the queue."""
    import app as app_module
    _, stub, _ = wired

    entries = app_module.cc_tasks_to_re_review_entries(
        app_module.state.labcore.active_tasks()
    )

    assert [e["lab_id"] for e in entries] == ["073126-41552"]
    assert entries[0]["task"]["id"] == 7
    assert entries[0]["task"]["source_program"] == "LabVision"


def test_customers_reach_the_form(wired) -> None:
    client, _, _ = wired
    assert client.get("/api/cc/customers").get_json() == ["Acme", "Globex"]


def test_everything_fails_loudly_once_labcore_goes_away(wired) -> None:
    """Not a silent success: the reviewer has to know the flag did not file."""
    client, stub, _ = wired
    stub.close()

    resp = client.post("/api/cc/tasks", json={"initial_problem": "x"})
    assert resp.status_code == 503
    assert resp.get_json()["labcore_down"] is True
