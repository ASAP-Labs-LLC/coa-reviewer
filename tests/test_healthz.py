"""``GET /healthz`` — the endpoint the updater trusts to decide a release is good.

Three properties matter, and each is here because getting it wrong breaks the
deployment rather than the app:

* **No auth.** The updater health-checks a release on a scratch port before it
  is live, with no session and no credentials. A 302 to the login page is a
  failed deploy.
* **Fast, with no LabCore round-trip.** LabCore is a real internet hop behind
  Cloudflare. If /healthz waited on it, a LabCore blip would read as "this
  release is broken" and trigger a rollback of a perfectly good release.
* **A truthful version.** The updater compares this against the tag it staged.
  A wrong or stale value means it cannot tell the new release from the old one.
"""

from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("flask")


@pytest.fixture
def anon_client():
    """A test client with **no** session — the updater's view of the app."""
    import app as app_module

    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


# ── contract ────────────────────────────────────────────────────────────────

def test_healthz_returns_200_without_auth(anon_client):
    resp = anon_client.get("/healthz")
    assert resp.status_code == 200, (
        f"got {resp.status_code}; the updater calls this with no session"
    )


def test_healthz_payload_shape(anon_client):
    body = anon_client.get("/healthz").get_json()
    assert body["status"] == "ok"
    assert set(body) >= {"status", "version", "labcore"}
    assert isinstance(body["version"], str) and body["version"]


def test_healthz_is_not_behind_require_portal():
    """Source-level guard: the decorator is applied by hand to ~37 routes, and
    adding it here would be an easy and silent mistake to make."""
    import inspect

    import app as app_module

    src = inspect.getsource(app_module.healthz)
    assert "require_portal" not in src


# ── version ─────────────────────────────────────────────────────────────────

def test_version_falls_back_to_dev_when_file_absent(tmp_path):
    import app as app_module

    assert app_module._read_version(tmp_path / "VERSION") == "dev"


def test_version_is_read_from_the_version_file(tmp_path):
    import app as app_module

    (tmp_path / "VERSION").write_text("2026.08.21-1\n", encoding="utf-8")
    assert app_module._read_version(tmp_path / "VERSION") == "2026.08.21-1"


def test_version_file_is_stripped_and_single_line(tmp_path):
    """A tag written by CI arrives with a trailing newline; a version with
    whitespace in it would never string-compare equal to the staged tag."""
    import app as app_module

    (tmp_path / "VERSION").write_text("  v1.2.3  \r\n", encoding="utf-8")
    assert app_module._read_version(tmp_path / "VERSION") == "v1.2.3"


def test_unreadable_version_file_does_not_break_healthz(tmp_path, anon_client):
    """A corrupt VERSION must degrade to "dev", not 500. A health check that
    errors makes a good release look broken."""
    import app as app_module

    bad = tmp_path / "VERSION"
    bad.write_bytes(b"\xff\xfe\x00broken")
    assert app_module._read_version(bad) == "dev"


def test_healthz_reports_the_module_version(anon_client, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "APP_VERSION", "2026.08.21-7")
    assert anon_client.get("/healthz").get_json()["version"] == "2026.08.21-7"


# ── LabCore reachability is last-known, never probed ────────────────────────

def test_healthz_makes_no_labcore_call(anon_client, monkeypatch):
    """The critical-path guarantee. Any outbound call here is a bug."""
    import labcore_client

    def explode(*a, **kw):  # pragma: no cover - must never run
        raise AssertionError("/healthz performed a network call")

    monkeypatch.setattr(labcore_client.requests, "get", explode)
    monkeypatch.setattr(labcore_client.requests, "post", explode)

    assert anon_client.get("/healthz").status_code == 200


@pytest.mark.parametrize(
    "last_reachable,expected",
    [(True, "reachable"), (False, "unreachable"), (None, "unknown")],
)
def test_healthz_reports_last_known_reachability(
    anon_client, monkeypatch, last_reachable, expected
):
    import app as app_module

    class Stub:
        pass

    stub = Stub()
    stub.last_reachable = last_reachable
    monkeypatch.setattr(app_module.state, "labcore", stub)

    assert anon_client.get("/healthz").get_json()["labcore"] == expected


# ── idleness, for unattended deploys ────────────────────────────────────────

