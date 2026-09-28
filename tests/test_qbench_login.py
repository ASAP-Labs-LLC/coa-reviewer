"""The QBench web login store (``qbench_login.LoginStore``).

The store keeps the reviewer's QBench username/password outside the release
so it survives updates, and it has to manage itself: the people using this app
will never create a folder, fix a permission or read a path. So every test here
asserts one of three things — the login comes back, the store picked a working
place by itself, or a failure degraded to "ask again" without raising and
without writing the password into a log.

The DPAPI layer is Windows-only; a fake with the same contract (bound to a
"user", so a different user cannot decrypt) exercises that branch everywhere.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import stat
import sys

import pytest

import qbench_login
from qbench_login import CryptoError, LoginStore

USER = "reviewer@example.com"
# Built, not written as one literal: nothing in the tree should look like a
# stored secret, and a distinctive value makes a leak into a log easy to spot.
SECRET = "pw-" + "only-for-tests-" + "7f3a"


class FakeDpapi:
    """DPAPI's contract without Windows: ciphertext only its own user can read."""

    scheme = "dpapi"

    def __init__(self, user: str = "alice") -> None:
        self._tag = b"FAKE:" + user.encode() + b":"

    def protect(self, data: bytes) -> bytes:
        return self._tag + bytes(b ^ 0x5A for b in data)

    def unprotect(self, blob: bytes) -> bytes:
        if not blob.startswith(self._tag):
            raise CryptoError("the data was protected by another user")
        return bytes(b ^ 0x5A for b in blob[len(self._tag):])


class BrokenDpapi(FakeDpapi):
    """DPAPI that refuses to work on this account."""

    def protect(self, data: bytes) -> bytes:
        raise CryptoError("CryptProtectData failed (error 5)")


def _store(tmp_path, crypto=None, *, require_encryption=None, primary=None,
           fallback=None) -> LoginStore:
    return LoginStore(
        primary or tmp_path / "appdata" / "ASAPLabs" / "coa-qbench-login.json",
        fallback or tmp_path / "data" / "qbench_login.json",
        crypto=crypto,
        require_encryption=(crypto is not None) if require_encryption is None
        else require_encryption,
    )


def _no_secret_logged(caplog) -> None:
    for rec in caplog.records:
        text = rec.getMessage()
        assert SECRET not in text, f"password leaked into a log line: {text!r}"


def _unusable(path) -> None:
    """Make ``path``'s directory impossible to create: it is a file."""
    blocker = path.parent
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("not a directory")


# ── location ─────────────────────────────────────────────────────────────

def test_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("COA_QBENCH_LOGIN_PATH", str(tmp_path / "x.json"))
    assert qbench_login.default_store_path() == str(tmp_path / "x.json")


