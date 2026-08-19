"""Process/port supervision primitives for the Run.pyw launcher.

Kept in its own module (no pystray/PIL imports) so the launcher's decision
logic is importable and testable — Run.pyw itself spawns a server and builds
a tray icon at module load, which makes it untestable by construction.

Why these exist
---------------
On 2026-07-31 ``taskkill /F /T`` timed out mid-tree while stopping the live
server (PID 11728). ``proc.wait(5)`` then confirmed the process was still
alive. Both failures were logged — and the launcher started a second server
anyway. On Windows ``SO_REUSEADDR`` let that second process bind the
still-occupied port without error, so it ran forever serving zero
connections while the original kept answering. Killing the inert twin just
made the launcher spawn another one.

Every function here answers a question the launcher previously assumed:
*did the process actually die*, and *is the new one actually serving*.
"""

from __future__ import annotations

import socket
import time


def port_has_listener(port, host="127.0.0.1", probe_timeout=0.5):
    """True if something is accepting connections on ``host:port`` right now.

    Probes by connecting, never by binding. A bind probe cannot answer this:
    on Windows ``SO_REUSEADDR`` permits binding a port another socket is
    actively listening on, and on BSD/macOS a wildcard bind succeeds over a
    listener bound to a specific address. Dropping ``SO_REUSEADDR`` from a
    bind probe would instead report a TIME_WAIT port as busy even though the
    real server (Werkzeug sets ``SO_REUSEADDR``) would bind it fine. A
    completed TCP handshake is the only unambiguous signal.
    """
    try:
        with socket.create_connection((host, port), timeout=probe_timeout):
            return True
    except OSError:
        return False


def wait_until_free(port, timeout=30.0, poll=0.5, host="127.0.0.1"):
    """Block until nothing is listening on ``port``.

    Returns True once the port is clear, False if a listener is still there
    when ``timeout`` expires. False means: do not start a server here.
    """
    deadline = time.monotonic() + timeout
    while True:
        if not port_has_listener(port, host):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def wait_until_serving(port, timeout=30.0, poll=0.5, host="127.0.0.1", is_alive=None):
    """Block until a server is accepting connections on ``port``.

    Returns False if the deadline passes with nothing listening — which is
    how an inert child is caught. If ``is_alive`` is given and reports the
    process has exited, gives up immediately rather than waiting out the
    full timeout.
    """
    deadline = time.monotonic() + timeout
    while True:
        if port_has_listener(port, host):
            return True
        if is_alive is not None and not is_alive():
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def stop_until_dead(is_alive, kill, attempts=3, verify_timeout=5.0, poll=0.25,
                    now=None, sleep=None):
    """Kill a process and verify it actually died.

    ``kill(attempt)`` is called with the 1-based attempt number so callers can
    escalate — e.g. ``taskkill /F /T`` first, then a plain ``taskkill /F`` on
    the parent when walking the tree hangs. After each attempt the process is
    polled via ``is_alive`` for up to ``verify_timeout`` seconds.

    Returns True only when ``is_alive()`` reports the process is gone.
    Returning False is the signal the launcher was missing: the old server is
    still holding the port, so do not spawn a replacement.

    ``now``/``sleep`` are injectable for deterministic tests.
    """
    now = now or time.monotonic
    sleep = sleep or time.sleep

    for attempt in range(1, attempts + 1):
        if not is_alive():
            return True
        kill(attempt)
        deadline = now() + verify_timeout
        while now() < deadline:
            if not is_alive():
                return True
            sleep(poll)
    return not is_alive()


def perform_restart(*, stop_server, port_is_free, sweep_orphans, start_server,
                    verify_serving, kill_child, log):
    """Stop the running server and bring up a replacement, safely.

    The ordering here is the fix for the 2026-07-31 incident. Previously the
    launcher called stop, logged whatever went wrong, slept four seconds, and
    started a new process unconditionally. Now every step's answer is acted
    on:

    1. Stop the server. If death cannot be confirmed, sweep strays — the
       ``_kill_orphans`` sweep already existed and would have caught the
       surviving PID, it was simply never reached on the failure path.
    2. Refuse to start while anything is still listening on the port. This is
       the step that makes an inert duplicate impossible: no spawn, no twin.
    3. After starting, confirm the child actually binds the port. A child that
       never serves is killed rather than tracked as healthy.

    Returns True if a replacement is up and serving, False otherwise. A False
    result means no server is running, so the caller should retry (with
    backoff) rather than assume success.
    """
    if not stop_server():
        log("Could not confirm the previous server died — sweeping strays.")
        sweep_orphans("stop_server could not confirm the process died")

    if not port_is_free():
        log("Port still has a live listener — sweeping strays and re-checking.")
        sweep_orphans("port still held after stop")
        if not port_is_free():
            log("ABORT: port still served by another process. Not starting a "
                "second server (that is what created the inert duplicate).")
            return False

    start_server()

    if not verify_serving():
        log("New server never started serving — killing it so it cannot "
            "linger as an inert duplicate.")
        kill_child()
        return False

    return True
