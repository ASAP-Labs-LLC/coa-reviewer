"""Unit tests for the ``bench/`` benchmark harness's own logic.

These deliberately cover only the parts that can be wrong *silently*:

* synthetic-data generation, because a sample the app drops on the floor
  makes the whole run measure fewer samples than it claims to;
* the N-samples invariant, which is the run's own tripwire for that;
* percentile / INP maths, because a wrong p95 is indistinguishable from a
  slow app; and
* results serialisation, because a run that cannot be written down took
  ten minutes for nothing.

Nothing here launches Chromium or starts a server — the browser-driving
half is covered by actually running it (``python -m bench.run``), which is
far too slow to belong in the normal suite.
"""

from __future__ import annotations

import json

import pytest

fitz = pytest.importorskip("fitz")

from bench import metrics, results, synth


# ── percentiles ──────────────────────────────────────────────────────────

def test_percentile_of_a_single_value_is_that_value() -> None:
    assert metrics.percentile([42.0], 95) == 42.0


def test_percentile_interpolates_between_ranks() -> None:
    """Linear interpolation (numpy's default), so p95 of 1..10 is 9.55.

    Nearest-rank would say 10. The difference matters at N=20, where a
    single outlier is 5% of the sample.
    """
    assert metrics.percentile(list(range(1, 11)), 95) == pytest.approx(9.55)


def test_percentile_ignores_input_order() -> None:
    assert metrics.percentile([9, 1, 5, 3, 7], 50) == 5


def test_percentile_of_nothing_is_none() -> None:
    assert metrics.percentile([], 95) is None


def test_p50_p95_p99_are_monotonic() -> None:
    vals = [float(v) for v in range(1, 201)]
    assert metrics.percentile(vals, 50) < metrics.percentile(vals, 95) < metrics.percentile(vals, 99)


# ── INP ──────────────────────────────────────────────────────────────────

def test_inp_takes_the_worst_interaction_when_there_are_few() -> None:
    """Under 50 interactions INP is defined as the worst one."""
    entries = [
        {"interactionId": 1, "duration": 40, "name": "click"},
        {"interactionId": 2, "duration": 120, "name": "keydown"},
        {"interactionId": 3, "duration": 8, "name": "click"},
    ]
    assert metrics.inp(entries) == 120


def test_inp_collapses_the_events_of_one_interaction() -> None:
    """pointerdown/pointerup/click share an interactionId and are ONE
    interaction whose duration is the largest of them — counting them
    separately would triple-weight every click."""
    entries = [
        {"interactionId": 7, "duration": 30, "name": "pointerdown"},
        {"interactionId": 7, "duration": 90, "name": "pointerup"},
        {"interactionId": 7, "duration": 90, "name": "click"},
    ]
    assert metrics.inp(entries) == 90
    assert metrics.interaction_durations(entries) == [90]


def test_inp_drops_events_with_no_interaction_id() -> None:
    """interactionId 0 means 'not an interaction' (mousemove, mouseover)."""
    entries = [
        {"interactionId": 0, "duration": 5000, "name": "mouseover"},
        {"interactionId": 4, "duration": 25, "name": "click"},
    ]
    assert metrics.inp(entries) == 25


def test_inp_discards_one_outlier_per_fifty_interactions() -> None:
    """The web-vitals definition: the (floor(n/50))-th worst, 0-indexed."""
    entries = [{"interactionId": i, "duration": float(i), "name": "click"}
               for i in range(1, 101)]
    # 100 interactions -> index 2 of the descending list -> 98
    assert metrics.inp(entries) == 98


def test_inp_of_nothing_is_none() -> None:
    assert metrics.inp([]) is None


# ── synthetic data ───────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def lab() -> synth.SyntheticLab:
    # Deliberately tiny PDFs: this test is about the *shape* of the data.
    return synth.SyntheticLab(count=20, seed=7, coa_bytes=20_000,
                              sif_bytes=20_000, samples_per_order=5)


def test_it_makes_exactly_the_requested_number_of_samples(lab) -> None:
    assert len(lab.samples) == 20


def test_every_sample_has_at_least_one_test(lab) -> None:
    """app.py drops a sample with no tests silently (the ``if info['test_ids']``
    filter in fetch_samples_for_tab). A generator that misses one makes the
    run measure 19 samples while reporting 20."""
    sample_ids = {s["id"] for s in lab.samples}
    tested = {t["sample_id"] for t in lab.tests_for(sorted(sample_ids))}
    assert tested == sample_ids


def test_the_served_count_equals_the_requested_count(lab) -> None:
    """The same invariant the live run asserts against ``#sample-count``,
    computed the way app.py computes it."""
    assert synth.served_count(lab.samples, lab.tests_for([s["id"] for s in lab.samples])) == 20


def test_a_sample_without_tests_is_counted_as_dropped() -> None:
    """Negative control for the invariant above — if this passes trivially
    the assertion above proves nothing."""
    samples = [{"id": 1, "lab_id": "a", "order_id": 1},
               {"id": 2, "lab_id": "b", "order_id": 1}]
    tests = [{"id": 10, "sample_id": 1}]
    assert synth.served_count(samples, tests) == 1


