"""TDD spec: the antigravity field renders proximity-scaled dots, and the
click wave crossfades dots into pre-rendered emoji sprites.

Supersedes test_ascii_ramp_render.py (2026-07-10): the ASCII character
field was replaced — per reviewer request — by round dots that dim and
shrink with distance from the cursor, and the click wave now reveals
celebration emojis as it sweeps over each dot.
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


def _body() -> str:
    return _function_body(APP_JS.read_text(encoding="utf-8"), "initAntigravity")


def test_dot_radius_constants_defined() -> None:
    """Dot size limits live in named constants so the ramp is tunable in
    one place."""
    body = _body()
    assert "DOT_MIN_RADIUS" in body and "DOT_MAX_RADIUS" in body, (
        "initAntigravity must define DOT_MIN_RADIUS / DOT_MAX_RADIUS"
    )


def test_cells_render_as_arcs() -> None:
    """Each grid cell draws a round dot via ctx.arc — the character-field
    model (fillText per cell) is gone from the per-frame path."""
    body = _body()
    assert "ctx.arc(" in body, "per-cell rendering must use ctx.arc (dots)"


def test_dot_size_scales_with_proximity() -> None:
    """Proximity drives dot size: dots grow toward DOT_MAX_RADIUS under
    the cursor and shrink toward DOT_MIN_RADIUS far away — distance from
    the mouse reads as dimming + shrinking."""
    body = _body()
    assert re.search(
        r"DOT_MIN_RADIUS\s*\+\s*\(DOT_MAX_RADIUS\s*-\s*DOT_MIN_RADIUS\)\s*\*",
        body,
    ), "dot radius must interpolate DOT_MIN_RADIUS→DOT_MAX_RADIUS by proximity"


def test_emoji_sprites_prerendered_and_blitted() -> None:
    """Wave emojis must render via pre-built sprites + drawImage. Drawing
    them with fillText per dot per frame re-rasterizes color glyphs at
    every unique fractional size, which measurably lagged the wave
    (2026-07-10 perf fix)."""
    body = _body()
    assert "emojiSprites" in body, "pre-rendered emoji sprite array missing"
    assert re.search(r"drawImage\(\s*emojiSprites", body), (
        "the wave must blit emojiSprites via ctx.drawImage, not fillText"
    )


def test_celebration_emoji_set_pinned() -> None:
    """The reviewer-chosen celebration set. Pinning it keeps the visual
    identity from drifting on future tweaks."""
    body = _body()
    for emoji in ("✨", "🎉", "⭐", "💥", "🌈", "🔥"):
        assert emoji in body, f"emoji {emoji} missing from EMOJIS set"


def test_wave_band_is_wide() -> None:
    """The click wave was widened from the original 110px band per
    reviewer request — keep it at 200px or more."""
    body = _body()
    m = re.search(r"PULSE_THICKNESS\s*=\s*(\d+)", body)
    assert m, "PULSE_THICKNESS constant not found"
    assert int(m.group(1)) >= 200, (
        "PULSE_THICKNESS must stay >= 200px — the wave is deliberately "
        "wider than the original 110px design"
    )


def test_sprite_build_centers_glyphs() -> None:
    """The sprite canvases must center their glyph (textAlign/baseline),
    otherwise every blitted emoji sits off-center from its dot."""
    body = _body()
    assert re.search(r'textAlign\s*=\s*["\']center["\']', body), (
        "sprite build must set textAlign center"
    )
    assert re.search(r'textBaseline\s*=\s*["\']middle["\']', body), (
        "sprite build must set textBaseline middle"
    )
