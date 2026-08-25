"""``python -m bench.run --samples 250`` — one benchmark run, start to finish.

Everything that could make two runs incomparable is a flag with a documented
default, and every one of them is written into the results file:

    --samples N            how many synthetic samples the app serves
    --coa-bytes            COA PDF size          (default 750_000 ~ 0.75 MB)
    --sif-bytes            order SIF PDF size    (default 400_000)
    --samples-per-order    SIF sharing factor    (default 5)
    --throttle             CDP CPU throttle rate (default 4x)
    --cores                navigator.hardwareConcurrency override (default 2)
    --js-heap-mb           V8 --max-old-space-size for the renderer (default 256)
    --rss-ceiling-mb       the budget peak server RSS is judged against (1536)
    --preview-workers      pins app.py's PREVIEW_POOL size (default 4)
    --qbench-post-ms       latency of POST /report/preview       (default 300)
    --qbench-poll-ms       latency of each poll GET              (default 150)
    --qbench-render-s      time until a preview reports SUCCESSFUL (default 4)
    --qbench-jitter        fractional, seeded, deterministic     (default 0.25)
    --fake-preview         the old fully-faked COASession (see below)

On the machine cap: macOS refuses RLIMIT_AS / RLIMIT_DATA / RLIMIT_RSS and
there is no Docker here, so the *server* side is not constrained — the
ceiling is a threshold the measured peak is reported against, not a limit
that is enforced. The *client* side genuinely is constrained: V8's heap cap
and the CDP CPU/core overrides are all enforced by Chromium itself.

On the preview path: by default this drives app.py's **real** ``COASession``
against ``bench/qbench_fake.py`` on loopback, so the run actually pays for the
single session lock held across every preview HTTP call, the 0.75 s floor of
the poll cadence, and the render wait. ``--fake-preview`` restores the older
``bench.fakes.FakeCoaSession``, which returns a URL instantly and measures none
of that — it is kept only so results recorded before this existed remain
reproducible.

The four ``--qbench-*`` knobs are what decide whether the pull lands inside
``SESSION_TIMEOUT_SECONDS`` (app.py:164), so all four are written into the
results file, along with the PASS/FAIL against that threshold and against
``SESSION_CLEANUP_SECONDS`` (app.py:165).

On ``--preview-workers``: app.py sizes its pool from
``min(8, max(4, os.cpu_count()))`` at import, with no environment override
and no way to change it without editing the file. The child process patches
``os.cpu_count`` before importing app instead, which pins the pool from the
outside; the value that actually resolved is reported back and recorded, so
even an un-pinned run stays legible.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bench import metrics                                   # noqa: E402
from bench.browser import INP_INIT_SCRIPT, BenchDriver       # noqa: E402
from bench.pdfserver import PdfFixtureServer                 # noqa: E402
from bench.qbench_fake import QBenchFakeServer               # noqa: E402
from bench.results import RESULTS_DIR, RunParams, RunResult  # noqa: E402
from bench.synth import SyntheticLab                         # noqa: E402


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="bench.run", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--samples", type=int, default=20)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--coa-bytes", type=int, default=750_000,
                    help="size of each COA PDF (a real one is 0.5-2 MB)")
    ap.add_argument("--sif-bytes", type=int, default=400_000)
    ap.add_argument("--samples-per-order", type=int, default=5,
                    help="samples sharing one order's SIF PDF")
    ap.add_argument("--throttle", type=float, default=4.0,
                    help="Emulation.setCPUThrottlingRate (1 = no throttling)")
    ap.add_argument("--cores", type=int, default=2,
                    help="Emulation.setHardwareConcurrencyOverride")
    ap.add_argument("--js-heap-mb", type=int, default=256,
                    help="V8 --max-old-space-size for the renderer")
    ap.add_argument("--rss-ceiling-mb", type=int, default=1536,
                    help="budget the measured peak server RSS is judged against")
    ap.add_argument("--preview-workers", type=int, default=4,
                    help="pins app.py's PREVIEW_POOL (0 = leave it to this machine)")
    ap.add_argument("--qbench-post-ms", type=float, default=300.0,
                    help="latency of POST /report/preview on the fake QBench")
    ap.add_argument("--qbench-poll-ms", type=float, default=150.0,
                    help="latency of each poll GET on the fake QBench")
    ap.add_argument("--qbench-render-s", type=float, default=4.0,
                    help="how long until a preview reports SUCCESSFUL")
    ap.add_argument("--qbench-jitter", type=float, default=0.25,
                    help="fractional latency jitter; seeded and deterministic")
    ap.add_argument("--fake-preview", action="store_true",
                    help="use the old instant FakeCoaSession instead of app.py's "
                         "real COASession (kept for reproducing earlier results)")
    ap.add_argument("--max-interactions", type=int, default=0,
                    help="cap measured interactions per pass (0 = every sample)")
    ap.add_argument("--timeout", type=float, default=1200.0,
                    help="seconds to wait for all previews to finish")
    ap.add_argument("--out", default=str(RESULTS_DIR))
    ap.add_argument("--keep-going", action="store_true",
                    help="write a result file even if the served count is short")
    return ap.parse_args(argv)


def _start_app_process(pdf_base: str, qbench_base: str, stats_path: Path,
                       args: argparse.Namespace, log_path: Path):
    env = dict(os.environ)
    env.update({
        "BENCH_SAMPLES": str(args.samples),
        "BENCH_SEED": str(args.seed),
        "BENCH_SAMPLES_PER_ORDER": str(args.samples_per_order),
        "BENCH_PDF_BASE": pdf_base,
        "BENCH_QBENCH_BASE": qbench_base,
        "BENCH_FAKE_PREVIEW": "1" if args.fake_preview else "",
        "BENCH_STATS_PATH": str(stats_path),
        "BENCH_CPU_COUNT": str(args.preview_workers or 0),
        # A throwaway data dir per run keeps archive/, app.log, changelog/ and
        # .secret_key out of the source tree.
        "COA_DATA_DIR": tempfile.mkdtemp(prefix="bench-coa-data-"),
        "PYTHONUNBUFFERED": "1",
    })
    env.pop("QBENCH_STORE_PATH", None)   # the child makes its own dummy store
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "bench.server"],
        cwd=str(PROJECT_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=log, text=True,
    )
    line = proc.stdout.readline()
    if not line:
        proc.kill()
        raise RuntimeError(
            "the app process died before it was ready:\n"
            + log_path.read_text("utf-8")[-4000:]
        )
    return proc, json.loads(line)


def main(argv=None) -> int:
    args = parse_args(argv)
    log_path = Path(tempfile.gettempdir()) / f"bench-server-N{args.samples}.log"

    pdf_server = PdfFixtureServer().start()
    # Always started, even in --fake-preview mode: it costs nothing idle, and
    # a run that silently had nowhere to send previews would be worse.
    qbench = QBenchFakeServer(
        post_ms=args.qbench_post_ms, poll_ms=args.qbench_poll_ms,
        render_s=args.qbench_render_s, jitter=args.qbench_jitter,
        seed=args.seed,
    ).start()
    stats_path = Path(tempfile.mkdtemp(prefix="bench-stats-")) / "child.json"
    proc = None
    try:
        proc, hello = _start_app_process(pdf_server.base_url, qbench.base_url,
                                         stats_path, args, log_path)
        app_url = f"http://127.0.0.1:{hello['port']}"
        print(f"[bench] app pid={hello['pid']} port={hello['port']} "
              f"prefix={hello['prefix']} preview_workers={hello['preview_workers']} "
              f"cpu_count={hello['cpu_count']}")
        print(f"[bench] preview path: {hello.get('preview_mode')} "
              f"({hello.get('coa_session_class')}) "
              f"qbench post={args.qbench_post_ms:.0f}ms poll={args.qbench_poll_ms:.0f}ms "
              f"render={args.qbench_render_s:.2f}s jitter={args.qbench_jitter}")

        lab = SyntheticLab(
            count=args.samples, seed=args.seed,
            coa_bytes=args.coa_bytes, sif_bytes=args.sif_bytes,
            samples_per_order=args.samples_per_order,
            prefix=hello["prefix"], base_url=pdf_server.base_url,
        )
        pdf_server.lab = lab
        qbench.lab = lab
        print(f"[bench] COA {len(lab.coa_pdf(lab.samples[0]['lab_id'])):,} B  "
              f"SIF {len(lab.sif_pdf(lab.samples[0]['order_id'])):,} B")

        result = _drive(args, lab, app_url, hello, proc)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        pdf_server.stop()
        qbench.stop()

    _record_preview_path(result, args, hello, qbench, stats_path)

    path = result.write(Path(args.out))
    print(result.summary_line())
    print(f"[bench] wrote {path}")
    if args.keep_going:
        return 0
    return 0 if (result.ok and result.pass_session_timeout is not False) else 1


def _read_child_stats(stats_path: Path) -> dict:
    """What only the app process could see: its session lock.

    Written to a file on the child's way out rather than sent up its stdout
    pipe, because by then the parent is inside a ``finally`` draining nothing
    and the read would be a race.
    """
    try:
        return json.loads(stats_path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _record_preview_path(result: RunResult, args, hello: dict,
                         qbench: QBenchFakeServer, stats_path: Path) -> None:
    """Fold the preview-path measurements into the result.

    This is the half of the run the older harness could not report at all,
    because the preview it measured was an f-string.
    """
    stats = _read_child_stats(stats_path)
    lock = stats.get("lock") or {}
    result.session_timeout_s = float(hello.get("session_timeout_s")
                                     or RunResult.SESSION_TIMEOUT_S)
    result.session_cleanup_s = float(hello.get("session_cleanup_s")
                                     or RunResult.SESSION_CLEANUP_S)
    result.preview_count = (qbench.previews_created
                            if not args.fake_preview
                            else int(stats.get("preview_calls") or 0))
    if lock:
        result.lock_wait_total_s = lock.get("wait_total_s")
        result.lock_hold_total_s = lock.get("hold_total_s")
        result.lock_wait_max_s = lock.get("wait_max_s")
        result.lock_acquisitions = int(lock.get("acquisitions") or 0)
        result.lock_contended = int(lock.get("contended") or 0)

    cls = stats.get("coa_session_class") or hello.get("coa_session_class")
    result.notes.append(
        f"preview path: {result.params.preview_mode} ({cls}); "
        f"{result.preview_count} previews, {qbench.polls_served} poll GETs, "
        f"{qbench.hits.get('pdf', 0)} PDF fetches on the preview URL")
    if not args.fake_preview:
        result.notes.append(
            "app.py's poll cadence sleeps BEFORE its first check "
            "(_preview_poll_delays, app.py:260-275), so every preview carries a "
            "~0.75s floor regardless of how fast the render is reported")
    result.notes.append(
        "SESSION_TIMEOUT_SECONDS / SESSION_CLEANUP_SECONDS are thresholds the "
        "measured pull is reported against, not limits enforced during it: "
        "get_user_state() refreshes last_active on every authenticated request "
        "(app.py:1307), so the frontend's own polling keeps the session alive "
        "while a slow pull runs")


def _drive(args, lab, app_url: str, hello: dict, proc) -> RunResult:
    from playwright.sync_api import sync_playwright

    params = RunParams(
        samples=args.samples, seed=args.seed,
        coa_bytes=args.coa_bytes, sif_bytes=args.sif_bytes,
        cpu_throttle=args.throttle, cores=args.cores,
        rss_ceiling_mb=args.rss_ceiling_mb, js_heap_mb=args.js_heap_mb,
        preview_workers=int(hello["preview_workers"]),
        samples_per_order=args.samples_per_order,
        cpu_count=int(hello["cpu_count"] or 0),
        machine=f"{os.uname().sysname} {os.uname().machine}",
        preview_mode=str(hello.get("preview_mode") or
                         ("fake" if args.fake_preview else "real")),
        qbench_post_ms=args.qbench_post_ms,
        qbench_poll_ms=args.qbench_poll_ms,
        qbench_render_s=args.qbench_render_s,
        qbench_jitter=args.qbench_jitter,
        max_interactions=args.max_interactions,
    )
    result = RunResult(samples=args.samples, params=params)
    rss = metrics.RssSampler(hello["pid"]).start()

    with sync_playwright() as pw:
        # channel="chromium" is REQUIRED: plain headless=True is
        # chrome-headless-shell, which has no PDF plugin, so every COA would
        # sit on about:blank for ever.
        browser = pw.chromium.launch(
            channel="chromium", headless=True,
            args=[f"--js-flags=--max-old-space-size={args.js_heap_mb}",
                  "--disable-dev-shm-usage"],
        )
        params.chromium = browser.version
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        context.add_init_script(INP_INIT_SCRIPT)
        page = context.new_page()
        cdp = context.new_cdp_session(page)
        cdp.send("Emulation.setHardwareConcurrencyOverride",
                 {"hardwareConcurrency": args.cores})
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": args.throttle})

        driver = BenchDriver(page, context, cdp, app_url, lab, timeout_s=args.timeout)
        try:
            driver.login()
            pull = driver.open_app(args.samples)
            result.served_count = driver.served_count()
            ready = driver.ready_count()

            if pull.completed:
                result.preview_all_ready_s = pull.elapsed_s
                print(f"[bench] {result.served_count} samples served, {ready} ready, "
                      f"previews took {pull.elapsed_s:.1f}s")
            else:
                # Recorded, not raised. This is the data point the sweep exists
                # to find, and an exception here would mean no file at all.
                result.pull_gave_up_after_s = pull.elapsed_s
                result.ready_at_giveup = pull.ready
                result.frontend_timed_out = pull.frontend_timed_out
                result.notes.append(f"the pull did not complete: {pull.reason}")
                print(f"[bench] PULL DID NOT COMPLETE after {pull.elapsed_s:.1f}s — "
                      f"{pull.ready}/{pull.total} ready. {pull.reason}")
                if pull.frontend_timed_out:
                    result.notes.append(
                        "static/js/app.js triggerTimeout() fires at INACTIVITY_MS "
                        "(10 min, the same value as SESSION_TIMEOUT_SECONDS) and "
                        "closes the EventSource. SSE is the only channel carrying "
                        "'sample_status', so every preview still outstanding at "
                        "that moment is unobservable to the reviewer from then on, "
                        "however long the server keeps working.")
                # The interaction passes measure switching between COAs. With
                # samples still un-rendered — and, after a frontend timeout, an
                # overlay over the UI — they would measure neither.
                return result

            if ready != args.samples:
                result.notes.append(
                    f"{args.samples - ready} of {args.samples} previews did not "
                    f"reach 'ready'; their timings are excluded")

            n = args.samples if args.max_interactions <= 0 else min(args.samples, args.max_interactions)
            driver.settle()

            # Warm-up traversal, unmeasured. app.py memoises tests_data and
            # attachments on the SampleRecord the first time a sample is
            # opened, so without this the first pass would be measuring a
            # cold server and the second a warm one, and the two would not
            # be comparable with each other or across N.
            driver.select(0)
            driver.watcher.locate_pane()
            driver.watcher.start()
            driver.select(0)
            for i in range(1, n):
                driver.arrow_down(i)
            print(f"[bench] warm-up traversal done ({n} samples)")

            # Pass 1 — switch sample with the keyboard.
            # Reset the interaction log first: boot's two clicks (the review
            # pill and Start) each block for a second or more while the app
            # initialises and every sample's preview lands, and leaving them
            # in would make INP a measurement of application start-up rather
            # than of reviewing.
            driver.reset_events()
            driver.clear_http_cache()
            driver.select(0)
            lost_switch: list[int] = []
            for i in range(1, n):
                ms = driver.arrow_down(i)
                if ms is None:
                    result.switch_failures += 1
                    lost_switch.append(i)
                else:
                    result.switch_ms.append(ms)
            print(f"[bench] switch pass: {len(result.switch_ms)} timed, "
                  f"{result.switch_failures} lost")

            # Pass 2 — open a COA by clicking it in the list.
            driver.clear_http_cache()
            lost_open: list[int] = []
            for i in range(n):
                ms = driver.select(i)
                if ms is None:
                    result.coa_open_failures += 1
                    lost_open.append(i)
                else:
                    result.coa_open_ms.append(ms)
            print(f"[bench] open pass: {len(result.coa_open_ms)} timed, "
                  f"{result.coa_open_failures} lost")

            if lost_switch or lost_open:
                # Named, not just counted: a lost interaction is a COA whose
                # masthead never appeared inside the timeout, and which one it
                # was is the only way to tell a slow app from a blind harness.
                result.notes.append(
                    f"interactions with no observed paint inside the timeout — "
                    f"switch rows {lost_switch}, open rows {lost_open}")

            sse = driver.sse_counts()
            if sse.get("resync"):
                result.notes.append(
                    f"the server shed queued SSE events {sse['resync']} time(s) "
                    f"and told the client to resync; each resync reloads all "
                    f"five tabs and rebuilds the sample list")

            events = driver.collect_events()
            result.inp_ms = metrics.inp(events)
            result.interaction_ms = metrics.interaction_durations(events)
            result.notes.extend(driver.watcher.notes)
            result.notes.append(
                f"paint signal: first screencast frame whose viewer pane carries "
                f"the COA's masthead colour; {driver.watcher.frames_seen} frames "
                f"observed. Timings are quantised to the screencast cadence.")
            result.notes.append(
                "renderSampleList staggers row entrance animations 14ms apart, "
                "capped at 24 rows (~336ms), on tab render only — it does not "
                "touch the per-sample switch path measured here.")
            result.notes.append(
                f"PDF fixture hits: {json.dumps(dict(_pdf_hits(driver)))}")
            result.sse_events = sse
        finally:
            try:
                driver.watcher.stop()
            except Exception:
                pass
            context.close()
            browser.close()
            # In the finally, so an abandoned pull still reports peak RSS —
            # the run that failed to finish is the one whose memory profile
            # is most worth having.
            result.peak_rss_mb = rss.stop()
            result.rss_samples = rss.samples

    return result


def _pdf_hits(driver) -> dict:
    return {"browser_api_pdf_requests": driver.pdf_requests}


if __name__ == "__main__":
    raise SystemExit(main())
