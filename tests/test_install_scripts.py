"""Invariants for the install.bat / install.sh setup scripts.

Three files now describe the same dependency set and must stay in lock-step:
``install.bat`` and ``install.sh`` (the human "set up my machine" path) and
``requirements.txt`` (the path the updater builds a release venv from). A
dependency present in one and missing from another means a release that
installs cleanly and then fails at import — which the updater would correctly
roll back, having wasted a deploy on a packaging mistake.

``requirements.txt`` is pinned with ``==`` while the install scripts float
with ``>=``. That is deliberate, not drift: a person setting up a laptop wants
current packages, whereas a deploy must install exactly what was tested, or a
health check can fail for reasons that have nothing to do with the code
change.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTALL_BAT = PROJECT_ROOT / "install.bat"
INSTALL_SH = PROJECT_ROOT / "install.sh"
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
REQUIREMENTS_DEV = PROJECT_ROOT / "requirements-dev.txt"


def _requirement_names(path: Path) -> dict[str, str]:
    """``{lowercased package name: pinned version}`` from a requirements file."""
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9._-]+)\s*==\s*(.+)$", line)
        assert m, f"{path.name}: {line!r} is not an == pin"
        found[m.group(1).lower().replace("_", "-")] = m.group(2).strip()
    return found

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
    """``install.sh`` must be executable where it is actually run.

    NTFS carries no POSIX exec bit, so on Windows the filesystem always reports
    the file as non-executable no matter what the repository says. What matters
    is the mode git records — that is what a POSIX checkout materialises — so on
    Windows we ask git instead of the filesystem. Asserting ``st_mode`` there
    tested the platform, not the repo.
    """
    import stat
    import subprocess
    import sys

    assert INSTALL_SH.is_file()

    if sys.platform == "win32":
        out = subprocess.run(
            ["git", "ls-files", "-s", "--", "install.sh"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
        )
        if out.returncode != 0 or not out.stdout.strip():
            pytest.skip("not a git checkout; cannot verify the recorded mode")
        recorded = out.stdout.split()[0]
        assert recorded == "100755", (
            f"install.sh is recorded in git as {recorded}, not 100755 — a POSIX "
            "checkout would not be executable. Fix with: "
            "git update-index --chmod=+x install.sh"
        )
        return

    mode = INSTALL_SH.stat().st_mode
    assert mode & stat.S_IXUSR, "install.sh must be executable (chmod +x)"


def test_install_sh_has_bash_shebang() -> None:
    first_line = INSTALL_SH.read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith("#!") and "sh" in first_line, (
        f"install.sh first line should be a shell shebang, got: {first_line!r}"
    )


# ── requirements.txt: what the updater builds a release venv from ───────────

def test_requirements_txt_exists() -> None:
    assert REQUIREMENTS.is_file(), (
        "requirements.txt is missing; the updater has nothing to build a "
        "release venv from"
    )


def test_requirements_dev_exists_and_includes_runtime() -> None:
    assert REQUIREMENTS_DEV.is_file(), "requirements-dev.txt is missing"
    assert "-r requirements.txt" in REQUIREMENTS_DEV.read_text(encoding="utf-8"), (
        "requirements-dev.txt must include the runtime set rather than "
        "restating it, or the two will drift"
    )


@pytest.mark.parametrize("package", [p for p in REQUIRED_PACKAGES if p != "pytest"])
def test_runtime_package_is_pinned_in_requirements(package: str) -> None:
    """Every runtime dependency appears in requirements.txt, pinned."""
    assert package in _requirement_names(REQUIREMENTS), (
        f"{package} is installed by the setup scripts but missing from "
        "requirements.txt, so a release venv would not have it"
    )


def test_pytest_is_not_a_runtime_dependency() -> None:
    """pytest belongs to the dev set. A release venv should not carry the test
    runner: it is weight on every deploy and it is not imported by the app."""
    assert "pytest" not in _requirement_names(REQUIREMENTS)
    assert "pytest" in _requirement_names(REQUIREMENTS_DEV)


def test_every_requirement_is_exactly_pinned() -> None:
    """No floating specifiers. _requirement_names asserts the == form, so this
    just proves the file is non-empty and parseable."""
    pins = _requirement_names(REQUIREMENTS)
    assert len(pins) >= len(REQUIRED_PACKAGES) - 1


def test_requirements_does_not_drift_below_install_script_floors() -> None:
    """A pin must satisfy the floor the install scripts advertise.

    Catches the case where requirements.txt is pinned to something older than
    ``install.bat`` claims to require — the deploy would then install a version
    the project has already declared too old.
    """
    bat = INSTALL_BAT.read_text(encoding="utf-8")
    pins = _requirement_names(REQUIREMENTS)

    for name, floor in re.findall(r'"([A-Za-z0-9._-]+)>=([0-9][^"]*)"', bat):
        key = name.lower().replace("_", "-")
        if key == "pytest" or key not in pins:
            continue
        pinned = pins[key]

        def parts(v: str) -> list[int]:
            out = []
            for chunk in re.split(r"[.\-+]", v):
                if chunk.isdigit():
                    out.append(int(chunk))
                else:
                    break
            return out

        assert parts(pinned) >= parts(floor), (
            f"requirements.txt pins {name}=={pinned}, below the "
            f"{name}>={floor} floor declared in install.bat"
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
