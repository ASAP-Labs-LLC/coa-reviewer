"""The Restart Application button is reachable from the mode picker.

The picker ("What are you checking today?": Sample Info (SIF) or Test
Results) is the screen a reviewer is looking at when QBench will not log in
or the app has wedged after a deploy. The only restart button lived in the
main toolbar, which is hidden until a mode is chosen and the app has
loaded, so the reviewer had nothing to press but F5.

Both buttons open the same confirm dialog and go busy together while the
restart runs. Source-level, like the other frontend guards.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
APP_CSS = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")


def _picker() -> str:
    start = INDEX_HTML.index('id="review-mode-modal"')
    end = INDEX_HTML.index("Boot Splash", start)
    return INDEX_HTML[start:end]


def _fn(name: str) -> str:
    body = APP_JS[APP_JS.index(f"function {name}"):]
    return body[:body.index("\nfunction ", 1)]


def test_the_mode_picker_has_a_restart_button() -> None:
    assert 'id="review-mode-restart-btn"' in _picker()


def test_it_is_a_plain_button_that_says_what_it_does() -> None:
    m = re.search(r'<button[^>]*id="review-mode-restart-btn"[^>]*>(.*?)</button>', _picker(), re.S)
    assert m, "the restart control must be a <button>"
    assert 'type="button"' in m.group(0), "never a form submit"
    assert "Restart" in m.group(1), "an icon alone does not say what it does here"


def test_it_is_styled_like_the_toolbar_restart_button() -> None:
    m = re.search(r'<button[^>]*id="review-mode-restart-btn"[^>]*>', _picker())
    assert m and "btn-restart" in m.group(0)


def test_it_sits_under_the_two_choices_not_beside_them() -> None:
    """A third pill would read as a third kind of review."""
    picker = _picker()
    pills_end = picker.index("</div>", picker.index('class="review-mode-pills"'))
    assert picker.index('id="review-mode-restart-btn"') > pills_end
    assert 'class="review-mode-footer"' in picker
    assert ".review-mode-footer" in APP_CSS


def test_the_toolbar_restart_button_is_still_there() -> None:
    assert 'id="restart-btn"' in INDEX_HTML


def test_both_buttons_open_the_same_confirm_dialog() -> None:
    body = _fn("restartButtons")
    assert "#restart-btn" in body and "#review-mode-restart-btn" in body
    assert 'restartButtons()) btn.addEventListener("click", handleRestartServer)' in APP_JS


def test_a_restart_in_progress_shows_on_both_buttons() -> None:
    assert "setRestartButtonsBusy(true)" in _fn("triggerServerRestart")
    assert "setRestartButtonsBusy(false)" in _fn("pollForRestart")
    for name in ("triggerServerRestart", "pollForRestart"):
        assert '$("#restart-btn")' not in _fn(name), f"{name} still updates only the toolbar button"


def test_the_idle_label_comes_back_after_a_failed_restart() -> None:
    """The toolbar button is an icon and the picker's is a sentence; the
    old code restored a third, hardcoded label to whichever it touched."""
    body = _fn("setRestartButtonsBusy")
    assert "idleLabel" in body
    assert '"Restarting...' in body
