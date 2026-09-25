"""The app's half of "Restart installs a staged update": read staged.json,
claim/result files for a switch request. See restart_update.py.

The updater is the other half (deploy/updater/updater.py); its acceptance of
these files is covered in tests/test_updater.py.
"""
from __future__ import annotations

import json

import pytest

from restart_update import (ACCEPTED_FILE, MARKER_FILE, REFUSED_FILE,
                            clear_switch_files, is_upgrade, marker_present,
                            read_switch_outcome, staged_update,
                            withdraw_switch_request, write_switch_request)


def staged(tmp_path, **kw):
    (tmp_path / "staged.json").write_text(json.dumps(kw), encoding="utf-8")


# ── is_upgrade ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("staged_tag,current,expected", [
    ("v4.0.0", "v3.5.0", True),
    ("v3.5.0", "v3.5.0", False),
    ("v3.4.9", "v3.5.0", False),
    ("4.0.0", "3.5.0", True),          # no leading v on either side
    ("v4.0.0", "dev", False),          # current unparsable
    ("dev", "v3.5.0", False),          # staged unparsable
    ("v4.0.0-rc1", "v3.5.0", False),   # pre-release suffix: unparsable
    ("", "v3.5.0", False),
    ("v4.0.0", "", False),
])
def test_is_upgrade(staged_tag, current, expected):
    assert is_upgrade(staged_tag, current) is expected


# ── staged_update ────────────────────────────────────────────────────────

def test_no_staged_file(tmp_path):
    assert staged_update(tmp_path, "v3.5.0") is None


def test_healthy_newer_tag(tmp_path):
    staged(tmp_path, tag="v4.0.0", healthy=True)
    assert staged_update(tmp_path, "v3.5.0") == "v4.0.0"


def test_same_tag_is_not_an_update(tmp_path):
    staged(tmp_path, tag="v3.5.0", healthy=True)
    assert staged_update(tmp_path, "v3.5.0") is None


def test_older_staged_tag_is_not_an_update(tmp_path):
    staged(tmp_path, tag="v3.4.0", healthy=True)
    assert staged_update(tmp_path, "v3.5.0") is None


def test_unhealthy_is_ignored(tmp_path):
    staged(tmp_path, tag="v4.0.0", healthy=False)
    assert staged_update(tmp_path, "v3.5.0") is None


def test_healthy_as_string_is_not_truthy_enough(tmp_path):
    """``healthy`` must be the JSON boolean ``true``, not the string "true" —
    a loose truthiness check would also accept "false"."""
    staged(tmp_path, tag="v4.0.0", healthy="true")
    assert staged_update(tmp_path, "v3.5.0") is None


def test_dev_build_never_switches(tmp_path):
    staged(tmp_path, tag="v4.0.0", healthy=True)
    assert staged_update(tmp_path, "dev") is None


def test_unparsable_staged_tag_never_switches(tmp_path):
    staged(tmp_path, tag="not-a-version", healthy=True)
    assert staged_update(tmp_path, "v3.5.0") is None


def test_corrupt_staged(tmp_path):
    (tmp_path / "staged.json").write_text("{nope", encoding="utf-8")
    assert staged_update(tmp_path, "v3.5.0") is None


# ── write_switch_request ─────────────────────────────────────────────────

def test_write_and_withdraw(tmp_path):
    assert write_switch_request(tmp_path, "v4.0.0", by="Dana P", now=5.0)
    doc = json.loads((tmp_path / MARKER_FILE).read_text())
    assert doc == {"tag": "v4.0.0", "by": "Dana P", "at": 5.0}
    assert withdraw_switch_request(tmp_path) is True
    assert withdraw_switch_request(tmp_path) is False


def test_write_refuses_when_a_marker_is_already_pending(tmp_path, caplog):
    caplog.set_level("INFO", logger="coa.restart")
    assert write_switch_request(tmp_path, "v4.0.0", by="Dana P", now=5.0)
    assert write_switch_request(tmp_path, "v4.0.1", by="Ryan C", now=6.0) is False
    doc = json.loads((tmp_path / MARKER_FILE).read_text())
    assert doc["tag"] == "v4.0.0"          # untouched
    assert "already requested" in caplog.text


def test_write_failure_on_unwritable_dir_leaves_no_partial_file(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    bad_dir = blocker / "sub"       # cannot mkdir: parent is a file
    assert write_switch_request(bad_dir, "v4.0.0", by="x", now=1.0) is False
    assert not (bad_dir / MARKER_FILE).exists()


def test_write_failure_read_only_dir_removes_partial_file(tmp_path):
    """If the marker gets created but the write itself fails, no half-written
    marker is left for the updater to trip over."""
    import unittest.mock as mock
    with mock.patch("os.fdopen", side_effect=OSError("disk full")):
        assert write_switch_request(tmp_path, "v4.0.0", by="x", now=1.0) is False
    assert not (tmp_path / MARKER_FILE).exists()


# ── read_switch_outcome / clear_switch_files ────────────────────────────

def test_read_switch_outcome_absent(tmp_path):
    assert read_switch_outcome(tmp_path) is None


def test_read_switch_outcome_accepted(tmp_path):
    (tmp_path / ACCEPTED_FILE).write_text(
        json.dumps({"tag": "v4.0.0", "by": "x", "at": 1.0, "accepted_at": 2.0}))
    got = read_switch_outcome(tmp_path)
    assert got["state"] == "accepted" and got["tag"] == "v4.0.0"


def test_read_switch_outcome_refused(tmp_path):
    (tmp_path / REFUSED_FILE).write_text(
        json.dumps({"tag": "v4.0.0", "by": "x", "why": "stale"}))
    got = read_switch_outcome(tmp_path)
    assert got["state"] == "refused" and got["why"] == "stale"


def test_read_switch_outcome_refused_wins_if_both_exist(tmp_path):
    (tmp_path / ACCEPTED_FILE).write_text(json.dumps({"tag": "v4.0.0"}))
    (tmp_path / REFUSED_FILE).write_text(json.dumps({"tag": "v4.0.0", "why": "x"}))
    assert read_switch_outcome(tmp_path)["state"] == "refused"


def test_read_switch_outcome_corrupt_is_ignored(tmp_path):
    (tmp_path / ACCEPTED_FILE).write_text("{nope")
    assert read_switch_outcome(tmp_path) is None


def test_clear_switch_files(tmp_path):
    (tmp_path / MARKER_FILE).write_text("{}")
    (tmp_path / ACCEPTED_FILE).write_text("{}")
    assert clear_switch_files(tmp_path) == 2
    assert not marker_present(tmp_path)
    assert clear_switch_files(tmp_path) == 0


def test_marker_present(tmp_path):
    assert marker_present(tmp_path) is False
    (tmp_path / MARKER_FILE).write_text("{}")
    assert marker_present(tmp_path) is True
