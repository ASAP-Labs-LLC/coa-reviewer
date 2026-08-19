"""Behavioral tests for the launcher's process/port supervision primitives.

These exist because of the 2026-07-31 inert-duplicate incident. From
launcher.log:

    12:01:08  Stopping server (PID 11728) + descendants
    12:01:21    taskkill /F /T failed for PID 11728: ... timed out after 8s
    12:01:26    PID 11728 did not reap within 5s
    12:01:30  Server started (PID 1852)          <-- spawned anyway
    12:35:29  Server process exited (code 4294967295)   <-- operator killed 1852
    12:35:33  Server started (PID 10356)         <-- whack-a-mole

The launcher *knew* the kill had failed — both the taskkill timeout and the
proc.wait() timeout were logged — and started a new server regardless. The
new child then bound the still-occupied port without error (Windows
SO_REUSEADDR) and served nobody.

The primitives below make "did it actually die?" and "is it actually
serving?" answerable, so the launcher can act on the answer instead of
logging it and moving on.
"""

from __future__ import annotations

import threading
import time

import supervisor
from tests.conftest import LocalServer as _Server, free_port as _free_port


# ── helpers ──────────────────────────────────────────────────────────────

def _listener(host: str = "127.0.0.1"):
    """A live accepting server on an ephemeral port. Returns (server, port)."""
    srv = _Server(host)
    return srv, srv.port


