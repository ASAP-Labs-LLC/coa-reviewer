"""Frontend guards for drag multi-select and the right-click menu.

Two bulk actions on a selection: re-render them, or file them all under one
Command Center listing. Grouping reuses the existing flag modal — it already
takes a list of sample chips, so a group is just a pre-filled selection.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")


def test_a_context_menu_exists() -> None:
    assert 'id="sample-context-menu"' in INDEX_HTML
    assert 'id="ctx-regenerate"' in INDEX_HTML
    assert 'id="ctx-group-cc"' in INDEX_HTML
    assert 'id="ctx-clear"' in INDEX_HTML


def test_drag_select_is_wired_to_the_list() -> None:
    """Press on a row and drag across others to take a range."""
    for handler in ("mousedown", "mouseenter", "mouseup"):
        assert handler in APP_JS, f"{handler} needed for drag selection"


def test_right_click_opens_the_menu_instead_of_the_browser_one() -> None:
    assert "contextmenu" in APP_JS


def test_the_selection_drives_the_bulk_regenerate_endpoint() -> None:
    assert "/api/regenerate-selected" in APP_JS


def test_grouping_reuses_the_existing_listing_form() -> None:
    """A group is just the flag modal opened with several sample chips —
    there is no second listing form to keep in sync."""
    body = APP_JS[APP_JS.index("function groupSelectedIntoListing"):]
    body = body[:body.index("\nfunction ")]
    assert "cc-modal" in body
    assert "CC.samples" in body


def test_selection_survives_a_list_re_render() -> None:
    """SSE status events re-render the list constantly while previews load;
    a selection that evaporated mid-drag would be unusable."""
    body = APP_JS[APP_JS.index("function renderSampleList"):]
    body = body[:body.index("\nfunction ")]
    assert "selected" in body, (
        "renderSampleList must re-apply the selection class after rebuilding"
    )


def test_the_selection_is_cleared_when_switching_tabs() -> None:
    """lab_ids are only meaningful with their tab; carrying a Yesterday
    selection into Due Out would regenerate the wrong samples."""
    assert "clearSampleSelection" in APP_JS
