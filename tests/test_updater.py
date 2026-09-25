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
import time
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


# ── auto-switch when idle ───────────────────────────────────────────────────

class TestAutoSwitch:
    """Deploying without a human means the idle check is the only thing
    standing between a reviewer and losing their in-progress review.

    A COA session holds records and a PDF cache in memory; a restart makes the
    reviewer re-pull everything. So "nobody is using it" has to be answered
    conservatively: anything unknown counts as in use.
    """

    def test_switches_when_idle_and_a_healthy_release_is_staged(self):
        assert updater.should_auto_switch(
            enabled=True, staged={"tag": "v2", "healthy": True}, current="v1",
            health={"active_sessions": 0, "idle_seconds": 600},
            min_idle_seconds=300, latest="v2",
        ) == (True, "")

    def test_never_deploys_a_staged_release_that_is_no_longer_latest(self):
        """A stale staged.json must not cause a downgrade.

        Found live: marking the deployed release as a prerelease moved
        GitHub's "latest" back to the previous tag, the updater staged that
        older release, and because staged != current it was one poll away from
        deploying it — silently rolling the lab back a version. Staging is
        driven by "latest", so the staged record can outlive the reason it was
        made.
        """
        ok, why = updater.should_auto_switch(
            enabled=True, staged={"tag": "v1", "healthy": True}, current="v2",
            health={"active_sessions": 0, "idle_seconds": 9999},
            min_idle_seconds=300, latest="v2",
        )
        assert ok is False and "latest" in why.lower()

    def test_refuses_when_latest_is_unknown(self):
        """A failed GitHub call means we cannot confirm the staged release is
        still the one to deploy, so we do not deploy it."""
        ok, why = updater.should_auto_switch(
            enabled=True, staged={"tag": "v2", "healthy": True}, current="v1",
            health={"active_sessions": 0, "idle_seconds": 9999},
            min_idle_seconds=300, latest=None,
        )
        assert ok is False

    def test_disabled_never_switches(self):
        ok, why = updater.should_auto_switch(
            enabled=False, staged={"tag": "v2", "healthy": True}, current="v1",
            health={"active_sessions": 0, "idle_seconds": 600},
            min_idle_seconds=300, latest="v2",
        )
        assert ok is False and "not enabled" in why

    def test_never_switches_with_an_active_session(self):
        ok, why = updater.should_auto_switch(
            enabled=True, staged={"tag": "v2", "healthy": True}, current="v1",
            health={"active_sessions": 1, "idle_seconds": 9999},
            min_idle_seconds=300, latest="v2",
        )
        assert ok is False and "session" in why.lower()

    def test_never_switches_before_the_idle_threshold(self):
        ok, why = updater.should_auto_switch(
            enabled=True, staged={"tag": "v2", "healthy": True}, current="v1",
            health={"active_sessions": 0, "idle_seconds": 10},
            min_idle_seconds=300, latest="v2",
        )
        assert ok is False and "idle" in why.lower()

    def test_never_switches_an_unhealthy_release(self):
        ok, _ = updater.should_auto_switch(
            enabled=True, staged={"tag": "v2", "healthy": False}, current="v1",
            health={"active_sessions": 0, "idle_seconds": 9999},
            min_idle_seconds=300, latest="v2",
        )
        assert ok is False

    def test_nothing_to_do_when_already_on_the_staged_release(self):
        ok, _ = updater.should_auto_switch(
            enabled=True, staged={"tag": "v2", "healthy": True}, current="v2",
            health={"active_sessions": 0, "idle_seconds": 9999},
            min_idle_seconds=300, latest="v2",
        )
        assert ok is False

    def test_unreadable_health_is_treated_as_in_use(self):
        """If /healthz cannot be read we do not know whether anyone is there.

        Guessing "idle" here deploys on top of whoever is mid-review, so the
        unknown case must resolve to "leave it alone" — the app is up (the
        supervisor would have restarted it otherwise), we simply cannot see
        inside it.
        """
        ok, why = updater.should_auto_switch(
            enabled=True, staged={"tag": "v2", "healthy": True}, current="v1",
            health=None, min_idle_seconds=300, latest="v2",)
        assert ok is False and "could not" in why.lower()

    def test_missing_idle_fields_are_treated_as_in_use(self):
        """An older release that predates the idle fields must not be read as
        idle just because the key is absent."""
        ok, _ = updater.should_auto_switch(
            enabled=True, staged={"tag": "v2", "healthy": True}, current="v1",
            health={"status": "ok"}, min_idle_seconds=300, latest="v2",)
        assert ok is False


