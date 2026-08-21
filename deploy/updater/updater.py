"""Self-updating deployment service for the ASAP Labs Flask apps.

It polls GitHub for a new release, stages it, proves it healthy on a scratch
port, and then **stops and waits for a human**. Switching is a separate,
explicit command. A staged release that has not been switched has changed
nothing about the running app.

Design rules, each of which exists because the alternative has a failure mode
that reaches the lab:

* **Refuse rather than guess.** A failed API call, an unverifiable asset, a
  missing checksum, an ambiguous state — all resolve to "do nothing this
  cycle". The updater runs unattended against production; "I could not tell"
  must never mean "assume it is fine".
* **Never switch on its own.** Staging is automatic because it is harmless;
  switching stops a service the lab is using, so a person decides when.
* **Always roll back automatically.** Once a switch *has* been made, nobody
  may be watching. An unhealthy new release is reverted without waiting for
  anyone.
* **Never write into a release.** Releases are immutable; state lives in
  ``<root>\\data`` and the junction is the only thing that moves.
* **Use supervisor.py for process and port work.** Those primitives encode the
  failure modes this box actually hit — a ``taskkill /F /T`` that timed out
  mid-tree, and Windows ``SO_REUSEADDR`` letting a second server bind a port
  that was still being served and then serve nothing. Probing by connecting
  rather than binding is not a style preference here.

Layout per app::

    <root>\\releases\\<tag>\\     unpacked release, immutable, has its own .venv
    <root>\\current              JUNCTION -> one of the releases
    <root>\\data\\               state + staged.json; deploys never touch this

Usage::

    updater.py run                     # the service loop
    updater.py poll                    # one cycle, then exit
    updater.py status
    updater.py switch --app coa --tag v1.0.1
    updater.py rollback --app coa
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

# ── actions ─────────────────────────────────────────────────────────────────

SLEEP = "sleep"
STAGE = "stage"

# Supervision outcomes.
SUPERVISE_OK = "ok"
SUPERVISE_START = "start"
SUPERVISE_PAUSED = "paused"
SUPERVISE_GIVING_UP = "giving_up"

# A crashlooping app must not be restarted forever: that turns a crash into a
# silent spin and hides the failure. Three starts in fifteen minutes is well
# clear of COA's one deliberate restart a day.
SUPERVISE_MAX_STARTS = 3
SUPERVISE_WINDOW_SECONDS = 900.0

DEFAULT_POLL_SECONDS = 300
DEFAULT_SUPERVISE_SECONDS = 20
DEFAULT_KEEP = 5
HEALTH_TIMEOUT = 60.0

log = logging.getLogger("updater")


class UpdaterError(RuntimeError):
    """Anything that should abort the current app's cycle, not the service."""


class ChecksumError(UpdaterError):
    """A downloaded asset is not the one that was published."""


class ReleaseError(UpdaterError):
    """A release cannot be installed from — malformed or missing assets."""


# ── checksums ───────────────────────────────────────────────────────────────

_SHA256_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(\S+)\s*$")


def parse_sha256_file(text: str) -> tuple[str, str]:
    """``("<digest>", "<filename>")`` from a ``sha256sum`` output line.

    Accepts both the text (``digest  name``) and binary (``digest *name``)
    forms. Anything else raises: a checksum file we cannot parse is not a
    weaker guarantee, it is no guarantee, and proceeding would silently drop
    the only defence against a corrupted download.
    """
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _SHA256_LINE.match(line)
        if not m:
            raise ChecksumError(f"unparseable checksum line: {line!r}")
        return m.group(1).lower(), m.group(2)
    raise ChecksumError("checksum file is empty")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_asset(path: Path, checksum_text: str) -> None:
    """Raise :class:`ChecksumError` unless ``path`` matches the published digest."""
    expected, _name = parse_sha256_file(checksum_text)
    actual = sha256_of(path)
    if actual != expected:
        raise ChecksumError(
            f"checksum mismatch for {path.name}: published {expected}, got {actual}"
        )


# ── versions ────────────────────────────────────────────────────────────────

def read_version(directory: Path | str) -> str:
    """The VERSION stamp in ``directory``, or ``"dev"``."""
    try:
        stamp = (Path(directory) / "VERSION").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return "dev"
    return stamp.splitlines()[0].strip() if stamp else "dev"


def differs_from(current: Optional[str], latest: Optional[str]) -> bool:
    """Whether ``latest`` is a different release from ``current``.

    Deliberately an equality test, not an ordering one. Tag schemes are not
    reliably comparable (``v1.0.10`` vs ``v1.0.9`` vs a date tag), and the
    updater's job is to track whatever GitHub calls *latest* — including a
    deliberate re-release of an older build to undo a bad one. Inventing an
    ordering here would refuse exactly that rollback.
    """
    return (current or "").strip().casefold() != (latest or "").strip().casefold()


# ── decisions ───────────────────────────────────────────────────────────────

