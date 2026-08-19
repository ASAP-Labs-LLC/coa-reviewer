"""The SIF pane distinguishes "entered online" from "document missing"."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
APP_CSS = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")


def test_online_entry_has_its_own_wording() -> None:
    assert "Online Entry" in APP_JS


def test_a_genuinely_missing_sif_reads_differently() -> None:
    """A paper order with no document is a problem; a portal order is not.
    One shared message hid the difference."""
    assert "online_entry" in APP_JS
    assert "missing" in APP_JS


def test_the_old_single_not_found_message_is_gone() -> None:
    """It could not tell the two apart, so it said nothing useful about either."""
    assert "No SIF found for this order" not in APP_JS


def test_the_two_states_are_styled_differently() -> None:
    """Expected-absence should not look like an alarm, and vice versa."""
    assert "sif-online" in APP_CSS or "sif-missing" in APP_CSS


def test_a_helper_maps_status_to_its_message() -> None:
    """One place decides the wording, so the pane and any badge agree."""
    assert "function sifPlaceholderText" in APP_JS