class TestIdleReporting:
    """/healthz reports whether anyone is using the app.

    The updater switches releases automatically when nobody is, so this is the
    signal that decides whether a reviewer gets interrupted mid-review.
    """

    def test_healthz_reports_sessions_and_idle(self, anon_client):
        body = anon_client.get("/healthz").get_json()
        assert "active_sessions" in body and "idle_seconds" in body
        assert isinstance(body["active_sessions"], int)
        assert isinstance(body["idle_seconds"], (int, float))

    def test_active_sessions_counts_real_sessions(self, anon_client, monkeypatch):
        import app as app_module
        from app import UserState

        uid = "test-uid-idle"
        with app_module._sessions_lock:
            app_module.user_sessions[uid] = UserState(uid, "RC")
        try:
            assert anon_client.get("/healthz").get_json()["active_sessions"] >= 1
        finally:
            with app_module._sessions_lock:
                app_module.user_sessions.pop(uid, None)

    def test_healthz_does_not_count_as_activity(self, anon_client):
        """The decisive one.

        The updater polls /healthz to ask "is anyone using this?". If asking
        the question counted as activity, the answer would always be "yes" and
        an idle-gated deploy could never fire. Worse, it would also suppress
        COA's own 3 AM auto-restart, which is gated on the same timestamp — so
        a monitoring call would silently disable a token refresh the app
        depends on.
        """
        import app as app_module

        app_module._last_request_time = time.time() - 600
        before = app_module._last_request_time

        anon_client.get("/healthz")

        assert app_module._last_request_time == before, (
            "/healthz reset the activity clock"
        )
        assert anon_client.get("/healthz").get_json()["idle_seconds"] > 500

    def test_api_health_also_does_not_count_as_activity(self, anon_client):
        """Same reasoning: the frontend polls it during a restart, and that is
        the app waiting for itself, not a person using it."""
        import app as app_module

        app_module._last_request_time = time.time() - 600
        before = app_module._last_request_time
        anon_client.get("/api/health")
        assert app_module._last_request_time == before

    def test_an_anonymous_request_does_not_count_as_activity(self, anon_client):
        """A monitor polling ``/`` is not a reviewer.

        Something on this network requests ``GET /`` every ~2.2 minutes and has
        done since long before this deployment — visible in the old server.log.
        With raw-request idleness that alone kept COA permanently "busy", so an
        idle-gated deploy could never have fired. The path cannot decide it,
        because ``/`` is also exactly what a real reviewer opens. Having a
        session can: a monitor carries no cookie, and a reviewer with work in
        progress always does.
        """
        import app as app_module

        app_module._last_request_time = time.time() - 600
        before = app_module._last_request_time

        anon_client.get("/")

        assert app_module._last_request_time == before, (
            "an unauthenticated GET / counted as a reviewer using the app"
        )

    def test_a_request_from_a_real_session_does_count(self, anon_client):
        """The guard must not have switched activity tracking off entirely."""
        import app as app_module
        from app import UserState

        uid = "test-uid-activity"
        with app_module._sessions_lock:
            app_module.user_sessions[uid] = UserState(uid, "RC")
        try:
            with anon_client.session_transaction() as sess:
                sess["uid"] = uid
            app_module._last_request_time = time.time() - 600
            anon_client.get("/")
            assert app_module._last_request_time > time.time() - 5
        finally:
            with app_module._sessions_lock:
                app_module.user_sessions.pop(uid, None)


def test_client_records_reachability_on_success_and_failure():
    """The flag /healthz reads has to actually be maintained by the client."""
    import requests as _requests

    from labcore_client import LabCoreClient, LabCoreUnavailable

    client = LabCoreClient(base_url="https://labvision.asaplabs.net")
    assert client.last_reachable is None, "should start unknown, not optimistic"

    client._mark_reachable(True)
    assert client.last_reachable is True

    client._mark_reachable(False)
    assert client.last_reachable is False


def test_failed_get_marks_unreachable(monkeypatch):
    import labcore_client
    from labcore_client import LabCoreClient, LabCoreUnavailable

    def boom(*a, **kw):
        raise labcore_client.requests.RequestException("no route to host")

    monkeypatch.setattr(labcore_client.requests, "get", boom)
    client = LabCoreClient(base_url="https://labvision.asaplabs.net")

    with pytest.raises(LabCoreUnavailable):
        client._get("/api/anything")

    assert client.last_reachable is False
