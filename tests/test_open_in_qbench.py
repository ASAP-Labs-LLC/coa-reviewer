"""TDD spec: an "Open in QBench" button that jumps to the selected
sample's QBench detail page.

URL format verified against the live QBench UI (2026-07-10): the samples
list links each row to `/sample?id=<sample_id>` — a query parameter, NOT
a path segment (`/sample/<id>` 404s).
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "js" / "app.js"
INDEX_HTML = ROOT / "templates" / "index.html"


def test_button_present_in_bottom_bar() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="open-qbench-btn"' in html, "Open in QBench button missing"


def test_button_wiring_survives_missing_element() -> None:
    """Flask can serve a cached (older) index.html while app.js is fresh —
    unguarded wiring of a new element bricks the whole boot sequence
    (production incident, 2026-07-10 afternoon). New-element wiring must
    use optional chaining."""
    js = APP_JS.read_text(encoding="utf-8")
    assert '$("#open-qbench-btn")?.addEventListener' in js, (
        "open-qbench-btn wiring must be null-safe (?.addEventListener)"
    )


def test_boot_survives_handler_wiring_failure() -> None:
    """initQBenchApp must not let a setupAppHandlers exception kill boot —
    a single missing element otherwise freezes the app at 'Checking
    QBench connection...'."""
    js = APP_JS.read_text(encoding="utf-8")
    import re
    m = re.search(r"try\s*\{[^}]*setupAppHandlers\(\)", js)
    assert m, "setupAppHandlers() call must be wrapped in try/catch"


def test_button_opens_sample_query_url() -> None:
    js = APP_JS.read_text(encoding="utf-8")
    assert "open-qbench-btn" in js, "button not wired in app.js"
    assert "asaplabs.qbench.net/sample?id=" in js, (
        "must use the verified /sample?id=<sample_id> URL format — "
        "path-style /sample/<id> returns 404 in QBench's UI"
    )


def test_button_enabled_by_sample_id_not_preview() -> None:
    """Unlike Download / Open in Browser (which need a rendered preview),
    the QBench link only needs the sample_id — it must not be gated on
    has_preview."""
    js = APP_JS.read_text(encoding="utf-8")
    import re
    # Null-safe pattern: const qbBtn = $("#open-qbench-btn"); if (qbBtn) qbBtn.disabled = ...
    m = re.search(r'qbBtn\.disabled\s*=\s*([^;]+);', js)
    assert m, "open-qbench-btn must be enabled/disabled with the other bottom-bar buttons"
    assert "sample_id" in m.group(1), "enablement must key off sample_id"
    assert "has_preview" not in m.group(1), "must not require has_preview"
