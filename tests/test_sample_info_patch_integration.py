"""Integration test: PATCH /api/sample-info/<lab_id> actually forwards
edits to QBench in the right shape.

The user reported that sample-info edits "aren't reaching QBench." That
could be the JS save path, the Flask route, or the qbench_client call —
but the path I can test in pytest is the route → update_sample handoff,
with a mocked api_client capturing the call args.

We assert:
  · Custom fields (e.g. fw, fuel_type) get nested under `custom_fields`.
  · Top-level fields (comments, source, tags, etc.) stay at the root.
  · The response body echoes QBench's response so DevTools can verify.
  · Unknown / non-whitelisted keys are silently dropped (400 if all dropped).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")  # skip whole module on bare-bones Python


@pytest.fixture
def app_with_mocked_qbench(monkeypatch, tmp_path):
    """Spin up the Flask app with a mocked api_client + an authenticated
    portal session. The yielded tuple is (test_client, mock_api)."""
    import app as app_module
    from app import UserState

    # Mock api_client so we can inspect what update_sample was called with.
    mock_api = MagicMock()
    mock_api.update_sample.return_value = {"data": [{"id": 999, "comments": "echo"}]}
    mock_api.fetch_sample.return_value = {"id": 999, "lab_id": "TEST"}
    mock_api.fetch_samples_by_lab_id.return_value = [{"id": 999, "lab_id": "TEST"}]
    monkeypatch.setattr(app_module.state, "api_client", mock_api)
    monkeypatch.setattr(app_module.state, "logged_in", True)

    # Install a UserState so @require_portal sees an active session.
    test_uid = "test-uid-integration"
    test_ustate = UserState(test_uid, "TestUser")
    with app_module._sessions_lock:
        app_module.user_sessions[test_uid] = test_ustate

    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = test_uid

    yield client, mock_api

    with app_module._sessions_lock:
        app_module.user_sessions.pop(test_uid, None)


def test_patch_fw_is_sent_at_top_level(app_with_mocked_qbench):
    """`fw` is a top-level QBench sample field (confirmed by inspecting a
    real QBench sample response 2026-05-20 — see
    tests/test_sample_info_field_locations.py). It must be PATCHed at the
    top level, not nested under custom_fields, or QBench silently drops it.

    This test originally asserted the opposite (that fw should nest under
    custom_fields). That assumption was wrong and produced the "edit goes
    green but doesn't save" production bug on 2026-05-20."""
    client, mock_api = app_with_mocked_qbench
    resp = client.patch("/api/sample-info/TEST", json={"fw": "FW-12345"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert mock_api.update_sample.call_count == 1
    sid, payload = mock_api.update_sample.call_args.args
    assert sid == 999
    assert payload == {"fw": "FW-12345"}, (
        f"fw must be sent at the top level — it is a top-level QBench "
        f"field, not custom. Got: {payload!r}"
    )


def test_patch_top_level_field_stays_flat(app_with_mocked_qbench):
    """`comments` is a standard top-level QBench sample field — no nesting."""
    client, mock_api = app_with_mocked_qbench
    resp = client.patch("/api/sample-info/TEST", json={"comments": "Looks good"})
    assert resp.status_code == 200
    sid, payload = mock_api.update_sample.call_args.args
    assert payload == {"comments": "Looks good"}, (
        f"top-level field must stay flat, got: {payload!r}"
    )


def test_patch_mixed_top_level_fields_stay_flat(app_with_mocked_qbench):
    """A single PATCH with multiple fields — all top-level per QBench's
    response — must produce one update_sample call with every field at
    the root, none nested under custom_fields."""
    client, mock_api = app_with_mocked_qbench
    resp = client.patch(
        "/api/sample-info/TEST",
        json={"comments": "ok", "fuel_type": "Diesel", "work_order": "WO-77"},
    )
    assert resp.status_code == 200
    sid, payload = mock_api.update_sample.call_args.args
    assert payload.get("comments") == "ok"
    assert payload.get("fuel_type") == "Diesel"
    assert payload.get("work_order") == "WO-77"
    assert "custom_fields" not in payload, (
        "none of these are custom fields per QBench's response; "
        f"got: {payload!r}"
    )


def test_patch_unknown_field_returns_400(app_with_mocked_qbench):
    """If nothing in the body matches the whitelist, the route refuses
    rather than firing a no-op PATCH to QBench."""
    client, mock_api = app_with_mocked_qbench
    resp = client.patch("/api/sample-info/TEST", json={"not_a_real_field": "x"})
    assert resp.status_code == 400
    assert mock_api.update_sample.call_count == 0


def test_patch_response_includes_qbench_result(app_with_mocked_qbench):
    """For diagnostics: the response must include what QBench actually
    returned so the user / DevTools can verify the change was accepted."""
    client, mock_api = app_with_mocked_qbench
    resp = client.patch("/api/sample-info/TEST", json={"comments": "x"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body.get("ok") is True
    # Either `qbench_response` or `result` — accept either naming
    qb_echo = body.get("qbench_response") or body.get("result")
    assert qb_echo is not None, (
        "PATCH response must echo QBench's response body so the user "
        "can verify what was accepted. Got: " + str(body)
    )
