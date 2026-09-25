"""Frontend guards for shared tags, the Review | History panel, the Time
Online link and the "Installing vX" restart message.

Source-level, like tests/test_cc_frontend.py — there is no JS test runner
here. Written before the markup and code they describe.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
APP_CSS = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")


def _body(name: str) -> str:
    """The source of one top-level function (closing brace in column 0)."""
    m = re.search(rf"(?:async )?function {name}\(.*?\)\s*\{{(.*?)\n\}}", APP_JS, re.S)
    assert m, f"could not isolate {name}()"
    return m.group(1)


def _tag(element_id: str) -> str:
    m = re.search(rf'<[^>]*\bid="{element_id}"[^>]*>', INDEX_HTML)
    assert m, f"#{element_id} missing from index.html"
    return m.group(0)


def _sse_case(kind: str) -> str:
    body = _body("handleSSE")
    start = body.index(f'case "{kind}"')
    nxt = body.find("case \"", start + 1)
    return body[start:nxt if nxt != -1 else len(body)]


# ── ◷ Time online link ───────────────────────────────────────────────────

def test_the_activity_link_opens_the_time_online_page_in_a_new_tab() -> None:
    tag = _tag("activity-link")
    assert tag.startswith("<a ")
    assert 'href="/activity"' in tag
    assert 'target="_blank"' in tag
    assert 'rel="noopener"' in tag
    assert 'aria-label="Time online"' in tag and 'title="Time online"' in tag


def test_the_activity_link_sits_immediately_before_the_restart_button() -> None:
    link = INDEX_HTML.index('id="activity-link"')
    restart = INDEX_HTML.index('id="restart-btn"')
    between = INDEX_HTML[link:restart]
    assert link < restart
    # Only the link's own markup (its SVG) lies between them — same container.
    assert "<div" not in between and "</div>" not in between
    assert between.count("</a>") == 1


# ── Review | History segmented control ───────────────────────────────────

@pytest.mark.parametrize("tab, panel", [("right-tab-review", "review-panel-body"),
                                         ("right-tab-history", "history-panel")])
def test_each_right_tab_is_a_tab_controlling_its_panel(tab: str, panel: str) -> None:
    t = _tag(tab)
    assert 'role="tab"' in t
    assert f'aria-controls="{panel}"' in t
    assert 'role="tabpanel"' in _tag(panel)


def test_the_segmented_control_is_the_first_thing_in_the_right_panel() -> None:
    panel = INDEX_HTML[INDEX_HTML.index('<div class="right-panel">'):]
    assert panel.index('role="tablist"') < panel.index('id="review-panel-body"')
    assert panel.index('id="review-panel-body"') < panel.index('id="att-list"')
    assert panel.index('id="info-editor"') < panel.index('id="history-panel"')


def test_the_history_panel_starts_hidden_and_holds_the_list() -> None:
    assert " hidden" in _tag("history-panel")
    history = INDEX_HTML[INDEX_HTML.index('id="history-panel"'):]
    assert 'id="history-list"' in history[:600]


def test_a_hidden_panel_is_really_hidden_despite_its_display_rule() -> None:
    assert re.search(r"#history-panel\[hidden\]", APP_CSS)
    assert re.search(r"#review-panel-body\[hidden\]", APP_CSS)


# ── functions and SSE cases exist ─────────────────────────────────────────

@pytest.mark.parametrize("name", ["renderTags", "setRightTab", "loadHistory", "renderHistory"])
def test_the_function_is_defined(name: str) -> None:
    assert re.search(rf"(?:async )?function {name}\(", APP_JS), f"{name}() missing"


@pytest.mark.parametrize("kind", ["tags", "sample_event"])
def test_the_sse_event_is_handled(kind: str) -> None:
    assert f'case "{kind}"' in _body("handleSSE")


def test_every_mark_request_says_which_mode() -> None:
    starts = [m.start() for m in re.finditer(r'fetch\("/api/mark"', APP_JS)]
    assert starts
    for s in starts:
        assert "mode" in APP_JS[s:s + 400], "a /api/mark request without mode"


# ── tags ──────────────────────────────────────────────────────────────────

def test_the_sample_row_carries_a_tags_slot() -> None:
    body = _body("renderSampleList")
    assert 'class="sample-tags"' in body
    assert "renderTags(s.tags)" in body


def test_tags_are_good_only_labelled_info_and_test_and_escaped() -> None:
    body = _body("renderTags")
    modes = APP_JS[APP_JS.index("const TAG_MODES"):][:200]
    assert "TAG_MODES" in body and '"INFO"' in modes and '"TEST"' in modes
    assert "tag-good" in body
    assert "escapeHtml(" in body or "escapeAttr(" in body


def test_a_tags_event_patches_rows_in_place_without_a_full_rerender() -> None:
    case = _sse_case("tags")
    assert "renderSampleList" not in case
    body = _body("applyTagsUpdate") if "applyTagsUpdate" in case else case
    assert "renderSampleList" not in body
    assert ".sample-tags" in body
    # Every stored copy of the lab id, on every tab.
    assert "state.samples" in body


def test_tag_css_uses_the_good_tokens() -> None:
    block = APP_CSS[APP_CSS.index(".tag-good"):][:600]
    assert "var(--good-soft)" in block and "var(--good)" in block


# ── right tab persistence and history loading ─────────────────────────────

def test_the_right_tab_choice_persists_without_trusting_localstorage() -> None:
    body = _body("setRightTab")
    assert "rightPanelTab" in body
    assert "try" in body and "catch" in body
    assert "loadHistory(" in body
    assert "aria-selected" in body


def test_selecting_a_sample_refreshes_the_history_when_it_is_showing() -> None:
    assert "loadHistory(" in _body("selectSample")


def test_history_ignores_a_response_for_a_sample_no_longer_selected() -> None:
    body = _body("loadHistory")
    assert "/api/sample-history/" in body
    assert "encodeURIComponent(" in body
    assert "state.currentSample" in body
    after_fetch = body[body.index("await "):]
    assert "lab_id !==" in after_fetch or "!== labId" in after_fetch


def test_a_sample_event_reload_is_debounced() -> None:
    case = _sse_case("sample_event")
    body = case + (_body("scheduleHistoryReload") if "scheduleHistoryReload" in case else "")
    assert "clearTimeout(" in body and "setTimeout(" in body
    assert re.search(r"HISTORY_DEBOUNCE_MS|300", body)
    assert re.search(r"HISTORY_DEBOUNCE_MS\s*=\s*300", APP_JS)


def test_only_the_selected_sample_reloads_on_a_sample_event() -> None:
    case = _sse_case("sample_event")
    body = case + (_body("scheduleHistoryReload") if "scheduleHistoryReload" in case else "")
    assert "state.currentSample" in body and "lab_id" in body


# ── history rendering ─────────────────────────────────────────────────────

HISTORY_FUNCS = ["renderHistory", "historyItemHtml", "historySentence", "historyNotes"]


def _history_source() -> str:
    return "\n".join(_body(n) for n in HISTORY_FUNCS)


def test_history_rendering_escapes_what_the_server_sends() -> None:
    src = _history_source()
    assert "escapeHtml(" in _body("renderHistory") or "historyItemHtml(" in _body("renderHistory")
    assert src.count("escapeHtml(") >= 5
    # No event field interpolated raw.
    assert not re.search(r"\$\{\s*ev\.(user|field|before|after|detail|kind)", src)


def test_history_rendering_is_bounded_and_says_so() -> None:
    src = _history_source()
    assert "HISTORY_LIMIT" in src
    assert "Showing the latest" in src
    assert re.search(r"HISTORY_LIMIT\s*=\s*200", APP_JS)


def test_history_groups_by_day() -> None:
    src = _history_source() + _body("historyDayLabel")
    assert '"Today"' in src and '"Yesterday"' in src


@pytest.mark.parametrize("kind", [
    "mark", "unmark", "test_result", "sample_info", "sample_sync", "comments",
    "attachment_deleted", "listing_created", "listing_completed", "external_change",
])
def test_every_history_kind_has_a_sentence(kind: str) -> None:
    assert f'"{kind}"' in _body("historySentence")


def test_external_changes_read_differently() -> None:
    src = _history_source()
    assert "outside COA Reviewer" in src
    assert "sometime between" in src
    assert "first noticed" in src
    for label in ["QBench test result", "QBench sample info", "QBench comments",
                  "LabVision result"]:
        assert label in APP_JS, f"source label {label!r} missing"
    assert "h-avatar--external" in src


@pytest.mark.parametrize("phrase", [
    "(cleared by Regenerate)", "(cleared by a LabVision sync)",
    "(overridden by a newer mark)", "(carried over from before v4)", "(same value)",
    "Earlier value not recorded",
])
def test_history_explains_its_detail_flags(phrase: str) -> None:
    assert phrase in APP_JS


def test_comments_use_a_disclosure() -> None:
    assert "<details" in _history_source()


def test_history_has_empty_and_error_states() -> None:
    assert "No changes recorded for this sample yet." in APP_JS
    assert "History is unavailable right now." in APP_JS


def test_history_css_is_quiet_and_respects_reduced_motion() -> None:
    assert ".h-avatar" in APP_CSS and ".h-item" in APP_CSS
    block = APP_CSS[APP_CSS.index(".h-avatar {"):][:500]
    assert "22px" in block
    rm = APP_CSS[APP_CSS.index("/* ── Shared tags"):]
    assert "prefers-reduced-motion" in rm


# ── restart installs a staged update ──────────────────────────────────────

def test_restart_reads_the_update_and_says_installing() -> None:
    body = _body("triggerServerRestart")
    assert "data.update" in body
    assert "Installing" in APP_JS[APP_JS.index("function triggerServerRestart"):]


def test_waiting_for_an_update_is_bounded_and_falls_back() -> None:
    body = _body("pollForUpdate")
    assert "/api/health" in body
    assert "version" in body
    assert "UPDATE_POLL_MAX" in body
    assert re.search(r"UPDATE_POLL_MAX\s*=\s*90", APP_JS)
    assert "Update not installed" in body
    assert "pollForRestart" in body


# ── follow-ups ───────────────────────────────────────────────────────────

def test_a_bulk_sample_events_message_reloads_the_selected_samples_history() -> None:
    case = _sse_case("sample_events")
    assert "data.lab_ids" in case
    assert "state.currentSample" in case
    assert "scheduleHistoryReload(" in case   # same debounce + History-showing check


def _last_rule(selector: str) -> str:
    """The body of the LAST rule whose selector is exactly `selector` — the
    one that wins when specificity ties."""
    bodies = re.findall(rf"(?:^|\n){re.escape(selector)}\s*\{{([^}}]*)\}}", APP_CSS)
    assert bodies, f"no rule for {selector}"
    return bodies[-1]


@pytest.mark.parametrize("selector", [".sample-item.selected", "body.dark .sample-item.selected"])
def test_the_selected_row_keeps_readable_text(selector: str) -> None:
    """The winning selected-row rule is a pale accent tint; its text must be
    the normal text colour, not --text-inverse (white on pale teal)."""
    body = _last_rule(selector)
    assert "color-mix(" in body, "expected the tinted selection background"
    assert re.search(r"(?<![-\w])color:\s*var\(--text\)", body), (
        f"{selector} leaves the text colour inverse on a pale background")


def test_the_status_icon_is_not_forced_inverse_on_the_selected_row() -> None:
    assert not re.search(r"\.sample-item\.selected \.status-icon\s*\{[^}]*text-inverse",
                         APP_CSS)


def test_the_history_list_clears_the_version_badge() -> None:
    block = APP_CSS[APP_CSS.index(".history-list {"):][:500]
    m = re.search(r"padding:\s*\S+\s+\S+\s+(\d+)px", block)
    assert m and int(m.group(1)) >= 40, "the last history item sits under #app-version"


# ── failed saves (upload finally refused) ─────────────────────────────────

def test_a_failed_save_says_it_did_not_save() -> None:
    sentence = _body("historySentence")
    assert "detail.failed" in sentence
    assert re.search(r"didn(?:'|\u2019|\\u2019)t save", sentence)
    assert 'class="h-failed"' in sentence


def test_the_failure_error_is_escaped_truncated_and_whole_on_hover() -> None:
    notes = _body("historyNotes")
    assert "detail.error" in notes
    assert "HISTORY_ERROR_CHARS" in notes and ".slice(0, HISTORY_ERROR_CHARS)" in notes
    assert 'title="${escapeHtml(err)}"' in notes
    assert "${escapeHtml(short)}" in notes


def test_an_already_saved_edit_is_noted() -> None:
    assert "detail.already_saved" in _body("historyNotes")
    assert "(already saved)" in APP_JS


def test_the_failure_accent_uses_the_warn_token_only() -> None:
    block = APP_CSS[APP_CSS.index(".h-failed {"):][:900]
    assert "var(--warn)" in block
    assert "h-item--failed" in _body("historyItemHtml")


def test_the_bulk_sample_events_handler_ignores_extra_keys() -> None:
    """The backend sends {type, kind, lab_ids}; the handler reads lab_ids only."""
    case = _sse_case("sample_events")
    assert "data.kind" not in case
