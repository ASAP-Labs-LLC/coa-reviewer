"""Invariants for the install.bat / install.sh setup scripts.

These scripts replace the old ``requirements.txt`` workflow. Two scripts
must stay in lock-step: every dependency, plus the Playwright chromium
install and pytest, has to appear in both.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTALL_BAT = PROJECT_ROOT / "install.bat"
INSTALL_SH = PROJECT_ROOT / "install.sh"

# Packages required by app.py + labcore_client.py + qbench_client.py +
# sif_test_app.py + Run.pyw (PyPI names, lower-cased for comparison).
REQUIRED_PACKAGES = [
    "flask",
    "pyjwt",
    "requests",
    "playwright",
    "pymupdf",
    "pyzbar",
    "pystray",
    "pillow",
    "pytest",  # for tests/
]


def test_install_bat_exists() -> None:
    assert INSTALL_BAT.is_file(), "install.bat is missing"


def test_install_sh_exists() -> None:
    assert INSTALL_SH.is_file(), "install.sh is missing"


def test_install_sh_is_executable() -> None:
    import stat

    assert INSTALL_SH.is_file()
    mode = INSTALL_SH.stat().st_mode
    assert mode & stat.S_IXUSR, "install.sh must be executable (chmod +x)"


def test_install_sh_has_bash_shebang() -> None:
    first_line = INSTALL_SH.read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith("#!") and "sh" in first_line, (
        f"install.sh first line should be a shell shebang, got: {first_line!r}"
    )


def test_install_sh_aborts_on_error() -> None:
    """Without ``set -e`` a failed pip step would leave the user with a half-installed env."""
    content = INSTALL_SH.read_text(encoding="utf-8")
    assert "set -e" in content, "install.sh should `set -e` so a failed step aborts setup"


@pytest.mark.parametrize("package", REQUIRED_PACKAGES)
def test_install_bat_mentions_package(package: str) -> None:
    content = INSTALL_BAT.read_text(encoding="utf-8").lower()
    assert package in content, f"install.bat is missing dependency '{package}'"


@pytest.mark.parametrize("package", REQUIRED_PACKAGES)
def test_install_sh_mentions_package(package: str) -> None:
    content = INSTALL_SH.read_text(encoding="utf-8").lower()
    assert package in content, f"install.sh is missing dependency '{package}'"


def test_install_bat_installs_playwright_chromium() -> None:
    assert "playwright install chromium" in INSTALL_BAT.read_text(encoding="utf-8").lower()


def test_install_sh_installs_playwright_chromium() -> None:
    assert "playwright install chromium" in INSTALL_SH.read_text(encoding="utf-8").lower()


# ── install.bat: Visual C++ runtime for pyzbar barcode detection ──────────


def test_install_bat_installs_vc2013_redistributable() -> None:
    """pyzbar's bundled libzbar-64.dll depends on msvcr120.dll from the
    Visual C++ 2013 runtime; on a fresh Windows machine barcode scanning
    fails until the redistributable is installed."""
    content = INSTALL_BAT.read_text(encoding="utf-8").lower()
    assert "vcredist" in content, (
        "install.bat must install the Visual C++ 2013 redistributable for pyzbar"
    )


def test_install_bat_skips_vc_redist_when_already_present() -> None:
    """Re-running install.bat shouldn't re-download the redistributable."""
    content = INSTALL_BAT.read_text(encoding="utf-8").lower()
    assert "msvcr120.dll" in content, (
        "install.bat should check for msvcr120.dll before downloading vcredist"
    )


# ── install.bat: observability (double-click runs are otherwise silent) ───


def test_install_bat_writes_install_log() -> None:
    """install.bat is usually launched by double-click; without a log file
    there is no way to tell afterwards whether it ran or why it failed."""
    content = INSTALL_BAT.read_text(encoding="utf-8").lower()
    assert "install.log" in content, (
        "install.bat should record its output in install.log"
    )


def test_install_bat_launches_app_on_success() -> None:
    """A successful install should end by starting the app (Run.pyw tray
    launcher) and closing the console, so setup flows straight into use."""
    import re

    content = INSTALL_BAT.read_text(encoding="utf-8").lower()
    assert re.search(r'start\s+""[^\n]*run\.pyw', content), (
        "install.bat should end with `start \"\" ...Run.pyw` so the app "
        "launches detached and the console can close"
    )


def test_install_bat_pauses_only_on_error() -> None:
    """Failures must stay readable (pause in the :error block), but the
    success path should close on its own after launching the app."""
    content = INSTALL_BAT.read_text(encoding="utf-8").lower()
    assert content.count("pause") == 1, (
        "exactly one pause — in the error path; success closes itself"
    )
    assert content.find("pause") > content.find("\n:error"), (
        "the pause must live in the :error block, not the success path"
    )


# ── install.sh: venv + launch behavior (Mac/Linux one-shot bootstrap) ──────


def test_install_sh_creates_venv() -> None:
    """install.sh must create a Python virtual environment so deps don't pollute the system Python."""
    content = INSTALL_SH.read_text(encoding="utf-8")
    assert "-m venv" in content, "install.sh should run `python -m venv` to create a virtual environment"


def test_install_sh_installs_into_venv() -> None:
    """pip/playwright commands must run via the venv, not the system interpreter."""
    content = INSTALL_SH.read_text(encoding="utf-8")
    assert ".venv/bin/" in content or "source .venv/bin/activate" in content, (
        "install.sh should invoke the venv (.venv/bin/... or `source .venv/bin/activate`)"
    )


def test_install_sh_launches_app() -> None:
    """install.sh should end by launching the Flask app."""
    content = INSTALL_SH.read_text(encoding="utf-8")
    assert "app.py" in content, "install.sh should launch app.py once setup completes"


def test_install_sh_venv_dir_name_is_dot_venv() -> None:
    """Match the existing `.venv/` convention used elsewhere in the repo."""
    content = INSTALL_SH.read_text(encoding="utf-8")
    assert ".venv" in content, "install.sh should standardize on the `.venv` directory name"
