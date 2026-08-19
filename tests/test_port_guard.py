"""Guards for the pre-flight port check in ``app.py``.

Background
----------
On 2026-07-31 the production launcher spawned a second ``app.py`` while the
previous one (PID 11728) was still alive — ``taskkill /F /T`` had timed out.
The new process did not exit; it bound port 5559 *alongside* the live server
and then sat inert, serving zero connections, while the old process kept
answering.

``_wait_for_port`` exists precisely to prevent that: it is supposed to return
False when the port is occupied so startup aborts with exit code 1. It failed
because it probed by **binding with SO_REUSEADDR** — the one socket option
whose entire purpose is to permit rebinding an address another socket already
holds. On Windows SO_REUSEADDR allows a second bind to a live listening
socket outright; on BSD/macOS it allows a wildcard (0.0.0.0) bind over a
listener bound to a specific address. Either way the guard reports "free"
while a server is answering on that port.

The probe must detect a *live listener*, which is what these tests pin down.
"""

from __future__ import annotations

import threading
import time

import app
from tests.conftest import LocalServer, free_port as _free_port


def _listening_socket(host: str):
    """A live accepting server on an ephemeral port. Returns (server, port)."""
    srv = LocalServer(host)
    return srv, srv.port


def test_reports_port_busy_when_listener_bound_to_loopback() -> None:
    """A server listening on 127.0.0.1 means the port is NOT available.

    This is the exact production failure, reproducible off-Windows: the old
    SO_REUSEADDR bind probe succeeds against a loopback-bound listener and
    reports the port free.
    """
    srv, port = _listening_socket("127.0.0.1")
    try:
        assert _wait_for_port_is_busy(port) is False, (
            f"_wait_for_port said port {port} is free while a listener is "
            f"accepting connections on 127.0.0.1:{port}"
        )
    finally:
        srv.close()


def test_reports_port_busy_when_listener_bound_to_wildcard() -> None:
    """Same, for a server listening on 0.0.0.0 — how app.run() binds."""
    srv, port = _listening_socket("0.0.0.0")
    try:
        assert _wait_for_port_is_busy(port) is False
    finally:
        srv.close()


def test_reports_port_available_when_nothing_is_listening() -> None:
    """The happy path must still work — a free port reports free promptly."""
    port = _free_port()
    started = time.time()
    assert app._wait_for_port(port, timeout=5.0) is True
    assert time.time() - started < 2.0, "free-port check should return immediately"


def test_waits_for_a_listener_to_go_away_within_the_timeout() -> None:
    """During a restart the old server may still be shutting down. The probe
    must keep polling and report success once the port is genuinely released,
    rather than giving up on the first attempt."""
    srv, port = _listening_socket("127.0.0.1")

    def _close_soon() -> None:
        time.sleep(1.5)
        srv.close()

    closer = threading.Thread(target=_close_soon, daemon=True)
    closer.start()
    try:
        assert app._wait_for_port(port, timeout=10.0) is True
    finally:
        closer.join(timeout=5)
        srv.close()


def _wait_for_port_is_busy(port: int) -> bool:
    """Call the real probe with a short timeout (it polls once per second)."""
    return app._wait_for_port(port, timeout=1.0)
