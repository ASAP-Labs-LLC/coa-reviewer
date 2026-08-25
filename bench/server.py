"""Run the real Flask app against a synthetic lab, in its own process.

Its own process on purpose: "peak server RSS" is the number the run has to
report, and measuring it in a process that also holds Playwright, the PDF
fixtures and the frame decoder would measure the harness.

The parent talks to this over three channels only — environment on the way
in, one JSON line on stdout when the port is bound, and SIGTERM on the way
out.

Order matters at the top of ``main``:

1. ``QBENCH_STORE_PATH`` and ``COA_DATA_DIR`` are set **before** ``import
   app``. Without the first, importing app raises ``QBenchSecretMissing``;
   without the second, ``archive/``, ``app.log``, ``changelog/`` and
   ``.secret_key`` are written into the source tree.
2. ``os.cpu_count`` is patched **before** ``import app``, because
   ``PREVIEW_POOL`` is sized at module scope from ``_preview_workers()``,
   which is ``min(8, max(4, os.cpu_count()))`` and has no env override.
   Patching the stdlib function in *this* process is the only way to pin the
   pool size without editing app.py. The value actually resolved is reported
   back to the parent either way, so runs stay comparable.

The preview path has two modes, chosen by ``BENCH_FAKE_PREVIEW``:

* **real** (default) — app.py's own ``COASession``, redirected at
  ``BENCH_QBENCH_BASE`` (the parent's ``bench/qbench_fake.py`` server). This is
  the only mode in which the single session lock, the poll cadence and the
  render wait are actually exercised.
* **fake** — ``bench.fakes.FakeCoaSession``, which returns a URL instantly.
  Kept so results produced before the real path existed stay reproducible.

The fake QBench server lives in the **parent**, not here, for the same reason
the PDF fixtures do: this process's RSS is a reported number, and generating
250 COAs in it would measure the harness.

On the way out this process writes its lock measurements to
``BENCH_STATS_PATH``. A file rather than stdout because the parent is inside a
``finally`` by then and pipe draining at exit is a race.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _prepare_isolation() -> None:
    """Point the app's two filesystem seams at throwaway locations."""
    if not os.environ.get("QBENCH_STORE_PATH"):
        store = Path(tempfile.mkdtemp(prefix="bench-qbench-")) / "qbench.json"
        store.write_text(json.dumps({"client_id": "x", "client_secret": "y"}), "utf-8")
        os.environ["QBENCH_STORE_PATH"] = str(store)
    if not os.environ.get("COA_DATA_DIR"):
        os.environ["COA_DATA_DIR"] = tempfile.mkdtemp(prefix="bench-coa-data-")


def _pin_cpu_count() -> None:
    forced = _env_int("BENCH_CPU_COUNT", 0)
    if forced > 0:
        os.cpu_count = lambda: forced          # noqa: E731  (see module docstring)


def main() -> int:
    _prepare_isolation()
    _pin_cpu_count()

    from bench.fakes import install
    from bench.realpreview import install_real_coa_session, lock_stats
    from bench.synth import SyntheticLab

    import app as app_module          # noqa: E402  (must follow the env setup)
    from werkzeug.serving import make_server

    count = _env_int("BENCH_SAMPLES", 20)
    seed = _env_int("BENCH_SEED", 1)
    per_order = _env_int("BENCH_SAMPLES_PER_ORDER", 5)
    pdf_base = os.environ.get("BENCH_PDF_BASE", "")
    if not pdf_base:
        raise SystemExit("BENCH_PDF_BASE is required (the loopback PDF fixture server)")

    fake_preview = os.environ.get("BENCH_FAKE_PREVIEW", "") == "1"
    qbench_base = os.environ.get("BENCH_QBENCH_BASE", "")
    if not fake_preview and not qbench_base:
        raise SystemExit("BENCH_QBENCH_BASE is required unless BENCH_FAKE_PREVIEW=1")

    # The tab we want populated. /api/start dispatches Due Out first in tests
    # mode, and asks QBench for it by lab-id prefix.
    prefix = app_module.business_days_ago(3).strftime("%m%d%y")

    lab = SyntheticLab(
        count=count, seed=seed, samples_per_order=per_order,
        prefix=prefix, base_url=pdf_base,
        # This process never renders a PDF; the parent serves them. Sizes are
        # irrelevant here and are left at whatever the parent used.
    )
    if fake_preview:
        session = install(app_module, lab, prefix, pdf_base)
    else:
        # Order matters: the real session must exist before install() puts it
        # on state, and QBENCH_BASE must be redirected before any preview runs.
        session = install_real_coa_session(app_module, qbench_base)
        install(app_module, lab, prefix, pdf_base, session=session)

    server = make_server("127.0.0.1", 0, app_module.app, threaded=True)
    port = server.server_port

    print(json.dumps({
        "ready": True,
        "port": port,
        "pid": os.getpid(),
        "prefix": prefix,
        "preview_workers": app_module._preview_workers(),
        "cpu_count": os.cpu_count(),
        "data_dir": os.environ["COA_DATA_DIR"],
        "preview_mode": "fake" if fake_preview else "real",
        "coa_session_class": type(session).__name__,
        # app.py's own thresholds, so the bar the parent reports against
        # cannot go stale in bench/results.py.
        "session_timeout_s": app_module.SESSION_TIMEOUT_SECONDS,
        "session_cleanup_s": app_module.SESSION_CLEANUP_SECONDS,
    }), flush=True)

    def _bye(*_a):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _bye)
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        server.server_close()
        _write_stats(session, lock_stats)
    return 0


def _write_stats(session, lock_stats) -> None:
    """Hand the parent what only this process can see."""
    path = os.environ.get("BENCH_STATS_PATH")
    if not path:
        return
    payload = {
        "coa_session_class": type(session).__name__,
        "lock": lock_stats(session),
        "preview_calls": getattr(session, "calls", None),
    }
    try:
        Path(path).write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
