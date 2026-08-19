"""Regression test: PATCH /api/sample-info/<lab_id> must send fields at
the same location they live in QBench's sample object — NOT blindly nest
under custom_fields.

Evidence (from production app.log, 2026-05-20):
    PATCH /samples for lab_id=030526-31004 sid=33383
      payload={'custom_fields': {'po_number': '12345678'}}
    QBench accepted update for lab_id=030526-31004 response=
      {... 'po_number': None, ... 'fw': None, 'work_order': None,
       'tank': 'E790422258', 'fuel_type': 'Diesel #2', ...}

QBench returned 200 OK but the top-level `po_number` stayed None — because
PO is a top-level QBench sample field, and writes nested under
`custom_fields.po_number` are silently dropped. The same applies to fw,
work_order, fuel_type, tank, generator, package_size, etc. — every key
the production QBench response shows at the top level must be sent at
the top level on PATCH.

These tests pin the correct field locations per the QBench response above.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")


# Fields confirmed top-level in the production QBench response. If a new
# editable field is added, decide its location by inspecting the GET
# response, not by guessing.
TOP_LEVEL_PER_QBENCH = [
    "po_number", "fw", "work_order", "fuel_type", "package_size",
    "time_of_collection", "customer_sample_id", "sample_taken_from",
    "tank", "generator", "component_model", "tank_capacity",
    "point_of_collection", "quantity_tank", "site_location", "Rush",
]


@pytest.fixture
def patch_client(monkeypatch):
    import app as app_module
    from app import UserState

    mock_api = MagicMock()
    mock_api.update_sample.return_value = {"data": [{"id": 33383}]}
    mock_api.fetch_samples_by_lab_id.return_value = [
        {"id": 33383, "lab_id": "030526-31004"}
    ]
    monkeypatch.setattr(app_module.state, "api_client", mock_api)
    monkeypatch.setattr(app_module.state, "logged_in", True)

    test_uid = "uid-field-location"
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


def test_po_number_is_sent_at_top_level_not_in_custom_fields(patch_client):
    """The original reported bug: editing PO appeared to save (green) but
    QBench's `po_number` stayed null because the payload nested it under
    `custom_fields`."""
    client, mock_api = patch_client
    resp = client.patch(
        "/api/sample-info/030526-31004", json={"po_number": "12345678"}
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    sid, payload = mock_api.update_sample.call_args.args
    assert payload.get("po_number") == "12345678", (
        f"po_number must be sent at the top level (it is a top-level QBench "
        f"sample field, not custom). Got payload: {payload!r}"
    )
    assert "po_number" not in (payload.get("custom_fields") or {}), (
        "po_number must NOT be nested under custom_fields — QBench silently "
        "drops the value when it's mis-nested"
    )


@pytest.mark.parametrize("field", TOP_LEVEL_PER_QBENCH)
def test_known_top_level_fields_sent_at_top_level(patch_client, field):
    """Every editable field that QBench exposes at the top of its sample
    object must be PATCHed at the top level — not nested under
    custom_fields, where QBench silently drops them."""
    client, mock_api = patch_client
    resp = client.patch(
        "/api/sample-info/030526-31004", json={field: "test-value"}
    )
    assert resp.status_code == 200
    _, payload = mock_api.update_sample.call_args.args
    assert payload.get(field) == "test-value", (
        f"{field!r} must be at the top level of the QBench payload. "
        f"Got: {payload!r}"
    )
    assert field not in (payload.get("custom_fields") or {}), (
        f"{field!r} must not be nested under custom_fields"
    )
