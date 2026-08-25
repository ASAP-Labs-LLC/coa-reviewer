"""The SSE queue has to be sized against how many events a pull actually emits.

Measured on the benchmark harness: a 20-sample run delivered 111 events, or
~5.5 per sample (each sample emits `sample_status` loading→ready, `sif_status`
loading→resolved, and a status line). The queue was 200 slots, so a pull
overflows at roughly 36 samples and badly past 70 — which is exactly where
reviewers report the app "breaks down".

Overflow is now survivable rather than fatal (`_shed_oldest` keeps the
subscriber and sends `resync`), but a resync reloads all five tabs mid-pull,
so it is recovery, not a working state. The queue should be large enough that
a normal day never reaches it, with shedding as the backstop for a genuinely
stalled browser rather than the routine path.

Sizing is cheap: these are small dicts, so even a few thousand slots is well
under a megabyte per connected browser.
"""

from __future__ import annotations

import pytest

pytest.importorskip("flask")

# Measured, N=20: 111 events / 20 samples.
EVENTS_PER_SAMPLE = 5.5
# A heavy day, with headroom over the 250 the harness drives.
BUSY_DAY_SAMPLES = 300


def test_the_queue_constant_is_exported() -> None:
    """The size must be a named constant, not a literal buried in the route."""
    import app
    assert isinstance(app.SSE_QUEUE_MAXSIZE, int)


def test_a_busy_day_does_not_overflow_the_queue() -> None:
    import app
    needed = int(EVENTS_PER_SAMPLE * BUSY_DAY_SAMPLES)
    assert app.SSE_QUEUE_MAXSIZE >= needed, (
        f"{app.SSE_QUEUE_MAXSIZE} slots cannot hold a {BUSY_DAY_SAMPLES}-sample "
        f"pull (~{needed} events); reviewers would hit resync on a normal day"
    )


def test_the_queue_is_not_so_large_it_becomes_the_leak() -> None:
    """Unbounded would just move the memory problem into the queue."""
    import app
    assert app.SSE_QUEUE_MAXSIZE <= 20_000


def test_the_stream_uses_the_constant() -> None:
    """A constant the route ignores is decoration."""
    import inspect
    import app
    source = inspect.getsource(app.sse_stream)
    assert "SSE_QUEUE_MAXSIZE" in source, (
        "sse_stream still hard-codes its queue size"
    )
