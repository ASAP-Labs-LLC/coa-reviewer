"""The benchmark's fake QBench, and the real ``COASession`` it exists to drive.

Every benchmark run so far replaced ``COASession`` wholesale with
``bench.fakes.FakeCoaSession``, whose ``generate_preview`` returns an f-string
instantly. That fake hid the three things a real pull actually spends its time
on, and none of them had ever been exercised by the harness:

* the single ``_session_lock`` held across the preview POST *and* every poll
  GET, which serialises every worker onto one connection;
* the per-preview poll cadence, whose first delay is 0.75 s — a hard floor no
  server, however fast, can get under;
* the pull outrunning ``SESSION_TIMEOUT_SECONDS``.

So these tests are about one thing: that the harness drives the *real*
``COASession`` against a loopback server that behaves like QBench, and that
every parameter which decides the answer is honoured and recorded.

Nothing here is a substitute for running ``python -m bench.run`` — the sweep is
the measurement. These only guard the parts that would be wrong silently.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

fitz = pytest.importorskip("fitz")

from bench import synth


# ── fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def lab() -> synth.SyntheticLab:
    # Tiny PDFs: these tests are about the protocol, not the byte count.
    return synth.SyntheticLab(count=4, seed=11, coa_bytes=20_000,
                              sif_bytes=20_000, samples_per_order=2)


@pytest.fixture
def instant(lab):
    """A fake QBench with no latency at all, for protocol assertions."""
    from bench.qbench_fake import QBenchFakeServer

    srv = QBenchFakeServer(lab, post_ms=0, poll_ms=0, render_s=0.0,
                           jitter=0.0, seed=3).start()
    try:
        yield srv
    finally:
        srv.stop()


def _post(srv, sample_id: int, report_config_id: str = "18") -> dict:
    body = urllib.parse.urlencode([
        ("sample_id", sample_id),
        ("report_config_id", report_config_id),
        ("test_ids", 1),
    ]).encode()
    req = urllib.request.Request(
        f"{srv.base_url}/report/preview", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _poll(srv, preview_id: int) -> dict:
    url = f"{srv.base_url}/report/preview/get?id={preview_id}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read())


# ── the POST ─────────────────────────────────────────────────────────────

def test_the_post_hands_back_an_integer_preview_id(instant, lab) -> None:
    """``generate_preview`` reads ``preview["id"]`` and bails on a falsy one."""
    got = _post(instant, lab.samples[0]["id"])
    assert isinstance(got["id"], int)
    assert got["id"]


def test_the_post_is_rejected_without_the_fields_qbench_requires(instant) -> None:
    """A harness that silently accepted an empty body would let a broken
    ``generate_preview`` look healthy."""
    for form in ([("report_config_id", "18")], [("sample_id", "1")]):
        req = urllib.request.Request(
            f"{instant.base_url}/report/preview",
            data=urllib.parse.urlencode(form).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=30)
        assert exc.value.code == 400


def test_every_post_gets_its_own_preview_id(instant, lab) -> None:
    ids = {_post(instant, s["id"])["id"] for s in lab.samples}
    assert len(ids) == len(lab.samples)


# ── the render state machine ─────────────────────────────────────────────

def test_the_render_status_is_pending_until_the_render_time_elapses(lab) -> None:
    from bench.qbench_fake import QBenchFakeServer

    srv = QBenchFakeServer(lab, post_ms=0, poll_ms=0, render_s=0.6,
                           jitter=0.0, seed=3).start()
    try:
        pid = _post(srv, lab.samples[0]["id"])["id"]
        assert _poll(srv, pid)["render_status"] == "PENDING"
        time.sleep(0.75)
        assert _poll(srv, pid)["render_status"] == "SUCCESSFUL"
    finally:
        srv.stop()


def test_an_unknown_preview_id_is_a_404_not_a_success(instant) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"{instant.base_url}/report/preview/get?id=9999",
                               timeout=30)
    assert exc.value.code == 404


# ── latency ──────────────────────────────────────────────────────────────

def test_the_post_latency_is_honoured(lab) -> None:
    from bench.qbench_fake import QBenchFakeServer

    srv = QBenchFakeServer(lab, post_ms=300, poll_ms=0, render_s=0.0,
                           jitter=0.0, seed=3).start()
    try:
        t0 = time.perf_counter()
        _post(srv, lab.samples[0]["id"])
        assert time.perf_counter() - t0 >= 0.30
    finally:
        srv.stop()


def test_the_poll_latency_is_honoured(lab) -> None:
    from bench.qbench_fake import QBenchFakeServer

    srv = QBenchFakeServer(lab, post_ms=0, poll_ms=200, render_s=0.0,
                           jitter=0.0, seed=3).start()
    try:
        pid = _post(srv, lab.samples[0]["id"])["id"]
        t0 = time.perf_counter()
        _poll(srv, pid)
        assert time.perf_counter() - t0 >= 0.20
    finally:
        srv.stop()


def test_the_server_is_threaded_so_workers_are_not_serialised_by_it(lab) -> None:
    """The contention under measurement is app.py's single session lock. A
    single-threaded fixture server would add contention of its own and the run
    would be measuring the harness."""
    from bench.qbench_fake import QBenchFakeServer

    srv = QBenchFakeServer(lab, post_ms=300, poll_ms=0, render_s=0.0,
                           jitter=0.0, seed=3).start()
    try:
        errors: list = []

        def go(sid):
            try:
                _post(srv, sid)
            except Exception as exc:      # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=go, args=(s["id"],)) for s in lab.samples]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - t0
        assert not errors
        # 4 x 300 ms serialised would be 1.2 s; concurrent is ~0.3 s.
        assert elapsed < 0.9, f"{len(threads)} concurrent POSTs took {elapsed:.2f}s"
    finally:
        srv.stop()


def test_a_client_that_hangs_up_does_not_spam_the_runs_stderr() -> None:
    """app.py probes the preview URL with stream=True and closes it without
    reading the body. socketserver prints a traceback per reset by default —
    two per sample, which would bury anything that actually went wrong."""
    from bench.qbench_fake import _QuietThreadingHTTPServer

    noisy = []
    try:
        raise ConnectionResetError(54, "Connection reset by peer")
    except ConnectionResetError:
        _QuietThreadingHTTPServer.handle_error(
            type("S", (), {"handle_error": lambda *a: noisy.append(a)})(),
            None, ("127.0.0.1", 1))
    assert noisy == []


def test_jitter_is_seeded_and_deterministic() -> None:
    """A run whose latencies differ between invocations cannot be compared
    with the run before it."""
    from bench.qbench_fake import jitter_factor

    a = [jitter_factor(7, "post", k) for k in range(20)]
    b = [jitter_factor(7, "post", k) for k in range(20)]
    c = [jitter_factor(8, "post", k) for k in range(20)]
    assert a == b
    assert a != c
    assert len(set(a)) > 1, "a constant 'jitter' is not jitter"


def test_jitter_stays_inside_the_requested_fraction() -> None:
    from bench.qbench_fake import jittered

    for k in range(200):
        v = jittered(1.0, 0.25, seed=2, kind="poll", key=k)
        assert 0.75 <= v <= 1.25
    assert jittered(1.0, 0.0, seed=2, kind="poll", key=1) == 1.0


# ── the PDF on the success URL ───────────────────────────────────────────

def test_the_success_url_serves_that_samples_real_pdf(instant, lab) -> None:
    """This is the URL ``generate_preview`` returns (app.py:815) and the one
    ``cache_pdf`` later fetches. It must carry the *same* PyMuPDF document the
    PDF fixture server serves, masthead colour and all, or paint detection in
    ``bench/browser.py`` stops working."""
    sample = lab.samples[2]
    pid = _post(instant, sample["id"])["id"]
    with urllib.request.urlopen(f"{instant.base_url}/report/preview?id={pid}",
                                timeout=30) as resp:
        assert resp.headers["Content-Type"] == "application/pdf"
        body = resp.read()

    assert body == lab.coa_pdf(sample["lab_id"])
    doc = fitz.open(stream=body, filetype="pdf")
    try:
        assert len(doc) >= 1
        want = synth.MARKERS[lab.marker_index(sample["lab_id"])]
        fills = [d.get("fill") for d in doc[0].get_drawings()]
        assert any(f and max(abs(a - b) for a, b in zip(f, want)) < 0.01
                   for f in fills), "the masthead colour marker is missing"
    finally:
        doc.close()


# ── the real COASession, end to end ──────────────────────────────────────

@pytest.fixture
def app_module(monkeypatch):
    """``app`` with every global the installer touches restored afterwards."""
    import app as mod

    monkeypatch.setattr(mod, "QBENCH_BASE", mod.QBENCH_BASE)
    monkeypatch.setattr(mod.state, "coa_session", mod.state.coa_session)
    monkeypatch.setattr(mod.state, "logged_in", mod.state.logged_in)
    return mod


def test_the_harness_installs_the_real_coa_session_not_the_fake(
        app_module, instant) -> None:
    """The whole point of this work. If this ever goes back to the fake, every
    number the sweep reports is meaningless."""
    from bench.fakes import FakeCoaSession
    from bench.realpreview import install_real_coa_session

    sess = install_real_coa_session(app_module, instant.base_url)

    assert type(sess) is app_module.COASession
    assert not isinstance(sess, FakeCoaSession)
    assert app_module.state.coa_session is sess
    assert app_module.state.logged_in is True
    # The module global generate_preview reads (app.py:153) now points at us.
    assert app_module.QBENCH_BASE == instant.base_url.rstrip("/")


def test_the_installed_session_never_calls_the_real_login(
        app_module, instant) -> None:
    """``login()`` drives Playwright against real QBench. A benchmark that
    reached it would either hang or hit production."""
    from bench.realpreview import install_real_coa_session

    sess = install_real_coa_session(app_module, instant.base_url)
    assert sess.login() is None          # no Playwright, no network
    sess.relogin()                       # the retry path app.py takes
    assert sess.csrf_token, "the 'logged in' guard at app.py:713 must still pass"


def test_the_real_generate_preview_runs_the_whole_protocol(
        app_module, instant, lab) -> None:
    """POST -> poll -> success URL, through app.py's own code."""
    from bench.realpreview import install_real_coa_session

    sess = install_real_coa_session(app_module, instant.base_url)
    sample = lab.samples[1]

    t0 = time.perf_counter()
    url = sess.generate_preview(sample_id=int(sample["id"]), test_ids=[1, 2],
                                order_id=int(sample["order_id"]))
    elapsed = time.perf_counter() - t0

    assert url and url.startswith(f"{instant.base_url}/report/preview?id=")
    # The 0.75 s first poll delay is a floor no server can get under; a result
    # faster than that would mean the poll loop was not actually run.
    assert elapsed >= 0.75, f"generate_preview returned in {elapsed:.3f}s"

    with urllib.request.urlopen(url, timeout=30) as resp:
        assert resp.read() == lab.coa_pdf(sample["lab_id"])


