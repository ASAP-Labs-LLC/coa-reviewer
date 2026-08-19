"""TDD spec for the second round of mode-related changes.

Three independent slices:

  1. Back-to-picker button — a top-bar control visible in both modes
     that re-opens the review-mode picker mid-session.
  2. Info editor renders every key in the sample dict (not the hardcoded
     subset). Editable fields stay whitelisted by the server.
  3. Default tab per mode: Info opens on Intaked, Tests on Due Out
     (instead of Yesterday being hardcoded-active in HTML).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "js" / "app.js"
INDEX_HTML = ROOT / "templates" / "index.html"


def _function_body_js(src: str, name: str) -> str:
    """Locate `function <name>(` or `async function <name>(`."""
    for prefix in ("function ", "async function "):
        idx = src.find(f"{prefix}{name}(")
        if idx != -1:
            next_fn = src.find("\nfunction ", idx + 1)
            next_async = src.find("\nasync function ", idx + 1)
            ends = [x for x in (next_fn, next_async) if x != -1] + [len(src)]
            return src[idx:min(ends)]
    raise AssertionError(f"function {name}( not found")


# ── (1) Back-to-picker button ────────────────────────────────────────

def test_html_has_change_mode_button() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="change-mode-btn"' in html, (
        'Top bar must include a button with id="change-mode-btn" to re-open '
        "the review-mode picker"
    )


def test_change_mode_button_visible_in_both_modes() -> None:
    """The button has no data-show-mode, so it appears in both Info + Tests."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    m = re.search(r'<button[^>]*\bid="change-mode-btn"[^>]*>', html)
    assert m, "change-mode-btn opening tag not found"
    assert "data-show-mode" not in m.group(0), (
        "change-mode-btn must not declare data-show-mode (visible in both modes)"
    )


def test_change_mode_button_handler_reopens_picker() -> None:
    """Click handler must call chooseReviewMode (which shows the picker
    and re-resolves to apply the new mode). Accept both plain `.` and
    null-safe `?.` access — both are valid bindings."""
    src = APP_JS.read_text(encoding="utf-8")
    pattern = re.compile(
        r'\$\("#change-mode-btn"\)\??\.addEventListener\("click"[^}]+\}',
        re.DOTALL,
    )
    m = pattern.search(src)
    assert m, "change-mode-btn click listener not wired in app.js"
    body = m.group(0)
    assert "chooseReviewMode" in body, (
        "change-mode-btn handler must invoke chooseReviewMode()"
    )


# ── (2) Info editor renders all sample-level fields ──────────────────

def test_load_sample_info_iterates_over_sample_dict() -> None:
    """Rendering must derive rows from the sample dict's own keys (so any
    new QBench field shows up) — not a frozen hardcoded list."""
    body = _function_body_js(APP_JS.read_text(encoding="utf-8"), "loadSampleInfo")
    # Look for an iteration over the sample object's keys/entries
    iterates = (
        "Object.keys(sample" in body
        or "Object.entries(sample" in body
        or "for (const " in body and " in sample" in body
        or "for (let " in body and " in sample" in body
    )
    assert iterates, (
        "loadSampleInfo must iterate over the sample dict (Object.keys / "
        "Object.entries / for-in) so all sample-level fields render, not "
        "just a hardcoded subset"
    )


def test_load_sample_info_no_longer_uses_hardcoded_field_list_exclusively() -> None:
    """The previous INFO_EDITOR_FIELDS array can stay as a *preferred*
    ordering list, but the function must not render only those keys."""
    src = APP_JS.read_text(encoding="utf-8")
    body = _function_body_js(src, "loadSampleInfo")
    # If INFO_EDITOR_FIELDS still exists, the function must also iterate
    # over the sample dict's own keys (proving it picks up extra fields).
    if "INFO_EDITOR_FIELDS" in src:
        assert "Object.keys(sample" in body or "Object.entries(sample" in body, (
            "INFO_EDITOR_FIELDS may be kept as a preferred-order hint, but "
            "loadSampleInfo must still iterate over Object.keys(sample) so "
            "QBench fields outside that list are rendered too"
        )


def test_load_sample_info_uses_server_editable_fields_list() -> None:
    """The server returns `editable_fields`; the renderer must check that
    list to decide which fields are inputs vs. read-only displays."""
    body = _function_body_js(APP_JS.read_text(encoding="utf-8"), "loadSampleInfo")
    assert "editable_fields" in body, (
        "loadSampleInfo must consult data.editable_fields to decide which "
        "rendered fields are inputs vs. read-only"
    )


# ── (3) Default tab per mode ─────────────────────────────────────────

def test_html_has_no_hardcoded_active_tab() -> None:
    """The active tab is now mode-driven from JS; HTML must not pre-mark
    Yesterday (or any tab) as active."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    # The classic Yesterday "active" hardcoding
    assert not re.search(
        r'<button[^>]*data-tab="Yesterday"[^>]*class="[^"]*\bactive\b',
        html,
    ), 'Yesterday tab must not be hardcoded as active in HTML'
    assert not re.search(
        r'<button[^>]*class="[^"]*\bactive\b[^"]*"[^>]*data-tab="Yesterday"',
        html,
    ), 'Yesterday tab must not be hardcoded as active in HTML'


def test_js_has_default_tab_for_mode_helper() -> None:
    """A helper function maps mode → default tab name."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "defaultTabForMode" in src, (
        "JS must define a defaultTabForMode(mode) helper that returns "
        '"Intaked" for info and "Due Out" for tests'
    )


def test_default_tab_for_mode_maps_correctly() -> None:
    src = APP_JS.read_text(encoding="utf-8")
    body = _function_body_js(src, "defaultTabForMode")
    assert '"Intaked"' in body or "'Intaked'" in body, (
        "defaultTabForMode must return 'Intaked' for info mode"
    )
    assert '"Due Out"' in body or "'Due Out'" in body, (
        "defaultTabForMode must return 'Due Out' for tests mode"
    )


def test_apply_review_mode_swaps_to_default_tab() -> None:
    """When the user changes modes mid-session, applyReviewMode must
    switch the visible tab to the new mode's default (otherwise the user
    could be stranded on a tab that was just hidden)."""
    body = _function_body_js(APP_JS.read_text(encoding="utf-8"), "applyReviewMode")
    assert "switchTab" in body and "defaultTabForMode" in body, (
        "applyReviewMode must call switchTab(defaultTabForMode(mode)) so a "
        "mid-session mode change lands on the right default tab"
    )
