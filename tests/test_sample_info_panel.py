"""TDD spec for Stage 3: sample-info right panel (Info mode).

Per Prompt.txt, Info mode replaces the test editor on the right with
the sample's QBench fields (editable, save on Enter), plus the overall
testing package ("panels" — show "-" when none).

Stages:
  · 3a backend helper: qbench_client.update_sample(sample_id, fields)
  · 3b Flask route:    /api/sample-info/<lab_id> GET + PATCH
  · 3c frontend:       #info-editor section + load + save-on-Enter
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "app.py"
APP_JS = ROOT / "static" / "js" / "app.js"
INDEX_HTML = ROOT / "templates" / "index.html"
QBENCH = ROOT / "qbench_client.py"


def _function_body_py(src: str, name: str) -> str:
    """Locate `def <name>(` — the `(` prevents matching `def name_xxx(`."""
    start = src.find(f"def {name}(")
    assert start != -1, f"def {name}( not found"
    next_def   = src.find("\ndef ", start + 1)
    next_route = src.find("\n@app.route", start + 1)
    ends = [x for x in (next_def, next_route) if x != -1] + [len(src)]
    return src[start:min(ends)]


# ── 3a: qbench_client.update_sample ──────────────────────────────────

def test_qbench_client_has_update_sample() -> None:
    src = QBENCH.read_text(encoding="utf-8")
    assert "def update_sample(" in src, (
        "qbench_client must expose update_sample(sample_id, fields) — a "
        "generic PATCH wrapper used by the Info editor"
    )


def test_update_sample_patches_samples_endpoint() -> None:
    body = _function_body_py(QBENCH.read_text(encoding="utf-8"), "update_sample")
    assert '"PATCH"' in body and '"/samples"' in body, (
        "update_sample must PATCH /samples (matches the pattern used by "
        "update_test_result and update_sample_comments)"
    )


# ── 3b: Flask /api/sample-info/<lab_id> route ────────────────────────

def test_app_has_sample_info_route() -> None:
    src = APP_PY.read_text(encoding="utf-8")
    assert "/api/sample-info/" in src, "/api/sample-info/<lab_id> route missing"


def test_sample_info_route_supports_get_and_patch() -> None:
    src = APP_PY.read_text(encoding="utf-8")
    m = re.search(r'@app\.route\(\s*"/api/sample-info/<[^>]+>"\s*,\s*methods=\[([^\]]+)\]', src)
    assert m, "sample-info route declaration missing or malformed"
    methods = m.group(1)
    assert '"GET"' in methods, "sample-info route must accept GET (fetch fields)"
    assert '"PATCH"' in methods, "sample-info route must accept PATCH (save edits)"


def test_sample_info_route_calls_qbench_update_sample() -> None:
    """PATCH branch must hand the validated payload to update_sample."""
    src = APP_PY.read_text(encoding="utf-8")
    # Find the route function — Flask name convention or by route decorator
    m = re.search(r'@app\.route\(\s*"/api/sample-info/<[^>]+>".*?\ndef\s+(\w+)\s*\(', src, re.DOTALL)
    assert m, "couldn't locate sample-info view function"
    fn_name = m.group(1)
    body = _function_body_py(src, fn_name)
    assert "update_sample" in body, (
        "sample-info PATCH branch must call state.api_client.update_sample"
    )


def test_sample_info_normalizes_panels_field() -> None:
    """The GET response must expose a `panels` field that's normalized
    (list of names, or empty). Frontend renders '-' on empty."""
    src = APP_PY.read_text(encoding="utf-8")
    m = re.search(r'@app\.route\(\s*"/api/sample-info/<[^>]+>".*?\ndef\s+(\w+)\s*\(', src, re.DOTALL)
    assert m
    body = _function_body_py(src, m.group(1))
    assert '"panels"' in body or "'panels'" in body, (
        'sample-info GET must include a "panels" key in the JSON response '
        '(the "overall testing package" the reviewer reads)'
    )


# ── 3c: Frontend info-editor ─────────────────────────────────────────

def test_html_has_info_editor_section() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    m = re.search(r'<[^>]*\bid="info-editor"[^>]*>', html)
    assert m, "#info-editor section missing from HTML"
    assert 'data-show-mode="info"' in m.group(0), (
        '#info-editor must declare data-show-mode="info" so the existing '
        "test editor still owns the right panel in Tests mode"
    )


def test_test_editor_is_tests_only() -> None:
    """The existing test editor must hide in Info mode (data-show-mode='tests')
    so the new info editor takes its slot."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    m = re.search(r'<[^>]*\bid="test-editor"[^>]*>', html)
    assert m, "#test-editor not found"
    assert 'data-show-mode="tests"' in m.group(0), (
        '#test-editor must declare data-show-mode="tests" so it disappears '
        "in Info mode"
    )


def test_js_has_load_sample_info_function() -> None:
    src = APP_JS.read_text(encoding="utf-8")
    assert "function loadSampleInfo" in src or "async function loadSampleInfo" in src, (
        "JS must define loadSampleInfo(labId) — fetches and renders the editor"
    )


def test_js_save_on_enter_handler_exists() -> None:
    """Enter on any info-editor input must save that field via PATCH."""
    src = APP_JS.read_text(encoding="utf-8")
    # Look for a keydown listener that checks Enter + PATCHes /api/sample-info
    assert "/api/sample-info/" in src and 'method: "PATCH"' in src, (
        "JS must PATCH /api/sample-info/<labId> when the user presses Enter"
    )
