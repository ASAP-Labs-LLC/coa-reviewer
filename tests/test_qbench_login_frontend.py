"""Source-level guards for the QBench login UI.

"Enter credentials manually" used to appear only after 90 seconds of a slow
auto-login, and was only wired on the saved-login path. It is now on the boot
splash from the first moment, wired once, and choosing it never stops the
auto-login running behind it — whichever finishes first wins.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
INDEX = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    """Body of a top-level JS function (up to the next top-level definition)."""
    m = re.search(rf"^(?:async )?function {name}\(.*?(?=^(?:async )?function |\Z)",
                  APP_JS, re.S | re.M)
    assert m, f"function {name} not found in app.js"
    return m.group(0)


def _tag(element_id: str) -> str:
    m = re.search(rf'<[^>]*id="{element_id}"[^>]*>', INDEX)
    assert m, f"#{element_id} not in index.html"
    return m.group(0)


def _element(element_id: str) -> str:
    """The element's opening tag through its first closing tag."""
    m = re.search(rf'<(\w+)[^>]*id="{element_id}"[^>]*>.*?</\1>', INDEX, re.S)
    assert m, f"#{element_id} not in index.html"
    return m.group(0)


# ── the boot splash button ───────────────────────────────────────────────

def test_manual_login_button_is_visible_immediately():
    tag = _tag("boot-show-login")
    assert "hidden" not in tag
    splash = INDEX[INDEX.index('id="boot-splash"'):]
    splash = splash[:splash.index("<!-- ── Session Timeout")]
    assert 'id="boot-show-login"' in splash
    slow = _element("boot-slow-notice")
    assert "boot-show-login" not in slow, (
        "the button must not sit inside the 90-second 'slow' notice"
    )


def test_manual_login_label_is_sentence_case():
    assert ">Enter credentials manually</button>" in _element("boot-show-login")
    assert "Enter Credentials Manually" not in INDEX


def test_manual_login_is_wired_once_outside_the_saved_login_branch():
    wiring = [m.start() for m in re.finditer(r'#boot-show-login"\)', APP_JS)]
    assert len(wiring) == 1, "wire #boot-show-login exactly once"
    assert "boot-show-login" not in _fn("initQBenchApp")
    assert "initBootManualLogin" in _fn("setupAppHandlers") or \
        "initBootManualLogin()" in APP_JS[:APP_JS.index("async function initQBenchApp")]


def test_manual_login_hides_the_splash_and_opens_the_login_modal():
    body = _fn("initBootManualLogin")
    assert 'hideModal("boot-splash")' in body
    assert 'showModal("login-modal")' in body


def test_no_ninety_second_gate_on_the_button():
    """The muted "taking longer" note may still appear after 90 s, on its own;
    nothing waits on a timer before offering the manual login."""
    for m in re.finditer(r"setTimeout\((.*?)\}, SLOW_LOGIN_MS\)", APP_JS, re.S):
        assert "boot-show-login" not in m.group(1)
        assert "boot-slow-notice" in m.group(1)


# ── auto-login keeps going behind the manual form ────────────────────────

def test_auto_login_success_closes_the_manual_login_form():
    sse = _fn("handleSSE")
    case = sse[sse.index('case "auto_login_done"'):]
    case = case[:case.index("break;")]
    ok_branch = case[case.index("if (data.ok)"):case.index("} else {")]
    assert 'hideModal("login-modal")' in ok_branch


def test_the_config_poll_survives_opening_the_manual_form_and_closes_it():
    poll = _fn("startAutoLoginPoll")
    assert 'hideModal("login-modal")' in poll
    # Stops on the app being shown, not on the splash being hidden — the
    # manual button hides the splash while auto-login is still running.
    assert '#boot-splash").classList.contains("hidden")' not in poll
    assert "AUTO_LOGIN_POLL_MAX" in poll                       # bounded


# ── forget saved login ───────────────────────────────────────────────────

def test_forget_link_exists_and_starts_hidden():
    tag = _tag("login-forget")
    assert "hidden" in tag
    assert "Forget saved login" in _element("login-forget")
    modal = INDEX[INDEX.index('id="login-modal"'):]
    assert modal.index('id="login-forget"') < modal.index("</div>\n    </div>")


def test_forget_link_is_shown_only_with_a_saved_login():
    body = _fn("initQBenchApp")
    assert "setSavedLoginUi(cfg.has_password)" in body
    ui = _fn("setSavedLoginUi")
    assert "login-forget" in ui and "placeholder" in ui


def test_forget_link_calls_the_route():
    body = _fn("forgetSavedLogin")
    assert '"/api/qbench-login/forget"' in body
    assert 'method: "POST"' in body
    assert "setSavedLoginUi(false)" in body
    assert "login-forget" in APP_JS[APP_JS.index("function setupAppHandlers"):]


# ── a login that couldn't be remembered ──────────────────────────────────

def test_unsaved_login_note_is_plain_english_and_conditional():
    body = _fn("handleLogin")
    assert "data.saved === false" in body and "save &&" in body
    assert "couldn’t remember your login" in APP_JS or \
        "couldn't remember your login" in APP_JS
    assert 'id="login-note"' in INDEX


def test_ui_never_mentions_storage_details():
    for word in ("APPDATA", "DPAPI", "qbench_login.json", "coa-qbench-login"):
        assert word not in APP_JS and word not in INDEX, word
