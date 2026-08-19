"""Tests for pure helper functions in ``app.py``.

Skipped wholesale if Flask isn't installed (``app.py`` imports Flask at
module top, so we can't import it without the dep).
"""

from __future__ import annotations

import json
from datetime import date

import pytest

pytest.importorskip("flask")

import app  # noqa: E402 — must come after importorskip


# ── business_days_ago ──────────────────────────────────────────────────────


def test_business_days_ago_one_day_from_friday_is_thursday() -> None:
    # 2026-01-09 is a Friday → minus 1 business day → Thursday 2026-01-08
    assert app.business_days_ago(1, from_date=date(2026, 1, 9)) == date(2026, 1, 8)


def test_business_days_ago_one_day_from_monday_skips_weekend() -> None:
    # 2026-01-12 is a Monday → minus 1 business day → Friday 2026-01-09
    assert app.business_days_ago(1, from_date=date(2026, 1, 12)) == date(2026, 1, 9)


def test_business_days_ago_two_days_from_monday() -> None:
    # 2026-01-12 Mon → minus 2 business days → Thursday 2026-01-08
    assert app.business_days_ago(2, from_date=date(2026, 1, 12)) == date(2026, 1, 8)


def test_business_days_ago_three_days_from_wednesday_crosses_weekend() -> None:
    # 2026-01-14 Wed → minus 3 business days → Friday 2026-01-09
    assert app.business_days_ago(3, from_date=date(2026, 1, 14)) == date(2026, 1, 9)


# ── load_config / save_config ─────────────────────────────────────────────


def test_load_config_returns_defaults_when_file_missing(isolated_app_paths) -> None:
    cfg = app.load_config()
    assert cfg == app.DEFAULT_CONFIG


def test_load_config_merges_file_over_defaults(isolated_app_paths) -> None:
    cfg_path, _ = isolated_app_paths
    cfg_path.write_text(json.dumps({"qbench_username": "alice"}), encoding="utf-8")

    cfg = app.load_config()

    assert cfg["qbench_username"] == "alice"
    # Defaults still present
    assert cfg["report_config_id"] == app.REPORT_CONFIG_ID


def test_load_config_ignores_corrupt_file(isolated_app_paths) -> None:
    cfg_path, _ = isolated_app_paths
    cfg_path.write_text("not json{", encoding="utf-8")

    cfg = app.load_config()

    assert cfg == app.DEFAULT_CONFIG


def test_save_config_writes_json_round_trip(isolated_app_paths) -> None:
    cfg_path, _ = isolated_app_paths
    app.save_config({"qbench_username": "bob", "labcore_host": "labpc"})

    loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert loaded["qbench_username"] == "bob"
    assert loaded["labcore_host"] == "labpc"


# ── load_re_review_state / save_re_review_state ───────────────────────────


def test_load_re_review_state_returns_empty_when_file_missing(isolated_app_paths) -> None:
    assert app.load_re_review_state() == {}


def test_load_re_review_state_parses_list_into_dict(isolated_app_paths) -> None:
    """The state file is persisted as a list of entries; on read it becomes a dict keyed by lab_id."""
    _, state_path = isolated_app_paths
    state_path.write_text(
        json.dumps([
            {"lab_id": "010126-001", "reason": "missing data"},
            {"lab_id": "010126-002", "reason": "wrong units"},
        ]),
        encoding="utf-8",
    )

    state = app.load_re_review_state()

    assert set(state) == {"010126-001", "010126-002"}
    assert state["010126-001"]["reason"] == "missing data"


def test_load_re_review_state_drops_entries_without_lab_id(isolated_app_paths) -> None:
    _, state_path = isolated_app_paths
    state_path.write_text(
        json.dumps([
            {"lab_id": "010126-001", "reason": "ok"},
            {"reason": "no lab_id"},
        ]),
        encoding="utf-8",
    )

    assert list(app.load_re_review_state()) == ["010126-001"]


def test_save_re_review_state_serializes_values_as_list(isolated_app_paths) -> None:
    _, state_path = isolated_app_paths
    app.save_re_review_state({
        "010126-001": {"lab_id": "010126-001", "reason": "missing data"},
    })

    raw = json.loads(state_path.read_text(encoding="utf-8"))
    assert isinstance(raw, list)
    assert raw == [{"lab_id": "010126-001", "reason": "missing data"}]


def test_re_review_state_round_trip(isolated_app_paths) -> None:
    payload = {
        "010126-001": {"lab_id": "010126-001", "reason": "x"},
        "010126-002": {"lab_id": "010126-002", "reason": "y"},
    }
    app.save_re_review_state(payload)
    assert app.load_re_review_state() == payload