# ── supervision ─────────────────────────────────────────────────────────────

class TestSupervisionDecision:
    """Something has to restart an app that exits on its own.

    COA does this by design: ``_auto_restart_worker`` calls ``os._exit(0)`` at
    3 AM to refresh long-lived Playwright and QBench tokens, and ``/api/restart``
    does the same when a reviewer clicks Restart. Under the shared-drive setup
    ``Run.pyw`` respawned it — that is the 03:00 line in launcher.log. The
    deployed layout has no Run.pyw, so without this the app would exit at 3 AM
    and simply never come back, and a reviewer clicking Restart would end the
    service for the day.

    The updater is already a loop that knows each app's port and how to start
    it, so it supervises. What it must *not* do is what Run.pyw did wrong:
    restart blindly, forever, with no way for a human to hold it down.
    """

    def test_running_app_is_left_alone(self):
        assert updater.supervision_decision(
            has_listener=True, paused=False, starts_in_window=0, max_starts=3
        ) == updater.SUPERVISE_OK

    def test_dead_app_is_started(self):
        assert updater.supervision_decision(
            has_listener=False, paused=False, starts_in_window=0, max_starts=3
        ) == updater.SUPERVISE_START

    def test_paused_app_is_never_started(self):
        """A human taking an app down deliberately must stay down. Without
        this, stopping an app for maintenance means fighting the updater."""
        assert updater.supervision_decision(
            has_listener=False, paused=True, starts_in_window=0, max_starts=3
        ) == updater.SUPERVISE_PAUSED

    def test_paused_beats_running(self):
        """Pause is about intent, not current state — it must not silently
        expire the moment the app happens to be up."""
        assert updater.supervision_decision(
            has_listener=True, paused=True, starts_in_window=0, max_starts=3
        ) == updater.SUPERVISE_PAUSED

    def test_a_crashlooping_app_is_given_up_on(self):
        """Restarting forever turns a crash into a silent 100%-CPU spin and
        hides the failure. Stop, and say so."""
        assert updater.supervision_decision(
            has_listener=False, paused=False, starts_in_window=3, max_starts=3
        ) == updater.SUPERVISE_GIVING_UP

    def test_the_storm_guard_allows_the_expected_daily_restart(self):
        """COA's 3 AM exit is one restart a day; that must not look like a
        crashloop."""
        assert updater.supervision_decision(
            has_listener=False, paused=False, starts_in_window=1, max_starts=3
        ) == updater.SUPERVISE_START


class TestPauseMarker:
    def test_absent_marker_is_not_paused(self, tmp_path):
        assert updater.is_paused(tmp_path) is False

    def test_present_marker_is_paused(self, tmp_path):
        (tmp_path / "paused").write_text("down for maintenance", encoding="utf-8")
        assert updater.is_paused(tmp_path) is True


class TestStartWindow:
    def test_counts_only_starts_inside_the_window(self):
        # now = 1000, window = 900s
        starts = [50, 90, 150, 995]
        assert updater.starts_within(starts, now=1000.0, window=900.0) == 2

    def test_empty_history_is_zero(self):
        assert updater.starts_within([], now=1000.0, window=900.0) == 0


# ── per-app launch arguments ────────────────────────────────────────────────

def _app(**cfg):
    base = {"name": "x", "repo": "o/r", "root": r"C:\ASAPApps\x", "port": 1234}
    base.update(cfg)
    return updater.App(base, {})


