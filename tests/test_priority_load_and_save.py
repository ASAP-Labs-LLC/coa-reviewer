"""TDD spec for three follow-ups:

  1. Priority tab loads FIRST. In Info mode the Intaked thread starts
     before Yesterday/Due Out; in Tests mode the Due Out thread starts
     before Yesterday/Re-review. Since the QBench rate-limiter is FIFO,
     starting first means landing first.

  2. Sample-info editor saves on BLUR (click-off) too, not just Enter.

  3. The "saved" green border persists until a NEW sample is selected
     (no auto-clear timeout). Re-rendering the editor when a different
     sample is chosen wipes the state naturally via innerHTML replacement.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "app.py"
APP_JS = ROOT / "static" / "js" / "app.js"


def _py_function_body(src: str, name: str) -> str:
    start = src.find(f"def {name}(")
    assert start != -1, f"def {name}( not found"
    next_def = src.find("\ndef ", start + 1)
    next_route = src.find("\n@app.route", start + 1)
    ends = [x for x in (next_def, next_route) if x != -1] + [len(src)]
    return src[start:min(ends)]


def _js_function_body(src: str, name: str) -> str:
    for prefix in ("async function ", "function "):
        idx = src.find(f"{prefix}{name}(")
        if idx != -1:
            ne = [
                src.find("\nfunction ", idx + 1),
                src.find("\nasync function ", idx + 1),
            ]
            ends = [x for x in ne if x != -1] + [len(src)]
            return src[idx:min(ends)]
    raise AssertionError(f"function {name}( not found")


# ── (1) Priority tab loads first ─────────────────────────────────────

def test_info_mode_dispatches_intaked_before_yesterday_and_due_out() -> None:
    body = _py_function_body(APP_PY.read_text(encoding="utf-8"), "start_pulling")
    info_idx = body.find('mode == "info"')
    assert info_idx != -1, "start_pulling has no `mode == \"info\"` branch"
    else_idx = body.find("\n    else:", info_idx)
    info_branch = body[info_idx : else_idx if else_idx != -1 else len(body)]

    intaked_pos   = info_branch.find('"Intaked"')
    yesterday_pos = info_branch.find('"Yesterday"')
    due_out_pos   = info_branch.find('"Due Out"')

    assert intaked_pos != -1,   "Intaked thread missing from info branch"
    assert yesterday_pos != -1, "Yesterday thread missing from info branch"
    assert due_out_pos != -1,   "Due Out thread missing from info branch"
    assert intaked_pos < yesterday_pos, (
        "Intaked must be dispatched BEFORE Yesterday in info mode "
        "(reviewers see Intaked's samples populate first)"
    )
    assert intaked_pos < due_out_pos, (
        "Intaked must be dispatched BEFORE Due Out in info mode"
    )


def test_tests_mode_dispatches_due_out_before_yesterday() -> None:
    body = _py_function_body(APP_PY.read_text(encoding="utf-8"), "start_pulling")
    else_idx = body.find("\n    else:")
    assert else_idx != -1, "start_pulling has no else branch (tests mode)"
    tests_branch = body[else_idx:]

    due_out_pos   = tests_branch.find('"Due Out"')
    yesterday_pos = tests_branch.find('"Yesterday"')

    assert due_out_pos != -1,   "Due Out thread missing from tests branch"
    assert yesterday_pos != -1, "Yesterday thread missing from tests branch"
    assert due_out_pos < yesterday_pos, (
        "Due Out must be dispatched BEFORE Yesterday in tests mode "
        "(it's the default tab + the most urgent column)"
    )


# ── (2) Save on blur (click-off) ─────────────────────────────────────

def test_info_editor_inputs_save_on_blur() -> None:
    body = _js_function_body(APP_JS.read_text(encoding="utf-8"), "loadSampleInfo")
    has_blur = (
        'addEventListener("blur"' in body
        or "addEventListener('blur'" in body
    )
    assert has_blur, (
        "Each .info-input must listen for `blur` so click-off saves the "
        "field (Enter is no longer the only commit path)"
    )


# ── (3) Saved-green state persists until next sample ─────────────────

def test_saved_class_is_not_auto_cleared_by_timeout() -> None:
    body = _js_function_body(
        APP_JS.read_text(encoding="utf-8"), "saveSampleInfoField"
    )
    # The previous implementation called classList.remove("info-input--saved")
    # inside a setTimeout to revert the green after ~1.4 s. That removal
    # must be gone — the green must stay until a new sample is selected.
    assert 'classList.remove("info-input--saved")' not in body, (
        "saveSampleInfoField must NOT remove info-input--saved on a timer — "
        "the green must persist until loadSampleInfo re-renders for a "
        "different sample (which clears it naturally via innerHTML)"
    )


def test_load_sample_info_replaces_inner_html() -> None:
    """A new sample load re-renders the editor, which wipes any leftover
    `info-input--saved` / `--error` classes via DOM replacement. This is
    the natural mechanism that resets the green between samples."""
    body = _js_function_body(APP_JS.read_text(encoding="utf-8"), "loadSampleInfo")
    assert "innerHTML" in body, (
        "loadSampleInfo must rebuild the field DOM via innerHTML so that "
        "selecting a new sample wipes the previous sample's saved/error state"
    )
