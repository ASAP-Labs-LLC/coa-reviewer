"""Source-level guards for Run.pyw launcher behavior.

Run.pyw is a .pyw entrypoint that imports pystray, builds a tray icon,
and spawns a server subprocess at module load. Importing it under pytest
is heavyweight and Windows-only, so we lock its behavioral invariants
by reading the source and asserting structural properties.

These are retroactive regression guards. New launcher work is test-first.
"""
from __future__ import annotations

from pathlib import Path

RUN_PYW = Path(__file__).resolve().parent.parent / "Run.pyw"


def _function_body(src: str, name: str) -> str:
    start = src.find(f"def {name}")
    assert start != -1, f"def {name} not found in Run.pyw"
    next_def = src.find("\ndef ", start + 1)
    return src[start:next_def] if next_def != -1 else src[start:]


def test_run_pyw_defines_orphan_cleanup_helpers() -> None:
    """A previous Run.pyw killed uncleanly left an app.py zombie that
    held port 5559 for 40 minutes. The launcher must:
      - kill its own subprocess's *process tree* (Playwright/Chromium too)
      - sweep stray python processes at this APP_DIR on startup AND quit
    """
    src = RUN_PYW.read_text(encoding="utf-8")
    for fn in ("_kill_pid_tree", "_list_orphans", "_kill_orphans"):
        assert f"def {fn}" in src, f"{fn} missing from Run.pyw"


def test_stop_server_uses_tree_kill_not_terminate() -> None:
    """proc.terminate() on Windows is TerminateProcess — only kills the
    immediate child. Playwright/Chromium descendants survive and keep
    port 5559 + log file handles open. Must use _kill_pid_tree instead."""
    body = _function_body(RUN_PYW.read_text(encoding="utf-8"), "stop_server")
    assert "_kill_pid_tree" in body, (
        "stop_server must use _kill_pid_tree(proc.pid) to walk the tree"
    )
    assert "proc.terminate()" not in body, (
        "stop_server still calls proc.terminate(); switch to _kill_pid_tree"
    )


def test_launcher_startup_sweeps_orphans_before_starting_server() -> None:
    """If a previous Run.pyw was killed via Task Manager and its app.py
    subprocess survived, the new launcher would fight that zombie for
    port 5559. Sweep first, then start."""
    src = RUN_PYW.read_text(encoding="utf-8")
    start_idx = src.find("proc = start_server()")
    assert start_idx != -1, "bootstrap `proc = start_server()` missing"
    bootstrap_chunk = src[:start_idx]
    assert "_kill_orphans" in bootstrap_chunk, (
        "Launcher startup must call _kill_orphans before start_server()"
    )


def test_on_quit_sweeps_orphans() -> None:
    """Tray Quit must catch anything stop_server() didn't track —
    e.g. a separately-launched debug instance at this APP_DIR."""
    body = _function_body(RUN_PYW.read_text(encoding="utf-8"), "on_quit")
    assert "_kill_orphans" in body, (
        "on_quit must call _kill_orphans after stop_server"
    )


def test_list_orphans_is_windows_only() -> None:
    """_list_orphans shells out to PowerShell to enumerate processes by
    command line. It must no-op on non-Windows so cross-platform pytest
    runs (or future macOS/Linux deployments) don't crash."""
    body = _function_body(RUN_PYW.read_text(encoding="utf-8"), "_list_orphans")
    assert "sys.platform" in body and 'win32' in body, (
        "_list_orphans must guard with sys.platform != 'win32' return []"
    )