class TestLaunchArgs:
    """The two apps are launched differently and the updater must not assume.

    COA takes its port from the ``PORT`` environment variable; LEM takes
    ``--port`` on the command line and ignores the environment entirely. An
    updater that only knew COA's way would start LEM on its **default** 5557
    while believing it had started it on the scratch port — which, during a
    health check, means starting a second copy of LEM on the live port.
    """

    def test_default_is_just_the_entry_point(self):
        argv = updater.launch_args(_app(entry="app.py"), port=5559,
                                   for_health_check=False)
        assert argv == ["app.py"]

    def test_port_arg_is_passed_when_configured(self):
        argv = updater.launch_args(
            _app(entry="web_server.pyw", port_arg="--port"),
            port=15557, for_health_check=False)
        assert argv == ["web_server.pyw", "--port", "15557"]

    def test_extra_args_always_apply(self):
        argv = updater.launch_args(
            _app(entry="web_server.pyw", args=["--no-tray"]),
            port=5557, for_health_check=False)
        assert argv == ["web_server.pyw", "--no-tray"]

    def test_health_args_apply_only_to_the_health_check(self):
        app = _app(entry="web_server.pyw", args=["--no-tray"],
                   health_args=["--no-publish"])

        live = updater.launch_args(app, port=5557, for_health_check=False)
        probe = updater.launch_args(app, port=15557, for_health_check=True)

        assert "--no-publish" not in live, (
            "the real launch must publish; only the throwaway one stays quiet"
        )
        assert "--no-publish" in probe
        assert "--no-tray" in live and "--no-tray" in probe


class TestLaunchEnv:
    def test_data_env_and_port_env_are_set(self):
        env = updater.launch_env(_app(data_env="COA_DATA_DIR"),
                                 port=5559, data_dir="D:/state", base={})
        assert env["COA_DATA_DIR"] == "D:/state"
        assert env["PORT"] == "5559"

    def test_port_env_is_omitted_when_the_port_goes_on_the_command_line(self):
        """Setting both invites them to disagree, and the CLI flag wins — so a
        stale PORT would be a lie sitting in the environment of a live app."""
        env = updater.launch_env(_app(data_env="LEM_DATA_DIR", port_arg="--port"),
                                 port=15557, data_dir="D:/state", base={})
        assert env["LEM_DATA_DIR"] == "D:/state"
        assert "PORT" not in env


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


# ── switch-requested (restart asking the updater for the staged release) ────
#
# Protocol: the marker is claimed, never deleted-and-forgotten — it becomes
# switch-accepted or switch-refused (with why), so a reviewer's restart
# button gets a real answer. honour_switch_request's gate is: shape-valid,
# fresh, not paused/mid-switch, staged+healthy+matching (may_switch), not
# already on that release, still GitHub's latest (a recent poll_once cache),
# and not a tag that was just rolled back from (held-tags.json).

def _coa_app(tmp_path):
    """An App rooted at tmp_path, with its data dir created — reuses the
    module's own ``_app`` helper rather than duplicating its config keys."""
    app = _app(root=str(tmp_path))
    app.data_dir.mkdir(parents=True, exist_ok=True)
    return app


def _write_marker(app, tag="v4.0.0", by="x", at=None):
    at = time.time() if at is None else at
    (app.data_dir / "switch-requested").write_text(
        json.dumps({"tag": tag, "by": by, "at": at}), encoding="utf-8")


def _seed_latest(monkeypatch, app, tag, *, age=0.0):
    """Make honour_switch_request believe poll_once last saw ``tag`` as
    latest ``age`` seconds ago. Replaces the whole cache dict rather than
    mutating the shared module one, so tests cannot leak into each other."""
    monkeypatch.setattr(updater, "_LATEST_CACHE", {app.name: (tag, time.time() - age)})


def _ready(app, monkeypatch, *, staged_tag="v4.0.0", current="v3.5.0"):
    """Stage a healthy release, make it look like the latest GitHub poll,
    and set current_version — the state under which a switch is allowed."""
    updater.write_staged(app.data_dir, tag=staged_tag, healthy=True, notes="ok")
    monkeypatch.setattr(app, "current_version", lambda: current)
    _seed_latest(monkeypatch, app, staged_tag)


@pytest.fixture(autouse=True)
def _reset_claim_failures():
    """_CLAIM_FAILURES is process-lifetime state in production (skip a file
    that could not be claimed); tests must not see a previous test's entry."""
    updater._CLAIM_FAILURES.clear()
    yield
    updater._CLAIM_FAILURES.clear()


