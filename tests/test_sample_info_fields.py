"""TDD spec for the QBench-aligned Info-editor field list.

The Info-mode right panel renders the 22 sample-level fields shown in the
QBench UI (per screenshot 2026-05-19), in that exact order, with the exact
labels and API keys. All are editable except READ_ONLY_FIELDS below.

QBench nests most of these under `sample.custom_fields`; the server-side
GET response flattens that onto the top-level dict so the frontend can
loop over a single shape, and the PATCH handler re-nests editable keys
that aren't top-level QBench standard fields.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "js" / "app.js"
APP_PY = ROOT / "app.py"


# (display label, API key) — order matters, exactly as in the QBench UI.
EXPECTED_FIELDS = [
    ("Lab ID",                "lab_id"),
    ("Field Works Number",    "fw"),
    ("Work Order",            "work_order"),
    ("Accession Number",      "accession_number"),
    ("Sample Type",           "fuel_type"),
    ("Package Size",          "package_size"),
    ("PO",                    "po_number"),
    ("Time of Collection",    "time_of_collection"),
    ("Customer Sample ID",    "customer_sample_id"),
    ("Sample Taken From",     "sample_taken_from"),
    ("Tank Serial Number",    "tank"),
    ("Generator",             "generator"),
    ("Component Model",       "component_model"),
    ("Tank Capacity",         "tank_capacity"),
    ("Point of Collection",   "point_of_collection"),
    ("Quantity in Tank",      "quantity_tank"),
    ("Site Location",         "site_location"),
    ("Tank",                  "source"),
    ("Tags",                  "tags"),
    ("Attachments",           "attachments"),
    ("Comments",              "comments"),
    ("Rush sample",           "Rush"),
]

# Fields that render in the editor but must NOT be editable. `source` is a
# structured QBench SOURCE object ({_self, _type, ...}); a free-text edit
# would send QBench a malformed value (decided 2026-07-10).
READ_ONLY_FIELDS = {"source"}


def _info_editor_fields_block(src: str) -> str:
    m = re.search(r"INFO_EDITOR_FIELDS\s*=\s*\[(.*?)\];", src, re.DOTALL)
    assert m, "INFO_EDITOR_FIELDS array not found in app.js"
    return m.group(1)


def test_info_editor_fields_in_screenshot_order() -> None:
    block = _info_editor_fields_block(APP_JS.read_text(encoding="utf-8"))
    last_idx = -1
    for label, key in EXPECTED_FIELDS:
        # Each entry is `{ key: "<key>", label: "<label>", ... }` —
        # check the key appears and is positioned after the previous one.
        key_token = f'key: "{key}"'
        idx = block.find(key_token)
        assert idx != -1, f"Field {key!r} missing from INFO_EDITOR_FIELDS"
        assert idx > last_idx, (
            f"Field {key!r} is out of order — expected after the previous "
            "entry in the QBench screenshot"
        )
        last_idx = idx


def test_each_key_paired_with_correct_label() -> None:
    src = APP_JS.read_text(encoding="utf-8")
    block = _info_editor_fields_block(src)
    for label, key in EXPECTED_FIELDS:
        # Either order in the object literal — key/label or label/key.
        pair_a = re.search(
            rf'key:\s*"{re.escape(key)}"[^{{}}]*label:\s*"{re.escape(label)}"',
            block,
        )
        pair_b = re.search(
            rf'label:\s*"{re.escape(label)}"[^{{}}]*key:\s*"{re.escape(key)}"',
            block,
        )
        assert pair_a or pair_b, (
            f"Field {key!r} is not paired with label {label!r} in INFO_EDITOR_FIELDS"
        )


def _editable_whitelist_block() -> str:
    src = APP_PY.read_text(encoding="utf-8")
    m = re.search(
        r"SAMPLE_EDITABLE_FIELDS\s*=\s*frozenset\(\{([^}]+)\}",
        src,
        re.DOTALL,
    )
    assert m, "SAMPLE_EDITABLE_FIELDS frozenset not found in app.py"
    return m.group(1)


def test_editable_fields_in_server_whitelist() -> None:
    """Every field except the read-only set must be in app.py's
    SAMPLE_EDITABLE_FIELDS so the PATCH handler accepts it."""
    block = _editable_whitelist_block()
    for _, key in EXPECTED_FIELDS:
        if key in READ_ONLY_FIELDS:
            continue
        # Accept double or single quotes
        assert (f'"{key}"' in block) or (f"'{key}'" in block), (
            f"Field {key!r} missing from SAMPLE_EDITABLE_FIELDS"
        )


def test_read_only_fields_not_in_server_whitelist() -> None:
    """Read-only fields must be absent from SAMPLE_EDITABLE_FIELDS — that
    single omission renders them read-only in the editor AND makes the
    PATCH handler reject edits to them."""
    block = _editable_whitelist_block()
    for key in READ_ONLY_FIELDS:
        assert (f'"{key}"' not in block) and (f"'{key}'" not in block), (
            f"Field {key!r} must NOT be in SAMPLE_EDITABLE_FIELDS"
        )


def test_get_sample_info_flattens_custom_fields() -> None:
    """QBench nests most ASAP-specific fields under sample.custom_fields;
    the GET handler must merge them onto the top level so the frontend
    sees one flat dict."""
    src = APP_PY.read_text(encoding="utf-8")
    fn_idx = src.find("def sample_info(lab_id")
    assert fn_idx != -1
    next_def = src.find("\ndef ", fn_idx + 1)
    next_route = src.find("\n@app.route", fn_idx + 1)
    ends = [x for x in (next_def, next_route) if x != -1] + [len(src)]
    body = src[fn_idx:min(ends)]
    assert "custom_fields" in body, (
        "sample_info must reference sample['custom_fields'] — QBench nests "
        "ASAP-specific fields there and the frontend renders flat"
    )


def test_patch_renests_custom_fields_for_qbench() -> None:
    """The PATCH handler must split incoming fields into top-level QBench
    keys (comments, source, tags, attachments, lab_id) vs custom_fields,
    because QBench's /samples PATCH expects custom fields nested."""
    src = APP_PY.read_text(encoding="utf-8")
    fn_idx = src.find("def sample_info(lab_id")
    next_def = src.find("\ndef ", fn_idx + 1)
    next_route = src.find("\n@app.route", fn_idx + 1)
    ends = [x for x in (next_def, next_route) if x != -1] + [len(src)]
    body = src[fn_idx:min(ends)]
    # The PATCH branch must distinguish top-level QBench fields from custom ones
    # before forwarding to update_sample.
    assert "TOP_LEVEL_SAMPLE_FIELDS" in src or "QBENCH_TOP_LEVEL_FIELDS" in src, (
        "A constant must enumerate which keys are top-level QBench sample "
        "fields vs custom_fields, so the PATCH handler can split them"
    )
    assert "custom_fields" in body, (
        "PATCH branch must build a custom_fields nested dict for QBench"
    )
