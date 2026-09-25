"""Frontend guards for the Time Online page (/activity).

Source-level, matching this project's existing frontend test style — there is
no JS test runner here. They pin the contract the page must keep: it shares
the review screen's tokens and theme, it only polls while someone is actually
looking at today, it handles a missing sign-in, and every user-provided
string is escaped before it reaches innerHTML.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "templates" / "activity.html"
JS = ROOT / "static" / "js" / "activity.js"
CSS = ROOT / "static" / "css" / "activity.css"


def _read(p: Path) -> str:
    assert p.exists(), f"{p.relative_to(ROOT)} is missing"
    return p.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html() -> str:
    return _read(TEMPLATE)


@pytest.fixture(scope="module")
def js() -> str:
    return _read(JS)


@pytest.fixture(scope="module")
def css() -> str:
    return _read(CSS)


# ── Template ─────────────────────────────────────────────────────────────

def test_template_links_shared_tokens_and_page_assets(html: str) -> None:
    assert "/static/css/app.css" in html, "app.css carries the colour tokens"
    assert "/static/css/activity.css" in html
    assert "/static/js/activity.js" in html


def test_template_has_root_and_version(html: str) -> None:
    assert 'id="activity-root"' in html
    assert 'id="app-version"' in html
    # Same filter as index.html so both pages print the version identically.
    assert 'app_version|default("dev", true)|replace("v", "", 1)' in html


@pytest.mark.parametrize("mode", ["system", "light", "dark"])
def test_template_has_theme_pips(html: str, mode: str) -> None:
    assert 'class="theme-pips"' in html
    assert f'data-theme="{mode}"' in html


def test_template_links_back_to_review(html: str) -> None:
    assert 'href="/"' in html


def test_template_does_not_load_the_review_app(html: str) -> None:
    assert "/static/js/app.js" not in html


# ── Polling ──────────────────────────────────────────────────────────────

def test_polls_every_two_minutes(js: str) -> None:
    assert re.search(r"setInterval\([^;]*120000\)", js) or (
        "120000" in js and "setInterval(" in js
    ), "poll interval must be 120000 ms"


def test_poll_is_gated_on_visibility_and_today(js: str) -> None:
    assert 'document.visibilityState === "visible"' in js
    assert "isToday" in js
    # The gate function must check both conditions together.
    m = re.search(r"function shouldPoll\(\)\s*\{(.*?)\n\}", js, re.S)
    assert m, "shouldPoll() gate missing"
    body = m.group(1)
    assert "visibilityState" in body and "isToday" in body


def test_listens_to_visibilitychange(js: str) -> None:
    assert 'addEventListener("visibilitychange"' in js


def test_stops_polling_with_clearInterval(js: str) -> None:
    assert "clearInterval(" in js


# ── Auth / errors ────────────────────────────────────────────────────────

def test_handles_401_with_sign_in_link(js: str) -> None:
    assert "401" in js
    assert "Sign in on the review screen first" in js
    assert 'href="/"' in js


def test_empty_and_first_run_copy(js: str) -> None:
    assert "Nobody was signed in on this day." in js
    assert "Time online is recorded from v4.0.0 on." in js


def test_truncated_note(js: str) -> None:
    assert "truncated" in js
    assert "isn't shown (too many records)" in js


def test_uses_activity_api(js: str) -> None:
    assert "/api/activity?date=" in js
    assert "/api/activity/range" in js


# ── Kinds ────────────────────────────────────────────────────────────────

KIND_LABELS = {
    "mark": "marks",
    "unmark": "cleared marks",
    "test_result": "test edits",
    "sample_info": "sample-info edits",
    "sample_sync": "LabVision syncs",
    "comments": "comment edits",
    "attachment_deleted": "attachments deleted",
    "listing_created": "listings filed",
    "listing_completed": "listings completed",
    "external_change": "outside changes",
}


@pytest.mark.parametrize("kind,label", sorted(KIND_LABELS.items()))
def test_maps_every_event_kind(js: str, kind: str, label: str) -> None:
    assert re.search(rf"\b{kind}\s*:", js), f"{kind} not mapped"
    assert f'"{label}"' in js, f"label {label!r} missing"


# ── Escaping ─────────────────────────────────────────────────────────────

def test_defines_local_escapeHtml(js: str) -> None:
    assert "function escapeHtml(" in js


def test_every_user_name_interpolation_is_escaped(js: str) -> None:
    raw = re.findall(r"\$\{\s*[\w.]*\buser\b\s*\}", js)
    assert not raw, f"unescaped user interpolation(s): {raw}"
    raw_name = re.findall(r"\$\{\s*name\s*\}", js)
    assert not raw_name, "interpolate names through escapeHtml()"
    assert re.search(r"escapeHtml\([\w.]*user\)", js)


def test_localstorage_access_is_guarded(js: str) -> None:
    # Every localStorage touch lives inside the two guarded helpers.
    uses = [m.start() for m in re.finditer(r"localStorage\.", js)]
    assert uses, "expected theme/debug localStorage use"
    for pos in uses:
        before = js[max(0, pos - 200):pos]
        assert "try" in before, "localStorage access outside try/catch"


def test_debug_logging_behind_flag(js: str) -> None:
    assert "activityDebug" in js
    assert 'console.debug("[activity]"' in js


def test_theme_logic_matches_review_screen(js: str) -> None:
    assert "function applyTheme(" in js
    assert '"theme"' in js
    assert "prefers-color-scheme: dark" in js
    assert 'classList.toggle("dark"' in js


# ── CSS ──────────────────────────────────────────────────────────────────

def test_css_respects_reduced_motion(css: str) -> None:
    assert "prefers-reduced-motion" in css


def test_css_reuses_tokens_not_a_new_palette(css: str) -> None:
    assert "var(--text)" in css and "var(--good)" in css
    assert "--bg:" not in css and "--text:" not in css
