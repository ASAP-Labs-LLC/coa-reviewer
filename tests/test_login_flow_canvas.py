"""TDD spec for "antigravity canvas behind every login-flow modal".

The canvas used to live inside #portal-login-modal, so it only showed
during portal login. We want it visible behind the boot-splash and
review-mode-modal as well — the full login flow becomes one continuous
visual experience, not three stylistically-different screens.

These tests were written *first* and watched fail before the feature
was implemented.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = ROOT / "templates" / "index.html"
APP_JS = ROOT / "static" / "js" / "app.js"
APP_CSS = ROOT / "static" / "css" / "app.css"


def _opening_tag(html: str, element_id: str) -> str:
    """Return the opening `<… id="X" …>` tag including all attributes."""
    pattern = re.compile(r"<\w+[^>]*\bid=\"" + re.escape(element_id) + r"\"[^>]*>")
    m = pattern.search(html)
    assert m, f"element with id={element_id!r} not found"
    return m.group(0)


def test_antigravity_canvas_lives_at_body_level_not_nested_in_a_modal() -> None:
    """Canvas must be a sibling of the modals so it can show behind any
    of them, not a child of one specific modal."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    canvas_idx = html.find('id="antigravity-canvas"')
    assert canvas_idx != -1, "antigravity-canvas missing from index.html"
    # Find every modal opening before the canvas
    modal_open_re = re.compile(r"<div[^>]*\bclass=\"[^\"]*\bmodal\b[^\"]*\"[^>]*>")
    # For each modal opening, check whether the canvas falls before its
    # corresponding closing </div>. We use a naive depth-balancing scan.
    for m in modal_open_re.finditer(html, 0, canvas_idx):
        # Walk forward from this modal's opening, balancing <div> nesting,
        # to find its closing tag.
        i = m.end()
        depth = 1
        while depth > 0 and i < len(html):
            open_at  = html.find("<div", i)
            close_at = html.find("</div>", i)
            if close_at == -1:
                break
            if open_at != -1 and open_at < close_at:
                depth += 1
                i = open_at + 4
            else:
                depth -= 1
                i = close_at + 6
        # If the canvas index sits before this modal closed, the canvas
        # is nested inside the modal. That's the regression we ban.
        modal_close = i
        assert canvas_idx >= modal_close, (
            "antigravity-canvas is nested inside a modal that opens at "
            f"index {m.start()} — it must be at body level so it can sit "
            "behind boot-splash and review-mode-modal too"
        )


def test_boot_splash_marked_no_scrim() -> None:
    """boot-splash must opt out of the default modal scrim/backdrop-blur
    so the canvas behind it is visible."""
    tag = _opening_tag(INDEX_HTML.read_text(encoding="utf-8"), "boot-splash")
    assert "no-scrim" in tag, (
        "boot-splash must include 'no-scrim' class so the canvas shows behind it"
    )


def test_review_mode_modal_marked_no_scrim() -> None:
    """review-mode-modal must opt out of the default modal scrim/backdrop-blur
    so the canvas behind it is visible."""
    tag = _opening_tag(INDEX_HTML.read_text(encoding="utf-8"), "review-mode-modal")
    assert "no-scrim" in tag, (
        "review-mode-modal must include 'no-scrim' class so the canvas shows behind it"
    )


def test_css_defines_modal_no_scrim_variant() -> None:
    """The .no-scrim modal variant must clear the dark backdrop and the
    backdrop-filter blur."""
    css = APP_CSS.read_text(encoding="utf-8")
    # Find a rule selector that targets .modal.no-scrim
    assert re.search(r"\.modal\.no-scrim\s*\{", css), (
        "CSS must define `.modal.no-scrim { ... }` to clear the scrim + blur"
    )
    # Pull the rule body
    rule = re.search(r"\.modal\.no-scrim\s*\{([^}]*)\}", css)
    assert rule, "couldn't extract .modal.no-scrim rule body"
    body = rule.group(1)
    assert "background" in body, "no-scrim must override the scrim background"
    assert "backdrop-filter" in body, "no-scrim must override the backdrop blur"


def test_antigravity_observes_app_visibility_not_portal_modal() -> None:
    """initAntigravity must track #app's visibility (the main UI) — so the
    canvas keeps running through portal-login → boot-splash → review-mode
    and only stops once the main app is on screen."""
    src = APP_JS.read_text(encoding="utf-8")
    fn_start = src.find("function initAntigravity")
    assert fn_start != -1
    next_fn = src.find("\nfunction ", fn_start + 1)
    body = src[fn_start:next_fn] if next_fn != -1 else src[fn_start:]
    assert 'getElementById("app")' in body or '"#app"' in body or "'#app'" in body, (
        "initAntigravity must observe #app's visibility (so canvas runs "
        "through all login-flow modals, not just portal-login-modal)"
    )
