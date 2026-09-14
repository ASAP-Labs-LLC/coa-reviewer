"""After a re-auth the browser must take its list from the server, not keep
the one it had.

Bug report 2026-09-14: the session behind the timeout overlay had been reaped
(idle cleanup) and `/api/portal-reauth` built an empty one. `submitReauth()`
hid the overlay and carried on with 80 samples and 40 marks that existed only
in the browser — Export, Regenerate Pending and every PDF then ran against an
empty server, and nothing on screen said so.

The server now restores the review from disk, but whether it restored,
rebuilt or started over, the client's copy is not the truth any more; the
only honest move is to reload every tab. Source-level, like
tests/test_cc_frontend.py.
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


def test_reauth_reloads_every_tab_from_the_server() -> None:
    body = _body("submitReauth")
    assert "restoreAllTabs" in body, (
        "submitReauth keeps the in-memory sample list after re-auth; a session "
        "rebuilt server-side leaves that list describing state the server "
        "does not have"
    )


def test_reauth_tells_the_reviewer_when_the_session_was_rebuilt() -> None:
    """'restored' (same session), 'recovered' (rebuilt from disk) and
    'started over' are three different situations for the person at the
    screen; the second and third must not read as the first."""
    body = _body("submitReauth")
    assert "recovered" in body, (
        "submitReauth ignores the server's `recovered` flag, so a review "
        "rebuilt from disk and a review that is gone show the same message"
    )


def test_restore_all_tabs_covers_every_tab_the_server_can_hold() -> None:
    """Intaked (info mode) was missing, so a reload silently dropped it."""
    body = _body("restoreAllTabs")
    for tab in ("Yesterday", "Due Out", "Re-review", "Search", "Custom Day", "Intaked"):
        assert f'"{tab}"' in body, f"restoreAllTabs does not reload {tab!r}"