def plan_poll(*, current: Optional[str], latest: Optional[str],
              staged: Optional[dict]) -> str:
    """What this poll cycle should do."""
    if not latest:
        # The API call failed or the repo has no release. Either way we know
        # nothing new, and acting on nothing is how an updater deletes a
        # working install.
        return SLEEP
    if not differs_from(current, latest):
        return SLEEP
    if staged and not differs_from(staged.get("tag"), latest):
        # Already staged, healthy or not. Restaging on a loop would rebuild the
        # same venv every interval and keep rewriting staged_at, and if it was
        # unhealthy it would keep failing identically.
        return SLEEP
    return STAGE


def may_switch(*, staged: Optional[dict], requested_tag: str) -> tuple[bool, str]:
    """Whether a switch to ``requested_tag`` is allowed, and why not."""
    if not staged:
        return False, "nothing staged"
    if differs_from(staged.get("tag"), requested_tag):
        return False, (
            f"staged release is {staged.get('tag')!r}, not {requested_tag!r} — "
            "refusing in case a newer release was staged since you looked"
        )
    if not staged.get("healthy"):
        return False, (
            f"staged release {requested_tag!r} failed its health check: "
            f"{staged.get('notes') or 'no detail'}"
        )
    return True, ""


def supervision_decision(*, has_listener: bool, paused: bool,
                         starts_in_window: int, max_starts: int) -> str:
    """Whether to (re)start an app this cycle.

    ``paused`` is checked first and regardless of whether the app is up: pause
    records a human's intent to keep it down, and intent that expires the
    moment the app happens to be running is not a pause at all.
    """
    if paused:
        return SUPERVISE_PAUSED
    if has_listener:
        return SUPERVISE_OK
    if starts_in_window >= max_starts:
        return SUPERVISE_GIVING_UP
    return SUPERVISE_START


def is_paused(data_dir: Path | str) -> bool:
    """Whether the app should be left down.

    ``<data>\\paused`` is a human's hold — Run.pyw's failure mode was that
    there was no way to stop the app without fighting the supervisor.

    ``<data>\\switching`` is held for the moments a switch has the app stopped.
    Without it, a ``switch`` run from the command line while the service loop
    is running would race it: the loop sees no listener, decides the app has
    died, and starts the *old* release from under the junction that is being
    repointed. Two markers rather than one, so a switch cannot clear a hold a
    person put there.
    """
    d = Path(data_dir)
    return (d / "paused").exists() or (d / "switching").exists()


def starts_within(starts: Sequence[float], *, now: float, window: float) -> int:
    """How many of ``starts`` (monotonic timestamps) fall inside ``window``."""
    return sum(1 for t in starts if now - t <= window)


def releases_to_prune(names_newest_first: Sequence[str], *, keep: int,
                      protected: Iterable[str]) -> list[str]:
    """Which release directories may be deleted.

    ``protected`` (``current`` and any rollback target) is never returned, and
    protecting an old release does **not** consume one of the ``keep`` slots —
    otherwise pinning an old release would silently evict a recent one.
    """
    guard = {p.strip().casefold() for p in protected if p}
    kept = 0
    doomed: list[str] = []
    for name in names_newest_first:
        if name.strip().casefold() in guard:
            continue
        if kept < keep:
            kept += 1
            continue
        doomed.append(name)
    return doomed


# ── staged.json ─────────────────────────────────────────────────────────────

def read_staged(data_dir: Path | str) -> Optional[dict]:
    """The staged record, or ``None`` if absent or unreadable.

    A corrupt record must not stop the service from booting; the worst case is
    that a release gets staged again.
    """
    try:
        raw = (Path(data_dir) / "staged.json").read_text(encoding="utf-8")
        got = json.loads(raw)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return got if isinstance(got, dict) else None


def write_staged(data_dir: Path | str, *, tag: str, healthy: bool, notes: str,
                 now: Optional[str] = None) -> None:
    """Write ``staged.json`` atomically.

    Via a temp file and ``os.replace`` so a crash mid-write cannot leave a
    truncated record — which ``read_staged`` would discard, losing the real one
    along with it.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "tag": tag,
        "staged_at": now or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "healthy": bool(healthy),
        "notes": notes,
    }
    tmp = data_dir / "staged.json.tmp"
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, data_dir / "staged.json")


# ── GitHub ──────────────────────────────────────────────────────────────────

def pick_assets(assets: Sequence[dict]) -> tuple[dict, dict]:
    """The ``.zip`` and its ``.sha256`` from a release's asset list."""
    zips = [a for a in assets if a.get("name", "").endswith(".zip")]
    sums = [a for a in assets if a.get("name", "").endswith(".sha256")]
    if not zips:
        raise ReleaseError("release has no .zip asset")
    if not sums:
        raise ReleaseError(
            "release has no .sha256 asset; an asset that cannot be verified "
            "is not installable"
        )
    return zips[0], sums[0]


