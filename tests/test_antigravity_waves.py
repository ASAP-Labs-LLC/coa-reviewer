"""TDD spec for click-spawned waves + bloom in the antigravity field.

Clicking the background spawns a ring pulse that radiates outward. As the
ring sweeps over each dot, that dot temporarily brightens (and gets a
glow halo) just like the cursor proximity does. The two systems share the
same proximity → brightness → bloom path, so the visual is unified.
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


def test_initantigravity_listens_for_pointerdown_or_click() -> None:
    """A click anywhere must spawn a wave at the click point."""
    body = _function_body(APP_JS.read_text(encoding="utf-8"), "initAntigravity")
    assert (
        'addEventListener("pointerdown"' in body
        or 'addEventListener("click"' in body
    ), (
        "initAntigravity must register a pointerdown/click listener that "
        "spawns a wave pulse at the cursor position"
    )


def test_initantigravity_tracks_pulses_collection() -> None:
    body = _function_body(APP_JS.read_text(encoding="utf-8"), "initAntigravity")
    assert "pulses" in body, (
        "initAntigravity must maintain a `pulses` array (active wave rings)"
    )


def test_pulses_expire_after_lifetime() -> None:
    """Pulses must be filtered out after their lifetime — otherwise the
    array grows forever and the tick loop slows down."""
    body = _function_body(APP_JS.read_text(encoding="utf-8"), "initAntigravity")
    assert "PULSE_LIFETIME" in body or "pulse_lifetime" in body.lower(), (
        "A PULSE_LIFETIME constant must gate pulse expiration"
    )
    # Some form of filtering / age check
    assert "pulses.filter" in body or "pulses.splice" in body or "expired" in body.lower(), (
        "Expired pulses must be removed from the array (filter/splice/loop)"
    )


def test_wave_contribution_combines_with_cursor_proximity() -> None:
    """The wave's proximity boost must merge with the cursor's so a dot
    swept by the wave brightens AND glows the same way a near-cursor dot
    does — single proximity → single alpha+radius+bloom path."""
    body = _function_body(APP_JS.read_text(encoding="utf-8"), "initAntigravity")
    # Either explicit Math.max(...) blend or assignment using both
    blends = (
        "Math.max(proximity" in body
        or "Math.max(cursorProximity" in body
        or "Math.max(waveBoost" in body
        or "proximity = Math.max" in body
    )
    assert blends, (
        "The wave's proximity contribution must combine with the cursor "
        "proximity via Math.max (so the dot's bloom follows whichever "
        "source is currently brightest)"
    )


def test_swept_cells_crossfade_to_emoji() -> None:
    """A swept dot crossfades into its emoji and back as the wave band
    passes (2026-07-10 design, superseding the ASCII character morph).
    The crossfade weight (emojiT) must derive from the wave boost via a
    steep smoothstep — color emoji at partial alpha over a dark background
    read muddy-brown, so the band is mostly fully-on with a narrow edge."""
    body = _function_body(APP_JS.read_text(encoding="utf-8"), "initAntigravity")
    assert "emojiT" in body, (
        "swept dots must compute an emojiT crossfade weight from the wave"
    )
    # Smoothstep shape: t*t*(3 - 2*t)
    assert re.search(r"emojiT\s*\*\s*emojiT\s*\*\s*\(\s*3\s*-\s*2\s*\*\s*emojiT\s*\)", body), (
        "emojiT must pass through a smoothstep (t*t*(3-2*t)) so the emoji "
        "band is mostly fully-opaque with a narrow fade edge"
    )
    # And the dot's own alpha must fade out as the emoji fades in.
    assert re.search(r"1\s*-\s*emojiT", body), (
        "the dot must fade by (1 - emojiT) while its emoji fades in — a "
        "crossfade, not an overlay"
    )