def test_the_fake_preview_mode_is_still_available(lab) -> None:
    """Earlier results were produced with the fully-faked session; it has to
    stay reachable or they stop being reproducible."""
    from bench.fakes import FakeCoaSession

    sess = FakeCoaSession(lab, "http://127.0.0.1:9/")
    assert sess.generate_preview(sample_id=lab.samples[0]["id"], test_ids=[1])


# ── the lock wrapper ─────────────────────────────────────────────────────

def test_the_instrumented_lock_measures_the_time_workers_spend_waiting() -> None:
    """Wrapping the session's own lock is how contention is measured without
    editing app.py."""
    from bench.realpreview import InstrumentedLock

    lock = InstrumentedLock(threading.Lock())
    started = threading.Event()

    def hold():
        with lock:
            started.set()
            time.sleep(0.25)

    t = threading.Thread(target=hold)
    t.start()
    started.wait(5)
    with lock:
        pass
    t.join()

    assert lock.acquisitions == 2
    assert lock.wait_total_s >= 0.15, lock.wait_total_s
    assert lock.hold_total_s >= 0.25, lock.hold_total_s


def test_an_uncontended_lock_records_almost_no_wait() -> None:
    """Negative control: without this the assertion above would pass on a
    wrapper that simply timed the whole block."""
    from bench.realpreview import InstrumentedLock

    lock = InstrumentedLock(threading.Lock())
    for _ in range(50):
        with lock:
            pass
    assert lock.acquisitions == 50
    assert lock.wait_total_s < 0.05