def read_credential(target: str) -> Optional[str]:
    """Read a Generic credential from Windows Credential Manager.

    Kept here rather than in config.json on purpose: a token in a config file
    ends up in a backup, a screenshot, or a repo.
    """
    if os.name != "nt":
        return os.environ.get("GITHUB_TOKEN") or None

    # Imported here, not at module scope: ctypes.wintypes does not exist off
    # Windows, and the decision logic above must stay importable so it can be
    # tested anywhere.
    import ctypes.wintypes as wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
            ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    advapi.CredReadW.restype = wintypes.BOOL

    ptr = ctypes.POINTER(CREDENTIAL)()
    if not advapi.CredReadW(target, 1, 0, ctypes.byref(ptr)):
        return None
    try:
        cred = ptr.contents
        if not cred.CredentialBlobSize:
            return None
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return blob.decode("utf-16-le", errors="ignore")
    finally:
        advapi.CredFree(ptr)


def _api(url: str, token: Optional[str], *, raw: bool = False,
         timeout: float = 30.0) -> Any:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "asaplabs-updater")
    req.add_header("Accept", "application/octet-stream" if raw
                   else "application/vnd.github+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read() if raw else json.loads(resp.read().decode("utf-8"))


def latest_release(repo: str, token: Optional[str]) -> Optional[dict]:
    """The latest release, or ``None`` if there is none or the call failed.

    Never raises: a GitHub outage is not a reason to take an app down, and the
    only correct response to "I don't know" is to do nothing this cycle.

    A rejected token falls back to one anonymous retry. Org policy can reject a
    token that is otherwise fine — ASAP Labs refuses fine-grained PATs with a
    lifetime over 366 days — and for a public repo an anonymous read succeeds
    where the authenticated one just 403'd. The retry is logged loudly rather
    than silently: on a private repo it will 404 and the token still needs
    fixing, and a token quietly doing nothing is its own kind of outage.
    """
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        return _api(url, token)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # A 404 here is ambiguous and the two cases need different actions.
            # GitHub answers 404 (not 403) for a private repo a fine-grained
            # token is not scoped to, which is indistinguishable from "this
            # repo has no releases yet" unless you ask about the repo itself.
            # Reporting the wrong one sends someone hunting for a missing
            # release when the real problem is the token's repository access.
            if _repo_visible(repo, token):
                log.info("%s: no releases published yet", repo)
            else:
                log.warning(
                    "%s: the repository is not visible with this credential. "
                    "If it is private, add it to the token's repository "
                    "access — a fine-grained PAT reports 404, not 403, for a "
                    "repo outside its selected list.", repo)
            return None
        body = ""
        try:
            body = exc.read().decode("utf-8", "ignore")[:300]
        except Exception:  # pragma: no cover - diagnostic only
            pass
        log.warning("%s: GitHub returned HTTP %s %s", repo, exc.code, body)
        if token and exc.code in (401, 403):
            log.warning("%s: token was rejected — retrying anonymously. Fix the "
                        "credential; a private repo will 404 here.", repo)
            try:
                return _api(url, None)
            except urllib.error.HTTPError as anon_exc:
                log.warning("%s: anonymous retry also failed (HTTP %s)",
                            repo, anon_exc.code)
            except (urllib.error.URLError, ValueError, OSError) as anon_exc:
                log.warning("%s: anonymous retry failed: %s", repo, anon_exc)
        return None
    except (urllib.error.URLError, ValueError, OSError) as exc:
        log.warning("%s: could not reach GitHub: %s", repo, exc)
        return None


def _repo_visible(repo: str, token: Optional[str]) -> bool:
    """Whether this credential can see the repository at all."""
    try:
        _api(f"https://api.github.com/repos/{repo}", token)
        return True
    except Exception:
        return False


def download(url: str, dest: Path, token: Optional[str]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(_api(url, token, raw=True, timeout=300.0))


# ── filesystem / junctions ──────────────────────────────────────────────────

def _force_rmtree(path: Path) -> None:
    """``shutil.rmtree`` that copes with Windows.

    Two things defeat a plain rmtree here. pip marks files in a venv read-only,
    and ``os.unlink`` refuses those with ``Access is denied`` rather than
    anything mentioning permissions bits. And a file that is still *open* —
    a leftover health-check process holding a ``.pyd`` — cannot be deleted at
    all, so the retry loop gives a dying process a moment to release it rather
    than failing the whole stage.
    """
    def on_error(func, target, exc_info):
        try:
            os.chmod(target, 0o700)
            func(target)
        except OSError:
            pass

    for attempt in range(3):
        try:
            shutil.rmtree(path, onexc=on_error)
        except TypeError:                                  # pragma: no cover
            shutil.rmtree(path, onerror=lambda f, t, e: on_error(f, t, e))
        except OSError:
            pass
        if not path.exists():
            return
        time.sleep(1.0 + attempt)
    if path.exists():
        raise UpdaterError(
            f"could not remove {path}; something is still holding a file open. "
            "Check for a stray python.exe from an interrupted health check."
        )


def _sweep_port(port: int) -> None:
    """Kill anything listening on ``port`` before we try to use it.

    A health check that was interrupted leaves a process holding the scratch
    port and the release's venv. Without this, every subsequent stage fails on
    a file it cannot delete, for a reason that looks nothing like the cause.
    """
    for pid in _pids_on_port(port):
        log.warning("scratch port %s held by PID %s (stray from an interrupted "
                    "health check) — killing it", port, pid)
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, text=True)


def is_junction(path: Path) -> bool:
    try:
        return bool(path.is_dir() and os.readlink(str(path)))
    except (OSError, ValueError):
        return False


def repoint_junction(link: Path, target: Path) -> None:
    """Point ``link`` at ``target``, replacing any existing junction.

    ``mklink /J`` rather than ``/D``: a directory junction needs no admin
    rights, a symlink does, and this runs as a scheduled task.

    A junction cannot be replaced while a file under it is open, so the caller
    must have stopped the app first — and confirmed it actually stopped.
    """
    link = Path(link)
    target = Path(target)
    if not target.is_dir():
        raise UpdaterError(f"junction target does not exist: {target}")
    if link.exists() or is_junction(link):
        if link.is_dir() and not is_junction(link):
            raise UpdaterError(
                f"{link} is a real directory, not a junction — refusing to "
                "delete it. Move it aside by hand."
            )
        os.rmdir(link)
    res = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                         capture_output=True, text=True)
    if res.returncode != 0 or not is_junction(link):
        raise UpdaterError(f"mklink failed: {res.stdout.strip()} {res.stderr.strip()}")


