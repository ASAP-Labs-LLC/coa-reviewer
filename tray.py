"""System tray icon for the running COA Reviewer, on Windows.

Run.pyw used to own the icon (Open Browser / Restart / Quit) along with the
process it supervised. The updater on ASAPSV1 now launches ``app.py``
directly from the release venv, so the icon moved into the app itself: every
release carries it, and a restart from it goes through the same path as the
Restart button in the UI, which respawns the app (the updater's supervision
is the backstop).

Off when not wanted: not Windows, ``--no-tray`` on the command line (what the
updater's staging health check passes via ``"health_args"``), or
``COA_NO_TRAY=1``. Never fatal: no desktop, no pystray, a backend error, and
the app logs it and serves without an icon. There is deliberately no Quit;
under the updater a quit is a restart with a 20-second delay, and stopping
the app for real is ``updater.py pause`` plus a kill.

pystray is imported only when an icon is wanted, so importing this module
costs nothing on macOS or in the test suite.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

APP_NAME = "COA Reviewer"
NO_TRAY_FLAG = "--no-tray"
NO_TRAY_ENV = "COA_NO_TRAY"

_lock = threading.Lock()
_icon = None


def tray_wanted(*, env: Mapping[str, str], platform: str,
                argv: Sequence[str]) -> tuple[bool, str]:
    """Whether this process should put an icon in the tray, and why not."""
    if platform != "win32":
        return False, "the tray icon is Windows-only"
    if NO_TRAY_FLAG in argv:
        return False, f"{NO_TRAY_FLAG} on the command line"
    if (env.get(NO_TRAY_ENV) or "").strip().lower() in ("1", "true", "yes", "on"):
        return False, f"{NO_TRAY_ENV} is set"
    return True, "Windows desktop"


def status_line(version: str, port: int, pid: int) -> str:
    """The inert menu entry that says which build this is and where."""
    return f"{version} · port {port} · PID {pid}"


def _make_image():
    # The same blue disc Run.pyw drew, so the icon looks like it always did.
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, 62, 62], fill=(30, 120, 200))
    d.ellipse([14, 14, 50, 50], fill=(255, 255, 255))
    d.ellipse([22, 22, 42, 42], fill=(30, 120, 200))
    return img


def _default_show_log(path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]  # Windows only
    else:
        webbrowser.open(Path(path).resolve().as_uri())


def start_tray(*, version: str, port: int, pid: int, log_path,
               restart: Callable[[], None],
               open_url: Optional[Callable[[str], object]] = None,
               show_log: Optional[Callable[[object], object]] = None,
               env: Optional[Mapping[str, str]] = None,
               platform: Optional[str] = None,
               argv: Optional[Sequence[str]] = None,
               pystray_module=None) -> Optional[threading.Thread]:
    """Put the icon up on its own daemon thread. Returns the thread, or None
    when there is no icon (not wanted, or could not be created)."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    argv = sys.argv if argv is None else argv
    wanted, why = tray_wanted(env=env, platform=platform, argv=argv)
    if not wanted:
        logger.info("No tray icon: %s", why)
        return None

    open_url = open_url or webbrowser.open
    show_log = show_log or _default_show_log
    url = f"http://127.0.0.1:{port}"

    try:
        if pystray_module is None:
            import pystray as pystray_module  # noqa: PLC0415  # only when wanted
        pystray = pystray_module

        def on_open(icon, item):
            open_url(url)

        def on_restart(icon, item):
            try:
                icon.notify("Restarting — back in a few seconds.", APP_NAME)
            except Exception:
                pass
            restart()

        def on_log(icon, item):
            show_log(log_path)

        menu = pystray.Menu(
            pystray.MenuItem(f"Open {APP_NAME}", on_open, default=True),
            pystray.MenuItem("Restart Application", on_restart),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(status_line(version, port, pid), None, enabled=False),
            pystray.MenuItem("Show Log", on_log),
        )
        icon = pystray.Icon(APP_NAME, _make_image(), APP_NAME, menu)
    except Exception as exc:
        logger.warning("Tray icon unavailable: %s", exc)
        return None

    def run() -> None:
        try:
            icon.run()
        except Exception as exc:
            logger.warning("Tray icon stopped: %s", exc)

    global _icon
    with _lock:
        _icon = icon
    thread = threading.Thread(target=run, name="tray-icon", daemon=True)
    thread.start()
    return thread


def stop_tray() -> None:
    """Take the icon down, if there is one. Safe to call any time."""
    global _icon
    with _lock:
        icon, _icon = _icon, None
    if icon is None:
        return
    try:
        icon.stop()
    except Exception as exc:
        logger.debug("Tray icon did not stop cleanly: %s", exc)
