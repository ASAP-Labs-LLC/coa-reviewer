"""TDD spec for review-mode routing — Stage 1.

Per Prompt.txt, the Info pill must stop being a dead end and instead
route to a fork of the Tests screen. Stage 1 focuses on:

  · Info pill resolves the picker promise with `mode='info'`
  · An `applyReviewMode(mode)` helper sets `body.mode-info` / `mode-tests`
  · Sidebar declares an `Intaked` tab (visible only in Info mode)
  · Re-review tab becomes Tests-only
  · Shared tabs (Yesterday, Due Out, Search, Custom Day) have no
    data-show-mode and stay visible in both
  · CSS hides tabs by `data-show-mode` based on the body class

Stage 2 (Intaked backend) and Stage 3 (sample-info right panel) are
separate test files.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "js" / "app.js"
APP_CSS = ROOT / "static" / "css" / "app.css"
INDEX_HTML = ROOT / "templates" / "index.html"


def _function_body_js(src: str, name: str) -> str:
    start = src.find(f"function {name}")
    assert start != -1, f"function {name} not found in app.js"
    next_fn = src.find("\nfunction ", start + 1)
    return src[start:next_fn] if next_fn != -1 else src[start:]


# ── Picker routing ───────────────────────────────────────────────────

def test_apply_review_mode_function_exists() -> None:
    src = APP_JS.read_text(encoding="utf-8")
    assert "function applyReviewMode" in src, (
        "applyReviewMode(mode) must exist so CSS can branch on the choice"
    )


def test_apply_review_mode_toggles_body_class() -> None:
    body = _function_body_js(APP_JS.read_text(encoding="utf-8"), "applyReviewMode")
    has_info_toggle = (
        'classList.toggle("mode-info"' in body
        or "classList.toggle('mode-info'" in body
    )
    has_tests_toggle = (
        'classList.toggle("mode-tests"' in body
        or "classList.toggle('mode-tests'" in body
    )
    assert has_info_toggle, "applyReviewMode must toggle body.mode-info"
    assert has_tests_toggle, "applyReviewMode must toggle body.mode-tests"


def test_review_mode_init_does_not_swap_to_dead_end() -> None:
    """Info must no longer fall into the 'coming soon' dead-end — the
    fork-of-Tests page is now built, so Info advances the app exactly
    like Tests does. The initReviewModeModal click handler must not
    reference the placeholder element."""
    body = _function_body_js(APP_JS.read_text(encoding="utf-8"), "initReviewModeModal")
    assert "review-mode-soon" not in body, (
        "initReviewModeModal still swaps to the .review-mode-soon dead-end "
        "for Info — placeholder must be removed and Info must resolve the "
        "picker promise"
    )


def test_html_has_no_dead_end_placeholder() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'class="review-mode-soon' not in html, (
        "Dead-end .review-mode-soon block still in HTML"
    )
    assert "coming soon" not in html.lower(), (
        '"coming soon" copy still in HTML — Info now routes to the fork-of-Tests page'
    )


# ── Sidebar tabs ─────────────────────────────────────────────────────

def test_html_has_intaked_tab_button() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'data-tab="Intaked"' in html, (
        "Sidebar must include an Intaked tab button for Info mode"
    )


def test_intaked_tab_is_info_only() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    m = re.search(r'<button[^>]*data-tab="Intaked"[^>]*>', html)
    assert m, "Intaked tab button not found"
    assert 'data-show-mode="info"' in m.group(0), (
        'Intaked tab must declare data-show-mode="info" so it only renders in Info mode'
    )


def test_re_review_tab_is_tests_only() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    m = re.search(r'<button[^>]*data-tab="Re-review"[^>]*>', html)
    assert m, "Re-review tab button not found"
    assert 'data-show-mode="tests"' in m.group(0), (
        'Re-review tab must declare data-show-mode="tests" so it disappears in Info mode'
    )


def test_shared_tabs_have_no_show_mode() -> None:
    """Yesterday, Due Out, Search, Custom Day appear in both modes —
    so they must not declare data-show-mode at all."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    for tab in ("Yesterday", "Due Out", "Search", "Custom Day"):
        m = re.search(rf'<button[^>]*data-tab="{re.escape(tab)}"[^>]*>', html)
        assert m, f"{tab} tab button not found"
        assert "data-show-mode" not in m.group(0), (
            f'{tab} tab should not declare data-show-mode (it appears in both modes)'
        )


def test_bootstrap_defaults_to_tests_mode_when_picker_skipped() -> None:
    """If chooseReviewMode resolves with null (cached HTML, modal element
    missing, etc.), the bootstrap must still apply Tests mode so the
    right-panel editor renders — otherwise both editors are visible."""
    src = APP_JS.read_text(encoding="utf-8")
    cr_idx = src.find("await chooseReviewMode")
    assert cr_idx != -1, "await chooseReviewMode() not found in bootstrap"
    # Look in the next ~800 chars for the fallback default
    window = src[cr_idx:cr_idx + 800]
    has_default = (
        'applyReviewMode("tests")' in window
        or 'currentReviewMode = "tests"' in window
        or "currentReviewMode = 'tests'" in window
    )
    assert has_default, (
        "Bootstrap must default currentReviewMode/applyReviewMode to "
        '"tests" near the chooseReviewMode await so the picker being skipped '
        "doesn't leave the right panel in a both-editors-visible state"
    )


def test_css_hides_tabs_by_mode() -> None:
    css = APP_CSS.read_text(encoding="utf-8")
    assert re.search(
        r"body\.mode-info\s+\[data-show-mode=\"tests\"\]", css
    ), "CSS must hide Tests-only tabs when body.mode-info is set"
    assert re.search(
        r"body\.mode-tests\s+\[data-show-mode=\"info\"\]", css
    ), "CSS must hide Info-only tabs when body.mode-tests is set"
