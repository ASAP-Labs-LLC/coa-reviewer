"""Lock in the post-consolidation invariants.

Before the consolidation, ``app.py`` and its helper modules reached into
sibling V3/COA reviewer and V3/Past Data Manager folders for state,
config, and credentials. After consolidation the web app is supposed to
be fully self-contained inside its own directory.

These tests fail loudly if anyone reintroduces a cross-folder reference.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_PY = PROJECT_ROOT / "app.py"
LABCORE_CLIENT_PY = PROJECT_ROOT / "labcore_client.py"


def test_app_py_has_no_coa_dir_constant() -> None:
    """``COA_DIR`` used to point at ``V3/COA reviewer/`` — it must be gone."""
    assert "COA_DIR" not in APP_PY.read_text(encoding="utf-8")


def test_app_py_has_no_past_data_manager_reference() -> None:
    assert "Past Data Manager" not in APP_PY.read_text(encoding="utf-8")


def test_app_py_has_no_app_dir_parent_traversal() -> None:
    """``APP_DIR.parent`` was used to reach sibling folders — kill it."""
    assert "APP_DIR.parent" not in APP_PY.read_text(encoding="utf-8")


def test_labcore_client_does_not_walk_above_its_own_dir() -> None:
    """``google_sheet.py`` carried these guards until Command Center replaced
    it and the module was deleted. ``labcore_client.py`` is the module that
    now sits in that slot, so the same cross-folder invariant applies to it."""
    src = LABCORE_CLIENT_PY.read_text(encoding="utf-8")
    assert "Past Data Manager" not in src
    assert "Path(__file__).parent.parent" not in src


def test_app_paths_are_children_of_data_dir() -> None:
    """After import, the state paths must live under ``DATA_DIR``.

    This used to assert ``APP_DIR``, back when code and state shared a
    directory. Deployment split them (see ``tests/test_data_dir.py``), so the
    invariant moved rather than disappeared: state belongs to DATA_DIR, and
    binding it to APP_DIR is now the bug — that is what a release swap
    destroys.

    The original cross-folder invariant is unchanged and still enforced by the
    tests above: nothing may traverse *out* of the tree into a sibling folder.

    Skipped if Flask isn't installed (importing ``app`` requires it).
    """
    pytest.importorskip("flask")
    import app

    for attr in (
        "CONFIG_FILE", "RE_REVIEW_STATE_FILE", "ARCHIVE_DIR",
        "LOGIN_LOG_FILE", "_SECRET_KEY_FILE", "_LOG_FILE",
        "FIELD_SETTINGS_FILE",
    ):
        path: Path = getattr(app, attr)
        assert path.resolve().parent == app.DATA_DIR.resolve(), (
            f"{attr} ({path}) is not inside DATA_DIR ({app.DATA_DIR})"
        )


def test_no_state_path_is_bound_to_app_dir_in_source() -> None:
    """Guard the regression directly in the source.

    An import-time check can't catch this on its own: with ``COA_DATA_DIR``
    unset DATA_DIR *equals* APP_DIR, so a state path wrongly written as
    ``APP_DIR / ...`` would still satisfy the test above on a developer box and
    only fail in production, which is precisely the wrong place to find out.
    """
    src = APP_PY.read_text(encoding="utf-8")
    for name in (
        "web_app_config.json", "re_review_state.json", '"archive"',
        "login.log", '".secret_key"', '"app.log"', '"changelog"',
    ):
        # field_settings.json is deliberately excluded from this loop: APP_DIR /
        # "field_settings.json" is the legitimate *template* path. The test
        # above proves the live one hangs off DATA_DIR.
        assert f"APP_DIR / {name}" not in src, (
            f"{name} is bound to APP_DIR; state must hang off DATA_DIR so a "
            "release swap cannot destroy it"
        )
