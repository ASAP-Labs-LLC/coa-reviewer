"""Every inline PDF pane opens with the whole page in view, not fit-to-width.

Chrome's built-in viewer takes its opening zoom from the URL fragment on the
iframe ``src`` and from nothing else (``#view=FitH`` is fit-to-width,
``#view=Fit`` is fit-to-page). A reviewer who resized the COA pane to see the
whole page lost that on every refresh because each load reasserted ``FitH``.

The fragment lives in one constant, ``PDF_VIEW_FRAGMENT``, and every inline
viewer appends it: a pane that built its own URL would silently keep the old
zoom.

Two-page view is deliberately *not* asserted here. Chromium's open-parameter
parser (``chrome/browser/resources/pdf/open_pdf_params_parser.ts``) knows
``page``, ``view``, ``zoom``, ``toolbar``, ``navpanes`` and ``nameddest`` only;
two-page view is a toolbar toggle with no URL form, so nothing this app puts
in the ``src`` can turn it on.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

FRAGMENT_DECL = re.compile(r'const PDF_VIEW_FRAGMENT\s*=\s*"([^"]*)"')


def _fragment() -> str:
    m = FRAGMENT_DECL.search(APP_JS)
    assert m, "app.js must declare `const PDF_VIEW_FRAGMENT = \"...\"` once"
    return m.group(1)


def _inline_viewer_loads() -> list[str]:
    """Every `_setPdfSrc(...)` call that points a pane at a COA or SIF."""
    calls = [
        line.strip()
        for line in APP_JS.splitlines()
        if "_setPdfSrc(" in line and ("/api/pdf/" in line or "/api/sif/" in line)
    ]
    assert calls, "expected at least one inline COA/SIF load in app.js"
    return calls


def test_the_default_view_is_fit_to_page() -> None:
    """`Fit` keeps the whole page visible whatever the pane's aspect ratio;
    `FitV` would overflow sideways in a narrow pane."""
    assert _fragment() == "#view=Fit"


def test_the_fragment_is_declared_exactly_once() -> None:
    assert len(FRAGMENT_DECL.findall(APP_JS)) == 1


def test_every_inline_pdf_pane_uses_the_shared_fragment() -> None:
    for call in _inline_viewer_loads():
        assert "${PDF_VIEW_FRAGMENT}" in call, (
            f"this pane builds its own zoom and will not follow the default: {call}"
        )


def test_no_inline_pdf_pane_still_asks_for_fit_to_width() -> None:
    assert "view=FitH" not in APP_JS, "a stale fit-to-width fragment survives"
