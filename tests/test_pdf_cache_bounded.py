"""The per-session COA cache must have a ceiling.

Measured, not assumed: the benchmark harness drove the app through 20 and then
250 samples and peak server RSS went 117 MB → 364 MB — a linear 1.07 MB per
sample, with nothing evicting. `pdf_cache` holds whole rendered COAs keyed by
lab_id, one entry per sample viewed, per browser session, and several reviewers
work the same day's samples at once on a modest lab server.

A ceiling turns unbounded growth into a fixed cost. Least-recently-used is the
right eviction order here because reviewing walks the list roughly in order and
almost never returns to a sample already cleared — and the sample on screen is
by definition the most recently used, so it is the last thing evicted.

Eviction costs a re-fetch from QBench on revisit, which is why the budget is
generous and configurable rather than tight.
"""

from __future__ import annotations

import pytest

pytest.importorskip("flask")


def _cache(max_bytes: int):
    from app import PdfCache
    return PdfCache(max_bytes=max_bytes)


def test_it_still_behaves_like_the_dict_it_replaces() -> None:
    """Call sites use `in`, [], .get, .pop and .clear — all must keep working."""
    c = _cache(1000)
    c["a"] = b"xxx"
    assert "a" in c
    assert c["a"] == b"xxx"
    assert c.get("a") == b"xxx"
    assert c.get("nope") is None
    assert c.pop("a", None) == b"xxx"
    assert c.pop("a", None) is None
    assert "a" not in c
    c["b"] = b"y"
    c.clear()
    assert "b" not in c
    assert len(c) == 0


def test_total_bytes_stay_under_the_ceiling() -> None:
    c = _cache(1000)
    for i in range(50):
        c[f"lab-{i}"] = b"z" * 100
    assert c.total_bytes <= 1000, f"cache overran its budget: {c.total_bytes}"


def test_the_least_recently_used_entry_goes_first() -> None:
    c = _cache(300)
    c["a"] = b"z" * 100
    c["b"] = b"z" * 100
    c["c"] = b"z" * 100
    c["d"] = b"z" * 100      # forces one eviction
    assert "a" not in c, "evicted the wrong entry"
    assert "d" in c


def test_reading_an_entry_makes_it_recent() -> None:
    """The COA on screen is read constantly; it must not be evicted."""
    c = _cache(300)
    c["a"] = b"z" * 100
    c["b"] = b"z" * 100
    c["c"] = b"z" * 100
    _ = c["a"]               # 'a' is now the most recent, 'b' the oldest
    c["d"] = b"z" * 100
    assert "a" in c, "reading an entry did not refresh its recency"
    assert "b" not in c


def test_membership_also_counts_as_use() -> None:
    """get_pdf checks `lab_id in ustate.pdf_cache` before serving it."""
    c = _cache(300)
    c["a"] = b"z" * 100
    c["b"] = b"z" * 100
    c["c"] = b"z" * 100
    assert "a" in c
    c["d"] = b"z" * 100
    assert "a" in c, "a membership test should count as a use"


def test_replacing_a_key_does_not_double_count() -> None:
    c = _cache(1000)
    c["a"] = b"z" * 100
    c["a"] = b"z" * 200
    assert c.total_bytes == 200
    assert c["a"] == b"z" * 200


def test_an_oversized_entry_is_not_cached_and_does_not_evict_everything() -> None:
    """A single COA larger than the whole budget must not empty the cache."""
    c = _cache(300)
    c["a"] = b"z" * 100
    c["huge"] = b"z" * 5000
    assert "huge" not in c
    assert "a" in c, "an unusable oversized entry wiped the useful ones"
    assert c.total_bytes <= 300


def test_user_state_uses_a_bounded_cache() -> None:
    """The wiring matters as much as the class."""
    from app import PdfCache, UserState
    ustate = UserState("test-uid-bounded", "RC")
    assert isinstance(ustate.pdf_cache, PdfCache)
    assert ustate.pdf_cache.max_bytes > 0
