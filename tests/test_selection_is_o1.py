"""Selecting a sample must not repaint the whole list.

Named by an independent critic as the biggest remaining gap, and the evidence
lines up: INP measures the app's own main-thread DOM work (the COA renders in
an out-of-process PDF frame, so it is excluded), and interactions p95 moved
184ms -> 240ms between 20 and 250 samples — roughly the whole switch
regression. A per-interaction cost that scales with row count is the only
thing that lifts a distribution's floor, and the observed 250-sample minimum
sits above the 20-sample maximum.

Two functions did it. `highlightSample` toggled a class on EVERY row to change
which one row is highlighted. On the click path `clearSampleSelection` ran
`paintSelection` first, a second full pass. So a click cost two full-list style
invalidations, a keyboard switch one, at 250 rows on 2 throttled cores.

Both share the `.selected` class, so single-select and multi-select have to
keep cooperating: painting a multi-selection still clears a single highlight
that is not part of it.
"""

from __future__ import annotations

import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "static" / "js" / "app.js"
SOURCE = APP_JS.read_text(encoding="utf-8")


def _body(name: str) -> str:
    m = re.search(rf"function {name}\(.*?\)\s*\{{(.*?)\n\}}", SOURCE, re.S)
    assert m, f"could not isolate {name}()"
    return m.group(1)


def test_highlighting_does_not_sweep_every_row() -> None:
    body = _body("highlightSample")
    assert "$$(" not in body, (
        "highlightSample still visits every .sample-item to change one"
    )
    assert ".forEach" not in body, "highlightSample still iterates the list"


def test_painting_a_selection_only_touches_changed_rows() -> None:
    body = _body("paintSelection")
    assert "$$(" not in body, (
        "paintSelection still visits every row on every plain click"
    )


def test_the_single_highlight_is_tracked_not_searched() -> None:
    assert re.search(r"_highlightedEl", SOURCE), (
        "expected the selected element to be held, so it can be un-highlighted "
        "directly instead of by scanning"
    )


def test_multi_select_still_clears_a_single_highlight() -> None:
    """The two mechanisms share one class; painting must not strand it."""
    body = _body("paintSelection")
    assert "_highlightedEl" in body, (
        "paintSelection no longer clears a single highlight that is not part "
        "of the multi-selection, so the old row stays lit"
    )


def test_the_previous_selection_is_remembered_for_cheap_clearing() -> None:
    assert re.search(r"_paintedIds", SOURCE), (
        "expected the previously painted ids to be tracked so clearing costs "
        "the size of the selection, not the size of the list"
    )
