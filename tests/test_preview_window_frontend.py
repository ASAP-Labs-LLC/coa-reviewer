"""The browser tells the server what it is looking at; the server renders a
window from there.

The server no longer renders anything on its own after a pull (see
tests/test_preview_window.py), so every way a sample becomes the one on
screen — click, arrow key, the auto-select when a tab renders — must reach
/api/focus, and a tab that finishes loading while it is the one on screen
must too, because renderSampleList() only re-selects when the current sample
is on another tab. Source-level, like tests/test_cc_frontend.py.
"""

from __future__ import annotations

import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "static" / "js" / "app.js"
SOURCE = APP_JS.read_text(encoding="utf-8")


def _body(name: str) -> str:
    m = re.search(rf"(?:async )?function {name}\(.*?\)\s*\{{(.*?)\n\}}", SOURCE, re.S)
    assert m, f"could not isolate {name}()"
    return m.group(1)


def test_there_is_one_place_that_asks_for_a_window() -> None:
    assert '"/api/focus"' in SOURCE, "nothing in the frontend calls /api/focus"
    assert re.search(r"function requestPreviewWindow\(", SOURCE), (
        "expected a single requestPreviewWindow() helper rather than ad-hoc fetches"
    )


def test_selecting_a_sample_asks_for_its_window() -> None:
    body = _body("selectSample")
    assert "requestPreviewWindow" in body, (
        "selectSample does not request a preview window, so a clicked or "
        "arrowed-to sample never renders"
    )


def test_a_tab_that_finishes_loading_on_screen_asks_for_its_window() -> None:
    """renderSampleList only auto-selects when the current sample is on a
    different tab; a Custom Day re-load lands on the same tab and would
    otherwise sit at pending forever."""
    m = re.search(r'case\s+"tab_loaded"\s*:(.{0,600}?)break;', SOURCE, re.S)
    assert m, "could not isolate the tab_loaded case"
    assert "requestPreviewWindow" in m.group(1), (
        "tab_loaded does not request a window for the tab on screen"
    )


def test_the_request_is_debounced() -> None:
    """Holding ArrowDown fires selectSample per row; one request for where
    the reviewer lands, not one per row passed."""
    body = _body("requestPreviewWindow")
    assert "setTimeout" in body and "clearTimeout" in body, (
        "requestPreviewWindow fires a request per call"
    )


def test_regenerate_pending_says_where_the_reviewer_is() -> None:
    """The server renders the window from the selected sample; without the
    lab_id it can only start from the top."""
    body = _body("handleRegeneratePending")
    m = re.search(r"JSON\.stringify\(\{([^}]*)\}\)", body)
    assert m, "could not find the request body in handleRegeneratePending"
    assert "lab_id" in m.group(1), (
        f"the regenerate-pending request body carries no lab_id: {m.group(1).strip()!r}"
    )