def unpack(zip_path: Path, dest: Path) -> Path:
    """Unpack a release zip so ``dest`` directly contains ``app.py``.

    The archive has a single top-level directory; flatten it, because the
    junction points at the release directory and the app must be immediately
    inside.
    """
    staging = dest.with_name(dest.name + ".unpacking")
    if staging.exists():
        _force_rmtree(staging)
    staging.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if name.startswith("/") or ".." in Path(name).parts:
                raise ReleaseError(f"refusing path traversal entry in zip: {name!r}")
        zf.extractall(staging)
    entries = [p for p in staging.iterdir()]
    root = entries[0] if len(entries) == 1 and entries[0].is_dir() else staging
    if dest.exists():
        _force_rmtree(dest)
    shutil.move(str(root), str(dest))
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    return dest


def build_venv(release_dir: Path, *, browsers_path: Optional[str] = None) -> Path:
    """Create ``<release>/.venv`` from the release's own requirements.txt."""
    venv_dir = release_dir / ".venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True,
                   capture_output=True, text=True)
    py = venv_dir / "Scripts" / "python.exe"
    if not py.exists():                                   # pragma: no cover
        py = venv_dir / "bin" / "python"
    reqs = release_dir / "requirements.txt"
    if not reqs.is_file():
        raise ReleaseError(f"release {release_dir.name} has no requirements.txt")
    subprocess.run([str(py), "-m", "pip", "install", "--quiet", "-r", str(reqs)],
                   check=True, capture_output=True, text=True)

    env = dict(os.environ)
    if browsers_path:
        env["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path
    if (release_dir / "requirements.txt").read_text(encoding="utf-8").find("playwright") >= 0:
        # Chromium caches outside the venv, so this is a fast no-op when the
        # matching build is already present and a real download only when the
        # playwright pin moved.
        subprocess.run([str(py), "-m", "playwright", "install", "chromium"],
                       check=False, capture_output=True, text=True, env=env)
    return py


# ── health ──────────────────────────────────────────────────────────────────

def _http_get_json(url: str, timeout: float = 3.0) -> Optional[dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def health_check(*, python: Path, release_dir: Path, argv: Sequence[str], port: int,
                 data_dir: Path, env_extra: dict, expected_version: Optional[str],
                 browsers_path: Optional[str] = None,
                 timeout: float = HEALTH_TIMEOUT) -> tuple[bool, str]:
    """Start the release on a scratch port and prove ``/healthz`` answers.

    Runs against a throwaway data directory so a release under test can never
    touch real state — the app seeds a fresh config there and writes its own
    archive/log, which is exactly what we want to exercise anyway.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    env = dict(env_extra)
    if browsers_path:
        env["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path

    proc = subprocess.Popen(
        [str(python), *argv], cwd=str(release_dir), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.time() + timeout
        body = None
        while time.time() < deadline:
            if proc.poll() is not None:
                out = (proc.stdout.read() or "")[-800:] if proc.stdout else ""
                return False, f"process exited with {proc.returncode}: {out.strip()}"
            body = _http_get_json(f"http://127.0.0.1:{port}/healthz")
            if body:
                break
            time.sleep(1.0)
        if not body:
            return False, f"/healthz did not answer within {timeout:.0f}s"
        if body.get("status") != "ok":
            return False, f"/healthz reported status={body.get('status')!r}"
        got = body.get("version")
        if expected_version and differs_from(got, expected_version):
            return False, (
                f"/healthz reports version {got!r} but {expected_version!r} was "
                "staged — the release did not take effect"
            )
        return True, f"healthy: version={got} labcore={body.get('labcore')}"
    finally:
        _stop_process(proc, port)


def _stop_process(proc: subprocess.Popen, port: int) -> None:
    """Stop a child and confirm it died, via supervisor when available.

    ``stop_until_dead`` passes the 1-based attempt number so the caller can
    escalate, which is the whole reason it exists: a polite terminate can hang
    on a process mid-syscall, and the launcher incident this encodes was a
    tree-kill that timed out while the process was still alive.
    """
    sup = _load_supervisor()

    def is_alive() -> bool:
        return proc.poll() is None

    def kill(attempt: int = 1) -> None:
        try:
            if attempt == 1:
                proc.terminate()   # polite first
            else:
                proc.kill()        # then not
        except OSError:
            pass

    if sup:
        if not sup.stop_until_dead(is_alive, kill):
            log.error("health-check process %s would not die; not trusting the "
                      "port to be free", proc.pid)
        elif not sup.wait_until_free(port, timeout=20.0):
            log.warning("port %s still has a listener after the health-check "
                        "process died", port)
    else:                                                  # pragma: no cover
        kill(1)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            kill(2)


_SUPERVISOR = None


def _load_supervisor():
    """Import ``supervisor`` from whichever release is current.

    Importing the app's own copy rather than vendoring one means the process
    and port handling always matches the code being managed.
    """
    global _SUPERVISOR
    if _SUPERVISOR is not None:
        return _SUPERVISOR or None
    for cand in _SUPERVISOR_PATHS:
        if (Path(cand) / "supervisor.py").is_file():
            sys.path.insert(0, str(cand))
            try:
                import supervisor as _s
                _SUPERVISOR = _s
                return _s
            except ImportError:                            # pragma: no cover
                continue
    _SUPERVISOR = False
    return None


_SUPERVISOR_PATHS: list[str] = []


# ── per-app operations ──────────────────────────────────────────────────────

class App:
    def __init__(self, cfg: dict, defaults: dict) -> None:
        self.name = cfg["name"]
        self.repo = cfg["repo"]
        self.root = Path(cfg["root"])
        self.port = int(cfg.get("port", 0))
        self.scratch_port = int(cfg.get("scratch_port", 15000 + (self.port % 1000)))
        self.entry = cfg.get("entry", "app.py")
        self.data_env = cfg.get("data_env", "COA_DATA_DIR")
        # How this app is told which port to use. COA reads the PORT
        # environment variable; LEM takes --port on the command line and
        # ignores the environment. Assuming COA's way would silently start LEM
        # on its default 5557 — during a health check, a second copy on the
        # live port.
        self.port_arg = cfg.get("port_arg") or ""
        self.args = list(cfg.get("args") or [])
        self.health_args = list(cfg.get("health_args") or [])
        self.keep = int(cfg.get("keep_releases", defaults.get("keep_releases", DEFAULT_KEEP)))
        self.browsers_path = defaults.get("playwright_browsers_path")

    @property
    def releases_dir(self) -> Path:
        return self.root / "releases"

    @property
    def current(self) -> Path:
        return self.root / "current"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    def current_version(self) -> str:
        return read_version(self.current)

    def release_names_newest_first(self) -> list[str]:
        if not self.releases_dir.is_dir():
            return []
        dirs = [p for p in self.releases_dir.iterdir() if p.is_dir()]
        dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return [p.name for p in dirs]

    def current_target_name(self) -> Optional[str]:
        try:
            return Path(os.readlink(str(self.current))).name
        except (OSError, ValueError):
            return None


def launch_args(app: App, *, port: int, for_health_check: bool) -> list:
    """The argv (after the interpreter) for starting ``app`` on ``port``."""
    argv = [app.entry]
    if app.port_arg:
        argv += [app.port_arg, str(port)]
    argv += app.args
    if for_health_check:
        argv += app.health_args
    return argv


def launch_env(app: App, *, port: int, data_dir: str, base: dict) -> dict:
    """Environment for starting ``app``.

    ``PORT`` is set only when the port is *not* passed on the command line.
    Setting both invites them to disagree, and the flag wins — leaving a stale
    PORT sitting in a live app's environment saying something untrue.
    """
    env = dict(base)
    env[app.data_env] = str(data_dir)
    if app.port_arg:
        env.pop("PORT", None)
    else:
        env["PORT"] = str(port)
    return env


def stage(app: App, release: dict, token: Optional[str]) -> None:
    """Download, verify, unpack, build and health-check — then stop."""
    tag = release["tag_name"]
    log.info("[%s] staging %s", app.name, tag)
    zip_asset, sum_asset = pick_assets(release.get("assets") or [])

    work = app.data_dir / "downloads"
    work.mkdir(parents=True, exist_ok=True)
    zip_path = work / zip_asset["name"]
    download(zip_asset["browser_download_url"], zip_path, token)
    checksum_text = _api(sum_asset["browser_download_url"], token,
                         raw=True).decode("utf-8", "ignore")

    try:
        verify_asset(zip_path, checksum_text)
    except ChecksumError as exc:
        log.error("[%s] %s", app.name, exc)
        write_staged(app.data_dir, tag=tag, healthy=False, notes=str(exc))
        return
    log.info("[%s] checksum verified", app.name)

    # Before touching the release directory: anything still listening on the
    # scratch port is a stray from an interrupted health check, and it will be
    # holding files inside the very venv we are about to replace.
    _sweep_port(app.scratch_port)

    target = app.releases_dir / tag
    unpack(zip_path, target)
    log.info("[%s] unpacked to %s", app.name, target)

    try:
        python = build_venv(target, browsers_path=app.browsers_path)
    except (subprocess.CalledProcessError, ReleaseError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        log.error("[%s] venv build failed: %s", app.name, detail[:400])
        write_staged(app.data_dir, tag=tag, healthy=False,
                     notes=f"venv build failed: {detail[:300]}")
        return
    log.info("[%s] venv built", app.name)

    probe_data = app.data_dir / "healthcheck"
    if probe_data.exists():
        shutil.rmtree(probe_data, ignore_errors=True)
    healthy, notes = health_check(
        python=python, release_dir=target,
        argv=launch_args(app, port=app.scratch_port, for_health_check=True),
        port=app.scratch_port, data_dir=probe_data,
        env_extra=launch_env(app, port=app.scratch_port, data_dir=str(probe_data),
                             base=os.environ),
        expected_version=tag, browsers_path=app.browsers_path,
    )
    shutil.rmtree(probe_data, ignore_errors=True)
    zip_path.unlink(missing_ok=True)

    log.info("[%s] health check %s: %s", app.name,
             "PASSED" if healthy else "FAILED", notes)
    write_staged(app.data_dir, tag=tag, healthy=healthy, notes=notes)
    if healthy:
        log.info("[%s] %s is staged and waiting for a human. Switch with: "
                 "updater.py switch --app %s --tag %s",
                 app.name, tag, app.name, tag)


def switch(app: App, tag: str, *, force: bool = False) -> bool:
    """Repoint the junction, restart, verify — and roll back if unhealthy."""
    staged = read_staged(app.data_dir)
    if not force:
        ok, why = may_switch(staged=staged, requested_tag=tag)
        if not ok:
            log.error("[%s] refusing to switch: %s", app.name, why)
            return False

    previous = app.current_target_name()
    target = app.releases_dir / tag
    if not target.is_dir():
        log.error("[%s] no such release: %s", app.name, target)
        return False

    log.info("[%s] switching %s -> %s", app.name, previous, tag)
    with _switch_guard(app):
        _stop_app(app)
        repoint_junction(app.current, target)
        _start_app(app)

    healthy, notes = _verify_live(app, expected=tag)
    if healthy:
        log.info("[%s] switch to %s confirmed: %s", app.name, tag, notes)
        write_staged(app.data_dir, tag=tag, healthy=True,
                     notes=f"switched and verified: {notes}")
        prune(app, protected={tag, previous or ""})
        return True

    log.error("[%s] %s is unhealthy after switch (%s) — rolling back to %s",
              app.name, tag, notes, previous)
    if not previous:
        log.critical("[%s] no previous release recorded; cannot roll back", app.name)
        write_staged(app.data_dir, tag=tag, healthy=False,
                     notes=f"unhealthy after switch and no rollback target: {notes}")
        return False

    _stop_app(app)
    repoint_junction(app.current, app.releases_dir / previous)
    _start_app(app)
    back_ok, back_notes = _verify_live(app, expected=previous)
    write_staged(app.data_dir, tag=tag, healthy=False, notes=(
        f"switch failed ({notes}); rolled back to {previous} "
        f"({'recovered' if back_ok else 'ROLLBACK ALSO UNHEALTHY: ' + back_notes})"
    ))
    if not back_ok:
        log.critical("[%s] rollback to %s is also unhealthy: %s",
                     app.name, previous, back_notes)
    return False


class _switch_guard:
    """Hold off supervision while a switch has the app deliberately stopped."""

    def __init__(self, app: App) -> None:
        self.marker = Path(app.data_dir) / "switching"

    def __enter__(self):
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text("switch in progress", encoding="utf-8")
        return self

    def __exit__(self, *exc):
        # Always cleared, including on failure: a stale marker would leave the
        # app unsupervised for as long as it sat there, which is exactly when
        # supervision is most needed.
        self.marker.unlink(missing_ok=True)
        return False


def rollback(app: App) -> bool:
    """Switch to the most recent release that is not the current one."""
    current = app.current_target_name()
    for name in app.release_names_newest_first():
        if name != current:
            log.info("[%s] rolling back to %s", app.name, name)
            return switch(app, name, force=True)
    log.error("[%s] no other release to roll back to", app.name)
    return False


def _stop_app(app: App) -> None:
    sup = _load_supervisor()
    subprocess.run(["taskkill", "/F", "/FI", f"WINDOWTITLE eq {app.name}"],
                   capture_output=True, text=True)
    for pid in _pids_on_port(app.port):
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, text=True)
    if sup and not sup.wait_until_free(app.port, timeout=30.0):
        raise UpdaterError(
            f"port {app.port} still has a listener; refusing to repoint the "
            "junction while the old release may still hold files open"
        )


def _pids_on_port(port: int) -> list[int]:
    if not port:
        return []
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and \
                parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
            try:
                pids.add(int(parts[4]))
            except ValueError:
                pass
    return sorted(pids)


def _start_app(app: App) -> None:
    python = app.current / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = Path(sys.executable)
    env = launch_env(app, port=app.port, data_dir=str(app.data_dir),
                     base=os.environ)
    if app.browsers_path:
        env["PLAYWRIGHT_BROWSERS_PATH"] = app.browsers_path
    argv = launch_args(app, port=app.port, for_health_check=False)
    subprocess.Popen([str(python), *argv], cwd=str(app.current), env=env,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))


def _verify_live(app: App, *, expected: str) -> tuple[bool, str]:
    deadline = time.time() + HEALTH_TIMEOUT
    while time.time() < deadline:
        body = _http_get_json(f"http://127.0.0.1:{app.port}/healthz")
        if body and body.get("status") == "ok":
            got = body.get("version")
            if expected and differs_from(got, expected):
                return False, f"/healthz reports {got!r}, expected {expected!r}"
            return True, f"version={got} labcore={body.get('labcore')}"
        time.sleep(1.0)
    return False, f"/healthz did not answer within {HEALTH_TIMEOUT:.0f}s"


def prune(app: App, *, protected: Iterable[str] = ()) -> None:
    guard = set(protected) | {app.current_target_name() or ""}
    doomed = releases_to_prune(app.release_names_newest_first(),
                               keep=app.keep, protected=guard)
    for name in doomed:
        path = app.releases_dir / name
        log.info("[%s] pruning old release %s", app.name, name)
        shutil.rmtree(path, ignore_errors=True)


# ── service ─────────────────────────────────────────────────────────────────

_START_HISTORY: dict = {}


def supervise(app: App) -> str:
    """Restart ``app`` if it has stopped serving.

    COA exits on purpose — ``_auto_restart_worker`` at 3 AM to refresh
    long-lived Playwright and QBench tokens, and ``/api/restart`` when a
    reviewer clicks Restart. Run.pyw used to respawn it; the deployed layout
    has no Run.pyw, so without this COA would exit at 3 AM and never return,
    and a reviewer clicking Restart would end the service for the day.

    Probing is by connecting, never by binding: on Windows ``SO_REUSEADDR``
    lets a bind succeed against a port that is already being served, which is
    how the 2026-07-31 inert-duplicate incident happened.
    """
    if not app.port:
        return SUPERVISE_OK

    sup = _load_supervisor()
    has_listener = (sup.port_has_listener(app.port) if sup
                    else bool(_pids_on_port(app.port)))

    history = _START_HISTORY.setdefault(app.name, [])
    now = time.monotonic()
    decision = supervision_decision(
        has_listener=has_listener,
        paused=is_paused(app.data_dir),
        starts_in_window=starts_within(history, now=now,
                                       window=SUPERVISE_WINDOW_SECONDS),
        max_starts=SUPERVISE_MAX_STARTS,
    )

    if decision == SUPERVISE_START:
        log.warning("[%s] not serving on port %s — starting it", app.name, app.port)
        history.append(now)
        history[:] = [t for t in history if now - t <= SUPERVISE_WINDOW_SECONDS]
        try:
            _start_app(app)
        except Exception:
            log.exception("[%s] failed to start", app.name)
            return decision
        if sup and sup.wait_until_serving(app.port, timeout=60.0):
            log.info("[%s] back up on port %s", app.name, app.port)
        else:
            log.error("[%s] started but nothing is serving port %s yet",
                      app.name, app.port)
    elif decision == SUPERVISE_GIVING_UP:
        log.critical(
            "[%s] has failed to stay up after %d starts in %.0f minutes — not "
            "restarting again. Something is wrong with the release; check "
            "%s\\app.log. Clear the condition and it will be picked up on the "
            "next poll.",
            app.name, SUPERVISE_MAX_STARTS, SUPERVISE_WINDOW_SECONDS / 60,
            app.data_dir)
    elif decision == SUPERVISE_PAUSED and not has_listener:
        log.info("[%s] down and paused (%s\\paused) — leaving it alone",
                 app.name, app.data_dir)
    return decision


def poll_once(app: App, token: Optional[str]) -> str:
    release = latest_release(app.repo, token)
    latest = release.get("tag_name") if release else None
    current = app.current_version()
    staged = read_staged(app.data_dir)
    action = plan_poll(current=current, latest=latest, staged=staged)
    log.info("[%s] current=%s latest=%s staged=%s -> %s", app.name, current,
             latest, (staged or {}).get("tag"), action)
    if action == STAGE:
        try:
            stage(app, release, token)
        except UpdaterError as exc:
            log.error("[%s] staging failed: %s", app.name, exc)
            write_staged(app.data_dir, tag=latest or "?", healthy=False, notes=str(exc))
    return action


def load_config(path: Path) -> tuple[list[App], dict]:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    apps = [App(a, cfg) for a in cfg.get("apps", [])]
    global _SUPERVISOR_PATHS
    _SUPERVISOR_PATHS = [str(a.current) for a in apps]
    return apps, cfg


def setup_logging(log_path: Path, verbose: bool = False) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.addHandler(fh)
    log.addHandler(sh)
    # Don't also hand records to the root logger. If anything upstream has
    # configured one, every deploy action gets logged twice, which reads like
    # the updater did the thing twice.
    log.propagate = False


def main(argv: Optional[Sequence[str]] = None) -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="ASAP Labs deployment updater")
    ap.add_argument("command", choices=["run", "poll", "status", "switch",
                                        "rollback", "pause", "resume", "start"])
    ap.add_argument("--config", default=str(here / "config.json"))
    ap.add_argument("--app")
    ap.add_argument("--tag")
    ap.add_argument("--force", action="store_true",
                    help="switch to a release that is not the staged/healthy "
                         "one. Automatic rollback still applies.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    cfg_path = Path(args.config)
    if not cfg_path.is_file():
        print(f"no config at {cfg_path}", file=sys.stderr)
        return 2
    apps, cfg = load_config(cfg_path)
    setup_logging(Path(cfg.get("log_file", here / "updater.log")), args.verbose)
    token = read_credential(cfg.get("credential_target", "asaplabs-github"))
    if not token:
        log.warning("no GitHub token available; polling anonymously "
                    "(60 requests/hour, and private repos will 404)")

    chosen = [a for a in apps if not args.app or a.name == args.app]
    if args.app and not chosen:
        print(f"no app named {args.app!r} in config", file=sys.stderr)
        return 2

    if args.command == "status":
        sup = _load_supervisor()
        for a in chosen:
            s = read_staged(a.data_dir) or {}
            if a.port:
                up = (sup.port_has_listener(a.port) if sup
                      else bool(_pids_on_port(a.port)))
                serving = f"SERVING on {a.port}" if up else f"DOWN (port {a.port})"
            else:
                serving = "no port configured"
            held = " [PAUSED]" if (Path(a.data_dir) / "paused").exists() else ""
            print(f"{a.name}: {serving}{held}  current={a.current_version()} "
                  f"junction->{a.current_target_name()} "
                  f"staged={s.get('tag')} healthy={s.get('healthy')}")
            if s.get("notes"):
                print(f"    notes: {s.get('notes')}")
        return 0

    if args.command in ("pause", "resume"):
        for a in chosen:
            marker = Path(a.data_dir) / "paused"
            if args.command == "pause":
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("paused by hand", encoding="utf-8")
                print(f"{a.name}: paused — the updater will not restart it. "
                      f"Stop it yourself if it is still running.")
            else:
                marker.unlink(missing_ok=True)
                print(f"{a.name}: resumed — it will be started within one poll.")
        return 0

    if args.command == "start":
        for a in chosen:
            if (Path(a.data_dir) / "paused").exists():
                print(f"{a.name}: is paused; run `resume` first", file=sys.stderr)
                continue
            print(f"{a.name}: {supervise(a)}")
        return 0

    if args.command == "switch":
        if not args.tag:
            print("--tag is required for switch", file=sys.stderr)
            return 2
        if args.force:
            log.warning("--force: skipping the staged/healthy guard. The "
                        "post-switch health check and automatic rollback still "
                        "apply — those are not bypassable.")
        return 0 if all(switch(a, args.tag, force=args.force) for a in chosen) else 1

    if args.command == "rollback":
        return 0 if all(rollback(a) for a in chosen) else 1

    if args.command == "poll":
        for a in chosen:
            poll_once(a, token)
        return 0

    interval = int(cfg.get("poll_seconds", DEFAULT_POLL_SECONDS))
    supervise_interval = int(cfg.get("supervise_seconds", DEFAULT_SUPERVISE_SECONDS))
    # Deliberately decoupled. Checking GitHub is a network call and every five
    # minutes is plenty; noticing that an app has stopped serving is a local
    # port probe costing nothing. Tying them together would mean a reviewer who
    # clicks Restart waits up to a full release-poll for the app to come back.
    log.info("updater started; release check every %ss, supervision every %ss",
             interval, supervise_interval)
    next_release_check = 0.0
    while True:
        now = time.monotonic()
        for a in chosen:
            # Supervision first, and in its own try: keeping the lab's apps up
            # matters more than checking GitHub, and a release-check failure
            # must not stop a dead app from being restarted.
            try:
                supervise(a)
            except Exception:
                log.exception("[%s] unhandled error while supervising", a.name)

        if now >= next_release_check:
            for a in chosen:
                try:
                    poll_once(a, token)
                except Exception:
                    log.exception("[%s] unhandled error in poll cycle", a.name)
            next_release_check = time.monotonic() + interval

        time.sleep(supervise_interval)


if __name__ == "__main__":
    raise SystemExit(main())
