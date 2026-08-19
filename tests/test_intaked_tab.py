"""TDD spec for Stage 2: Intaked tab backend.

Info mode replaces the Tests sidebar's Re-review tab with a new Intaked
tab (samples with lab_id prefix = MMDDYY of 1 business day ago). The
existing Yesterday + Due Out tabs are unchanged.

Server-side dispatch: POST /api/start now reads `mode` from the request
body and selects the third tab accordingly. The frontend must send the
current mode along with the start request.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "app.py"
APP_JS = ROOT / "static" / "js" / "app.js"


def _function_body_py(src: str, name: str) -> str:
    """Return source between `def <name>` and the next top-level def/route."""
    start = src.find(f"def {name}")
    assert start != -1, f"def {name} not found in app.py"
    next_def = src.find("\ndef ", start + 1)
    next_route = src.find("\n@app.route", start + 1)
    ends = [x for x in (next_def, next_route) if x != -1] + [len(src)]
    return src[start:min(ends)]


# ── Server side ──────────────────────────────────────────────────────

def test_start_pulling_reads_mode_from_request_body() -> None:
    body = _function_body_py(APP_PY.read_text(encoding="utf-8"), "start_pulling")
    assert "request.get_json" in body or 'request.json' in body, (
        "start_pulling must read the requested mode from the POST body "
        "(via request.get_json or request.json)"
    )
    assert '"mode"' in body or "'mode'" in body, (
        'start_pulling must reference the "mode" key in the request body'
    )


def test_start_pulling_dispatches_intaked_fetch() -> None:
    body = _function_body_py(APP_PY.read_text(encoding="utf-8"), "start_pulling")
    assert "business_days_ago(1)" in body, (
        "Intaked = 1 business day ago — start_pulling must use that offset"
    )
    assert '"Intaked"' in body, (
        'start_pulling must dispatch a fetch for the "Intaked" tab'
    )


def test_start_pulling_branches_info_vs_tests() -> None:
    body = _function_body_py(APP_PY.read_text(encoding="utf-8"), "start_pulling")
    assert 'mode == "info"' in body or "mode == 'info'" in body, (
        "start_pulling must branch on `mode == 'info'` to swap Re-review "
        "for Intaked"
    )


def test_start_pulling_keeps_shared_tabs_unconditional() -> None:
    """Yesterday + Due Out fetch on every start regardless of mode."""
    body = _function_body_py(APP_PY.read_text(encoding="utf-8"), "start_pulling")
    assert '"Yesterday"' in body and "business_days_ago(2)" in body, (
        "Yesterday tab (2 business days ago) must still always be fetched"
    )
    assert '"Due Out"' in body and "business_days_ago(3)" in body, (
        "Due Out tab (3 business days ago) must still always be fetched"
    )


# ── Client side ──────────────────────────────────────────────────────

def test_frontend_sends_mode_in_start_request() -> None:
    """handleStart must POST the current review mode so the server knows
    whether to dispatch Intaked or Re-review."""
    src = APP_JS.read_text(encoding="utf-8")
    fn_start = src.find("async function handleStart")
    assert fn_start != -1, "handleStart not found in app.js"
    next_fn = src.find("\nasync function ", fn_start + 1)
    if next_fn == -1:
        next_fn = src.find("\nfunction ", fn_start + 1)
    body = src[fn_start:next_fn] if next_fn != -1 else src[fn_start:]
    # The fetch call must include a JSON body with the current mode
    assert "currentReviewMode" in body, (
        "handleStart must reference currentReviewMode when posting /api/start"
    )
    assert 'method: "POST"' in body and (
        '"Content-Type": "application/json"' in body
        or "'Content-Type': 'application/json'" in body
    ), (
        "handleStart's /api/start fetch must send Content-Type: application/json"
    )