def test_honour_switch_request_switches_to_staged(tmp_path, monkeypatch):
    app = _coa_app(tmp_path)
    _ready(app, monkeypatch)
    _write_marker(app, tag="v4.0.0", by="Dana P")
    calls = []
    monkeypatch.setattr(updater, "switch", lambda a, tag, **kw: calls.append(tag) or True)
    assert updater.honour_switch_request(app) is True
    assert calls == ["v4.0.0"]
    assert not (app.data_dir / "switch-requested").exists()


def test_accepted_file_is_written_before_switch_is_called(tmp_path, monkeypatch):
    app = _coa_app(tmp_path)
    _ready(app, monkeypatch)
    _write_marker(app, tag="v4.0.0", by="Dana P")
    seen = {}

    def fake_switch(a, tag, **kw):
        seen["accepted_exists"] = (app.data_dir / "switch-accepted").exists()
        return True
    monkeypatch.setattr(updater, "switch", fake_switch)
    assert updater.honour_switch_request(app) is True
    assert seen["accepted_exists"] is True
    doc = json.loads((app.data_dir / "switch-accepted").read_text())
    assert doc["tag"] == "v4.0.0" and doc["by"] == "Dana P" and "accepted_at" in doc


def test_honour_switch_request_noop_without_marker(tmp_path, monkeypatch):
    app = _coa_app(tmp_path)
    monkeypatch.setattr(updater, "switch", lambda *a, **k: pytest.fail("must not switch"))
    assert updater.honour_switch_request(app) is False


def test_honour_switch_request_unhealthy_staged_refused(tmp_path, monkeypatch):
    app = _coa_app(tmp_path)
    updater.write_staged(app.data_dir, tag="v4.0.0", healthy=False, notes="boom")
    monkeypatch.setattr(app, "current_version", lambda: "v3.5.0")
    _seed_latest(monkeypatch, app, "v4.0.0")
    _write_marker(app, tag="v4.0.0")
    monkeypatch.setattr(updater, "switch", lambda *a, **k: pytest.fail("must not switch"))
    assert updater.honour_switch_request(app) is False
    doc = json.loads((app.data_dir / "switch-refused").read_text())
    assert "health check" in doc["why"]


def test_honour_switch_request_switching_marker_refused(tmp_path, monkeypatch):
    app = _coa_app(tmp_path)
    _ready(app, monkeypatch)
    _write_marker(app)
    (app.data_dir / "switching").write_text("switch in progress", encoding="utf-8")
    monkeypatch.setattr(updater, "switch", lambda *a, **k: pytest.fail("must not switch"))
    assert updater.honour_switch_request(app) is False
    doc = json.loads((app.data_dir / "switch-refused").read_text())
    assert "already in progress" in doc["why"]


def test_honour_switch_request_paused_refused(tmp_path, monkeypatch):
    app = _coa_app(tmp_path)
    _ready(app, monkeypatch)
    _write_marker(app)
    (app.data_dir / "paused").write_text("down for maintenance", encoding="utf-8")
    monkeypatch.setattr(updater, "switch", lambda *a, **k: pytest.fail("must not switch"))
    assert updater.honour_switch_request(app) is False
    doc = json.loads((app.data_dir / "switch-refused").read_text())
    assert doc["why"] == "app is paused"


def test_honour_switch_request_noop_when_already_on_tag(tmp_path, monkeypatch):
    app = _coa_app(tmp_path)
    _ready(app, monkeypatch, staged_tag="v4.0.0", current="v4.0.0")
    _write_marker(app, tag="v4.0.0")
    monkeypatch.setattr(updater, "switch", lambda *a, **k: pytest.fail("must not switch"))
    assert updater.honour_switch_request(app) is False


@pytest.mark.parametrize("bad_marker", [
    "garbage",                                    # not JSON
    "[]",                                          # not an object
    json.dumps({"tag": "", "at": 1.0}),            # empty tag
    json.dumps({"tag": "v4.0.0"}),                 # missing at
    json.dumps({"tag": "v4.0.0", "at": "now"}),    # at not a number
    json.dumps({"tag": "v4.0.0", "at": True}),     # bool is not a number here
    json.dumps({"tag": "x" * 65, "at": 1.0}),      # tag too long
])
def test_malformed_switch_request_never_switches_and_leaves_refused(
        tmp_path, monkeypatch, bad_marker):
    app = _coa_app(tmp_path)
    _ready(app, monkeypatch)
    (app.data_dir / "switch-requested").write_text(bad_marker, encoding="utf-8")
    monkeypatch.setattr(updater, "switch", lambda *a, **k: pytest.fail("must not switch"))
    assert updater.honour_switch_request(app) is False
    assert not (app.data_dir / "switch-requested").exists()
    doc = json.loads((app.data_dir / "switch-refused").read_text())
    assert doc["why"] == "malformed switch request"


