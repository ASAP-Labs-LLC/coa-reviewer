"""COA Reviewer Web App — system tray launcher with auto-restart on file changes."""
import subprocess
import sys
import os
import time
import glob
import threading
import ctypes
import webbrowser
import shutil
import traceback

# Hide this launcher's own console window
if sys.platform == "win32":
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 0)

import pystray
from PIL import Image, ImageDraw

import supervisor

APP_DIR = os.path.dirname(os.path.abspath(__file__))
script = os.path.join(APP_DIR, "app.py")
LOG_FILE = os.path.join(APP_DIR, "server.log")
LAUNCHER_LOG = os.path.join(APP_DIR, "launcher.log")
MAX_LOG_SIZE = 1 * 1024 * 1024  # 1 MB
PORT = 5559

# How long to wait for the outgoing server to release the port. Replaces a
# flat sleep: the old code slept 4 seconds and started regardless of whether
# anything was still listening.
PORT_FREE_TIMEOUT = 30
# How long a fresh app.py gets to bind and answer. It re-checks the port
# itself for up to 15s before binding, so this must comfortably exceed that.
SERVE_TIMEOUT = 45
# Kill escalation: each attempt is verified before the next one is tried.
KILL_ATTEMPTS = 3
KILL_VERIFY_TIMEOUT = 6

proc = None
proc_lock = threading.Lock()
_log_fh = None

log_window_proc = None
log_window_lock = threading.Lock()


def _llog(msg):
    """Write a line to launcher.log so we can debug Run.pyw itself."""
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LAUNCHER_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def get_mtimes():
    mtimes = {}
    for path in glob.glob(os.path.join(APP_DIR, "*.py")):
        try:
            mtimes[path] = os.path.getmtime(path)
        except OSError:
            pass
    return mtimes


def _rotate_log():
    """Truncate server.log if it's too large."""
    try:
        if not os.path.exists(LOG_FILE):
            return
        if os.path.getsize(LOG_FILE) <= MAX_LOG_SIZE:
            return
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        keep = lines[-500:] if len(lines) > 500 else lines
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write(f"--- Log rotated at {time.strftime('%Y-%m-%d %H:%M:%S')} (was {len(lines)} lines) ---\n")
            f.writelines(keep)
    except Exception:
        pass


def _close_log_handle():
    """Close the current log file handle if open."""
    global _log_fh
    if _log_fh:
        try:
            _log_fh.close()
        except Exception:
            pass
        _log_fh = None


