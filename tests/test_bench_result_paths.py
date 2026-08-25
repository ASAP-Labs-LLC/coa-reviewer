"""A benchmark that overwrites its own comparisons is not a benchmark.

`write()` named every file `run-N{samples}.json`, so each 250-sample run
clobbered the previous one. Three runs of the same N only survived this
session because they were copied by hand, and one earlier comparison was lost
outright — the artifact behind a claim was destroyed by the tool that produced
it.

`--out` compounded it: it is a directory, so passing a filename created a
DIRECTORY named `run-N250-cap64.json/` with the real file inside. Passing an
explicit `.json` path should write that file.
"""

from __future__ import annotations

import json

import pytest


def _result(tmp_path):
    from bench.results import RunParams, RunResult
    params = RunParams(samples=7, seed=1, coa_bytes=1000, sif_bytes=1000,
                       cpu_throttle=1.0, cores=2, rss_ceiling_mb=1536,
                       js_heap_mb=256, preview_workers=4, samples_per_order=5)
    return RunResult(samples=7, served_count=7, params=params)


def test_an_explicit_json_path_writes_that_file(tmp_path) -> None:
    target = tmp_path / "my-run.json"
    r = _result(tmp_path)
    path = r.write(target)
    assert path == target, f"wrote {path}, expected {target}"
    assert target.is_file(), "an explicit .json path must be a file, not a directory"
    json.loads(target.read_text(encoding="utf-8"))


def test_a_directory_still_gets_a_default_name(tmp_path) -> None:
    r = _result(tmp_path)
    path = r.write(tmp_path)
    assert path.parent == tmp_path
    assert path.name.startswith("run-N7")
    assert path.is_file()


def test_two_runs_of_the_same_size_do_not_clobber(tmp_path) -> None:
    """The whole point: a repeat must not destroy the run it is compared to."""
    a = _result(tmp_path).write(tmp_path / "run-a.json")
    b = _result(tmp_path).write(tmp_path / "run-b.json")
    assert a != b
    assert a.is_file() and b.is_file()
