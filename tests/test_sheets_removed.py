"""Google Sheets is gone; Command Center is the only double-check store.

The Master Logbook sheet was the source of truth for flagged COAs and for the
Re-review queue. Command Center replaced both. These guards fail if any part
of the spreadsheet path is reintroduced — a half-live logbook that silently
receives some flags but not others is worse than either system alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_PY = (ROOT / "app.py").read_text(encoding="utf-8")
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
INSTALL_SH = (ROOT / "install.sh").read_text(encoding="utf-8")
INSTALL_BAT = (ROOT / "install.bat").read_text(encoding="utf-8")


def test_google_sheet_module_is_deleted() -> None:
    assert not (ROOT / "google_sheet.py").exists(), "google_sheet.py still present"


@pytest.mark.parametrize("marker", [
    "GoogleSheetsManager",
    "from google_sheet",
    "import google_sheet",
    "DoubleCheckRow",
    "append_double_check",
    "read_uncompleted_rows",
    "complete_re_review",
    "find_row_by_lab_id",
])
def test_app_py_has_no_sheets_references(marker: str) -> None:
    assert marker not in APP_PY, f"app.py still references {marker}"


def test_app_py_has_no_check_sheet_route() -> None:
    """/api/check-sheet answered "is this already flagged?" from the sheet.
    /api/cc/lookup answers it from Command Center now."""
    assert "check-sheet" not in APP_PY


@pytest.mark.parametrize("key", ["google_sheets_id", "credentials_path"])
def test_config_no_longer_carries_sheets_settings(key: str) -> None:
    assert key not in APP_PY, f"{key} still in the config defaults"


def test_credentials_json_is_no_longer_looked_up() -> None:
    """The Google service-account key is not needed by anything now."""
    assert "credentials.json" not in APP_PY


@pytest.mark.parametrize("dep", [
    "google-api-python-client", "google-auth", "google-auth-httplib2",
])
def test_install_scripts_dropped_the_google_dependencies(dep: str) -> None:
    assert dep not in INSTALL_SH, f"install.sh still installs {dep}"
    assert dep not in INSTALL_BAT, f"install.bat still installs {dep}"


@pytest.mark.parametrize("marker", [
    "sample.double_check",   # the DoubleCheckRow payload on a record
    "check_outcome",         # sheet column E
    "client_name",           # sheet column G
    "/api/check-sheet",
])
def test_frontend_has_no_double_check_row_rendering(marker: str) -> None:
    """The Re-review panel renders the Command Center listing instead.

    Note this does NOT ban the bare string "double_check": that is also
    LabCore's task-type enum (CC_TASK_TYPES), which the flag form legitimately
    sends. Only the spreadsheet-row fields are gone.
    """
    assert marker not in APP_JS, f"app.js still references {marker}"


def test_labcore_client_is_wired_in_instead() -> None:
    assert "LabCoreClient" in APP_PY
    assert "state.labcore" in APP_PY or "self.labcore" in APP_PY
