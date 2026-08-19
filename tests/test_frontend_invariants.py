"""Source-level regression guards for frontend changes.

These are *invariant* tests, not TDD tests in the strict sense: the
production code already existed when they were written. They protect
against the most likely regression modes — silently reintroducing
removed concepts or losing the robustness of recent fixes.

For new frontend work the discipline is test-first.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "js" / "app.js"
APP_CSS = ROOT / "static" / "css" / "app.css"
INDEX_HTML = ROOT / "templates" / "index.html"


# ── Theme-pack removal ───────────────────────────────────────────────
# Five theme packs (aurora, brutalist, terminal, editorial, synthwave) were
# removed. Studio is the only theme. The supporting modal, gallery card UI,
# and pack-switching JS are gone with them.

THEME_PACK_JS_MARKERS = [
    "THEME_PACKS",
    "applyThemePack",
    "initThemePacks",
    "renderThemesGrid",
    "openThemesModal",
    "getCurrentPack",
    "PACK_CLASSES",
]

THEME_PACK_CSS_SELECTORS = [
    "body.theme-aurora",
    "body.theme-brutalist",
    "body.theme-terminal",
    "body.theme-editorial",
    "body.theme-synthwave",
    ".themes-modal-content",
    ".themes-grid",
    ".theme-card",
    ".theme-preview-aurora",
]


@pytest.mark.parametrize("marker", THEME_PACK_JS_MARKERS)
def test_app_js_has_no_theme_pack_identifiers(marker: str) -> None:
    src = APP_JS.read_text(encoding="utf-8")
    assert marker not in src, (
        f"theme-pack identifier {marker!r} reintroduced in app.js — Studio is "
        "now the only theme; only the dark/auto/light cycle remains"
    )


@pytest.mark.parametrize("selector", THEME_PACK_CSS_SELECTORS)
def test_app_css_has_no_theme_pack_selectors(selector: str) -> None:
    src = APP_CSS.read_text(encoding="utf-8")
    assert selector not in src, f"theme-pack CSS {selector!r} reintroduced"


@pytest.mark.parametrize(
    "marker", ['id="themes-modal"', 'id="themes-btn"', 'id="themes-grid"', 'id="themes-close"']
)
def test_index_html_has_no_themes_modal_elements(marker: str) -> None:
    src = INDEX_HTML.read_text(encoding="utf-8")
    assert marker not in src, f"{marker!r} reintroduced in index.html"


# ── Crash-loop hardening ─────────────────────────────────────────────
# A cached old index.html alongside fresh app.js caused
# `chooseReviewMode` to crash on `modal.querySelector(...)` against a
# null modal. The catch block above swallowed the JS error and triggered
# the boot-splash 3-second reload loop — every UI bug became a refresh
# loop that looked like a server crash. Two invariants prevent regression.

def _function_body_js(src: str, name: str) -> str:
    start = src.find(f"function {name}")
    assert start != -1, f"function {name} not found in app.js"
    next_fn = src.find("\nfunction ", start + 1)
    end = next_fn if next_fn != -1 else len(src)
    return src[start:end]


def test_choose_review_mode_null_checks_the_modal() -> None:
    body = _function_body_js(APP_JS.read_text(encoding="utf-8"), "chooseReviewMode")
    assert "if (!modal)" in body, (
        "chooseReviewMode must early-return on missing #review-mode-modal. "
        "Without the null-check, a Cloudflare- or browser-cached old HTML "
        "served alongside fresh JS crashes the bootstrap."
    )


def test_bootstrap_network_try_does_not_wrap_choose_review_mode() -> None:
    """The reload-after-3s recovery is for network failures only. UI bugs
    must not trigger it — they should be logged and proceed past."""
    src = APP_JS.read_text(encoding="utf-8")
    fetch_idx = src.find('fetch("/api/portal-session"')
    assert fetch_idx != -1, "portal-session fetch missing from bootstrap"
    try_idx = src.rfind("try {", 0, fetch_idx)
    assert try_idx != -1, "no try { ... } wraps the portal-session fetch"
    catch_idx = src.find("} catch", try_idx)
    assert catch_idx != -1
    network_try_body = src[try_idx:catch_idx]
    assert "chooseReviewMode" not in network_try_body, (
        "chooseReviewMode is inside the portal-session network try/catch — "
        "any UI failure will hit the boot-splash 3-second reload loop"
    )
    assert "initQBenchApp" not in network_try_body, (
        "initQBenchApp is inside the portal-session network try/catch — "
        "any init failure will trigger the boot-splash reload loop"
    )