def test_lab_ids_are_unique(lab) -> None:
    assert len({s["lab_id"] for s in lab.samples}) == 20


def test_samples_share_orders_the_way_a_real_batch_does(lab) -> None:
    """SIF PDFs are cached per order, so orders must be shared or the SIF
    cache never gets exercised."""
    orders = {s["order_id"] for s in lab.samples}
    assert len(orders) == 4  # 20 samples / 5 per order


def test_prefix_matches_what_app_asks_for(lab) -> None:
    """fetch_samples_by_lab_id_prefix is called with %m%d%y; the lab_ids must
    start with that prefix or the UI shows nothing."""
    for s in lab.samples:
        assert s["lab_id"].startswith(lab.prefix), s["lab_id"]


# ── attachments ──────────────────────────────────────────────────────────

def test_the_sif_attachment_survives_app_pys_filename_filter(lab) -> None:
    """_sif_find_candidates rejects any filename containing coa/certificate/
    report, and anything not ending .pdf."""
    import app  # noqa: F401  (conftest has already made this importable)
    from app import _sif_find_candidates

    atts = lab.order_attachments(lab.samples[0]["order_id"])
    assert _sif_find_candidates(atts), "the SIF attachment was filtered out"


def test_the_coa_attachments_do_not_masquerade_as_sifs(lab) -> None:
    from app import _sif_find_candidates

    atts = lab.sample_attachments(lab.samples[0]["id"])
    assert _sif_find_candidates(atts) == []


# ── PDFs ─────────────────────────────────────────────────────────────────

def test_the_coa_is_a_pdf_a_real_reader_can_open(lab) -> None:
    """The repo's existing fake is b'%PDF-fake', which Chromium renders as an
    error page rather than a document — measuring that would measure nothing."""
    doc = fitz.open(stream=lab.coa_pdf(lab.samples[0]["lab_id"]), filetype="pdf")
    assert len(doc) >= 1
    doc.close()


def test_the_coa_is_close_to_the_requested_size(lab) -> None:
    """PDF size is a run parameter and is recorded in the JSON; a generator
    that ignores it makes two runs incomparable."""
    n = len(lab.coa_pdf(lab.samples[0]["lab_id"]))
    assert 20_000 <= n <= 20_000 * 1.15


def test_the_sif_carries_each_labs_id_as_text(lab) -> None:
    """app.py's _sif_find_page does a text search before it rasterises and
    barcode-scans. Without the text the run would measure pyzbar instead."""
    order_id = lab.samples[0]["order_id"]
    members = [s for s in lab.samples if s["order_id"] == order_id]
    doc = fitz.open(stream=lab.sif_pdf(order_id), filetype="pdf")
    assert len(doc) == len(members)
    text = "".join(doc[p].get_text() for p in range(len(doc)))
    doc.close()
    for s in members:
        assert s["lab_id"] in text


def test_the_page_app_py_finds_is_the_samples_own_page(lab) -> None:
    from app import _sif_find_page

    order_id = lab.samples[0]["order_id"]
    members = [s for s in lab.samples if s["order_id"] == order_id]
    pdf = lab.sif_pdf(order_id)
    for i, s in enumerate(members):
        assert _sif_find_page(pdf, s["lab_id"]) == i


def test_consecutive_samples_get_different_paint_markers(lab) -> None:
    """The COA-rendered signal is 'the marker colour for THIS lab appeared in
    the viewer pane'. Two adjacent samples sharing a colour would make a
    switch look instantaneous because the previous COA already matched."""
    marks = [lab.marker_index(s["lab_id"]) for s in lab.samples]
    assert all(a != b for a, b in zip(marks, marks[1:]))


def test_the_same_seed_produces_the_same_lab() -> None:
    a = synth.SyntheticLab(count=5, seed=3, coa_bytes=10_000, sif_bytes=10_000)
    b = synth.SyntheticLab(count=5, seed=3, coa_bytes=10_000, sif_bytes=10_000)
    assert [s["lab_id"] for s in a.samples] == [s["lab_id"] for s in b.samples]


# ── results serialisation ────────────────────────────────────────────────

def _sample_run() -> results.RunResult:
    return results.RunResult(
        samples=20,
        params=results.RunParams(samples=20, seed=1, coa_bytes=750_000,
                                 sif_bytes=400_000, cpu_throttle=4, cores=2,
                                 rss_ceiling_mb=1536, js_heap_mb=256,
                                 preview_workers=4, samples_per_order=5),
        served_count=20,
        coa_open_ms=[100.0, 120.0, 90.0],
        switch_ms=[80.0, 85.0],
        inp_ms=64.0,
        interaction_ms=[64.0, 32.0],
        peak_rss_mb=412.5,
        rss_samples=17,
    )


