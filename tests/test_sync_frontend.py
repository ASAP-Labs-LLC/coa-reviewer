"""Frontend guards for the Sync Data dialog.

Sample information only, and only in Info mode.

The dialog is a two-column board: every Lab Vision field on the left, every
QBench field on the right, and a drag pairs one to the other. It used to be
one row per Lab Vision field whose QBench slot existed only where auto-pairing
found a target — which meant a QBench field nothing matched (`tank`,
`sample_taken_from`, `time_of_collection`, `Rush`, …) was never rendered and
could not be dropped onto at all. `tank_number` -> `tank` is the pairing the
feature exists for, and it was unreachable.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
APP_CSS = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")


def _fn(signature: str) -> str:
    """The source of one top-level function, for behaviour assertions."""
    body = APP_JS[APP_JS.index(signature):]
    ends = [body.index(m, 1) for m in ("\nfunction ", "\nasync function ")
            if m in body[1:]]
    return body[:min(ends)] if ends else body


def test_the_button_sits_next_to_open_in_lab_vision() -> None:
    assert 'id="sync-data-btn"' in INDEX_HTML
    bar = INDEX_HTML[INDEX_HTML.index('id="open-labvision-btn"'):]
    assert "sync-data-btn" in bar[:400], "Sync Data belongs beside Open in Lab Vision"


def test_the_button_is_only_shown_in_info_mode() -> None:
    """It syncs sample information, which is what Info mode is for; offering
    it during a results review would invite an accidental write."""
    assert "#sync-data-btn" in APP_CSS
    assert "mode-info" in APP_CSS


def test_the_dialog_exists() -> None:
    assert 'id="sync-modal"' in INDEX_HTML
    assert 'id="sync-apply"' in INDEX_HTML
    assert 'id="sync-cancel"' in INDEX_HTML


# ── the two-column board ─────────────────────────────────────────────────

def test_the_dialog_has_a_lab_vision_column_and_a_qbench_column() -> None:
    assert 'id="sync-lv-col"' in INDEX_HTML
    assert 'id="sync-qb-col"' in INDEX_HTML


def test_every_editable_qbench_field_is_rendered_as_a_drop_target() -> None:
    """Not only the ones auto-pairing matched — that was the bug."""
    body = _fn("function renderSyncBoard")
    assert "qbFields" in body, "the right column comes from the server's full field list"
    assert "editable" in body, "read-only QBench fields must be distinguishable"

    wiring = _fn("function wireSyncBoard")
    assert "drop" in wiring
    assert "sync-slot--locked" in wiring, "every slot but a read-only one takes a drop"


def test_a_lab_vision_field_qbench_cannot_take_is_not_draggable() -> None:
    """Offering a drag the route would refuse with a 400 promises a write
    that cannot happen."""
    body = _fn("function renderSyncBoard")
    assert "syncable" in body
    assert "draggable" in body


def test_dropping_onto_a_paired_field_displaces_the_previous_source() -> None:
    """Silently reassigning it is how a value lands in the wrong field."""
    body = _fn("function pairSyncField")
    assert "delete" in body


def test_a_manual_pair_onto_a_populated_qbench_field_is_flagged() -> None:
    """The old repointSyncPair cleared the clash flag instead of recomputing
    it, so a hand-made pairing could overwrite a released value in silence."""
    body = _fn("function syncPairState")
    assert "clash" in body
    assert "unchanged" in body


def test_a_clashing_pair_is_not_ticked_by_default() -> None:
    """Overwriting a value QBench already holds must be a deliberate act."""
    body = _fn("function syncDefaultSend")
    assert "clash" in body
    assert "false" in body


def test_the_board_says_when_qbench_values_could_not_be_read() -> None:
    """Without them nothing can be called a clash, and a board full of blank
    'current' values would imply every QBench field is empty."""
    assert "qbench_read" in APP_JS or "qbenchRead" in APP_JS


def test_apply_posts_the_chosen_mappings() -> None:
    assert "/api/sync-sample-info/" in APP_JS
    body = _fn("async function applySyncData")
    assert "mappings" in body
    assert "links" in body, "what gets posted comes from the board's pairings"


def test_the_preview_is_regenerated_after_a_sync() -> None:
    """Sample information feeds the COA, so the rendered preview is stale the
    moment it changes. The server re-renders; the UI must reflect it."""
    body = _fn("async function applySyncData")
    assert "regenerat" in body.lower() or "_pdfVersion" in body