def _kill_pid_tree(pid, tree=True, timeout=20):
    """Force-kill ``pid``, and every descendant when ``tree`` is set.

    ``Popen.terminate()`` on Windows is ``TerminateProcess`` — it only kills
    the immediate process and leaves Playwright/Chromium subprocesses
    orphaned (still holding port 5559, still writing to server.log, still
    impossible to bind a new server to). ``taskkill /F /T`` walks the
    process tree and kills everything in one shot.

    Walking the tree is also what hangs: on 2026-07-31 ``taskkill /F /T``
    blew through its 8-second budget against a Chromium tree whose working
    directory is on a network share, was killed mid-walk by the timeout, and
    left the root process alive. Hence ``tree=False`` — the escalation path
    targets the parent alone, which cannot stall enumerating children — and a
    much more generous default timeout.
    """
    if pid is None:
        return
    if sys.platform == "win32":
        cmd = ["taskkill", "/F"] + (["/T"] if tree else []) + ["/PID", str(pid)]
        try:
            subprocess.run(
                cmd, check=False, timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            _llog(f"  {' '.join(cmd)} failed: {e}")
    else:
        try:
            os.kill(pid, 9)
        except Exception:
            pass


def _list_orphans():
    """Return PIDs of every python/pythonw whose command line references this
    ``APP_DIR`` and is **not** this launcher process. Used to mop up zombies
    left behind by a previous run (or by an uncleanly-killed Run.pyw).

    Windows-only — returns ``[]`` everywhere else. Best-effort: any failure
    returns an empty list rather than raising, so launcher boot never blocks
    on this.
    """
    if sys.platform != "win32":
        return []
    ps = shutil.which("powershell") or "powershell"
    # APP_DIR may contain single quotes; PowerShell escapes them by doubling.
    needle = APP_DIR.replace("'", "''")
    script = (
        "Get-CimInstance Win32_Process -Filter "
        "\"Name='python.exe' or Name='pythonw.exe'\" "
        f"| Where-Object {{ $_.CommandLine -like '*{needle}*' }} "
        "| Select-Object -ExpandProperty ProcessId "
        "| ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.check_output(
            [ps, "-NoProfile", "-NonInteractive", "-Command", script],
            stderr=subprocess.DEVNULL,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).decode("utf-8", errors="replace").strip()
    except Exception as e:
        _llog(f"  _list_orphans powershell failed: {e}")
        return []
    if not out:
        return []
    try:
        import json
        data = json.loads(out)
    except Exception:
        return []
    pids = [data] if isinstance(data, int) else list(data)
    self_pid = os.getpid()
    return [int(p) for p in pids if int(p) != self_pid]


def _kill_orphans(reason):
    """Find and kill any zombie python at this APP_DIR. Idempotent + safe to
    call at startup, on quit, and during recovery."""
    pids = _list_orphans()
    if not pids:
        return
    _llog(f"Cleaning up {len(pids)} stray python(s) at {APP_DIR} "
          f"(reason: {reason}): {pids}")
    for pid in pids:
        _kill_pid_tree(pid)


def start_server():
    global _log_fh
    _rotate_log()
    _log_fh = open(LOG_FILE, "a")
    env = os.environ.copy()
    env["COA_WATCHER_ACTIVE"] = "1"
    p = subprocess.Popen(
        [sys.executable, "-u", script],
        stdout=_log_fh,
        stderr=_log_fh,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    _llog(f"Server started (PID {p.pid})")
    return p


def stop_server():
    """Stop the tracked server. Returns True only when it is confirmed gone.

    The old version fired one ``taskkill``, logged whatever went wrong, and
    returned nothing — so callers had no way to know the process survived.
    That silence is what allowed a second server to be started alongside a
    live one.
    """
    with proc_lock:
        p = proc
        if p is None or p.poll() is not None:
            _close_log_handle()
            return True

        _llog(f"Stopping server (PID {p.pid}) + descendants")

        def _kill(attempt):
            # Attempt 2 drops /T: enumerating a big Chromium tree is the part
            # that stalls, and killing the root alone still frees the port.
            tree = attempt != 2
            _llog(f"  kill attempt {attempt}/{KILL_ATTEMPTS} for PID {p.pid} "
                  f"(tree={tree})")
            _kill_pid_tree(p.pid, tree=tree)

        dead = supervisor.stop_until_dead(
            is_alive=lambda: p.poll() is None,
            kill=_kill,
            attempts=KILL_ATTEMPTS,
            verify_timeout=KILL_VERIFY_TIMEOUT,
        )
        if dead:
            _llog(f"  PID {p.pid} confirmed stopped")
        else:
            _llog(f"  WARNING: PID {p.pid} STILL ALIVE after {KILL_ATTEMPTS} "
                  f"attempts — refusing to start a second server")
        _close_log_handle()
        return dead


def _start_tracked():
    """Spawn the server and record it as the tracked child."""
    global proc
    with proc_lock:
        try:
            proc = start_server()
        except Exception as e:
            _llog(f"  start_server FAILED: {e}\n{traceback.format_exc()}")
            proc = None


def _verify_serving():
    """True once the tracked child is actually answering on PORT."""
    p = proc
    if p is None:
        return False
    return supervisor.wait_until_serving(
        PORT, timeout=SERVE_TIMEOUT, poll=0.5,
        is_alive=lambda: p.poll() is None,
    )


def _kill_tracked_child():
    p = proc
    if p is not None and p.poll() is None:
        _llog(f"  killing non-serving child PID {p.pid}")
        _kill_pid_tree(p.pid)


def _do_restart(reason="unknown"):
    """The core restart sequence. Returns True if a server is up and serving.

    Ordering and failure handling live in ``supervisor.perform_restart`` so
    they are covered by real tests; this function only supplies the
    Windows-specific pieces.
    """
    _llog(f"Restart sequence starting (reason: {reason})")
    try:
        ok = supervisor.perform_restart(
            stop_server=stop_server,
            port_is_free=lambda: supervisor.wait_until_free(
                PORT, timeout=PORT_FREE_TIMEOUT, poll=0.5),
            sweep_orphans=_kill_orphans,
            start_server=_start_tracked,
            verify_serving=_verify_serving,
            kill_child=_kill_tracked_child,
            log=lambda msg: _llog(f"  {msg}"),
        )
    except Exception as e:
        _llog(f"  restart sequence crashed: {e}\n{traceback.format_exc()}")
        ok = False
    _llog(f"Restart sequence complete (reason: {reason}, serving={ok})")
    return ok


def is_log_open():
    global log_window_proc
    with log_window_lock:
        return log_window_proc is not None and log_window_proc.poll() is None


def on_toggle_log(icon, item):
    global log_window_proc
    with log_window_lock:
        if log_window_proc is not None and log_window_proc.poll() is None:
            log_window_proc.terminate()
            log_window_proc = None
            return
        powershell = shutil.which("powershell") or "powershell"
        cmd = f"Get-Content -Path '{LOG_FILE}' -Wait -Tail 40"
        log_window_proc = subprocess.Popen(
            [powershell, "-NoExit", "-Command", cmd],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )


def on_restart(icon, item):
    _do_restart("tray menu")
    try:
        icon.notify("Application restarted", "COA Reviewer")
    except Exception:
        pass


def on_open_browser(icon, item):
    webbrowser.open(f"http://127.0.0.1:{PORT}")


def on_quit(icon, item):
    _llog("Quit selected from tray menu")
    stop_server()
    # Belt-and-suspenders: stop_server only kills the child we tracked. Any
    # python at this APP_DIR that we did NOT spawn (e.g. an earlier zombie
    # that was respawning itself, or a separately-launched debug instance)
    # gets caught here.
    _kill_orphans("quit from tray")
    try:
        icon.stop()
    except Exception:
        pass
    os._exit(0)


def watch_files(icon):
    """Monitor server process health and file changes. NEVER let this thread die."""
    global proc
    mtimes = get_mtimes()
    failures = 0
    _llog("File watcher started")

    while True:
        try:
            time.sleep(1)

            # ── Check 1: is the server missing or dead? ──
            needs_restart = False
            exit_code = None
            with proc_lock:
                if proc is None:
                    # A previous restart declined to start (port still held)
                    # or the spawn itself failed. Keep trying.
                    needs_restart = True
                elif proc.poll() is not None:
                    exit_code = proc.returncode
                    needs_restart = True

            if needs_restart:
                _llog(f"Server not running (exit code {exit_code}), restarting...")
                ok = _do_restart(f"process exited with code {exit_code}")
                if ok:
                    failures = 0
                    try:
                        icon.notify(
                            f"Application restarted (exit code {exit_code})",
                            "COA Reviewer",
                        )
                    except Exception:
                        pass
                else:
                    # Backoff matters here: without it a permanently occupied
                    # port would spin this loop once a second forever.
                    failures += 1
                    delay = min(300, 15 * failures)
                    _llog(f"  restart did not come up ({failures} consecutive); "
                          f"retrying in {delay}s")
                    if failures == 1:
                        try:
                            icon.notify(
                                "Server did not come up — retrying. "
                                "Check launcher.log.", "COA Reviewer",
                            )
                        except Exception:
                            pass
                    time.sleep(delay)
                mtimes = get_mtimes()
                continue

            # ── Check 2: did any .py files change? ──
            new_mtimes = get_mtimes()
            changed = [f for f, t in new_mtimes.items() if mtimes.get(f) != t]
            if changed:
                names = ", ".join(os.path.basename(f) for f in changed)
                _llog(f"Files changed: {names}")
                _do_restart(f"file change: {names}")
                mtimes = get_mtimes()
                try:
                    icon.notify(f"Restarted — {names}", "COA Reviewer")
                except Exception:
                    pass
            else:
                mtimes = new_mtimes

        except Exception as e:
            # NEVER let this thread die — log the error and keep going
            _llog(f"watch_files error (recovering): {e}\n{traceback.format_exc()}")
            time.sleep(3)


def make_icon():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, 62, 62], fill=(30, 120, 200))
    d.ellipse([14, 14, 50, 50], fill=(255, 255, 255))
    d.ellipse([22, 22, 42, 42], fill=(30, 120, 200))
    return img


# Start server on launch
_llog("="*40)
_llog("Launcher starting")
# If a previous Run.pyw was killed uncleanly (Task Manager, crash, blue
# screen) its app.py subprocess may still be alive holding port 5559.
# Sweep before starting our own so we don't fight that zombie for the port.
_kill_orphans("launcher startup")
# Route the first start through the same verified path as every restart, so
# a launcher started while another server is live refuses to add a duplicate
# instead of quietly creating one. The watcher retries if this doesn't take.
if not _do_restart("launcher startup"):
    _llog("Initial start did not come up — the file watcher will keep retrying.")

menu = pystray.Menu(
    pystray.MenuItem("Open Browser", on_open_browser, default=True),
    pystray.MenuItem("Restart Application", on_restart),
    pystray.Menu.SEPARATOR,
    pystray.MenuItem(
        lambda item: "Hide Log" if is_log_open() else "Show Log",
        on_toggle_log,
    ),
    pystray.Menu.SEPARATOR,
    pystray.MenuItem("Quit", on_quit),
)
icon = pystray.Icon("COA Reviewer", make_icon(), "COA Reviewer", menu)

watcher = threading.Thread(target=watch_files, args=(icon,), daemon=True)
watcher.start()

icon.run()
