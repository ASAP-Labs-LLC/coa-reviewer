"""The reviewer's QBench web login, kept on this computer so it survives updates.

COA Reviewer signs in to QBench's website (Playwright, ``COASession``) with a
person's username and password. That login used to sit in plain text in
``web_app_config.json``; it now lives here, outside the release, next to the
QBench API key store (``qbench_secrets.py``):

* **Primary** — ``COA_QBENCH_LOGIN_PATH`` when set, else
  ``%APPDATA%\\ASAPLabs\\coa-qbench-login.json`` on Windows, else
  ``~/.config/asaplabs/coa-qbench-login.json``.
* **Fallback** — a file in the app's ``DATA_DIR`` (the caller passes it).

The store manages itself; nobody is ever asked to create a folder or fix a
permission. :meth:`LoginStore.select` probes the primary (create the folder,
write/read/delete a probe file, and on Windows prove DPAPI works for this
account) and falls back to the data directory when it can't — logging one
WARNING that says which place was chosen and why. Loading looks in the chosen
place, then the other one, so a login saved in either is found after an update.
Saving re-probes, writes to the chosen place, and tries the other before giving
up; a successful save removes the stale copy in the other place.

File shape::

    {"version": 1, "username": "...",
     "password": {"scheme": "dpapi" | "plain", "data": "<base64>"},
     "saved_at": "2026-09-28T12:00:00+00:00"}

``dpapi`` is Windows DPAPI in CURRENT_USER scope: only the Windows account that
saved the file can read it back. ``plain`` is base64 only — used where DPAPI
does not exist (macOS/Linux dev boxes, file mode 0600) or, as a last resort on
Windows, in the fallback location when DPAPI refuses to work on the account.

Contract: nothing here raises for I/O, parsing or decryption. A problem is a
WARNING (never containing the password or its ciphertext) and a ``None`` /
``False`` result — worst case the reviewer types the login again.
"""
from __future__ import annotations

import base64
import binascii
import functools
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, NamedTuple, Optional, Protocol, Tuple, Union

__all__ = [
    "CryptoError",
    "LoginStore",
    "default_crypto",
    "default_store_path",
]

log = logging.getLogger("coa.credentials")

FILE_VERSION = 1
MAX_FILE_BYTES = 64 * 1024          # a login is ~1 KB; anything bigger is not ours
MAX_FIELD_CHARS = 1024              # username / password length ceiling
MAX_WARNINGS_REMEMBERED = 64        # bound on the warn-once memory
PROBE_BYTES = b"coa-reviewer probe\n"
LOCK_RETRIES = 5                    # PermissionError retries (Windows file locks)
LOCK_RETRY_SECONDS = 0.05

# DPAPI parameters. The entropy is not a secret — it scopes our blobs so that
# another program running as the same user can't unprotect them by accident.
ENTROPY = b"ASAPLabs COA Reviewer QBench login"
CRYPTPROTECT_UI_FORBIDDEN = 0x1

PathLike = Union[str, os.PathLike]

_plain_notice_logged = False


class SavedLogin(NamedTuple):
    username: str
    password: str
    saved_at: Optional[float]       # epoch seconds; None if unrecorded/unparseable


def _parse_saved_at(value) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class CryptoError(RuntimeError):
    """Encryption or decryption failed (wrong user, no DPAPI, bad blob)."""


class Crypto(Protocol):
    scheme: str

    def protect(self, data: bytes) -> bytes: ...

    def unprotect(self, blob: bytes) -> bytes: ...


# ── location ─────────────────────────────────────────────────────────────

def default_store_path() -> str:
    """Where the login is kept unless something overrides it.

    Mirrors ``qbench_secrets.default_store_path`` so both QBench stores sit
    side by side in ``%APPDATA%\\ASAPLabs``.
    """
    override = os.environ.get("COA_QBENCH_LOGIN_PATH", "").strip()
    if override:
        return override
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "ASAPLabs", "coa-qbench-login.json")
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".config", "asaplabs", "coa-qbench-login.json")


# ── DPAPI (Windows) ──────────────────────────────────────────────────────