def test_the_instrumented_lock_is_a_drop_in_for_threading_lock() -> None:
    """app.py uses it as a context manager and nothing else, but a wrapper that
    cannot be acquired/released directly is a trap for the next reader."""
    from bench.realpreview import InstrumentedLock

    lock = InstrumentedLock(threading.Lock())
    assert lock.acquire() is True
    lock.release()
    with lock:
        pass
    assert lock.acquisitions == 2


# ── what the results file has to say ─────────────────────────────────────

def _params(**over):
    from bench.results import RunParams

    base = dict(samples=250, seed=1, coa_bytes=750_000, sif_bytes=400_000,
                cpu_throttle=4.0, cores=2, rss_ceiling_mb=1536, js_heap_mb=256,
                preview_workers=4, samples_per_order=5)
    base.update(over)
    return RunParams(**base)


def _run(ready_s, **over):
    from bench.results import RunResult

    kw = dict(samples=250, served_count=250, preview_all_ready_s=ready_s,
              preview_count=250, lock_wait_total_s=88.5, lock_acquisitions=1500,
              lock_hold_total_s=210.0)
    kw.update(over)
    return RunResult(params=_params(), **kw)


def test_the_result_records_every_latency_parameter_that_decides_the_answer() -> None:
    """These four are the run. A file that does not carry them cannot be
    compared with the run beside it."""
    doc = _run(412.0).to_dict()
    p = doc["params"]
    assert p["qbench_post_ms"] == 300
    assert p["qbench_poll_ms"] == 150
    assert p["qbench_render_s"] == 4.0
    assert p["qbench_jitter"] == 0.25
    assert p["preview_mode"] == "real"


