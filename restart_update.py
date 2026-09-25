"""The app's half of "Restart installs a staged update".

The updater on ASAPSV1 stages and health-checks each new release into
``<root>\\data\\staged.json`` — the same directory the app gets as
``COA_DATA_DIR``. When someone clicks Restart and a newer, healthy release is
staged, the app writes ``switch-requested``. The updater does not delete that
file when it acts: it *claims* the request by renaming it into
``switch-accepted`` or ``switch-refused`` (with the reason), so the outcome is
visible to whoever asked instead of silently vanishing. Pickup is usually
≤ 20 s (one supervision tick); the app falls back to an ordinary restart after
``PICKUP_SECONDS`` (60 s) if nothing claims the marker.

The app never touches the junction itself: ``switch`` kills the app's whole
process tree, so a helper the app spawned would die with it.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("coa.restart")

STAGED_FILE = "staged.json"
MARKER_FILE = "switch-requested"
ACCEPTED_FILE = "switch-accepted"
REFUSED_FILE = "switch-refused"

# Must stay comfortably above updater.MAX_REQUEST_AGE_SECONDS (45s): the
# updater refuses a request once it looks stale, so the app must not still be
# waiting past the point the updater would have refused it for exactly that.
PICKUP_SECONDS = 60.0     # updater must claim the marker within this
SWITCH_SECONDS = 120.0    # …and stop this process within this after that

_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$", re.IGNORECASE)


def _parse_semver(tag: Optional[str]) -> Optional[Tuple[int, int, int]]:
    """``(major, minor, patch)`` from a ``vX.Y.Z`` (or ``X.Y.Z``) tag, or
    ``None`` for anything else — a build tag, ``"dev"``, a pre-release
    suffix. Unparsable is never an error here, just "cannot compare"."""
    if not isinstance(tag, str):
        return None
    m = _SEMVER.match(tag.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def is_upgrade(staged_tag: Optional[str], current: Optional[str]) -> bool:
    """Whether ``staged_tag`` is a strictly newer semver release than
    ``current``. Anything unparsable on either side is not comparable, so
    this returns ``False`` rather than guessing — that also covers a dev
    build's ``current == "dev"`` for free."""
    s, c = _parse_semver(staged_tag), _parse_semver(current)
    if s is None or c is None:
        return False
    return s > c


def staged_update(data_dir: Path | str, current_version: str) -> Optional[str]:
    """The staged tag if it is healthy and a real upgrade over what is
    running, else ``None``."""
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
    if not is_upgrade(tag, current_version):
        return None
    return tag


def write_switch_request(data_dir: Path | str, tag: str, *, by: str, now: float) -> bool:
    """Ask the updater to switch. Refuses (returns ``False``) rather than
    overwriting a request that is still pending — two clicks in a row must
    not silently drop the first one's audit trail.

    Written with ``O_CREAT | O_EXCL`` directly at the final path: a
    check-then-rename would leave a window where two callers both see "no
    marker" and one overwrites the other's request.
    """
    data_dir = Path(data_dir)
    path = data_dir / MARKER_FILE
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("could not create %s: %s", data_dir, exc)
        return False
    # `at` is wall-clock time.time(), not monotonic: the updater is a
    # separate process and compares it against its own time.time() for
    # freshness, which only means something across processes in wall time.
    payload = json.dumps({"tag": tag, "by": by, "at": now}).encode("utf-8")
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        logger.info("switch already requested (%s exists); not overwriting it", path)
        return False
    except OSError as exc:
        logger.warning("could not open %s: %s", path, exc)
        return False
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
    except OSError as exc:
        logger.warning("could not write %s: %s", path, exc)
        try:
            path.unlink()
        except OSError:
            pass
        return False
    logger.info("switch to %s requested by %s", tag, by)
    return True


def marker_present(data_dir: Path | str) -> bool:
    return (Path(data_dir) / MARKER_FILE).exists()


def withdraw_switch_request(data_dir: Path | str) -> bool:
    """Remove the marker. True if it was still there (nobody had claimed it
    yet)."""
    try:
        (Path(data_dir) / MARKER_FILE).unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("could not withdraw switch request: %s", exc)
        return False
    return True


def read_switch_outcome(data_dir: Path | str) -> Optional[dict]:
    """What the updater did with the last switch request, or ``None`` if it
    has not claimed one (yet, or ever). ``{"state": "accepted"|"refused",
    ...}``. If both files somehow exist, ``refused`` wins — that is the safer
    thing for a caller to believe if the picture is ambiguous."""
    data_dir = Path(data_dir)
    for name, state in ((REFUSED_FILE, "refused"), (ACCEPTED_FILE, "accepted")):
        try:
            raw = (data_dir / name).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("could not read %s: %s", name, exc)
            continue
        try:
            doc = json.loads(raw)
        except ValueError as exc:
            logger.warning("corrupt %s: %s", name, exc)
            continue
        if not isinstance(doc, dict):
            continue
        return {"state": state, **doc}
    return None


def clear_switch_files(data_dir: Path | str) -> int:
    """Remove marker/accepted/refused. Called at app startup (a new process
    means whatever the files describe already happened, one way or another)
    and before falling back to an ordinary restart. Returns how many existed."""
    data_dir = Path(data_dir)
    removed = 0
    for name in (MARKER_FILE, ACCEPTED_FILE, REFUSED_FILE):
        try:
            (data_dir / name).unlink()
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("could not remove %s: %s", name, exc)
    return removed