def test_stale_switch_request_refused(tmp_path, monkeypatch):
    app = _coa_app(tmp_path)
    _ready(app, monkeypatch)
    _write_marker(app, at=time.time() - (updater.MAX_REQUEST_AGE_SECONDS + 1))
    monkeypatch.setattr(updater, "switch", lambda *a, **k: pytest.fail("must not switch"))
    assert updater.honour_switch_request(app) is False
    doc = json.loads((app.data_dir / "switch-refused").read_text())
    assert "stale" in doc["why"]


def test_switch_request_from_slightly_in_the_future_is_tolerated(tmp_path, monkeypatch):
    """Small clock skew between the app and the updater must not refuse a
    request that was, from the app's clock, made just now."""
    app = _coa_app(tmp_path)
    _ready(app, monkeypatch)
    _write_marker(app, at=time.time() + 3.0)
    monkeypatch.setattr(updater, "switch", lambda a, tag, **kw: True)
    assert updater.honour_switch_request(app) is True


def test_switch_request_refused_without_a_recent_poll(tmp_path, monkeypatch):
    app = _coa_app(tmp_path)
    updater.write_staged(app.data_dir, tag="v4.0.0", healthy=True, notes="ok")
    monkeypatch.setattr(app, "current_version", lambda: "v3.5.0")
    monkeypatch.setattr(updater, "_LATEST_CACHE", {})   # never polled
    _write_marker(app, tag="v4.0.0")
    monkeypatch.setattr(updater, "switch", lambda *a, **k: pytest.fail("must not switch"))
    assert updater.honour_switch_request(app) is False
    doc = json.loads((app.data_dir / "switch-refused").read_text())
    assert "latest" in doc["why"]


def test_switch_request_refused_when_a_newer_poll_beat_it(tmp_path, monkeypatch):
    app = _coa_app(tmp_path)
    updater.write_staged(app.data_dir, tag="v4.0.0", healthy=True, notes="ok")
    monkeypatch.setattr(app, "current_version", lambda: "v3.5.0")
    _seed_latest(monkeypatch, app, "v4.0.1")   # GitHub has moved on
    _write_marker(app, tag="v4.0.0")
    monkeypatch.setattr(updater, "switch", lambda *a, **k: pytest.fail("must not switch"))
    assert updater.honour_switch_request(app) is False
    doc = json.loads((app.data_dir / "switch-refused").read_text())
    assert "no longer GitHub's latest" in doc["why"]


def test_switch_request_refused_when_the_poll_cache_is_stale(tmp_path, monkeypatch):
    app = _coa_app(tmp_path)
    _ready(app, monkeypatch)
    _seed_latest(monkeypatch, app, "v4.0.0", age=updater.DEFAULT_POLL_SECONDS * 3)
    _write_marker(app, tag="v4.0.0")
    monkeypatch.setattr(updater, "switch", lambda *a, **k: pytest.fail("must not switch"))
    assert updater.honour_switch_request(app) is False


def test_held_tag_refuses_a_restart_time_switch(tmp_path, monkeypatch):
    app = _coa_app(tmp_path)
    _ready(app, monkeypatch)
    updater.hold_tag(app.data_dir, "v4.0.0")
    _write_marker(app, tag="v4.0.0")
    monkeypatch.setattr(updater, "switch", lambda *a, **k: pytest.fail("must not switch"))
    assert updater.honour_switch_request(app) is False
    doc = json.loads((app.data_dir / "switch-refused").read_text())
    assert "rolled back" in doc["why"]


def test_release_tag_clears_a_hold(tmp_path):
    """What the CLI `switch` command does before calling switch(): a human
    explicitly choosing a tag overrides an earlier automatic hold on it."""
    app = _coa_app(tmp_path)
    updater.hold_tag(app.data_dir, "v4.0.0")
    assert updater._is_held(app.data_dir, "v4.0.0")
    updater.release_tag(app.data_dir, "v4.0.0")
    assert not updater._is_held(app.data_dir, "v4.0.0")


