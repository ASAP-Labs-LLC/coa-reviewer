"""The updater's decision logic, tested without Windows or a network.

What is covered here is everything that can be decided from data: which tag is
newer, whether a downloaded asset is the one that was published, which releases
may be deleted, and what the poll loop should do next. The junction swap and
the process work cannot be honestly faked — those are verified live on the
server and reported separately.

The bias throughout is that the updater must refuse rather than guess. It runs
unattended against production, so "I could not tell" has to mean "do nothing",
never "assume it is fine".
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UPDATER_DIR = PROJECT_ROOT / "deploy" / "updater"
if str(UPDATER_DIR) not in sys.path:
    sys.path.insert(0, str(UPDATER_DIR))

# A hard import, not importorskip: deploy/updater/updater.py is part of this
# repository, so its absence is a failure, not a reason to go quiet.
import updater  # noqa: E402


# ── checksum ────────────────────────────────────────────────────────────────

def test_parse_sha256_file_reads_digest_and_name():
    digest, name = updater.parse_sha256_file(
        "6e0667042a1f6d7bf1622fd20f4bb985a428ed4c17a048b4a4da9c74aa38222c  coa.zip\n"
    )
    assert digest == "6e0667042a1f6d7bf1622fd20f4bb985a428ed4c17a048b4a4da9c74aa38222c"
    assert name == "coa.zip"


def test_parse_sha256_file_accepts_binary_star_form():
    """``sha256sum -b`` writes ``digest *name``."""
    digest, name = updater.parse_sha256_file("a" * 64 + " *coa.zip\n")
    assert digest == "a" * 64
    assert name == "coa.zip"


@pytest.mark.parametrize("bad", ["", "not-a-checksum", "xyz  f.zip", "abc  ", "a" * 63 + "  f.zip"])
def test_parse_sha256_file_rejects_junk(bad):
    with pytest.raises(updater.ChecksumError):
        updater.parse_sha256_file(bad)


def test_verify_asset_accepts_a_matching_file(tmp_path):
    blob = tmp_path / "coa.zip"
    blob.write_bytes(b"release bytes")
    digest = hashlib.sha256(b"release bytes").hexdigest()
    updater.verify_asset(blob, f"{digest}  coa.zip")  # must not raise


def test_verify_asset_rejects_a_tampered_file(tmp_path):
    """The whole point of publishing a checksum."""
    blob = tmp_path / "coa.zip"
    blob.write_bytes(b"release bytes")
    wrong = hashlib.sha256(b"different bytes").hexdigest()
    with pytest.raises(updater.ChecksumError):
        updater.verify_asset(blob, f"{wrong}  coa.zip")


def test_verify_asset_rejects_a_truncated_download(tmp_path):
    blob = tmp_path / "coa.zip"
    full = b"release bytes that got cut off"
    blob.write_bytes(full[:10])
    with pytest.raises(updater.ChecksumError):
        updater.verify_asset(blob, f"{hashlib.sha256(full).hexdigest()}  coa.zip")


# ── version comparison ──────────────────────────────────────────────────────

def test_read_version_returns_dev_when_absent(tmp_path):
    assert updater.read_version(tmp_path) == "dev"


def test_read_version_strips(tmp_path):
    (tmp_path / "VERSION").write_text("v1.0.0\r\n", encoding="utf-8")
    assert updater.read_version(tmp_path) == "v1.0.0"


@pytest.mark.parametrize(
    "current,latest,differs",
    [
        ("v1.0.0", "v1.0.0", False),
        ("v1.0.0", "v1.0.1", True),
        ("dev", "v1.0.0", True),
        ("v1.0.0", "V1.0.0", False),   # tag case is not meaningful
        ("v1.0.0 ", "v1.0.0", False),  # whitespace is not a new release
    ],
)
def test_differs_from(current, latest, differs):
    assert updater.differs_from(current, latest) is differs


# ── the poll decision table ─────────────────────────────────────────────────

def test_poll_sleeps_when_already_on_latest():
    assert updater.plan_poll(current="v1.0.0", latest="v1.0.0", staged=None) == updater.SLEEP


def test_poll_stages_a_new_release():
    assert updater.plan_poll(current="v1.0.0", latest="v1.0.1", staged=None) == updater.STAGE


def test_poll_does_not_restage_what_is_already_staged():
    """Re-downloading and rebuilding a venv every 5 minutes while a human
    decides is wasteful and would rewrite staged_at forever."""
    staged = {"tag": "v1.0.1", "healthy": True}
    assert updater.plan_poll(current="v1.0.0", latest="v1.0.1", staged=staged) == updater.SLEEP


def test_poll_does_not_restage_a_release_already_known_bad():
    """A staged release that failed its health check must not be retried on a
    loop — it would rebuild the same broken venv every interval."""
    staged = {"tag": "v1.0.1", "healthy": False}
    assert updater.plan_poll(current="v1.0.0", latest="v1.0.1", staged=staged) == updater.SLEEP


def test_poll_stages_a_newer_release_over_a_stale_staged_one():
    staged = {"tag": "v1.0.1", "healthy": True}
    assert updater.plan_poll(current="v1.0.0", latest="v1.0.2", staged=staged) == updater.STAGE


def test_poll_sleeps_when_staged_matches_current_after_a_switch():
    """After a successful switch, current == staged.tag == latest."""
    staged = {"tag": "v1.0.1", "healthy": True}
    assert updater.plan_poll(current="v1.0.1", latest="v1.0.1", staged=staged) == updater.SLEEP


def test_poll_sleeps_when_latest_is_unknown():
    """A failed API call must not be read as "no releases" and must never
    trigger anything."""
    assert updater.plan_poll(current="v1.0.0", latest=None, staged=None) == updater.SLEEP


# ── switch guard ────────────────────────────────────────────────────────────

def test_switch_refused_without_a_staged_release():
    ok, why = updater.may_switch(staged=None, requested_tag="v1.0.1")
    assert not ok and "nothing staged" in why.lower()


def test_switch_refused_when_staged_is_unhealthy():
    staged = {"tag": "v1.0.1", "healthy": False, "notes": "healthz never returned 200"}
    ok, why = updater.may_switch(staged=staged, requested_tag="v1.0.1")
    assert not ok and "health" in why.lower()


def test_switch_refused_when_the_request_names_a_different_tag():
    """Guards the race where a newer release is staged between a human
    reading the page and clicking the button."""
    staged = {"tag": "v1.0.2", "healthy": True}
    ok, why = updater.may_switch(staged=staged, requested_tag="v1.0.1")
    assert not ok


def test_switch_allowed_for_a_healthy_staged_release():
    staged = {"tag": "v1.0.1", "healthy": True}
    ok, _ = updater.may_switch(staged=staged, requested_tag="v1.0.1")
    assert ok


# ── retention ───────────────────────────────────────────────────────────────

def test_prune_keeps_the_five_most_recent():
    names = [f"v1.0.{i}" for i in range(9, -1, -1)]  # newest first
    doomed = updater.releases_to_prune(names, keep=5, protected=set())
    assert doomed == ["v1.0.4", "v1.0.3", "v1.0.2", "v1.0.1", "v1.0.0"]


def test_prune_keeps_everything_when_under_the_limit():
    assert updater.releases_to_prune(["v1.0.1", "v1.0.0"], keep=5, protected=set()) == []


def test_prune_never_deletes_current():
    """Deleting the release behind the junction destroys the running app."""
    names = [f"v1.0.{i}" for i in range(9, -1, -1)]
    doomed = updater.releases_to_prune(names, keep=5, protected={"v1.0.0"})
    assert "v1.0.0" not in doomed


def test_prune_never_deletes_a_rollback_target():
    """The release we rolled back *from* stays, so the failure can be examined
    rather than silently swept."""
    names = [f"v1.0.{i}" for i in range(9, -1, -1)]
    doomed = updater.releases_to_prune(names, keep=5, protected={"v1.0.1", "v1.0.2"})
    assert "v1.0.1" not in doomed and "v1.0.2" not in doomed


def test_prune_protection_does_not_consume_a_retention_slot():
    """Protecting an old release must not push a recent one out of the window."""
    names = [f"v1.0.{i}" for i in range(9, -1, -1)]
    doomed = updater.releases_to_prune(names, keep=5, protected={"v1.0.0"})
    for recent in names[:5]:
        assert recent not in doomed


# ── staged.json ─────────────────────────────────────────────────────────────

def test_staged_record_roundtrips(tmp_path):
    updater.write_staged(tmp_path, tag="v1.0.1", healthy=True, notes="ok", now="2026-08-21T18:00:00Z")
    got = updater.read_staged(tmp_path)
    assert got["tag"] == "v1.0.1"
    assert got["healthy"] is True
    assert got["staged_at"] == "2026-08-21T18:00:00Z"
    assert got["notes"] == "ok"


def test_read_staged_tolerates_a_corrupt_file(tmp_path):
    """A half-written staged.json must not crash the service on next boot."""
    (tmp_path / "staged.json").write_text("{not json", encoding="utf-8")
    assert updater.read_staged(tmp_path) is None


def test_read_staged_returns_none_when_absent(tmp_path):
    assert updater.read_staged(tmp_path) is None


def test_write_staged_is_atomic(tmp_path):
    """Written via a temp file and replaced, so a crash mid-write cannot leave
    a truncated record that read_staged would discard along with the real one."""
    updater.write_staged(tmp_path, tag="v1.0.1", healthy=True, notes="", now="x")
    assert not list(tmp_path.glob("*.tmp")), "temp file left behind"
    assert json.loads((tmp_path / "staged.json").read_text(encoding="utf-8"))["tag"] == "v1.0.1"


# ── supervisor contract ─────────────────────────────────────────────────────

def test_kill_callable_matches_supervisor_contract():
    """``stop_until_dead`` calls ``kill(attempt)`` with a 1-based attempt number.

    A zero-argument ``kill`` raises TypeError at the worst possible moment —
    after the release is unpacked and the venv built, while a process is up on
    the scratch port. This caught exactly that during the first live run.
    """
    import supervisor

    calls: list[int] = []
    alive = [True]

    def is_alive() -> bool:
        return alive[0]

    def kill(attempt: int = 1) -> None:
        calls.append(attempt)
        alive[0] = False

    assert supervisor.stop_until_dead(is_alive, kill, verify_timeout=0.2, poll=0.01)
    assert calls == [1], "kill should have been called once, with the attempt number"


def test_kill_callable_escalates_when_the_first_attempt_fails():
    """A process that survives terminate must get a harder kill, not a retry of
    the same polite one."""
    import supervisor

    calls: list[int] = []
    alive = [True]

    def is_alive() -> bool:
        return alive[0]

    def kill(attempt: int = 1) -> None:
        calls.append(attempt)
        if attempt >= 2:      # only the escalated kill works
            alive[0] = False

    assert supervisor.stop_until_dead(is_alive, kill, verify_timeout=0.2, poll=0.01)
    assert calls[:2] == [1, 2]


# ── asset selection ─────────────────────────────────────────────────────────

def test_pick_assets_finds_zip_and_checksum():
    assets = [
        {"name": "coa-reviewer-v1.0.0.zip", "browser_download_url": "u1"},
        {"name": "coa-reviewer-v1.0.0.zip.sha256", "browser_download_url": "u2"},
    ]
    zip_a, sum_a = updater.pick_assets(assets)
    assert zip_a["browser_download_url"] == "u1"
    assert sum_a["browser_download_url"] == "u2"


def test_pick_assets_refuses_a_release_with_no_checksum():
    """An unverifiable asset is not installable."""
    assets = [{"name": "coa.zip", "browser_download_url": "u1"}]
    with pytest.raises(updater.ReleaseError):
        updater.pick_assets(assets)


def test_pick_assets_refuses_a_release_with_no_zip():
    assets = [{"name": "coa.zip.sha256", "browser_download_url": "u2"}]
    with pytest.raises(updater.ReleaseError):
        updater.pick_assets(assets)
