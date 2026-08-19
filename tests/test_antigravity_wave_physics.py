"""TDD spec: wave ripples must push dots outward with elastic spring-back.

The click-spawned wave currently only brightens dots as the ring sweeps
through. The user wants the same physics behavior the cursor has — the
ring pushes dots outward from the pulse center, and the spring/damping
system already in place pulls them back, producing a "bounce" as the
ripple passes through.
"""
from __future__ import annotations

import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "static" / "js" / "app.js"


def _function_body(src: str, name: str) -> str:
    start = src.find(f"function {name}(")
    assert start != -1, f"function {name}( not found"
    next_top = src.find("\nfunction ", start + 1)
    next_async = src.find("\nasync function ", start + 1)
    ends = [x for x in (next_top, next_async) if x != -1] + [len(src)]
    return src[start:min(ends)]


def _pulses_loop_body(body: str) -> str:
    """Return the body of the `for (... pi < livePulses.length ...)` block
    inside the tick function."""
    m = re.search(
        r"for\s*\([^)]*pi\s*<\s*livePulses\.length[^)]*\)\s*\{",
        body,
    )
    assert m, "livePulses iteration not found in tick()"
    start = m.end() - 1  # position of the `{`
    depth = 0
    for i in range(start, len(body)):
        if body[i] == "{":
            depth += 1
        elif body[i] == "}":
            depth -= 1
            if depth == 0:
                return body[start + 1 : i]
    raise AssertionError("unbalanced braces around livePulses loop")


def test_wave_push_magnitude_constant_exists() -> None:
    """A dedicated constant controls how hard the wave shoves dots.
    Tunable separately from the cursor's MAX_PUSH."""
    body = _function_body(APP_JS.read_text(encoding="utf-8"), "initAntigravity")
    assert "WAVE_MAX_PUSH" in body or "WAVE_PUSH" in body, (
        "A WAVE_MAX_PUSH (or WAVE_PUSH) constant must exist so the wave's "
        "kick strength is tunable independently of the cursor's MAX_PUSH"
    )


def test_wave_loop_assigns_dot_velocity() -> None:
    """The pulses loop must contribute to each dot's velocity vector, so
    the existing spring + damping integration springs the dot back to
    rest after the ring sweeps past."""
    body = _function_body(APP_JS.read_text(encoding="utf-8"), "initAntigravity")
    loop = _pulses_loop_body(body)
    assert "d.vx" in loop, (
        "wave-ring loop must add to d.vx so the dot gets an outward kick "
        "from the pulse — visual brightening alone is not enough"
    )
    assert "d.vy" in loop, "wave-ring loop must also add to d.vy"


def test_wave_push_uses_outward_normal_from_pulse_center() -> None:
    """The kick direction is the unit vector from the pulse center to the
    dot — i.e. uses the (d.x - p.x) / dist normal that's already computed
    for the visual proximity. Sign must be `+=`, not `-=`, so dots travel
    outward (riding the ring), not inward."""
    body = _function_body(APP_JS.read_text(encoding="utf-8"), "initAntigravity")
    loop = _pulses_loop_body(body)
    # Outward push uses += with a positive normal × force expression
    assert re.search(r"d\.vx\s*\+=", loop), (
        "d.vx must be incremented (`+=`) by the outward-normal × force"
    )
    assert re.search(r"d\.vy\s*\+=", loop), (
        "d.vy must be incremented (`+=`) by the outward-normal × force"
    )
    # The kick should scale with the ring band proximity (ringT) AND the
    # pulse age (lifeT) so it fades over time — same shape as the visual.
    assert "lifeT" in loop, (
        "wave push must fade with pulse age (multiply by p.lifeT)"
    )
