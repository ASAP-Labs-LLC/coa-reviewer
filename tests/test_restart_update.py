"""The app's half of "Restart installs a staged update": read staged.json,
write/withdraw the switch-requested marker. See restart_update.py."""
from __future__ import annotations

import json

from restart_update import (MARKER_FILE, staged_update, withdraw_switch_request,
                            write_switch_request)


def staged(tmp_path, **kw):
    (tmp_path / "staged.json").write_text(json.dumps(kw), encoding="utf-8")


def test_no_staged_file(tmp_path):
    assert staged_update(tmp_path, "v3.5.0") is None


def test_healthy_newer_tag(tmp_path):
    staged(tmp_path, tag="v4.0.0", healthy=True)
    assert staged_update(tmp_path, "v3.5.0") == "v4.0.0"


def test_same_tag_is_not_an_update(tmp_path):
    staged(tmp_path, tag="V3.5.0 ", healthy=True)
    assert staged_update(tmp_path, "v3.5.0") is None


def test_unhealthy_is_ignored(tmp_path):
    staged(tmp_path, tag="v4.0.0", healthy=False)
    assert staged_update(tmp_path, "v3.5.0") is None


def test_dev_build_never_switches(tmp_path):
    staged(tmp_path, tag="v4.0.0", healthy=True)
    assert staged_update(tmp_path, "dev") is None


def test_corrupt_staged(tmp_path):
    (tmp_path / "staged.json").write_text("{nope", encoding="utf-8")
    assert staged_update(tmp_path, "v3.5.0") is None


def test_write_and_withdraw(tmp_path):
    assert write_switch_request(tmp_path, "v4.0.0", by="Dana P", now=5.0)
    doc = json.loads((tmp_path / MARKER_FILE).read_text())
    assert doc == {"tag": "v4.0.0", "by": "Dana P", "at": 5.0}
    assert withdraw_switch_request(tmp_path) is True
    assert withdraw_switch_request(tmp_path) is False