class DpapiCrypto:
    """Windows DPAPI via ctypes, CURRENT_USER scope, never prompting."""

    scheme = "dpapi"

    def __init__(self, entropy: bytes = ENTROPY, loader=None) -> None:
        """``loader(name)`` returns the DLL named ``crypt32``/``kernel32``;
        the default is ``ctypes.WinDLL(name, use_last_error=True)``. Tests
        inject stand-ins built from real ctypes callbacks, so this binding
        runs for real on every OS."""
        import ctypes
        from ctypes import wintypes

        if loader is None:
            def loader(name):
                return ctypes.WinDLL(name, use_last_error=True)

        class DataBlob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        self._ctypes = ctypes
        self._DataBlob = DataBlob
        self._entropy = entropy
        crypt32 = loader("crypt32")
        kernel32 = loader("kernel32")
        blob_p = ctypes.POINTER(DataBlob)
        self._protect = crypt32.CryptProtectData
        self._protect.argtypes = [blob_p, wintypes.LPCWSTR, blob_p, ctypes.c_void_p,
                                  ctypes.c_void_p, wintypes.DWORD, blob_p]
        self._protect.restype = wintypes.BOOL
        self._unprotect = crypt32.CryptUnprotectData
        self._unprotect.argtypes = [blob_p, ctypes.c_void_p, blob_p, ctypes.c_void_p,
                                    ctypes.c_void_p, wintypes.DWORD, blob_p]
        self._unprotect.restype = wintypes.BOOL
        self._local_free = kernel32.LocalFree
        self._local_free.argtypes = [ctypes.c_void_p]
        self._local_free.restype = ctypes.c_void_p

    def _blob(self, data: bytes):
        ctypes = self._ctypes
        buf = ctypes.create_string_buffer(data, len(data))
        ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
        return self._DataBlob(len(data), ptr), buf       # keep buf alive

    def _call(self, fn, name: str, data: bytes) -> bytes:
        ctypes = self._ctypes
        data_in, _keep_in = self._blob(data)
        entropy, _keep_ent = self._blob(self._entropy)
        out = self._DataBlob()
        ok = fn(ctypes.byref(data_in), None, ctypes.byref(entropy), None, None,
                CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
        if not ok:
            last_error = getattr(ctypes, "get_last_error", lambda: 0)()   # Windows-only
            raise CryptoError(f"{name} failed (Windows error {last_error})")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            self._local_free(ctypes.cast(out.pbData, ctypes.c_void_p))

    def protect(self, data: bytes) -> bytes:
        return self._call(self._protect, "CryptProtectData", data)

    def unprotect(self, blob: bytes) -> bytes:
        return self._call(self._unprotect, "CryptUnprotectData", blob)


def default_crypto() -> Optional[Crypto]:
    """DPAPI on Windows; ``None`` (plain storage) everywhere else.

    A Windows machine where DPAPI can't even be loaded also gets ``None`` —
    the store then refuses the primary location and keeps the login, plain,
    in the data directory rather than not at all.
    """
    if os.name != "nt":
        return None
    try:
        return DpapiCrypto()
    except (OSError, AttributeError, ImportError) as exc:
        log.warning("Windows DPAPI is unavailable (%s); a saved QBench login "
                    "will not be encrypted", exc)
        return None


# ── file primitives ──────────────────────────────────────────────────────

def _retry_locked(action, *args):
    """Run ``action(*args)``, retrying briefly on ``PermissionError``.

    Windows refuses to replace or delete a file while any handle has it open
    (a reader, an antivirus scan). That is a moment's wait, not an unusable
    folder. Bounded: LOCK_RETRIES attempts, LOCK_RETRY_SECONDS apart.
    """
    for attempt in range(1, LOCK_RETRIES + 1):
        try:
            return action(*args)
        except PermissionError:
            if attempt == LOCK_RETRIES:
                raise
            time.sleep(LOCK_RETRY_SECONDS)
    return None                                     # unreachable


def _write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via a uniquely named temp file in the same
    folder (``mkstemp``: 0600, safe across threads and processes).

    Readers see the old file or the new one, never half of either.
    Raises ``OSError``; the caller decides what a failure means.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.",
                                    suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        _retry_locked(os.replace, str(tmp), str(path))
    except BaseException:
        _unlink_quietly(tmp)
        raise


def _probe_folder(path: Path) -> None:
    """Prove the folder ``path`` would live in can be created, written, read
    and cleaned up — with a uniquely named file, never ``path`` itself.
    Raises ``OSError``/``ValueError``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, probe_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.",
                                      suffix=".probe")
    probe = Path(probe_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(PROBE_BYTES)
        if _read_bounded(probe) != PROBE_BYTES:
            raise ValueError("probe file read back wrong")
    finally:
        _unlink_quietly(probe)


def _read_bounded(path: Path) -> Optional[bytes]:
    """The file's bytes, ``None`` if it doesn't exist. Raises ``OSError`` or
    ``ValueError`` (too large)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_FILE_BYTES + 1)
    except (FileNotFoundError, NotADirectoryError):
        return None
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"file is too large ({len(data)}+ bytes)")
    return data


