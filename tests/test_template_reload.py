"""Flask must reload templates from disk without a process restart.

With debug off, Flask/Jinja cache compiled templates for the process
lifetime — but static files (app.js/app.css) are always served fresh.
Editing index.html + app.js together then only restarts half the change,
and the served HTML/JS skew can brick the boot sequence (production
incident, 2026-07-10: cached index.html lacked a button the fresh app.js
wired unguarded)."""
from __future__ import annotations

import app


def test_templates_auto_reload_enabled() -> None:
    assert app.app.config.get("TEMPLATES_AUTO_RELOAD") is True, (
        "TEMPLATES_AUTO_RELOAD must be True so index.html edits are served "
        "immediately, matching static-file behavior"
    )
