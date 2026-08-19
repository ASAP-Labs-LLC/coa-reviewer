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


def test_app_paths_are_children_of_app_dir() -> None:
    """After import, the path constants must live under ``APP_DIR``.

    Skipped if Flask isn't installed (importing ``app`` requires it).
    """
    pytest.importorskip("flask")
    import app

    for attr in ("CONFIG_FILE", "RE_REVIEW_STATE_FILE", "ARCHIVE_DIR"):
        path: Path = getattr(app, attr)
        assert app.APP_DIR in path.resolve().parents or path.resolve() == app.APP_DIR, (
            f"{attr} ({path}) is not inside APP_DIR ({app.APP_DIR})"
        )
