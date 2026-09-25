"""The running app owns its own system tray icon.

Run.pyw used to put a pystray icon in the Windows tray (Open Browser /
Restart / Quit). The updater on ASAPSV1 launches ``app.py`` directly from
the release venv, so since the cutover there has been no icon and, from the
desktop, no way to restart the app short of Task Manager. The icon now
lives in the app process, where it ships with every release.

Rules:

* Windows only, and off when asked (``--no-tray`` on the command line, or
  ``COA_NO_TRAY=1``). The updater's staging health check starts every
  release on a scratch port for a few seconds; ``"health_args":
  ["--no-tray"]`` in its config keeps that run off the tray.
* Never fatal. No interactive desktop, no pystray, a backend error: the app
  logs it and serves without an icon.
* Restart goes through the same path as the toolbar button, so the app
  respawns itself and the updater's supervision is the backstop.
* No Quit. Under the updater a quit is a restart with a 20-second delay;
  stopping the app for real is ``updater.py pause`` plus a kill.

pystray is faked here; PIL draws the real image.
"""

from __future__ import annotations

import re
import threading
import types
from pathlib import Path

import pytest

import tray

ROOT = Path(__file__).resolve().parent.parent
APP_PY = (ROOT / "app.py").read_text(encoding="utf-8")
TRAY_PY = (ROOT / "tray.py").read_text(encoding="utf-8")


# ── a stand-in for pystray ───────────────────────────────────────────────

class FakeMenuItem:
    def __init__(self, text, action=None, default=False, enabled=True):
        self.text, self.action, self.default, self.enabled = text, action, default, enabled


class FakeMenu:
    SEPARATOR = "----"

    def __init__(self, *items):
        self.items = items


class FakeIcon:
    instances: list = []

    def __init__(self, name, image, title, menu):
        self.name, self.image, self.title, self.menu = name, image, title, menu
        self.ran_in = None
        self.stopped = False
        self.notifications = []
        FakeIcon.instances.append(self)

    def run(self):
        self.ran_in = threading.current_thread()

    def stop(self):
        self.stopped = True

    def notify(self, message, title=None):
        self.notifications.append((message, title))


class BrokenIcon(FakeIcon):
    def __init__(self, *a, **k):
        raise RuntimeError("no interactive desktop")


def _fake_pystray(icon_cls=FakeIcon):
    return types.SimpleNamespace(Icon=icon_cls, Menu=FakeMenu, MenuItem=FakeMenuItem)


@pytest.fixture(autouse=True)
def _clean():
    FakeIcon.instances.clear()
    tray.stop_tray()
    yield
    tray.stop_tray()


def _start(**overrides):
    calls = {"restart": 0, "opened": [], "logs": []}
    kwargs = dict(
        version="v3.2.0", port=5559, pid=4242, log_path=Path("C:/ASAPApps/coa/data/app.log"),
        restart=lambda: calls.__setitem__("restart", calls["restart"] + 1),
        open_url=lambda url: calls["opened"].append(url),
        show_log=lambda path: calls["logs"].append(path),
        env={}, platform="win32", argv=["app.py"], pystray_module=_fake_pystray(),
    )
    kwargs.update(overrides)
    thread = tray.start_tray(**kwargs)
    if thread is not None:
        thread.join(timeout=2)
    return thread, calls


def _labels(icon):
    return [i if i == FakeMenu.SEPARATOR else i.text for i in icon.menu.items]


def _item(icon, text):
    return next(i for i in icon.menu.items if i != FakeMenu.SEPARATOR and i.text == text)


# ── when there is an icon ────────────────────────────────────────────────

def test_no_tray_off_windows() -> None:
    wanted, why = tray.tray_wanted(env={}, platform="darwin", argv=["app.py"])
    assert wanted is False
    assert "Windows" in why


def test_no_tray_when_the_environment_says_so() -> None:
    wanted, why = tray.tray_wanted(env={"COA_NO_TRAY": "1"}, platform="win32", argv=["app.py"])
    assert wanted is False
    assert "COA_NO_TRAY" in why


def test_no_tray_when_the_command_line_says_so() -> None:
    """What the updater's health check passes, via `health_args`."""
    wanted, why = tray.tray_wanted(env={}, platform="win32", argv=["app.py", "--no-tray"])
    assert wanted is False
    assert "--no-tray" in why


def test_a_tray_on_windows_by_default() -> None:
    wanted, _ = tray.tray_wanted(env={}, platform="win32", argv=["app.py"])
    assert wanted is True


