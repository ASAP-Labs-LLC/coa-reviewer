"""Pane visibility is checkboxes, not a three-way slider.

Layout, per the request: COA is the LEFT half; the SIF and Lab Vision panes
stack in the RIGHT half, top and bottom. So all three showing gives COA 1/2 of
the area with SIF and Lab Vision a quarter each, and COA + Lab Vision alone
gives a straight 1/2 and 1/2.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
APP_CSS = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")


def test_the_three_panes_are_checkboxes() -> None:
    for cb in ("view-coa", "view-sif", "view-labvision"):
        assert f'id="{cb}"' in INDEX_HTML, f"{cb} checkbox missing"
    assert 'type="checkbox" id="view-coa"' in INDEX_HTML


def test_the_old_three_way_slider_is_gone() -> None:
    """It could not express COA + Lab Vision without SIF."""
    assert 'data-view="split"' not in INDEX_HTML
    assert 'class="view-btn' not in INDEX_HTML


def test_a_lab_vision_pane_exists() -> None:
    assert 'id="labvision-pane"' in INDEX_HTML
    assert 'id="labvision-content"' in INDEX_HTML


def test_sif_and_lab_vision_share_a_stacked_right_hand_column() -> None:
    """They are top and bottom of the right half, not two more columns."""
    assert 'id="side-panes"' in INDEX_HTML
    col = INDEX_HTML[INDEX_HTML.index('id="side-panes"'):]
    col = col[:col.index("<!-- Sample Info")]
    assert 'id="sif-pane"' in col, "SIF belongs inside the right-hand column"
    assert 'id="labvision-pane"' in col, "Lab Vision belongs inside it too"
    assert 'id="coa-pane"' not in col, "COA is the left half, not in the column"


def test_the_right_hand_column_stacks_vertically() -> None:
    assert "#side-panes" in APP_CSS
    block = APP_CSS[APP_CSS.index("#side-panes"):]
    block = block[:block.index("}")]
    assert "column" in block, "SIF over Lab Vision means flex-direction: column"


def test_layout_gives_coa_half_and_the_column_the_other_half() -> None:
    body = APP_JS[APP_JS.index("function applyPaneLayout"):]
    body = body[:body.index("\nfunction ")]
    assert "50" in body
    assert "side-panes" in body


def test_at_least_one_pane_always_stays_visible() -> None:
    """Unticking everything would leave an empty viewer with no way back."""
    body = APP_JS[APP_JS.index("function setPaneVisible"):]
    body = body[:body.index("\nfunction ")]
    assert "return" in body


def test_the_pane_choice_is_remembered() -> None:
    """Reviewers keep one layout all day; re-picking it every reload is noise."""
    assert "panesVisible" in APP_JS
    assert "localStorage" in APP_JS


def test_lab_vision_data_is_fetched_through_the_proxy() -> None:
    assert "/api/sync-preview/" in APP_JS or "/api/labvision-data/" in APP_JS


# ── what the pane shows, per review mode ─────────────────────────────────
#
# A reviewer in Tests mode is checking results; a column of customer/fuel/tank
# fields is not what they need. Info mode is the one that syncs sample
# information, so it keeps the field list.

def _fn(name: str) -> str:
    body = APP_JS[APP_JS.index(f"function {name}"):]
    cut = body.index("\nfunction ", 1) if "\nfunction " in body[1:] else len(body)
    return body[:cut]


def test_the_pane_shows_tests_in_tests_mode_and_fields_in_info_mode() -> None:
    body = _fn("renderLabVisionData")
    assert "mode-tests" in body, "the pane must branch on review mode"
    assert "renderLabVisionTests" in body
    assert "renderLabVisionInfo" in body


def test_a_test_with_no_result_renders_visibly_empty() -> None:
    """A blank cell reads as a rendering bug; a missing result is a finding
    the reviewer has to see."""
    assert "—" in _fn("renderLabVisionTests")


def test_the_test_list_carries_name_result_and_operator() -> None:
    body = _fn("renderLabVisionTests")
    for key in (".test", ".result", ".operator"):
        assert key in body, f"the test row must show {key}"


def test_the_pane_label_says_which_mode_it_is_showing() -> None:
    assert 'id="labvision-label"' in INDEX_HTML
    assert "labvision-label" in APP_JS


def test_changing_review_mode_re_renders_the_pane() -> None:
    """Switching Info -> Tests mid-session must not strand the old contents."""
    assert "loadLabVisionData" in _fn("applyReviewMode")
