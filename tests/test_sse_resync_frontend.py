"""The frontend must act on the `resync` event the server can now emit.

`UserState._shed_oldest` drops stale events when a browser falls behind and
sends `resync` to say a gap happened. If nothing consumes it the server is
announcing a gap to no one, and the UI keeps showing the stale statuses the
event exists to correct — the same deaf-UI symptom, just reached differently.

Source-level, like tests/test_cc_frontend.py: there is no JS runner here, and
a missing switch case is exactly the kind of drift this catches.
"""

from __future__ import annotations

import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "static" / "js" / "app.js"
SOURCE = APP_JS.read_text(encoding="utf-8")


def test_resync_is_handled_in_the_sse_switch() -> None:
    assert re.search(r'case\s+"resync"\s*:', SOURCE), (
        "handleSSE has no `resync` case, so the server's gap signal is dropped"
    )


def test_resync_reloads_the_tabs() -> None:
    """A gap is only repaired by refetching state, not by a status line."""
    m = re.search(r'case\s+"resync"\s*:(.{0,900}?)break;', SOURCE, re.S)
    assert m, "could not isolate the resync case body"
    body = m.group(1)
    assert "restoreAllTabs" in body, (
        f"resync must reload tab state; body was: {body.strip()!r}"
    )


def test_resync_is_debounced() -> None:
    """A burst of shed events must not turn into a burst of full reloads.

    Overflow happens during the heaviest part of a pull, which is the worst
    possible moment to fire a reload per dropped event.
    """
    m = re.search(r'case\s+"resync"\s*:(.{0,900}?)break;', SOURCE, re.S)
    assert m, "could not isolate the resync case body"
    body = m.group(1)
    assert "Timeout" in body or "_resync" in body, (
        f"resync looks undebounced; body was: {body.strip()!r}"
    )