def test_default_path_is_next_to_the_qbench_api_key_store(monkeypatch, tmp_path):
    monkeypatch.delenv("COA_QBENCH_LOGIN_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    got = qbench_login.default_store_path()
    if os.name == "nt":
        assert got == os.path.join(str(tmp_path / "Roaming"), "ASAPLabs",
                                   "coa-qbench-login.json")
    else:
        assert got == os.path.join(str(tmp_path), ".config", "asaplabs",
                                   "coa-qbench-login.json")


def test_constructing_a_store_touches_nothing(tmp_path):
    store = _store(tmp_path)
    assert not (tmp_path / "appdata").exists()
    assert not (tmp_path / "data").exists()
    assert store.path == tmp_path / "appdata" / "ASAPLabs" / "coa-qbench-login.json"


# ── round trips ──────────────────────────────────────────────────────────

def test_plain_roundtrip(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="coa.credentials")
    store = _store(tmp_path)
    assert store.load() is None
    assert store.save(USER, SECRET) is True
    assert store.load() == (USER, SECRET)
    doc = json.loads(store.path.read_text(encoding="utf-8"))
    assert doc["version"] == 1
    assert doc["username"] == USER
    assert doc["password"]["scheme"] == "plain"
    assert SECRET not in store.path.read_text(encoding="utf-8")
    assert doc["saved_at"]
    _no_secret_logged(caplog)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_plain_file_is_private_to_its_owner(tmp_path):
    store = _store(tmp_path)
    assert store.save(USER, SECRET)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_plain_scheme_says_once_that_it_is_not_encrypted(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(qbench_login, "_plain_notice_logged", False)
    caplog.set_level(logging.INFO, logger="coa.credentials")
    store = _store(tmp_path)
    store.save(USER, SECRET)
    store.save(USER, SECRET)
    notices = [r for r in caplog.records if "not encrypted" in r.getMessage()]
    assert len(notices) == 1


def test_dpapi_roundtrip_via_fake(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="coa.credentials")
    store = _store(tmp_path, FakeDpapi())
    assert store.save(USER, SECRET)
    doc = json.loads(store.path.read_text(encoding="utf-8"))
    assert doc["password"]["scheme"] == "dpapi"
    blob = base64.b64decode(doc["password"]["data"])
    assert blob.startswith(b"FAKE:alice:")
    assert store.load() == (USER, SECRET)
    for rec in caplog.records:           # neither the password nor its ciphertext
        assert doc["password"]["data"] not in rec.getMessage()
    _no_secret_logged(caplog)


def test_a_different_windows_user_cannot_read_it(tmp_path, caplog):
    _store(tmp_path, FakeDpapi("alice")).save(USER, SECRET)
    caplog.set_level(logging.WARNING, logger="coa.credentials")
    bob = _store(tmp_path, FakeDpapi("bob"))
    assert bob.load() is None
    warned = " ".join(r.getMessage() for r in caplog.records)
    assert "different Windows account" in warned
    assert str(bob.path) in warned
    _no_secret_logged(caplog)


def test_a_dpapi_file_on_a_machine_without_dpapi_is_not_readable(tmp_path):
    _store(tmp_path, FakeDpapi()).save(USER, SECRET)
    assert _store(tmp_path).load() is None


def test_saving_again_replaces_the_login(tmp_path):
    store = _store(tmp_path)
    store.save(USER, SECRET)
    store.save("other@example.com", SECRET + "2")
    assert store.load() == ("other@example.com", SECRET + "2")


def test_blank_values_are_not_saved(tmp_path):
    store = _store(tmp_path)
    assert store.save("", SECRET) is False
    assert store.save(USER, "  ") is False
    assert store.load() is None


# ── damaged files ────────────────────────────────────────────────────────

@pytest.mark.parametrize("content", [
    "not json{",
    "[]",
    json.dumps({"version": 1, "username": USER}),
    json.dumps({"version": 1, "username": USER,
                "password": {"scheme": "rot13", "data": "eA=="}}),
    json.dumps({"version": 1, "username": USER,
                "password": {"scheme": "plain", "data": "%%%not-base64"}}),
    json.dumps({"version": 99, "username": USER,
                "password": {"scheme": "plain", "data": "eA=="}}),
])
def test_a_damaged_file_reads_as_no_login(tmp_path, caplog, content):
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(content, encoding="utf-8")
    caplog.set_level(logging.WARNING, logger="coa.credentials")
    assert store.load() is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_an_oversized_file_is_not_read(tmp_path, caplog):
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_bytes(b" " * (qbench_login.MAX_FILE_BYTES + 1))
    caplog.set_level(logging.WARNING, logger="coa.credentials")
    assert store.load() is None
    assert "too large" in " ".join(r.getMessage() for r in caplog.records)


def test_a_repeated_warning_is_logged_once(tmp_path, caplog):
    """/api/config polls every 3 s during boot; one bad file must not flood
    the log with the same warning."""
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text("not json{", encoding="utf-8")
    caplog.set_level(logging.WARNING, logger="coa.credentials")
    for _ in range(5):
        store.load()
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_atomic_write_leaves_no_temp_file(tmp_path):
    store = _store(tmp_path)
    store.save(USER, SECRET)
    leftovers = [p.name for p in store.path.parent.iterdir() if p != store.path]
    assert leftovers == []


def test_a_failed_write_keeps_the_previous_login(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.save(USER, SECRET)

    def boom(*a, **k):
        raise OSError("disk full")
    with monkeypatch.context() as mp:
        mp.setattr(qbench_login.os, "replace", boom)
        assert store.save("new@example.com", "new-pass") is False
    assert store.load() == (USER, SECRET)
    leftovers = [p.name for p in store.path.parent.iterdir() if p != store.path]
    assert leftovers == []


def test_clear_removes_the_login(tmp_path):
    store = _store(tmp_path)
    store.save(USER, SECRET)
    assert store.clear() is True
    assert store.load() is None
    assert store.clear() is True           # nothing to clear is still "clear"


# ── self-managing location ───────────────────────────────────────────────

def test_select_uses_appdata_when_it_works(tmp_path):
    store = _store(tmp_path)
    assert store.select() == store.primary
    assert store.primary.parent.is_dir()           # created by the app itself
    assert list(store.primary.parent.iterdir()) == []  # the probe cleaned up


def test_unwritable_appdata_falls_back_to_the_data_dir(tmp_path, caplog):
    store = _store(tmp_path)
    _unusable(store.primary)
    caplog.set_level(logging.WARNING, logger="coa.credentials")
    assert store.select() == store.fallback
    warned = " ".join(r.getMessage() for r in caplog.records)
    assert str(store.fallback) in warned and str(store.primary) in warned
    assert store.save(USER, SECRET) is True
    assert store.fallback.is_file()
    assert store.load() == (USER, SECRET)


def test_fallback_login_survives_an_update(tmp_path):
    """A new process on the same DATA_DIR — what a release swap is — still
    finds a login the last one had to keep in the data dir."""
    first = _store(tmp_path)
    _unusable(first.primary)
    assert first.save(USER, SECRET)
    second = _store(tmp_path)                 # fresh process, same locations
    assert second.load() == (USER, SECRET)


def test_dpapi_failing_on_this_account_falls_back_to_the_data_dir(tmp_path, caplog):
    store = _store(tmp_path, BrokenDpapi())
    caplog.set_level(logging.WARNING, logger="coa.credentials")
    assert store.select() == store.fallback
    assert store.save(USER, SECRET) is True
    doc = json.loads(store.fallback.read_text(encoding="utf-8"))
    assert doc["password"]["scheme"] == "plain"
    assert store.load() == (USER, SECRET)
    _no_secret_logged(caplog)


def test_fallback_is_still_encrypted_when_dpapi_works(tmp_path):
    store = _store(tmp_path, FakeDpapi())
    _unusable(store.primary)
    assert store.save(USER, SECRET)
    doc = json.loads(store.fallback.read_text(encoding="utf-8"))
    assert doc["password"]["scheme"] == "dpapi"


def test_both_unusable_returns_false_without_raising(tmp_path, caplog):
    store = _store(tmp_path)
    _unusable(store.primary)
    _unusable(store.fallback)
    caplog.set_level(logging.WARNING, logger="coa.credentials")
    assert store.select() is None
    assert store.save(USER, SECRET) is False
    assert store.load() is None
    assert store.clear() is True        # nothing exists, so nothing is left
    _no_secret_logged(caplog)


def test_load_finds_a_login_in_the_secondary_location(tmp_path):
    store = _store(tmp_path)
    other = LoginStore(store.fallback, store.primary)   # saved when only DATA_DIR worked
    assert other.save(USER, SECRET)
    assert store.select() == store.primary
    assert store.load() == (USER, SECRET)


def test_a_save_that_fails_in_the_chosen_place_tries_the_other(tmp_path, monkeypatch):
    store = _store(tmp_path)
    assert store.select() == store.primary
    real = qbench_login._write_atomic

    def flaky(path, data):
        if path == store.primary:
            raise OSError("share went away")
        return real(path, data)
    with monkeypatch.context() as mp:
        mp.setattr(qbench_login, "_write_atomic", flaky)
        assert store.save(USER, SECRET) is True
        assert store.fallback.is_file()
    assert store.load() == (USER, SECRET)


def test_a_save_removes_the_stale_copy_in_the_other_place(tmp_path):
    store = _store(tmp_path)
    LoginStore(store.fallback, store.primary).save(USER, "old-pass")
    assert store.save(USER, SECRET)
    assert not store.fallback.exists()
    assert store.load() == (USER, SECRET)


def test_forget_clears_both_locations(tmp_path):
    store = _store(tmp_path)
    store.save(USER, SECRET)
    LoginStore(store.fallback, None).save(USER, "old-pass")   # a copy in each
    assert store.primary.exists() and store.fallback.exists()
    assert store.clear() is True
    assert not store.primary.exists() and not store.fallback.exists()


def test_the_primary_is_read_first(tmp_path):
    store = _store(tmp_path)
    LoginStore(store.fallback, store.primary).save("fallback@example.com", "b")
    # Write the primary directly so the fallback copy is not cleaned up.
    LoginStore(store.primary, None).save("primary@example.com", "a")
    assert store.load() == ("primary@example.com", "a")


# ── DPAPI binding (source-level: it only runs on Windows) ────────────────

def test_default_crypto_is_dpapi_only_on_windows():
    crypto = qbench_login.default_crypto()
    if os.name == "nt":
        assert crypto is not None and crypto.scheme == "dpapi"
    else:
        assert crypto is None


class FakeWinDLLs:
    """crypt32 + kernel32 stand-ins for ``DpapiCrypto(loader=...)``.

    Real ctypes function pointers (CFUNCTYPE callbacks) with real C memory
    (libc malloc/free), so the production binding's structures, byref
    marshalling, argtypes, string_at and LocalFree all run for real on any
    OS. Ciphertext is bound to ``user``; the entropy and flags DpapiCrypto
    passes are recorded so the test can check them.
    """

    def __init__(self, user: bytes = b"alice") -> None:
        import ctypes
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        self.ctypes, self.Blob, self.user = ctypes, Blob, user
        self.libc = ctypes.CDLL(None)
        self.libc.malloc.restype = ctypes.c_void_p
        self.libc.malloc.argtypes = [ctypes.c_size_t]
        self.libc.free.argtypes = [ctypes.c_void_p]
        self.allocated, self.freed, self.calls = set(), set(), []
        vp = ctypes.c_void_p
        crypt = ctypes.CFUNCTYPE(wintypes.BOOL, vp, vp, vp, vp, vp, wintypes.DWORD, vp)
        free = ctypes.CFUNCTYPE(vp, vp)
        # Keep the callback objects alive as long as this fake.
        self._protect = crypt(lambda *a: self._crypt("protect", *a))
        self._unprotect = crypt(lambda *a: self._crypt("unprotect", *a))
        self._free = free(self._local_free)

    def __call__(self, name: str):
        from types import SimpleNamespace
        if name == "crypt32":
            return SimpleNamespace(CryptProtectData=self._protect,
                                   CryptUnprotectData=self._unprotect)
        assert name == "kernel32", name
        return SimpleNamespace(LocalFree=self._free)

    def _bytes(self, addr) -> bytes:
        blob = self.Blob.from_address(addr)
        return self.ctypes.string_at(blob.pbData, blob.cbData)

    def _crypt(self, kind, data_in, descr, entropy, reserved, prompt, flags, data_out):
        data = self._bytes(data_in)
        self.calls.append((kind, self._bytes(entropy) if entropy else None, flags,
                           descr, prompt))
        tag = b"DPAPI:" + self.user + b":"
        if kind == "protect":
            out = tag + bytes(b ^ 0x5A for b in data)
        elif data.startswith(tag):
            out = bytes(b ^ 0x5A for b in data[len(tag):])
        else:
            return 0                       # FALSE: another user's blob
        ptr = self.libc.malloc(max(len(out), 1))
        self.ctypes.memmove(ptr, out, len(out))
        self.allocated.add(ptr)
        blob = self.Blob.from_address(data_out)
        blob.cbData = len(out)
        blob.pbData = self.ctypes.cast(ptr, self.ctypes.POINTER(self.ctypes.c_ubyte))
        return 1

    def _local_free(self, ptr):
        self.freed.add(ptr)
        self.libc.free(ptr)
        return None


def test_dpapi_binding_round_trips_through_real_ctypes():
    dlls = FakeWinDLLs()
    crypto = qbench_login.DpapiCrypto(loader=dlls)
    blob = crypto.protect(SECRET.encode())
    assert blob.startswith(b"DPAPI:alice:") and SECRET.encode() not in blob
    assert crypto.unprotect(blob) == SECRET.encode()
    for kind, entropy, flags, descr, prompt in dlls.calls:
        assert entropy == qbench_login.ENTROPY
        assert flags == qbench_login.CRYPTPROTECT_UI_FORBIDDEN == 0x1
        assert descr is None and prompt is None
    assert dlls.allocated and dlls.freed == dlls.allocated, "every output buffer LocalFree'd"


def test_dpapi_binding_reports_another_users_blob_as_crypto_error():
    blob = qbench_login.DpapiCrypto(loader=FakeWinDLLs(b"alice")).protect(b"x")
    bob = qbench_login.DpapiCrypto(loader=FakeWinDLLs(b"bob"))
    with pytest.raises(CryptoError):
        bob.unprotect(blob)


def test_the_store_runs_on_the_real_dpapi_binding(tmp_path, caplog):
    alice = qbench_login.DpapiCrypto(loader=FakeWinDLLs(b"alice"))
    store = _store(tmp_path, alice)
    assert store.select() == store.primary
    assert store.save(USER, SECRET)
    assert json.loads(store.primary.read_text())["password"]["scheme"] == "dpapi"
    assert store.load() == (USER, SECRET)
    caplog.set_level(logging.WARNING, logger="coa.credentials")
    bob = _store(tmp_path, qbench_login.DpapiCrypto(loader=FakeWinDLLs(b"bob")))
    assert bob.load() is None
    assert "different Windows account" in caplog.text


@pytest.mark.skipif(sys.platform != "win32", reason="real DPAPI needs Windows")
def test_real_dpapi_roundtrip(tmp_path):
    store = LoginStore(tmp_path / "a.json", tmp_path / "b.json")
    assert store.save(USER, SECRET)
    assert store.load() == (USER, SECRET)


class FaultyDpapi(FakeDpapi):
    """A crypto layer that fails the way a ctypes binding can: not CryptoError."""

    def unprotect(self, blob: bytes) -> bytes:
        raise RuntimeError("access violation")


def test_an_unexpected_crypto_fault_never_escapes(tmp_path):
    _store(tmp_path, FakeDpapi()).save(USER, SECRET)
    faulty = _store(tmp_path, FaultyDpapi())
    assert faulty.load() is None
    assert faulty.select() == faulty.fallback      # probe refuses the primary
    assert faulty.save(USER, SECRET) is True       # fallback keeps it, plain
    doc = json.loads(faulty.fallback.read_text(encoding="utf-8"))
    assert doc["password"]["scheme"] == "plain"
    assert faulty.load() == (USER, SECRET)


# ── concurrency ──────────────────────────────────────────────────────────

def test_concurrent_saves_all_succeed_without_bogus_fallbacks(tmp_path, caplog):
    import threading
    store = _store(tmp_path)
    caplog.set_level(logging.WARNING, logger="coa.credentials")
    results = []

    def worker(n):
        for i in range(60):
            results.append(store.save(f"user{n}@example.com", f"pass-{n}-{i}"))
            store.load()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(results) == 240 and all(results)
    assert [r.getMessage() for r in caplog.records] == []
    assert not store.fallback.exists()
    leftovers = [p.name for p in store.primary.parent.iterdir() if p != store.primary]
    assert leftovers == []


def test_a_briefly_locked_file_is_retried(tmp_path, monkeypatch):
    """Windows refuses os.replace while another handle has the file open."""
    store = _store(tmp_path)
    real = os.replace
    calls = {"n": 0}

    def locked_twice(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(13, "The process cannot access the file")
        return real(src, dst)
    with monkeypatch.context() as mp:
        mp.setattr(qbench_login.os, "replace", locked_twice)
        assert store.save(USER, SECRET) is True
    assert store.primary.is_file() and not store.fallback.exists(), (
        "a moment's lock is retried in place, not treated as an unusable folder"
    )
    assert store.load() == (USER, SECRET)


def test_a_file_that_stays_locked_gives_up_in_bounded_time(tmp_path, monkeypatch):
    import time
    store = _store(tmp_path)

    def always_locked(src, dst):
        raise PermissionError(13, "locked")
    monkeypatch.setattr(qbench_login.os, "replace", always_locked)
    started = time.monotonic()
    assert store.save(USER, SECRET) is False
    assert time.monotonic() - started < 2.0


def test_unencrypted_fallback_on_windows_warns_once(tmp_path, caplog):
    """No DPAPI for the app's account on Windows: the fallback copy is only
    base64. Say so once, loudly enough to be found, not on every save."""
    store = _store(tmp_path, None, require_encryption=True)
    caplog.set_level(logging.WARNING, logger="coa.credentials")
    assert store.save(USER, SECRET) and store.save(USER, SECRET + "2")
    warned = [r for r in caplog.records
              if "Keeping QBench login unencrypted" in r.getMessage()]
    assert len(warned) == 1
    assert json.loads(store.fallback.read_text())["password"]["scheme"] == "plain"


def test_the_username_is_only_logged_at_debug(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="coa.credentials")
    store = _store(tmp_path)
    store.save(USER, SECRET)
    store.load()
    store.save("", "")
    assert any(USER in r.getMessage() for r in caplog.records
               if r.levelno == logging.DEBUG)
    assert not any(USER in r.getMessage() for r in caplog.records
                   if r.levelno > logging.DEBUG)


def test_load_entry_reports_when_the_login_was_saved(tmp_path):
    import time
    store = _store(tmp_path)
    assert store.load_entry() is None
    before = time.time()
    store.save(USER, SECRET)
    entry = store.load_entry()
    assert (entry.username, entry.password) == (USER, SECRET)
    assert before - 1 <= entry.saved_at <= time.time() + 1


def test_load_entry_tolerates_a_missing_or_bad_saved_at(tmp_path):
    store = _store(tmp_path)
    store.save(USER, SECRET)
    doc = json.loads(store.path.read_text())
    doc["saved_at"] = "not a time"
    store.path.write_text(json.dumps(doc))
    entry = store.load_entry()
    assert entry.password == SECRET and entry.saved_at is None
