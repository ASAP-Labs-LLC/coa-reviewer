"""Frontend guards for the Command Center UI.

Source-level, matching this project's existing frontend test style — there is
no JS test runner here. They are still written before the markup they
describe.

The enum tests matter most: the type / status / department options are
LabCore's, defined in `apps/LabCore/src/LabCore.py` (CC_TASK_TYPES,
CC_TASK_STATUSES) and `labvision.html` (CC_DEPTS). A value that drifts out of
those sets is silently coerced to a default by LabCore's writer, so a listing
would file under the wrong type or status with no error anywhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")


# ── The flag modal is a full listing form ────────────────────────────────

CC_FORM_FIELDS = [
    "cc-problem",      # initial_problem (required)
    "cc-type",         # listing type
    "cc-context",      # free-text background
    "cc-customer",     # customer name
    "cc-samples",      # attached lab_id chips
    "cc-status",       # starting board status
    "cc-department",   # department pill
]


@pytest.mark.parametrize("field_id", CC_FORM_FIELDS)
def test_flag_modal_exposes_every_listing_field(field_id: str) -> None:
    assert f'id="{field_id}"' in INDEX_HTML, f"{field_id} missing from the flag modal"


def test_flag_modal_replaced_the_old_double_check_wording() -> None:
    """Flagging files a Command Center listing now, not a spreadsheet row."""
    assert "Double Check sheet" not in INDEX_HTML
    assert "sent to Double Check" not in INDEX_HTML


# ── LabCore enums ────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [
    "double_check", "customer_clarification", "maintenance", "other",
])
def test_type_options_match_labcore_task_types(value: str) -> None:
    assert f'value="{value}"' in INDEX_HTML


@pytest.mark.parametrize("value", [
    "open", "in_progress", "waiting", "urgent", "ready_to_release",
])
def test_status_options_match_labcore_task_statuses(value: str) -> None:
    assert f'value="{value}"' in INDEX_HTML


@pytest.mark.parametrize("dept", ["Office", "Lab", "Admin", "Shipping", "Other"])
def test_department_options_match_labcore_departments(dept: str) -> None:
    assert f'value="{dept}"' in INDEX_HTML


# ── Conflict resolution ──────────────────────────────────────────────────

def test_a_conflict_modal_exists_for_samples_already_listed(request) -> None:
    """LabCore refuses a second listing on a sample unless force_create is
    set; the reviewer gets to choose."""
    assert 'id="cc-conflict-modal"' in INDEX_HTML
    assert 'id="cc-conflict-create"' in INDEX_HTML     # create anyway
    assert 'id="cc-conflict-cancel"' in INDEX_HTML


def test_creating_anyway_sends_force_create() -> None:
    """Without force_create LabCore just returns the same conflict again."""
    assert "force_create" in APP_JS


# ── Resolve-listing modal (complete / continue / back out) ───────────────

def test_resolution_modal_offers_all_three_choices() -> None:
    assert 'id="cc-resolve-modal"' in INDEX_HTML
    assert 'id="cc-resolve-complete"' in INDEX_HTML
    assert 'id="cc-resolve-continue"' in INDEX_HTML
    assert 'id="cc-resolve-back"' in INDEX_HTML


def test_completing_a_listing_collects_notes() -> None:
    """LabCore rejects a completion with empty completion_notes."""
    assert 'id="cc-resolve-notes"' in INDEX_HTML


# ── New buttons ──────────────────────────────────────────────────────────

def test_uncheck_button_exists() -> None:
    assert 'id="uncheck-btn"' in INDEX_HTML


def test_regenerate_pending_button_exists() -> None:
    assert 'id="regen-pending-btn"' in INDEX_HTML


def test_open_in_lab_vision_button_exists() -> None:
    assert 'id="open-labvision-btn"' in INDEX_HTML


# ── Wiring ───────────────────────────────────────────────────────────────

def test_js_creates_listings_through_the_proxy() -> None:
    """LabCore serves no CORS headers, so the browser must never address it
    directly — every call goes through this server."""
    assert "/api/cc/tasks" in APP_JS
    assert "/api/cc/lookup/" in APP_JS


def test_js_never_calls_labcore_directly() -> None:
    assert ":8080/api/" not in APP_JS, (
        "browser cannot reach LabCore directly — no CORS headers"
    )


def test_js_calls_the_complete_endpoint() -> None:
    assert "/complete" in APP_JS


def test_js_calls_regenerate_pending() -> None:
    assert "/api/regenerate-pending" in APP_JS


def test_js_sends_the_uncheck_outcome() -> None:
    assert '"uncheck"' in APP_JS or "'uncheck'" in APP_JS


def test_lab_vision_link_targets_the_sample_route() -> None:
    """LabVision's hash router: #/sample/{labId} opens sample detail."""
    assert "#/sample/" in APP_JS


def test_js_no_longer_checks_the_double_check_sheet() -> None:
    """/api/check-sheet is removed along with the spreadsheet."""
    assert "/api/check-sheet" not in APP_JS


# ── Regressions from the 2026-07-31 production report ────────────────────
# Reported together: "uncheck / regenerate pending are greyed out", "arrow
# keys not working", "no UI comes up for bad", "open in lab vision not
# working". All four were one class of problem — button state and CC config
# were only computed at moments that hadn't happened yet.

def test_command_center_config_refreshes_button_state_when_it_lands() -> None:
    """initCommandCenter() is fire-and-forget. Until it resolves,
    CC.labVisionUrl is empty and "Open in Lab Vision" computes as disabled.
    If nothing re-runs the button state afterwards, the button stays dead for
    whatever sample is already selected."""
    body = APP_JS[APP_JS.index("async function initCommandCenter"):]
    body = body[:body.index("\nfunction ")]
    assert "updateActionButtons" in body, (
        "initCommandCenter must refresh button state once the config arrives"
    )


def test_regenerate_pending_state_is_not_gated_on_selecting_a_sample() -> None:
    """Regenerate Pending is a tab-level action. It was only recomputed inside
    updateActionButtons(), which only ran from selectSample() — so on a freshly
    loaded tab, before clicking any sample, it sat greyed out despite the tab
    being full of pending samples."""
    assert "function updateTabActionButtons" in APP_JS, (
        "tab-level button state needs its own function, callable without a "
        "selected sample"
    )
    # ...and it must actually be called from the tab-render path.
    render = APP_JS[APP_JS.index("function renderSampleList"):]
    render = render[:render.index("\nfunction ")]
    assert "updateTabActionButtons" in render, (
        "renderSampleList must refresh tab-level buttons"
    )


def test_status_updates_do_not_reload_a_pdf_already_on_screen() -> None:
    """Un-marking flips status back to `ready`, which re-enters the
    status-change path. Without a guard that reloads the very iframe the
    reviewer is looking at, for no reason."""
    body = APP_JS[APP_JS.index("function updateSampleStatus"):]
    body = body[:body.index("\nfunction ")]
    assert "_currentPdfLab" in body, (
        "updateSampleStatus must not reload a PDF that is already displayed"
    )