class _FakeClock:
    """Deterministic monotonic clock + sleep, so timeout logic is testable
    without the test actually waiting."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


# ── port_has_listener ────────────────────────────────────────────────────

def test_port_has_listener_true_while_a_server_is_accepting() -> None:
    srv, port = _listener()
    try:
        assert supervisor.port_has_listener(port) is True
    finally:
        srv.close()


def test_port_has_listener_false_on_an_unused_port() -> None:
    assert supervisor.port_has_listener(_free_port()) is False


# ── wait_until_free ──────────────────────────────────────────────────────

def test_wait_until_free_reports_failure_while_the_old_server_survives() -> None:
    """The 11728 case: the port never frees, so the launcher must be told
    NO rather than being allowed to spawn into it."""
    srv, port = _listener()
    try:
        assert supervisor.wait_until_free(port, timeout=1.0, poll=0.1) is False
    finally:
        srv.close()


def test_wait_until_free_succeeds_once_the_port_is_released() -> None:
    """A normal restart: the old server takes a moment to let go."""
    srv, port = _listener()
    threading.Timer(0.5, srv.close).start()
    try:
        assert supervisor.wait_until_free(port, timeout=5.0, poll=0.1) is True
    finally:
        srv.close()


# ── wait_until_serving ───────────────────────────────────────────────────

def test_wait_until_serving_detects_a_server_that_comes_up() -> None:
    port = _free_port()
    holder = {}

    def _bring_up() -> None:
        holder["srv"] = _Server("127.0.0.1", port)

    timer = threading.Timer(0.4, _bring_up)
    timer.start()
    try:
        assert supervisor.wait_until_serving(port, timeout=5.0, poll=0.1) is True
    finally:
        timer.join(timeout=5)
        if "srv" in holder:
            holder["srv"].close()


def test_wait_until_serving_reports_failure_when_nothing_binds() -> None:
    """An inert child — alive, but never answering. This is the check the
    launcher was missing entirely."""
    assert supervisor.wait_until_serving(_free_port(), timeout=1.0, poll=0.1) is False


def test_wait_until_serving_gives_up_early_when_the_process_dies() -> None:
    """No point waiting out the full timeout for a child that already exited."""
    started = time.time()
    result = supervisor.wait_until_serving(
        _free_port(), timeout=30.0, poll=0.1, is_alive=lambda: False
    )
    assert result is False
    assert time.time() - started < 2.0, "should not wait out the timeout"


# ── stop_until_dead ──────────────────────────────────────────────────────

def test_stop_until_dead_confirms_a_process_that_dies_on_the_first_kill() -> None:
    alive = {"v": True}
    kills = []

    def kill(attempt: int) -> None:
        kills.append(attempt)
        alive["v"] = False

    clock = _FakeClock()
    assert supervisor.stop_until_dead(
        is_alive=lambda: alive["v"], kill=kill,
        now=clock.now, sleep=clock.sleep,
    ) is True
    assert kills == [1], "should stop after the first successful kill"


def test_stop_until_dead_retries_when_the_first_kill_does_not_take() -> None:
    """taskkill /F /T timed out mid-tree in production. Retrying is the point."""
    state = {"alive": True, "kills": 0}

    def kill(attempt: int) -> None:
        state["kills"] += 1
        if state["kills"] >= 2:
            state["alive"] = False

    clock = _FakeClock()
    assert supervisor.stop_until_dead(
        is_alive=lambda: state["alive"], kill=kill,
        attempts=3, now=clock.now, sleep=clock.sleep,
    ) is True
    assert state["kills"] == 2


def test_stop_until_dead_reports_failure_when_the_process_never_dies() -> None:
    """PID 11728. Returning False is what lets the launcher refuse to spawn."""
    kills = []
    clock = _FakeClock()
    assert supervisor.stop_until_dead(
        is_alive=lambda: True, kill=lambda a: kills.append(a),
        attempts=3, now=clock.now, sleep=clock.sleep,
    ) is False
    assert kills == [1, 2, 3], "every attempt should be used before giving up"


def test_stop_until_dead_does_not_kill_an_already_dead_process() -> None:
    kills = []
    clock = _FakeClock()
    assert supervisor.stop_until_dead(
        is_alive=lambda: False, kill=lambda a: kills.append(a),
        now=clock.now, sleep=clock.sleep,
    ) is True
    assert kills == []


# ── perform_restart ──────────────────────────────────────────────────────
#
# The ordering that was wrong in production. Each test below is a step the
# launcher previously skipped.

def _restart_spy(**overrides):
    """Build a perform_restart call with recording defaults. Returns
    (result, calls) where calls is an ordered list of step names."""
    calls = []

    def _rec(name, value):
        def _fn(*_args, **_kwargs):
            calls.append(name)
            return value
        return _fn

    kwargs = dict(
        stop_server=_rec("stop", True),
        port_is_free=_rec("port_check", True),
        sweep_orphans=_rec("sweep", None),
        start_server=_rec("start", None),
        verify_serving=_rec("verify", True),
        kill_child=_rec("kill_child", None),
        log=lambda _msg: None,
    )
    for key, value in overrides.items():
        kwargs[key] = value
    return supervisor.perform_restart(**kwargs), calls


def test_perform_restart_starts_the_server_on_the_happy_path() -> None:
    ok, calls = _restart_spy()
    assert ok is True
    assert "start" in calls
    assert calls.count("start") == 1


def test_perform_restart_refuses_to_spawn_while_the_port_is_still_held() -> None:
    """THE regression test. On 2026-07-31 the launcher spawned PID 1852 into a
    port PID 11728 was still serving, producing an inert duplicate. It must
    now decline to start anything at all."""
    ok, calls = _restart_spy(
        stop_server=lambda: False,          # kill could not be confirmed
        port_is_free=lambda: False,         # old server still listening
    )
    assert ok is False
    assert "start" not in calls, (
        "spawned a server into an occupied port — this is the original bug"
    )


def test_perform_restart_sweeps_strays_when_the_kill_is_unconfirmed() -> None:
    """_kill_orphans already existed and would have caught PID 11728; the
    failure path just never called it."""
    ok, calls = _restart_spy(stop_server=lambda: False)
    assert "sweep" in calls
    assert ok is True


def test_perform_restart_rechecks_the_port_after_sweeping() -> None:
    """A sweep that frees the port should let the restart proceed rather than
    aborting on the pre-sweep reading."""
    readings = iter([False, True])
    ok, calls = _restart_spy(port_is_free=lambda: next(readings))
    assert ok is True
    assert "start" in calls


def test_perform_restart_kills_a_child_that_never_binds_the_port() -> None:
    """An inert child must not be left running and tracked as healthy."""
    ok, calls = _restart_spy(verify_serving=lambda: False)
    assert ok is False
    assert "kill_child" in calls


def test_perform_restart_checks_the_port_before_starting_not_after() -> None:
    ok, calls = _restart_spy()
    assert calls.index("port_check") < calls.index("start")