def _unlink_quietly(path: Path) -> bool:
    """Remove ``path``; True if it is gone afterwards."""
    try:
        _retry_locked(path.unlink)
    except (FileNotFoundError, NotADirectoryError):   # never existed
        return True
    except OSError as exc:
        log.warning("Could not remove %s: %s", path, exc)
        return False
    return True


# ── the store ────────────────────────────────────────────────────────────

def _locked(method):
    """Serialise a public LoginStore method on the store's re-entrant lock:
    Flask serves requests on threads, and a save racing a load or another
    save would otherwise see half a probe or replace a file mid-read."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class LoginStore:
    """One QBench web login, kept in whichever of two places works."""

    def __init__(self, primary: PathLike, fallback: Optional[PathLike] = None, *,
                 crypto: Optional[Crypto] = None,
                 require_encryption: Optional[bool] = None) -> None:
        self.primary = Path(primary)
        self.fallback = Path(fallback) if fallback else None
        self._crypto = crypto
        # On Windows the primary is only used if DPAPI works there; a store
        # given a crypto layer holds itself to the same rule.
        self._require_encryption = (crypto is not None) if require_encryption is None \
            else bool(require_encryption)
        self._chosen: Optional[Path] = None
        self._selected = False          # has select() run (and logged) yet?
        self._warned: set = set()
        self._lock = threading.RLock()
        self._unencrypted_warned = False

    @classmethod
    def for_this_machine(cls, fallback: PathLike) -> "LoginStore":
        """The real store: default location, DPAPI on Windows."""
        crypto = default_crypto()
        return cls(default_store_path(), fallback, crypto=crypto,
                   require_encryption=(os.name == "nt"))

    @property
    def path(self) -> Path:
        """The location in use (the primary until a probe says otherwise)."""
        return self._chosen or self.primary

    # ── choosing a location ──────────────────────────────────────────────

    @_locked
    def select(self) -> Optional[Path]:
        """Probe and pick the location to save to. Never raises.

        Logs only when the answer changes, so re-probing before every save
        doesn't repeat the same line.
        """
        chosen, problem = self._pick()
        if not self._selected or chosen != self._chosen:
            if chosen == self.primary:
                log.info("QBench login is kept in %s", chosen)
            elif chosen is not None:
                log.warning("QBench login will be kept in %s because %s could not "
                            "be used (%s)", chosen, self.primary, problem)
            else:
                log.warning("QBench login can't be saved on this computer (%s). "
                            "The reviewer will be asked to sign in after each "
                            "restart.", problem)
        self._selected = True
        self._chosen = chosen
        return chosen

    def _pick(self) -> Tuple[Optional[Path], str]:
        ok, why = self._probe(self.primary, need_crypto=self._require_encryption)
        if ok:
            return self.primary, ""
        if self.fallback is None:
            return None, f"{self.primary}: {why}"
        ok2, why2 = self._probe(self.fallback, need_crypto=False)
        if ok2:
            return self.fallback, why
        return None, f"{self.primary}: {why}; {self.fallback}: {why2}"

    def _probe(self, path: Path, *, need_crypto: bool) -> Tuple[bool, str]:
        try:
            _probe_folder(path)
        except (OSError, ValueError) as exc:
            return False, f"folder not usable: {exc}"
        if need_crypto:
            return self._probe_crypto()
        return True, "ok"

    def _probe_crypto(self) -> Tuple[bool, str]:
        if self._crypto is None:
            return False, "encryption is unavailable"
        try:
            if self._crypto.unprotect(self._crypto.protect(PROBE_BYTES)) != PROBE_BYTES:
                return False, "encryption round trip mismatched"
        except Exception as exc:        # a ctypes fault is as fatal as a refusal
            return False, f"encryption failed: {exc}"
        return True, "ok"

    def _locations(self) -> Iterator[Path]:
        first = self._chosen or self.primary
        yield first
        for other in (self.primary, self.fallback):
            if other is not None and other != first:
                yield other

    # ── load ─────────────────────────────────────────────────────────────

    @_locked
    def load(self) -> Optional[Tuple[str, str]]:
        """``(username, password)`` from the first place that has a readable
        login, else ``None``. Never raises."""
        entry = self.load_entry()
        return (entry.username, entry.password) if entry else None

    @_locked
    def load_entry(self) -> Optional[SavedLogin]:
        """The login plus when it was saved (``saved_at``, epoch seconds), so a
        caller can tell it from an older or newer copy elsewhere. Never raises."""
        for path in self._locations():
            try:
                found = self._load_one(path)
            except Exception:           # last-resort guard: boot must go on
                log.exception("Unexpected error reading saved QBench login at %s", path)
                found = None
            if found is not None:
                log.debug("Read saved QBench login for %s from %s", found[0], path)
                return found
        return None

    def _load_one(self, path: Path) -> Optional[SavedLogin]:
        try:
            raw = _read_bounded(path)
        except (OSError, ValueError) as exc:
            self._warn_once(path, "unreadable", "Saved QBench login at %s is not "
                            "readable: %s", path, exc)
            return None
        if raw is None:
            return None
        try:
            return self._decode(raw)
        except CryptoError:
            self._warn_once(path, "decrypt", "Saved QBench login at %s could not be "
                            "decrypted — saved by a different Windows account?", path)
        except (ValueError, TypeError, KeyError, UnicodeDecodeError,
                binascii.Error) as exc:
            self._warn_once(path, "damaged", "Saved QBench login at %s is damaged "
                            "(%s); ignoring it", path, type(exc).__name__)
        return None

    def _decode(self, raw: bytes) -> SavedLogin:
        doc = json.loads(raw.decode("utf-8"))
        if not isinstance(doc, dict) or doc.get("version") != FILE_VERSION:
            raise ValueError("unsupported file version")
        username = doc["username"]
        pw = doc["password"]
        if not isinstance(username, str) or not isinstance(pw, dict):
            raise ValueError("wrong field types")
        blob = base64.b64decode(pw["data"], validate=True)
        scheme = pw.get("scheme")
        if scheme == "plain":
            secret = blob
        elif scheme == "dpapi":
            if self._crypto is None or self._crypto.scheme != "dpapi":
                raise CryptoError("DPAPI is not available here")
            try:
                secret = self._crypto.unprotect(blob)
            except CryptoError:
                raise
            except Exception as exc:    # a ctypes fault reads as "can't decrypt"
                raise CryptoError(str(exc)) from exc
        else:
            raise ValueError("unknown scheme")
        password = secret.decode("utf-8")
        if not username.strip() or not password.strip():
            raise ValueError("blank login")
        return SavedLogin(username, password, _parse_saved_at(doc.get("saved_at")))

    # ── save ─────────────────────────────────────────────────────────────

    @_locked
    def save(self, username: str, password: str) -> bool:
        """Remember the login. True once one location holds it. Never raises."""
        username = (username or "").strip()
        password = (password or "").strip()
        if not username or not password or len(username) > MAX_FIELD_CHARS \
                or len(password) > MAX_FIELD_CHARS:
            log.warning("Not saving QBench login: username/password blank or too long")
            return False
        self.select()
        for path in self._locations():
            try:
                saved = self._save_one(path, username, password)
            except Exception:           # last-resort guard: login must go on
                log.exception("Unexpected error saving QBench login to %s", path)
                saved = False
            if saved:
                self._drop_other_copies(path)
                log.info("Saved QBench login in %s", path)
                log.debug("Saved QBench login is for %s", username)
                return True
        log.warning("QBench login could not be saved anywhere; it is used for "
                    "this session only")
        return False

    def _save_one(self, path: Path, username: str, password: str) -> bool:
        field = self._encode(path, password)
        if field is None:
            return False
        doc = {"version": FILE_VERSION, "username": username, "password": field,
               "saved_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}
        try:
            _write_atomic(path, json.dumps(doc, indent=2).encode("utf-8"))
        except OSError as exc:
            log.warning("Could not save QBench login to %s: %s", path, exc)
            return False
        return True

    def _encode(self, path: Path, password: str) -> Optional[dict]:
        """The ``password`` field for ``path``, or ``None`` if it may not be
        written there. Encrypts only when a full round trip works — a blob
        that can't be decrypted again is a login lost, not a login kept."""
        secret = password.encode("utf-8")
        if self._crypto is not None:
            ok, why = self._probe_crypto()
            if ok:
                try:
                    blob = self._crypto.protect(secret)
                    return {"scheme": self._crypto.scheme,
                            "data": base64.b64encode(blob).decode("ascii")}
                except Exception as exc:    # CryptoError, or a ctypes fault
                    why = f"encryption failed: {exc}"
        else:
            why = "encryption is unavailable"
        if self._require_encryption:
            if path == self.primary:
                log.warning("Not saving QBench login in %s: %s", path, why)
                return None
            self._warn_unencrypted_once(path, why)
        else:
            _note_plain_once()
        return {"scheme": "plain", "data": base64.b64encode(secret).decode("ascii")}

    def _drop_other_copies(self, kept: Path) -> None:
        for path in (self.primary, self.fallback):
            if path is not None and path != kept and path.exists():
                if _unlink_quietly(path):
                    log.debug("Removed older QBench login copy at %s", path)

    # ── clear ────────────────────────────────────────────────────────────

    @_locked
    def clear(self) -> bool:
        """Forget the login everywhere. True if no copy is left."""
        gone = True
        for path in (self.primary, self.fallback):
            if path is not None:
                gone = _unlink_quietly(path) and gone
        self._warned.clear()
        log.info("Forgot saved QBench login" if gone
                 else "Could not forget every copy of the saved QBench login")
        return gone

    # ── logging helpers ──────────────────────────────────────────────────

    def _warn_unencrypted_once(self, path: Path, why: str) -> None:
        """Windows without working DPAPI for this account: the fallback copy is
        only base64 and protected by nothing but the data folder's
        permissions. Said once per process, not on every save."""
        if self._unencrypted_warned:
            return
        self._unencrypted_warned = True
        log.warning("Keeping QBench login unencrypted (base64 only) in %s — %s; "
                    "only the folder's permissions protect it", path, why)

    def _warn_once(self, path: Path, kind: str, msg: str, *args) -> None:
        """WARNING the first time this (path, problem, file version) shows up;
        DEBUG after. /api/config polls during boot, so one bad file would
        otherwise log the same line every three seconds."""
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            stamp = None
        key = (str(path), kind, stamp)
        if key in self._warned:
            log.debug(msg, *args)
            return
        if len(self._warned) >= MAX_WARNINGS_REMEMBERED:
            self._warned.clear()
        self._warned.add(key)
        log.warning(msg, *args)


def _note_plain_once() -> None:
    global _plain_notice_logged
    if _plain_notice_logged:
        return
    _plain_notice_logged = True
    log.info("The saved QBench login is not encrypted on this operating system "
             "(DPAPI is Windows-only); the file is readable by its owner only")

