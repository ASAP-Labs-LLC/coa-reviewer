"""Syncing LabVision sample information into QBench.

Sample-information fields only — deliberately not test results. The two
systems name the same field differently often enough that the reviewer gets
to re-pair them by hand, so the auto-pairing here only has to be predictable,
never clever: it must never invent a pairing that silently writes a value
into the wrong QBench field.

Overwrite rule: a QBench field that is already populated is reported as a
clash and left alone unless the reviewer explicitly opts in. Blank fields
fill automatically.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app import normalize_field_name, pair_sample_fields

pytest.importorskip("flask")


# ── name normalisation ───────────────────────────────────────────────────

@pytest.mark.parametrize("a,b", [
    ("fuel_type", "fuel type"),
    ("Fuel_Type", "fueltype"),
    ("work_order", "Work Order"),
    ("po_number", "PO-Number"),
    ("site_location", "Site  Location"),
])
def test_names_that_differ_only_in_case_or_punctuation_normalise_together(a, b) -> None:
    assert normalize_field_name(a) == normalize_field_name(b)


@pytest.mark.parametrize("a,b", [
    ("tank_number", "tank"),
    ("sample_from", "sample_taken_from"),
    ("collection_date", "time_of_collection"),
])
def test_genuinely_different_names_do_not_normalise_together(a, b) -> None:
    """These are the real LabVision/QBench pairs that a reviewer has to make
    by hand. Auto-pairing them would write to the wrong field."""
    assert normalize_field_name(a) != normalize_field_name(b)


# ── pairing ──────────────────────────────────────────────────────────────

QB_FIELDS = {"fuel_type", "work_order", "po_number", "tank_capacity",
             "site_location", "sample_taken_from", "tank", "time_of_collection"}


def test_identical_names_pair_exactly() -> None:
    pairs = pair_sample_fields({"fuel_type": "Diesel #1"}, QB_FIELDS, {})
    assert pairs[0]["source"] == "fuel_type"
    assert pairs[0]["target"] == "fuel_type"
    assert pairs[0]["match"] == "exact"


def test_names_differing_by_case_or_punctuation_pair_as_normalised() -> None:
    pairs = pair_sample_fields({"Work Order": "03397832"}, QB_FIELDS, {})
    assert pairs[0]["target"] == "work_order"
    assert pairs[0]["match"] == "normalized"


def test_an_unmatched_source_is_offered_unpaired() -> None:
    """It still appears so the reviewer can drag it onto a target — it just
    has no destination guessed for it."""
    pairs = pair_sample_fields({"tank_number": "T-4"}, QB_FIELDS, {})
    assert pairs[0]["source"] == "tank_number"
    assert pairs[0]["target"] is None
    assert pairs[0]["match"] is None


def test_empty_source_values_are_skipped() -> None:
    """Syncing a blank over a populated QBench field would destroy data for
    no reason."""
    pairs = pair_sample_fields({"fuel_type": "", "po_number": "   "}, QB_FIELDS, {})
    assert pairs == []


def test_a_field_qbench_cannot_accept_is_not_offered() -> None:
    """customer_name is read-only in QBench; showing it would promise a sync
    that silently does nothing."""
    pairs = pair_sample_fields({"customer_name": "Acme"}, QB_FIELDS, {})
    assert pairs == []


# ── clash detection ──────────────────────────────────────────────────────

def test_a_blank_qbench_field_is_marked_ready_to_fill() -> None:
    pairs = pair_sample_fields({"fuel_type": "Diesel #1"}, QB_FIELDS,
                               {"fuel_type": ""})
    assert pairs[0]["clash"] is False
    assert pairs[0]["current"] == ""


def test_a_differing_qbench_value_is_flagged_as_a_clash(monkeypatch) -> None:
    """The reviewer sees old -> new and decides; nothing is written silently."""
    pairs = pair_sample_fields({"fuel_type": "Diesel #1"}, QB_FIELDS,
                               {"fuel_type": "Diesel #2"})
    assert pairs[0]["clash"] is True
    assert pairs[0]["current"] == "Diesel #2"
    assert pairs[0]["value"] == "Diesel #1"


def test_an_identical_qbench_value_is_not_a_clash() -> None:
    """Nothing would change, so there is nothing to decide."""
    pairs = pair_sample_fields({"fuel_type": "Diesel #1"}, QB_FIELDS,
                               {"fuel_type": "Diesel #1"})
    assert pairs[0]["clash"] is False
    assert pairs[0]["unchanged"] is True


# ── the routes ───────────────────────────────────────────────────────────

@pytest.fixture
def sync(monkeypatch, tmp_path):
    import app as app_module
    from app import SampleRecord, UserState
    from change_log import ChangeLog

    lc = MagicMock()
    lc.sample_data.return_value = {
        "lab_id": "073126-41552", "fuel_type": "Diesel #1",
        "work_order": "03397832", "tank_number": "T-4",
        "customer_name": "Acme",
        "tests": [
            {"test": "Water & Sediment", "result": "0.02", "operator": "RC"},
            {"test": "Flash Point", "result": ""},
        ],
    }
    monkeypatch.setattr(app_module.state, "labcore", lc)
    monkeypatch.setattr(app_module.state, "change_log", ChangeLog(tmp_path / "cl"))
    monkeypatch.setattr(app_module.state, "logged_in", True)

    api = MagicMock()
    api.fetch_sample.return_value = {"id": 5, "lab_id": "073126-41552",
                                     "fuel_type": "", "work_order": "OLD"}
    api.update_sample.return_value = {"data": [{"id": 5}]}
    monkeypatch.setattr(app_module.state, "api_client", api)

    uid = "test-uid-sync"
    ustate = UserState(uid, "RC")
    ustate.add_record(SampleRecord(lab_id="073126-41552", tab="Yesterday",
                                   sample_id=5, test_ids=[9]))
    with app_module._sessions_lock:
        app_module.user_sessions[uid] = ustate

    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = uid

    yield client, lc, api, ustate

    with app_module._sessions_lock:
        app_module.user_sessions.pop(uid, None)


def test_the_preview_route_returns_pairs_with_current_qbench_values(sync) -> None:
    client, _, _, _ = sync
    data = client.get("/api/sync-preview/073126-41552").get_json()

    by_source = {p["source"]: p for p in data["pairs"]}
    assert by_source["fuel_type"]["target"] == "fuel_type"
    assert by_source["fuel_type"]["clash"] is False        # QBench blank
    assert by_source["work_order"]["clash"] is True        # QBench "OLD"
    assert by_source["tank_number"]["target"] is None      # needs a drag


# ── the Lab Vision test list (Tests-mode pane) ───────────────────────────
#
# LabCore's /api/sample already returns these; the preview used to drop them,
# so the pane had nothing to show a reviewer checking results.

def test_the_preview_returns_the_lab_vision_test_list(sync) -> None:
    client, _, _, _ = sync
    tests = client.get("/api/sync-preview/073126-41552").get_json()["tests"]

    assert [t["test"] for t in tests] == ["Water & Sediment", "Flash Point"]
    assert tests[0]["result"] == "0.02"
    assert tests[0]["operator"] == "RC"


def test_a_test_with_no_operator_still_reports_the_field(sync) -> None:
    """An older LabCore DB has no operator column and omits the key entirely.
    The pane must not have to defend against a missing field."""
    client, _, _, _ = sync
    tests = client.get("/api/sync-preview/073126-41552").get_json()["tests"]

    assert tests[1]["operator"] == ""
    assert tests[1]["result"] == ""


def test_a_sample_with_no_tests_reports_an_empty_list(sync) -> None:
    client, lc, _, _ = sync
    lc.sample_data.return_value = {"lab_id": "073126-41552"}

    assert client.get("/api/sync-preview/073126-41552").get_json()["tests"] == []


# ── the sync board's two columns ─────────────────────────────────────────

def test_every_editable_qbench_field_is_offered_as_a_drop_target(sync) -> None:
    """The bug: the dialog only ever rendered QBench fields that auto-matched
    a Lab Vision name, so `tank` — the target of the one drag the feature
    exists for — could not be dropped onto at all."""
    import app as app_module
    client, _, _, _ = sync
    qb = client.get("/api/sync-preview/073126-41552").get_json()["qb_fields"]

    offered = {f["name"] for f in qb if f["editable"]}
    assert offered == set(app_module.SAMPLE_EDITABLE_FIELDS)
    for unmatched in ("tank", "sample_taken_from", "time_of_collection", "Rush"):
        assert unmatched in offered, f"{unmatched} must be droppable"


def test_qbench_fields_carry_the_value_qbench_currently_holds(sync) -> None:
    client, _, _, _ = sync
    qb = {f["name"]: f for f in
          client.get("/api/sync-preview/073126-41552").get_json()["qb_fields"]}

    assert qb["work_order"]["current"] == "OLD"
    assert qb["fuel_type"]["current"] == ""
    assert qb["tank"]["current"] == ""


def test_fields_qbench_will_not_accept_are_shown_read_only(sync) -> None:
    """The reviewer sees the whole QBench record, but the board never offers
    a drop that the route would refuse with a 400."""
    import app as app_module
    client, _, api, _ = sync
    api.fetch_sample.return_value = {"id": 5, "lab_id": "073126-41552",
                                     "date_created": "2026-07-31"}
    qb = client.get("/api/sync-preview/073126-41552").get_json()["qb_fields"]

    read_only = {f["name"] for f in qb if not f["editable"]}
    assert "date_created" in read_only
    assert not (read_only & set(app_module.SAMPLE_EDITABLE_FIELDS))


def test_the_lab_vision_column_shows_fields_that_cannot_be_synced(sync) -> None:
    """customer_name is real Lab Vision data worth seeing, but QBench will not
    take it — so it is shown and not draggable, rather than hidden."""
    client, _, _, _ = sync
    lv = {f["name"]: f for f in
          client.get("/api/sync-preview/073126-41552").get_json()["lv_fields"]}

    assert lv["fuel_type"]["syncable"] is True
    assert lv["fuel_type"]["value"] == "Diesel #1"
    assert lv["customer_name"]["syncable"] is False
    assert lv["lab_id"]["syncable"] is False, "syncing lab_id would rename the sample"
    assert lv["tests"]["syncable"] is False


def test_an_unreadable_qbench_sample_still_offers_every_field(sync) -> None:
    """Losing QBench costs clash detection, not the whole board."""
    import app as app_module
    client, _, api, _ = sync
    api.fetch_sample.side_effect = RuntimeError("QBench down")
    data = client.get("/api/sync-preview/073126-41552").get_json()

    assert data["qbench_read"] is False
    offered = {f["name"] for f in data["qb_fields"] if f["editable"]}
    assert offered == set(app_module.SAMPLE_EDITABLE_FIELDS)
    assert all(f["current"] == "" for f in data["qb_fields"])


def test_applying_a_sync_writes_only_the_chosen_pairs(sync) -> None:
    client, _, api, _ = sync
    resp = client.post("/api/sync-sample-info/073126-41552", json={
        "mappings": [{"source": "fuel_type", "target": "fuel_type"}]})

    assert resp.status_code == 200
    sid, payload = api.update_sample.call_args.args
    assert sid == 5
    # fuel_type is a custom field in QBench, so it nests.
    sent = {**payload, **(payload.get("custom_fields") or {})}
    assert sent["fuel_type"] == "Diesel #1"
    assert "work_order" not in sent, "unselected pairs must not be written"


def test_a_reviewer_can_repair_a_field_onto_a_different_target(sync) -> None:
    """The whole point of the drag: LabVision's tank_number is QBench's tank."""
    client, _, api, _ = sync
    client.post("/api/sync-sample-info/073126-41552", json={
        "mappings": [{"source": "tank_number", "target": "tank"}]})

    _, payload = api.update_sample.call_args.args
    sent = {**payload, **(payload.get("custom_fields") or {})}
    assert sent["tank"] == "T-4"


def test_a_target_qbench_will_not_accept_is_refused(sync) -> None:
    """Otherwise QBench returns 200 and silently drops the value."""
    client, _, api, _ = sync
    resp = client.post("/api/sync-sample-info/073126-41552", json={
        "mappings": [{"source": "fuel_type", "target": "not_a_real_field"}]})

    assert resp.status_code == 400
    assert api.update_sample.call_count == 0


def test_an_empty_mapping_changes_nothing(sync) -> None:
    client, _, api, _ = sync
    resp = client.post("/api/sync-sample-info/073126-41552", json={"mappings": []})

    assert resp.status_code == 400
    assert api.update_sample.call_count == 0


def test_a_sync_is_written_to_the_audit_log(sync, tmp_path) -> None:
    import json
    client, _, _, _ = sync
    client.post("/api/sync-sample-info/073126-41552", json={
        "mappings": [{"source": "fuel_type", "target": "fuel_type"}]})

    rows = []
    for f in (tmp_path / "cl").glob("qbench_edits-*.jsonl"):
        rows += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    synced = [r for r in rows if r["event"] == "sample_sync"]
    assert synced, "a sync must be attributable"
    assert synced[-1]["lab_id"] == "073126-41552"
    assert synced[-1]["fields"] == {"fuel_type": "Diesel #1"}


def test_a_successful_sync_regenerates_the_preview(sync) -> None:
    """The COA is stale the moment sample info changes."""
    client, _, _, ustate = sync
    ustate.records[("Yesterday", "073126-41552")].preview_url = "http://old"

    client.post("/api/sync-sample-info/073126-41552", json={
        "mappings": [{"source": "fuel_type", "target": "fuel_type"}]})

    assert ustate.records[("Yesterday", "073126-41552")].preview_url is None


def test_an_unreachable_labcore_reports_itself(sync) -> None:
    from labcore_client import LabCoreUnavailable
    client, lc, _, _ = sync
    lc.sample_data.side_effect = LabCoreUnavailable("down")

    resp = client.get("/api/sync-preview/073126-41552")
    assert resp.status_code == 503
    assert resp.get_json()["labcore_down"] is True
