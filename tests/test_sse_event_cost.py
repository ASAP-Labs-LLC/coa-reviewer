"""Per-event client work must not scale with the whole sample set.

Every `sample_status` event ran three full passes over the samples and
allocated two throwaway arrays:

  * `samples.find(...)`                                   — linear, but early-exits
  * `Object.values(state.samples).flat().every(...)`      — flattens EVERY tab, every time
  * `updateTabActionButtons()`'s `.filter(...).length`    — allocates to count

At 250 samples the app now delivers ~500 status events (it used to silently
drop most of them, which hid this cost), so that is roughly 375k element
visits and 1,000 array allocations during the busiest part of a pull — while
the reviewer is trying to interact. That is an INP problem, and INP measured
248 ms at 250 samples against Chrome's 200 ms threshold.

The completion check is the worst offender and the easiest to make cheap: it
only needs to know whether ANY sample is unfinished, so it can stop at the
first one instead of visiting all of them and building an array to do it.

Source-level, like tests/test_cc_frontend.py. The real proof is the measured
INP; these guards stop the pattern coming back.
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


def test_status_updates_do_not_flatten_every_tab() -> None:
    body = _body("updateSampleStatus")
    assert ".flat()" not in body, (
        "updateSampleStatus still flattens every tab's samples on every event"
    )


def test_the_completion_check_can_stop_early() -> None:
    """`every()` over a freshly built array cannot short-circuit usefully —
    it still had to build the array first."""
    assert re.search(r"function _anyUnfinished\(", SOURCE), (
        "expected a dedicated early-exiting completion check"
    )
    body = _body("_anyUnfinished")
    assert "return true" in body, "the scan never short-circuits"
    assert ".flat()" not in body and ".map(" not in body, (
        "the completion check still allocates"
    )


def test_the_tab_button_check_does_not_allocate_to_count() -> None:
    body = _body("updateTabActionButtons")
    assert ".filter(" not in body, (
        "updateTabActionButtons still builds an array just to test emptiness"
    )
    assert ".some(" in body, "expected an early-exiting emptiness test"


def test_the_status_set_is_not_rebuilt_per_element() -> None:
    """A literal inside the predicate allocates once per sample visited."""
    body = _body("updateTabActionButtons")
    assert not re.search(r'\[\s*"good"\s*,\s*"bad"\s*\]\.includes', body), (
        "the ['good','bad'] literal is rebuilt for every sample visited"
    )
