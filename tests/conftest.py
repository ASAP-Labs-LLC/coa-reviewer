"""Shared pytest fixtures for the COA Reviewer Web App.

Notes
-----
Importing ``app`` runs heavy module-level side effects (creates AppState,
which instantiates QBenchAPIClient + LabCoreClient, opens log
handlers, and creates ``archive/``). That's fine for these tests because
none of it makes outbound network calls and the few filesystem touches
land in the project root.

For the helper-function tests we monkeypatch the module-level path
constants (``CONFIG_FILE``, ``RE_REVIEW_STATE_FILE``) to point inside a
``tmp_path`` so we never read or clobber real config/state.
"""

from __future__ import annotations

# QBench credentials come from a local store (see qbench_secrets). Point that
# store at a throwaway file with dummy values BEFORE anything imports ``app``,
# which builds an AppState -> QBenchAPIClient at module scope. Without this the
# suite would either fail on an unconfigured machine or, worse, pick up a
# developer's real credentials.
import json as _json
import os as _os
import tempfile as _tempfile

_store = _os.path.join(_tempfile.mkdtemp(prefix="qbench-test-store-"), "qbench.json")
with open(_store, "w", encoding="utf-8") as _fh:
    _json.dump({"client_id": "test-client-id", "client_secret": "test-client-secret"}, _fh)
_os.environ.setdefault("QBENCH_STORE_PATH", _store)

# Same reasoning for state. Importing ``app`` creates archive/, opens a
# rotating app.log, seeds a config and appends to the change log; with
# COA_DATA_DIR unset all of that lands in the source tree, so simply running
# the suite dirtied the tracked changelog/*.jsonl files with test audit
# entries. Point DATA_DIR at a throwaway directory before ``app`` is imported.
# ``setdefault`` so tests/test_data_dir.py can still probe both branches.
_os.environ.setdefault("COA_DATA_DIR", _tempfile.mkdtemp(prefix="coa-test-data-"))

import socket
import sys
import threading
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class LocalServer:
    """A real TCP server that binds, listens, AND accepts, for port-probe tests.

    Accepting is not incidental. A socket that only listens fills its accept
    backlog after a handful of probe connections; further connects then fail
    and the port looks free, which would make port-probe tests pass or fail
    for reasons having nothing to do with the code under test. The real
    server accepts, so the test double must too.

    Pass ``port=0`` for an ephemeral port; read the assigned one from ``.port``.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.host = host
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(50)
        self.port = self.sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
                conn.close()
            except OSError:
                return

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2)

    def __enter__(self) -> "LocalServer":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def free_port() -> int:
    """Reserve then release an ephemeral port, returning its number."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def isolated_app_paths(tmp_path, monkeypatch):
    """Redirect ``app.CONFIG_FILE`` and ``app.RE_REVIEW_STATE_FILE`` into ``tmp_path``.

    Use this in any test that touches load_config / save_config /
    load_re_review_state / save_re_review_state so the real files on
    disk are never read or written.
    """
    import app

    cfg_path = tmp_path / "web_app_config.json"
    state_path = tmp_path / "re_review_state.json"
    monkeypatch.setattr(app, "CONFIG_FILE", cfg_path)
    monkeypatch.setattr(app, "RE_REVIEW_STATE_FILE", state_path)
    return cfg_path, state_path
