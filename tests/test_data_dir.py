"""``DATA_DIR`` — state lives outside the code directory.

Why these run in a subprocess
-----------------------------
``DATA_DIR`` and every path derived from it are resolved at **import time**
(``ARCHIVE_DIR.mkdir()`` runs at module scope), so they cannot be re-resolved
by monkeypatching an already-imported ``app``. Each test therefore imports
``app`` fresh in a child process with a specific ``COA_DATA_DIR`` and reports
what the module actually computed. That is the behaviour the deployment
depends on: a release directory that is swapped underneath the app must never
have been the place its state lived.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

pytest.importorskip("flask")


def _probe(data_dir: str | None, extra_env: dict[str, str] | None = None) -> dict:
    """Import ``app`` in a child process and report its resolved paths."""
    env = dict(os.environ)
    env["QBENCH_STORE_PATH"] = os.environ["QBENCH_STORE_PATH"]
    if data_dir is None:
        env.pop("COA_DATA_DIR", None)
    else:
        env["COA_DATA_DIR"] = str(data_dir)
    if extra_env:
        env.update(extra_env)

    snippet = (
        "import json, sys; sys.path.insert(0, r'%s'); import app; "
        "print('<<<' + json.dumps({"
        "'DATA_DIR': str(app.DATA_DIR),"
        "'APP_DIR': str(app.APP_DIR),"
        "'CONFIG_FILE': str(app.CONFIG_FILE),"
        "'RE_REVIEW_STATE_FILE': str(app.RE_REVIEW_STATE_FILE),"
        "'ARCHIVE_DIR': str(app.ARCHIVE_DIR),"
        "'LOGIN_LOG_FILE': str(app.LOGIN_LOG_FILE),"
        "'SECRET_KEY_FILE': str(app._SECRET_KEY_FILE),"
        "'LOG_FILE': str(app._LOG_FILE),"
        "'CHANGE_LOG_DIR': str(app.state.change_log.directory),"
        "'FIELD_SETTINGS_FILE': str(app.FIELD_SETTINGS_FILE),"
        "}) + '>>>')" % PROJECT_ROOT
    )
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT), timeout=180,
    )
    if "<<<" not in proc.stdout:
        raise AssertionError(
            f"probe failed (rc={proc.returncode})\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return json.loads(proc.stdout.split("<<<", 1)[1].split(">>>", 1)[0])


# ── resolution ──────────────────────────────────────────────────────────────

def test_data_dir_follows_env_var(tmp_path):
    """``COA_DATA_DIR`` wins when set."""
    got = _probe(tmp_path)
    assert Path(got["DATA_DIR"]) == tmp_path.resolve()


def test_data_dir_falls_back_to_app_dir():
    """Unset ``COA_DATA_DIR`` keeps the old behaviour, so nothing breaks for
    anyone still running off the share."""
    got = _probe(None)
    assert Path(got["DATA_DIR"]) == Path(got["APP_DIR"])


# ── every state path follows DATA_DIR ───────────────────────────────────────

@pytest.mark.parametrize(
    "attr,name",
    [
        ("CONFIG_FILE", "web_app_config.json"),
        ("RE_REVIEW_STATE_FILE", "re_review_state.json"),
        ("ARCHIVE_DIR", "archive"),
        ("LOGIN_LOG_FILE", "login.log"),
        ("SECRET_KEY_FILE", ".secret_key"),
        ("LOG_FILE", "app.log"),
        ("CHANGE_LOG_DIR", "changelog"),
        ("FIELD_SETTINGS_FILE", "field_settings.json"),
    ],
)
def test_state_path_lives_under_data_dir(tmp_path, attr, name):
    got = _probe(tmp_path)
    path = Path(got[attr])
    assert path.parent == tmp_path.resolve(), f"{attr} -> {path}, expected under {tmp_path}"
    assert path.name == name


def test_code_dir_stays_clean(tmp_path):
    """The point of the exercise: with ``COA_DATA_DIR`` set, importing the app
    must not create ``archive/`` (or anything else) beside the source."""
    got = _probe(tmp_path)
    assert (tmp_path / "archive").is_dir(), "archive/ was not created under DATA_DIR"
    assert Path(got["ARCHIVE_DIR"]).resolve() != (Path(got["APP_DIR"]) / "archive").resolve()


# ── first-boot config seeding ───────────────────────────────────────────────

def test_first_boot_seeds_config_from_template(tmp_path):
    """An empty data dir gets a config copied from the shipped default."""
    _probe(tmp_path)
    cfg = tmp_path / "web_app_config.json"
    assert cfg.is_file(), "first boot did not create web_app_config.json"
    template = json.loads((PROJECT_ROOT / "web_app_config.default.json").read_text("utf-8"))
    assert set(json.loads(cfg.read_text("utf-8"))) >= set(template)


def test_first_boot_does_not_overwrite_existing_config(tmp_path):
    """A real config is never clobbered by the template. This is the one that
    would cost live credentials if it regressed."""
    cfg = tmp_path / "web_app_config.json"
    original = {
        "qbench_username": "real-user",
        "qbench_password": "real-password",
        "report_config_id": "18",
        "labcore_url": "https://labvision.asaplabs.net",
    }
    cfg.write_text(json.dumps(original), encoding="utf-8")

    _probe(tmp_path)

    assert json.loads(cfg.read_text("utf-8")) == original


def test_field_settings_are_seeded_then_never_overwritten(tmp_path):
    """A reviewer hiding a column must survive a deploy.

    This is written at runtime by /api/field-settings, so the shipped copy is
    a first-boot template only — re-seeding over it would silently revert the
    reviewer's choice with no error anywhere.
    """
    _probe(tmp_path)
    settings = tmp_path / "field_settings.json"
    assert settings.is_file(), "field settings were not seeded into DATA_DIR"

    customised = {"sample_info_hidden": ["tags"], "show_extra_fields": True}
    settings.write_text(json.dumps(customised), encoding="utf-8")

    _probe(tmp_path)

    assert json.loads(settings.read_text("utf-8")) == customised


def test_existing_secret_key_survives(tmp_path):
    """``.secret_key`` in DATA_DIR is reused, so session cookies survive a
    release swap rather than logging every reviewer out."""
    key = "a" * 64
    (tmp_path / ".secret_key").write_text(key, encoding="utf-8")
    got = _probe(tmp_path)
    assert Path(got["SECRET_KEY_FILE"]) == (tmp_path / ".secret_key").resolve()
    assert (tmp_path / ".secret_key").read_text("utf-8").strip() == key
