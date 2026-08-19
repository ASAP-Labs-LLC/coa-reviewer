"""Flask routes that proxy Command Center to the browser.

They exist because LabCore serves no CORS headers and has no OPTIONS handler,
so a page served from :5559 cannot call LabCore on :8080 directly. Everything
goes through this server.

The LabCoreClient itself is exercised against a real stub HTTP server in
tests/test_labcore_client.py; here it is mocked so these tests are about route
behavior — auth, defaults, stamping, failure codes.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")


@pytest.fixture
def cc_client(monkeypatch):
    """(test_client, mock_labcore) with an authenticated portal session."""
    import app as app_module
    from app import UserState

    mock = MagicMock()
    mock.base_url = "https://labvision.asaplabs.net"
    mock.is_available.return_value = True
    mock.sample_info.return_value = {}
    mock.check_duplicate.return_value = {"conflict": False, "existing_tasks": []}
    mock.create_task.return_value = {"ok": True, "task_id": 42}
    mock.complete_task.return_value = {"ok": True}
    mock.customers.return_value = []
    monkeypatch.setattr(app_module.state, "labcore", mock)

    uid = "test-uid-cc"
    with app_module._sessions_lock:
        app_module.user_sessions[uid] = UserState(uid, "RC")

    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = uid

    yield client, mock

    with app_module._sessions_lock:
        app_module.user_sessions.pop(uid, None)


@pytest.fixture
def anon_client():
    """A client with no portal session."""
    import app as app_module
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


# ── /api/cc/config ───────────────────────────────────────────────────────

def test_labvision_url_is_the_configured_labcore_url(cc_client) -> None:
    """LabVision is served by LabCore itself, at a fixed public hostname
    (https://labvision.asaplabs.net behind Cloudflare). So the browser link is
    simply that URL.

    An earlier version derived it from request.host, which was correct only
    while LabCore ran on the COA Reviewer machine. Against a public hostname
    that logic produces http://<coa-reviewer-host>:8080 — a URL that does not
    exist — so the button would open a dead page for every reviewer.
    """
    client, mock = cc_client
    mock.base_url = "https://labvision.asaplabs.net"

    assert client.get("/api/cc/config").get_json()["lab_vision_url"] == \
        "https://labvision.asaplabs.net"


def test_labvision_url_ignores_the_host_the_reviewer_reached_us_on(cc_client) -> None:
    import app as app_module

    app_module.state.labcore.base_url = "https://labvision.asaplabs.net"
    with app_module.app.test_request_context("/", base_url="http://labpc:5559"):
        url = app_module._lab_vision_base_url()

    assert url == "https://labvision.asaplabs.net"
    assert "labpc" not in url


def test_config_reports_labcore_availability(cc_client) -> None:
    client, mock = cc_client
    mock.is_available.return_value = False
    assert client.get("/api/cc/config").get_json()["available"] is False


def test_config_requires_a_portal_session(anon_client) -> None:
    assert anon_client.get("/api/cc/config").status_code == 401


# ── /api/cc/lookup/<lab_id> ──────────────────────────────────────────────

def test_lookup_merges_autofill_and_existing_listings(cc_client) -> None:
    """One round trip so the flag modal can open already populated."""
    client, mock = cc_client
    mock.sample_info.return_value = {"customer_name": "Acme", "fuel_type": "Diesel"}
    mock.check_duplicate.return_value = {
        "conflict": True, "existing_tasks": [{"id": 7, "status": "open"}],
    }

    data = client.get("/api/cc/lookup/073126-41552").get_json()

    assert data["customer_name"] == "Acme"
    assert data["fuel_type"] == "Diesel"
    assert data["conflict"] is True
    assert data["existing_tasks"] == [{"id": 7, "status": "open"}]
    mock.sample_info.assert_called_once_with("073126-41552")


def test_lookup_returns_503_when_labcore_is_down(cc_client) -> None:
    from labcore_client import LabCoreUnavailable
    client, mock = cc_client
    mock.sample_info.side_effect = LabCoreUnavailable("nope")

    resp = client.get("/api/cc/lookup/073126-41552")
    assert resp.status_code == 503
    assert resp.get_json()["labcore_down"] is True


# ── POST /api/cc/tasks ───────────────────────────────────────────────────

def test_create_stamps_the_reviewer_and_source_program(cc_client) -> None:
    """source_program is how a listing is attributable to COA Reviewer."""
    client, mock = cc_client
    client.post("/api/cc/tasks", json={
        "initial_problem": "Potency reads low",
        "sample_ids": [{"lab_id": "073126-41552"}],
    })

    params = mock.create_task.call_args.args[0]
    assert params["created_by"] == "RC"
    assert params["source_program"] == "COAReviewer"
    assert params["initial_problem"] == "Potency reads low"


def test_create_defaults_type_to_double_check_and_status_to_open(cc_client) -> None:
    client, mock = cc_client
    client.post("/api/cc/tasks", json={"initial_problem": "x"})

    params = mock.create_task.call_args.args[0]
    assert params["type"] == "double_check"
    assert params["status"] == "open"


def test_create_forwards_the_full_listing_form(cc_client) -> None:
    client, mock = cc_client
    client.post("/api/cc/tasks", json={
        "initial_problem": "x", "type": "customer_clarification",
        "customer": "Acme", "context": "background", "status": "urgent",
        "department": "Lab", "sample_ids": [{"lab_id": "073126-41552"}],
    })

    params = mock.create_task.call_args.args[0]
    assert params["type"] == "customer_clarification"
    assert params["customer"] == "Acme"
    assert params["context"] == "background"
    assert params["status"] == "urgent"
    assert params["department"] == "Lab"


def test_create_rejects_a_blank_problem(cc_client) -> None:
    client, mock = cc_client
    resp = client.post("/api/cc/tasks", json={"initial_problem": "   "})

    assert resp.status_code == 400
    assert mock.create_task.call_count == 0


def test_create_passes_conflict_through_untouched(cc_client) -> None:
    """The modal needs the existing listings to offer add/create-anyway/cancel."""
    client, mock = cc_client
    mock.create_task.return_value = {
        "conflict": True, "existing_tasks": [{"id": 3}],
    }
    data = client.post("/api/cc/tasks", json={"initial_problem": "x"}).get_json()

    assert data["conflict"] is True
    assert data["existing_tasks"] == [{"id": 3}]


def test_create_forwards_force_create_to_override_the_dedup(cc_client) -> None:
    client, mock = cc_client
    client.post("/api/cc/tasks", json={"initial_problem": "x", "force_create": True})
    assert mock.create_task.call_args.args[0]["force_create"] is True


def test_create_fails_loudly_when_labcore_is_down(cc_client) -> None:
    """A flag that silently fails to file is worse than one that visibly
    refuses — the reviewer would move on believing it was recorded."""
    from labcore_client import LabCoreUnavailable
    client, mock = cc_client
    mock.create_task.side_effect = LabCoreUnavailable("down")

    resp = client.post("/api/cc/tasks", json={"initial_problem": "x"})
    assert resp.status_code == 503
    assert resp.get_json()["labcore_down"] is True


# ── POST /api/cc/tasks/<id>/complete ─────────────────────────────────────

def test_complete_sends_notes_and_the_reviewer_initials(cc_client) -> None:
    client, mock = cc_client
    client.post("/api/cc/tasks/7/complete", json={"notes": "Re-ran, confirmed"})

    kwargs = mock.complete_task.call_args.kwargs
    assert mock.complete_task.call_args.args[0] == 7
    assert kwargs["notes"] == "Re-ran, confirmed"
    assert kwargs["completed_by"] == "RC"


def test_complete_rejects_blank_notes(cc_client) -> None:
    """LabCore requires completion notes; catch it before the round trip."""
    client, mock = cc_client
    resp = client.post("/api/cc/tasks/7/complete", json={"notes": "  "})

    assert resp.status_code == 400
    assert mock.complete_task.call_count == 0


# ── /api/cc/customers ────────────────────────────────────────────────────

def test_customers_passes_the_list_through(cc_client) -> None:
    client, mock = cc_client
    mock.customers.return_value = ["Acme", "Globex"]
    assert client.get("/api/cc/customers").get_json() == ["Acme", "Globex"]


# ── /api/cc/check/<lab_id> — the hot path ────────────────────────────────
# Measured against live LabCore on 2026-07-31: check_duplicate ~150ms,
# sample_info ~173ms. /api/cc/lookup ran both sequentially (~272ms) and
# markGood() awaited it on EVERY Good mark before advancing. Marking a queue
# of samples felt sluggish for no reason: resolving a listing needs only the
# duplicate check — the customer/fuel autofill is for the flag form.

def test_check_asks_only_for_duplicates(cc_client) -> None:
    client, mock = cc_client
    mock.check_duplicate.return_value = {
        "conflict": True, "existing_tasks": [{"id": 7, "status": "open"}],
    }

    data = client.get("/api/cc/check/073126-41552").get_json()

    assert data["conflict"] is True
    assert data["existing_tasks"] == [{"id": 7, "status": "open"}]
    mock.check_duplicate.assert_called_once_with(["073126-41552"])
    assert mock.sample_info.call_count == 0, (
        "the autofill lookup is not needed to resolve a listing — it doubles "
        "the latency of every Good mark"
    )


def test_check_returns_503_when_labcore_is_down(cc_client) -> None:
    from labcore_client import LabCoreUnavailable
    client, mock = cc_client
    mock.check_duplicate.side_effect = LabCoreUnavailable("down")

    resp = client.get("/api/cc/check/073126-41552")
    assert resp.status_code == 503
    assert resp.get_json()["labcore_down"] is True


def test_lookup_runs_its_two_calls_concurrently(cc_client) -> None:
    """The flag modal still needs both, but they are independent — running
    them in sequence made opening the modal cost the sum of both."""
    import time
    client, mock = cc_client
    mock.sample_info.side_effect = lambda *_a: (time.sleep(0.25) or {})
    mock.check_duplicate.side_effect = lambda *_a: (
        time.sleep(0.25) or {"conflict": False, "existing_tasks": []})

    started = time.perf_counter()
    assert client.get("/api/cc/lookup/073126-41552").status_code == 200
    elapsed = time.perf_counter() - started

    assert elapsed < 0.40, (
        f"lookup took {elapsed:.2f}s — the two calls are still sequential"
    )