def test_a_result_round_trips_through_json(tmp_path) -> None:
    run = _sample_run()
    path = run.write(tmp_path)
    assert path.name == "run-N20.json"
    loaded = json.loads(path.read_text("utf-8"))
    assert loaded["samples"] == 20
    assert loaded["served_count"] == 20
    assert loaded["params"]["coa_bytes"] == 750_000


def test_the_result_reports_percentiles_not_just_raw_timings(tmp_path) -> None:
    """A run that only stores the raw list makes every reader recompute the
    metric the run exists to report."""
    doc = _sample_run().to_dict()
    assert doc["coa_open"]["p95"] == pytest.approx(metrics.percentile([100.0, 120.0, 90.0], 95))
    assert doc["coa_open"]["n"] == 3
    assert doc["switch"]["p95"] == pytest.approx(metrics.percentile([80.0, 85.0], 95))


def test_the_result_records_the_resolved_preview_worker_count(tmp_path) -> None:
    """_preview_workers() is min(8, max(4, cpu_count)) with no env override,
    so two machines run different pool sizes. Recording it is what makes the
    numbers comparable."""
    assert _sample_run().to_dict()["params"]["preview_workers"] == 4


def test_the_result_flags_a_run_that_measured_fewer_samples_than_asked(tmp_path) -> None:
    run = _sample_run()
    run.served_count = 19
    assert run.to_dict()["ok"] is False
    run.served_count = 20
    assert run.to_dict()["ok"] is True


def test_the_summary_line_is_one_line_and_names_the_numbers() -> None:
    line = _sample_run().summary_line()
    assert "\n" not in line
    assert "N=20" in line
    assert "p95" in line


def test_a_run_with_no_measurements_still_serialises(tmp_path) -> None:
    """A failed run must produce a file that says so, not a traceback."""
    run = _sample_run()
    run.coa_open_ms = []
    run.switch_ms = []
    run.inp_ms = None
    doc = run.to_dict()
    assert doc["coa_open"]["p95"] is None
    assert doc["inp_ms"] is None
    run.write(tmp_path)


# ── the fakes, against the real app code path ────────────────────────────
#
# These run the actual ``fetch_samples_for_tab`` — the function whose silent
# drop the whole N-samples invariant exists to guard against — with the
# harness's doubles wired in. No browser, no server, no PDFs: the preview
# fan-out at the end of that function is disabled by leaving coa_session
# unset, which is exactly the branch app.py already guards.

@pytest.fixture
def wired_app(monkeypatch, lab):
    import app as app_module
    from bench import fakes

    monkeypatch.setattr(app_module.state, "api_client",
                        fakes.build_api_client(lab, lab.prefix))
    monkeypatch.setattr(app_module.state, "labcore", fakes.build_labcore())
    monkeypatch.setattr(app_module.state, "coa_session", None)
    monkeypatch.setattr(app_module.state, "logged_in", True)
    return app_module


def test_the_app_serves_every_synthetic_sample(wired_app, lab) -> None:
    """The invariant, checked through app.py's own map-filter-record path."""
    from datetime import datetime

    ustate = wired_app.UserState("bench-test-uid", "Bench Reviewer")
    target = datetime.strptime(lab.prefix, "%m%d%y").date()
    wired_app.fetch_samples_for_tab("Due Out", target, ustate)

    assert len(ustate.records) == lab.count
    assert {r.lab_id for r in ustate.records.values()} == {s["lab_id"] for s in lab.samples}
    assert all(r.test_ids for r in ustate.records.values())
    assert all(r.order_id for r in ustate.records.values())


def test_a_non_matching_date_serves_nothing(wired_app, lab) -> None:
    """Only the Due Out prefix is answered. /api/start fans out over three
    tabs; answering all of them would triple the run's real N."""
    from datetime import date

    ustate = wired_app.UserState("bench-test-uid-2", "Bench Reviewer")
    wired_app.fetch_samples_for_tab("Yesterday", date(1999, 1, 4), ustate)
    assert ustate.records == {}


def test_the_labcore_double_survives_jsonify(wired_app) -> None:
    """/api/cc/config returns labcore.base_url straight through jsonify; a
    bare MagicMock attribute serialises into something unusable."""
    import json

    from app import state as app_state

    json.dumps({"lab_vision_url": app_state.labcore.base_url,
                "available": app_state.labcore.is_available()})


def test_the_fake_preview_session_returns_a_loopback_url(lab) -> None:
    from bench.fakes import FakeCoaSession

    sess = FakeCoaSession(lab, "http://127.0.0.1:9/")
    url = sess.generate_preview(sample_id=lab.samples[0]["id"], test_ids=[1],
                                order_id=lab.samples[0]["order_id"],
                                attachment_ids=None, skip_attachments=False)
    assert url == f"http://127.0.0.1:9/coa/{lab.samples[0]['lab_id']}.pdf"
    # app.py probes the URL for a redirect and closes the response.
    resp = sess._session.get(url, timeout=30, allow_redirects=True, stream=True)
    assert resp.url == url
    resp.close()
