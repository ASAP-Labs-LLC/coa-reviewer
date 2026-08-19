"""TDD spec: shared field-visibility settings for the Sample Info editor.

Reviewers pick which Sample Info (SIF) fields are visible via a gear
button + checkbox modal. The choice applies to ALL reviewers (one shared
config) and is hard-saved to field_settings.json in the project root so
it survives server restarts and logins (2026-07-10 request).
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "app.py"
APP_JS = ROOT / "static" / "js" / "app.js"
INDEX_HTML = ROOT / "templates" / "index.html"


@pytest.fixture
def isolated_settings_path(tmp_path, monkeypatch):
    """Redirect app.FIELD_SETTINGS_FILE into tmp_path so tests never touch
    the real settings file."""
    import app

    p = tmp_path / "field_settings.json"
    monkeypatch.setattr(app, "FIELD_SETTINGS_FILE", p)
    return p


def test_defaults_when_file_missing(isolated_settings_path) -> None:
    """No settings file yet → everything visible."""
    import app

    assert app.load_field_settings() == {
        "sample_info_hidden": [],
        "show_extra_fields": True,
    }


def test_save_then_load_round_trips(isolated_settings_path) -> None:
    import app

    app.save_field_settings(
        {"sample_info_hidden": ["fw", "generator"], "show_extra_fields": False}
    )
    loaded = app.load_field_settings()
    assert sorted(loaded["sample_info_hidden"]) == ["fw", "generator"]
    assert loaded["show_extra_fields"] is False
    assert isolated_settings_path.exists(), "settings must be hard-saved to disk"


def test_corrupt_file_falls_back_to_defaults(isolated_settings_path) -> None:
    """A truncated/hand-mangled JSON file must not take the app down —
    fall back to everything-visible."""
    import app

    isolated_settings_path.write_text("{not valid json", encoding="utf-8")
    assert app.load_field_settings() == {
        "sample_info_hidden": [],
        "show_extra_fields": True,
    }


def test_api_route_exists() -> None:
    """GET returns the shared settings; PUT saves them."""
    src = APP_PY.read_text(encoding="utf-8")
    assert "/api/field-settings" in src, "field-settings route missing"


def test_frontend_has_gear_button_and_modal() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="field-settings-btn"' in html, "gear button missing from info editor header"
    assert 'id="field-settings-modal"' in html, "field settings modal missing"


def test_app_js_fetches_and_applies_settings() -> None:
    js = APP_JS.read_text(encoding="utf-8")
    assert "/api/field-settings" in js, "app.js must fetch the shared settings"
    assert "sample_info_hidden" in js, "app.js must filter by sample_info_hidden"
