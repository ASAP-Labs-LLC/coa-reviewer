"""The app's half of "Restart installs a staged update".

The updater on ASAPSV1 stages and health-checks each new release into
``<root>\\data\\staged.json`` — the same directory the app gets as
``COA_DATA_DIR``. When someone clicks Restart and a newer, healthy release is
staged, the app writes ``switch-requested``; the updater consumes it on its
next supervision tick (≤ 20 s) and runs its normal ``switch`` (post-switch
health check and automatic rollback included). The app never touches the
junction itself: ``switch`` kills the app's whole process tree, so a helper
the app spawned would die with it.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("coa.restart")

STAGED_FILE = "staged.json"
MARKER_FILE = "switch-requested"
PICKUP_SECONDS = 60.0     # updater must consume the marker within this
SWITCH_SECONDS = 120.0    # …and stop this process within this after that


def _norm(tag: Optional[str]) -> str:
    return (tag or "").strip().lstrip("vV").casefold()


def staged_update(data_dir: Path | str, current_version: str) -> Optional[str]:
    """The staged tag if it is healthy and not what is running, else None."""
    if _norm(current_version) in ("", "dev"):
        return None
    try:
        doc = json.loads((Path(data_dir) / STAGED_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        logger.warning("could not read %s: %s", STAGED_FILE, exc)
        return None
    if not isinstance(doc, dict) or doc.get("healthy") is not True:
        return None
    tag = str(doc.get("tag") or "").strip()
    if not tag or _norm(tag) == _norm(current_version):
        return None
    return tag


def write_switch_request(data_dir: Path | str, tag: str, *, by: str, now: float) -> bool:
    path = Path(data_dir) / MARKER_FILE
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({"tag": tag, "by": by, "at": now}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("could not write %s: %s", path, exc)
        return False
    logger.info("switch to %s requested by %s", tag, by)
    return True


def marker_present(data_dir: Path | str) -> bool:
    return (Path(data_dir) / MARKER_FILE).exists()


def withdraw_switch_request(data_dir: Path | str) -> bool:
    """Remove the marker. True if it was still there (nobody consumed it)."""
    try:
        (Path(data_dir) / MARKER_FILE).unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("could not withdraw switch request: %s", exc)
        return False
    return True