def test_start_tray_does_nothing_where_an_icon_is_not_wanted() -> None:
    thread, _ = _start(platform="darwin")
    assert thread is None
    assert FakeIcon.instances == []


def test_pystray_is_only_imported_when_an_icon_is_wanted() -> None:
    """The suite runs on macOS and the module is imported by app.py."""
    assert re.search(r"^(import pystray|from pystray)", TRAY_PY, re.M) is None


# ── what the icon offers ─────────────────────────────────────────────────

def test_the_menu_offers_open_restart_status_and_the_log() -> None:
    _start()
    icon = FakeIcon.instances[-1]
    assert _labels(icon) == [
        "Open COA Reviewer",
        "Restart Application",
        FakeMenu.SEPARATOR,
        "v3.2.0 \u00b7 port 5559 \u00b7 PID 4242",
        "Show Log",
    ]
    assert icon.title == "COA Reviewer"


def test_no_quit_entry() -> None:
    _start()
    assert "Quit" not in _labels(FakeIcon.instances[-1])


def test_double_click_opens_the_app() -> None:
    _start()
    assert _item(FakeIcon.instances[-1], "Open COA Reviewer").default is True


def test_the_status_line_is_information_not_a_button() -> None:
    _start()
    assert _item(FakeIcon.instances[-1], "v3.2.0 \u00b7 port 5559 \u00b7 PID 4242").enabled is False


def test_status_line_for_a_dev_checkout() -> None:
    assert tray.status_line("dev", 5559, 7) == "dev \u00b7 port 5559 \u00b7 PID 7"


def test_open_goes_to_the_local_address() -> None:
    _, calls = _start()
    icon = FakeIcon.instances[-1]
    item = _item(icon, "Open COA Reviewer")
    item.action(icon, item)
    assert calls["opened"] == ["http://127.0.0.1:5559"]


def test_restart_calls_back_once_and_says_so() -> None:
    _, calls = _start()
    icon = FakeIcon.instances[-1]
    item = _item(icon, "Restart Application")
    item.action(icon, item)
    assert calls["restart"] == 1
    assert icon.notifications and "Restart" in icon.notifications[0][0]


def test_show_log_opens_the_app_log() -> None:
    _, calls = _start()
    icon = FakeIcon.instances[-1]
    item = _item(icon, "Show Log")
    item.action(icon, item)
    assert calls["logs"] == [Path("C:/ASAPApps/coa/data/app.log")]


# ── how it runs ──────────────────────────────────────────────────────────

def test_the_icon_runs_on_its_own_daemon_thread() -> None:
    """Flask owns the main thread; the tray must never hold up serving."""
    thread, _ = _start()
    icon = FakeIcon.instances[-1]
    assert thread is not None and thread.daemon
    assert icon.ran_in is thread


def test_a_tray_that_cannot_be_created_is_not_fatal() -> None:
    thread, _ = _start(pystray_module=_fake_pystray(BrokenIcon))
    assert thread is None


def test_stop_tray_takes_the_icon_down() -> None:
    _start()
    icon = FakeIcon.instances[-1]
    tray.stop_tray()
    assert icon.stopped is True


def test_stop_tray_with_no_icon_is_fine() -> None:
    tray.stop_tray()


# ── how app.py uses it ───────────────────────────────────────────────────

def _main_block() -> str:
    return APP_PY[APP_PY.index('if __name__ == "__main__"'):]


def test_the_app_starts_the_tray_before_it_serves() -> None:
    main = _main_block()
    assert "start_tray(" in main
    assert main.index("start_tray(") < main.index("app.run(")


def test_the_tray_and_the_restart_button_share_one_restart_path() -> None:
    assert "def request_restart(" in APP_PY
    route = APP_PY[APP_PY.index('@app.route("/api/restart"'):]
    route = route[:route.index("\n@app.route", 1)]
    assert "request_restart(" in route
    # The tray's callback is a named helper so its behaviour can be tested
    # (tests/test_restart_switch.py); it must still go through request_restart.
    assert "restart=_restart_from_tray" in _main_block()
    helper = APP_PY[APP_PY.index("def _restart_from_tray"):]
    helper = helper[:helper.index("\ndef ", 1)]
    assert "request_restart(" in helper


def test_shutting_down_takes_the_icon_with_it() -> None:
    body = APP_PY[APP_PY.index("def _graceful_shutdown"):]
    body = body[:body.index("\ndef ", 1)]
    assert "stop_tray()" in body