def test_the_result_says_pass_or_fail_against_both_app_thresholds() -> None:
    doc = _run(412.0).to_dict()["verdict"]
    assert doc["session_timeout_s"] == 600
    assert doc["session_cleanup_s"] == 720
    assert doc["pass_session_timeout"] is True
    assert doc["pass_session_cleanup"] is True


def test_a_pull_between_the_two_thresholds_fails_only_the_first() -> None:
    doc = _run(660.0).to_dict()["verdict"]
    assert doc["pass_session_timeout"] is False
    assert doc["pass_session_cleanup"] is True


def test_a_pull_past_the_cleanup_threshold_fails_both() -> None:
    doc = _run(801.0).to_dict()["verdict"]
    assert doc["pass_session_timeout"] is False
    assert doc["pass_session_cleanup"] is False


def test_a_pull_that_never_finished_is_not_reported_as_a_pass() -> None:
    """``preview_all_ready_s`` is None when the wait timed out. Defaulting an
    unknown to True would turn a failed run into a green one."""
    doc = _run(None).to_dict()["verdict"]
    assert doc["pass_session_timeout"] is None
    assert doc["pass_session_cleanup"] is None


def test_the_thresholds_are_the_ones_app_py_actually_uses() -> None:
    """A tripwire: if app.py's constants move, the bar the sweep reports
    against moves with them instead of silently going stale."""
    import app

    from bench.results import RunResult

    assert RunResult.SESSION_TIMEOUT_S == app.SESSION_TIMEOUT_SECONDS == 600
    assert RunResult.SESSION_CLEANUP_S == app.SESSION_CLEANUP_SECONDS == 720


def test_the_result_records_the_lock_wait_and_the_preview_count() -> None:
    doc = _run(412.0).to_dict()
    assert doc["lock"]["wait_total_s"] == 88.5
    assert doc["lock"]["acquisitions"] == 1500
    assert doc["lock"]["hold_total_s"] == 210.0
    assert doc["preview_count"] == 250


def test_the_summary_line_names_the_verdict() -> None:
    line = _run(412.0).summary_line()
    assert "\n" not in line
    assert "PASS" in line


# ── the run's own flags ──────────────────────────────────────────────────

def test_the_run_exposes_every_latency_knob_with_the_documented_default() -> None:
    from bench.run import parse_args

    a = parse_args([])
    assert a.qbench_post_ms == 300
    assert a.qbench_poll_ms == 150
    assert a.qbench_render_s == 4.0
    assert a.qbench_jitter == 0.25
    assert a.fake_preview is False