def test_held_tags_file_is_bounded(tmp_path):
    for i in range(updater.MAX_HELD_TAGS + 10):
        updater.hold_tag(tmp_path, f"v0.0.{i}")
    assert len(updater._read_held_tags(tmp_path)) == updater.MAX_HELD_TAGS


def test_rollback_holds_the_abandoned_release(tmp_path, monkeypatch):
    app = _coa_app(tmp_path)
    monkeypatch.setattr(app, "current_target_name", lambda: "v3.5.0")
    monkeypatch.setattr(app, "release_names_newest_first",
                        lambda: ["v3.5.0", "v3.4.0"])
    monkeypatch.setattr(updater, "switch", lambda a, tag, **kw: True)
    assert updater.rollback(app) is True
    assert updater._is_held(app.data_dir, "v3.5.0")


def test_switch_rollback_after_unhealthy_holds_the_bad_tag_and_uses_switch_guard(
        tmp_path, monkeypatch):
    """Covers both C2 (an unhealthy switch holds the tag it rolled back from)
    and M7 (the rollback's stop/repoint/start is inside _switch_guard, so
    supervision does not race in while the app is deliberately stopped)."""
    app = _coa_app(tmp_path)
    (app.releases_dir / "v4.0.0").mkdir(parents=True)
    updater.write_staged(app.data_dir, tag="v4.0.0", healthy=True, notes="ok")
    monkeypatch.setattr(app, "current_target_name", lambda: "v3.5.0")
    monkeypatch.setattr(updater, "_stop_app", lambda a: None)
    monkeypatch.setattr(updater, "repoint_junction", lambda link, target: None)
    marker_seen_during_rollback = []

    def fake_start_app(a):
        marker_seen_during_rollback.append((a.data_dir / "switching").exists())
    monkeypatch.setattr(updater, "_start_app", fake_start_app)

    calls = {"n": 0}

    def fake_verify(a, *, expected):
        calls["n"] += 1
        return (False, "boom") if calls["n"] == 1 else (True, "recovered")
    monkeypatch.setattr(updater, "_verify_live", fake_verify)

    assert updater.switch(app, "v4.0.0") is False
    assert updater._is_held(app.data_dir, "v4.0.0")
    # _start_app is called once for the initial switch (guarded) and once for
    # the rollback (also guarded) — both must see the marker.
    assert marker_seen_during_rollback == [True, True]
    assert not (app.data_dir / "switching").exists()   # cleared afterwards


def test_claim_failure_is_not_retried_on_the_next_tick(tmp_path, monkeypatch):
    """If neither the rename nor the fallback unlink can claim the marker
    (e.g. a Windows sharing violation), the updater must not keep trying
    every tick — it remembers the exact file and leaves it alone."""
    app = _coa_app(tmp_path)
    _ready(app, monkeypatch)
    _write_marker(app, tag="v4.0.0")
    attempts = []

    def fail_replace(*a, **k):
        attempts.append("replace")
        raise OSError("sharing violation")

    def fail_unlink(self):
        attempts.append("unlink")
        raise OSError("sharing violation")

    monkeypatch.setattr(updater.os, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    monkeypatch.setattr(updater, "switch", lambda a, tag, **kw: True)

    assert updater.honour_switch_request(app) is False
    first_attempts = len(attempts)
    assert first_attempts > 0

    # Second tick, same unclaimed marker still on disk: must not attempt again.
    assert updater.honour_switch_request(app) is False
    assert len(attempts) == first_attempts


def test_loop_wiring_calls_honour_after_supervise(tmp_path):
    """Source-level guard: the run loop's exceptions-contained supervise/
    honour ordering (verified functionally above) must not silently regress
    to only one of the two, or to honour running first."""
    src = (Path(__file__).resolve().parent.parent / "deploy" / "updater"
          / "updater.py").read_text(encoding="utf-8")
    loop = src[src.index("while True:"):]
    i_supervise = loop.index("supervise(a)")
    i_honour = loop.index("honour_switch_request(a")
    assert i_supervise < i_honour
    between = loop[i_supervise:i_honour]
    assert between.count("except Exception:") >= 1