def test_the_fully_faked_mode_is_reachable_from_the_command_line() -> None:
    from bench.run import parse_args

    assert parse_args(["--fake-preview"]).fake_preview is True


# ── a pull that never finishes ───────────────────────────────────────────
#
# The sweep's most important data point is the configuration that does NOT
# complete. The first version of this harness raised a Playwright timeout out
# of `_drive`, so that run wrote no file at all — the run that answers the
# question destroyed its own evidence. A pull that gives up must be recorded,
# with how long it waited and how far it got.

def _abandoned(waited_s, **over):
    from bench.results import RunResult

    kw = dict(samples=250, served_count=250, preview_all_ready_s=None,
              pull_gave_up_after_s=waited_s, ready_at_giveup=137,
              preview_count=250)
    kw.update(over)
    return RunResult(params=_params(), **kw)


def test_a_pull_abandoned_past_the_threshold_is_a_fail_not_an_unknown() -> None:
    """It waited 1800s and the samples were not ready. That is not 'we don't
    know' — it is the clearest FAIL the sweep can produce."""
    doc = _abandoned(1800.0).to_dict()["verdict"]
    assert doc["pass_session_timeout"] is False
    assert doc["pass_session_cleanup"] is False
    assert doc["completed"] is False


def test_a_pull_abandoned_before_the_threshold_stays_unknown() -> None:
    """Giving up at 120s says nothing about the 600s bar."""
    doc = _abandoned(120.0).to_dict()["verdict"]
    assert doc["pass_session_timeout"] is None
    assert doc["pass_session_cleanup"] is None


def test_the_result_records_how_long_it_waited_and_how_far_it_got() -> None:
    doc = _abandoned(1800.0).to_dict()["verdict"]
    assert doc["gave_up_after_s"] == 1800.0
    assert doc["ready_at_giveup"] == 137
    assert doc["preview_all_ready_s"] is None


def test_a_completed_pull_is_marked_completed() -> None:
    doc = _run(412.0).to_dict()["verdict"]
    assert doc["completed"] is True
    assert doc["gave_up_after_s"] is None


def test_an_abandoned_run_still_serialises_and_names_the_verdict(tmp_path) -> None:
    run = _abandoned(1800.0)
    assert "FAIL" in run.summary_line()
    path = run.write(tmp_path / "abandoned.json")
    assert path.is_file()
    json.loads(path.read_text("utf-8"))


# ── the frontend's own 10-minute wall ────────────────────────────────────

def test_the_frontends_inactivity_wall_is_the_same_ten_minutes() -> None:
    """``INACTIVITY_MS`` in static/js/app.js and ``SESSION_TIMEOUT_SECONDS``
    in app.py are the same 10 minutes, and ``triggerTimeout()`` closes the
    EventSource. So a pull that runs past 600s without the reviewer touching
    anything loses the only channel by which a preview reports 'ready' — the
    remaining samples can never be observed to land, however long you wait.
    That is what the sweep has to be able to distinguish from 'merely slow',
    and it is why the driver watches for the overlay."""
    from pathlib import Path

    import app

    js = Path(app.__file__).resolve().parent.joinpath("static/js/app.js").read_text("utf-8")
    assert "const INACTIVITY_MS = 10 * 60 * 1000;" in js
    assert app.SESSION_TIMEOUT_SECONDS == 600
    # The line that makes a late preview unobservable.
    assert "state.eventSource.close();" in js


def test_the_driver_knows_how_to_see_that_overlay() -> None:
    """A harness that cannot tell 'the app locked the reviewer out' from
    'still working' reports a 30-minute timeout for both."""
    from bench.browser import PULL_STATE_JS

    assert "timeout-overlay" in PULL_STATE_JS
    assert "hidden" in PULL_STATE_JS


def test_the_pull_outcome_carries_everything_the_result_needs() -> None:
    from bench.browser import PullOutcome

    out = PullOutcome(completed=False, elapsed_s=1800.0, ready=137, total=250,
                      frontend_timed_out=True, reason="frontend inactivity overlay")
    assert out.completed is False
    assert out.ready == 137
    assert out.frontend_timed_out is True
