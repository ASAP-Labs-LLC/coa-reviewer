"""The QBench credentials live in a local store, never in the source tree."""
import os

import pytest

import qbench_secrets


def test_reads_client_secret_from_environment(monkeypatch):
    monkeypatch.setenv("QBENCH_CLIENT_SECRET", "env-secret")
    assert qbench_secrets.get_client_secret() == "env-secret"


def test_falls_back_to_local_store_file(monkeypatch, tmp_path):
    monkeypatch.delenv("QBENCH_CLIENT_SECRET", raising=False)
    store = tmp_path / "qbench.json"
    store.write_text('{"client_secret": "stored-secret"}')
    monkeypatch.setenv("QBENCH_STORE_PATH", str(store))
    assert qbench_secrets.get_client_secret() == "stored-secret"


def test_raises_with_actionable_message_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("QBENCH_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("QBENCH_STORE_PATH", str(tmp_path / "missing.json"))
    with pytest.raises(qbench_secrets.QBenchSecretMissing) as exc:
        qbench_secrets.get_client_secret()
    message = str(exc.value)
    assert "client_secret" in message
    assert "missing.json" in message


def test_default_store_path_on_windows_uses_appdata(monkeypatch):
    monkeypatch.delenv("QBENCH_STORE_PATH", raising=False)
    monkeypatch.setattr(qbench_secrets.os, "name", "nt")
    monkeypatch.setenv("APPDATA", "C:/Users/ryan/AppData/Roaming")
    assert qbench_secrets.default_store_path() == os.path.join(
        "C:/Users/ryan/AppData/Roaming", "ASAPLabs", "qbench.json")


def test_default_store_path_elsewhere_uses_config_home(monkeypatch):
    monkeypatch.delenv("QBENCH_STORE_PATH", raising=False)
    monkeypatch.setattr(qbench_secrets.os, "name", "posix")
    monkeypatch.setenv("HOME", "/Users/ryan")
    assert qbench_secrets.default_store_path() == os.path.join(
        "/Users/ryan", ".config", "asaplabs", "qbench.json")


def test_client_id_resolves_from_store_too(monkeypatch, tmp_path):
    monkeypatch.delenv("QBENCH_CLIENT_ID", raising=False)
    store = tmp_path / "qbench.json"
    store.write_text('{"client_id": "stored-id"}')
    monkeypatch.setenv("QBENCH_STORE_PATH", str(store))
    assert qbench_secrets.get_client_id() == "stored-id"


def test_no_hardcoded_credential_literals_in_source():
    """CLIENT_SECRET/CLIENT_ID must never be assigned a literal in the tree.

    The literal is deliberately not written here -- this asserts the shape of
    the assignment, so the test cannot itself reintroduce the secret.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    pattern = re.compile(
        r"""^\s*(CLIENT_SECRET|CLIENT_ID)\s*=\s*['"][^'"]{8,}['"]""", re.M)
    offenders = []
    for path in root.rglob("*.py"):
        if any(p in path.parts for p in (".git", ".venv", "venv", "tests")):
            continue
        if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"hardcoded credentials in: {offenders}"


def test_client_resolves_credentials_from_store_when_not_passed(monkeypatch, tmp_path):
    """Constructing with no credentials pulls them from the local store."""
    monkeypatch.delenv("QBENCH_CLIENT_ID", raising=False)
    monkeypatch.delenv("QBENCH_CLIENT_SECRET", raising=False)
    store = tmp_path / "qbench.json"
    store.write_text('{"client_id": "cid-from-store", "client_secret": "sec-from-store"}')
    monkeypatch.setenv("QBENCH_STORE_PATH", str(store))

    import qbench_client
    client = qbench_client.QBenchAPIClient()

    assert client.client_id == "cid-from-store"
    assert client.client_secret == "sec-from-store"


def test_importing_client_does_not_require_configured_store(monkeypatch, tmp_path):
    """Import must not explode on a machine that has not been set up yet."""
    monkeypatch.delenv("QBENCH_CLIENT_ID", raising=False)
    monkeypatch.delenv("QBENCH_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("QBENCH_STORE_PATH", str(tmp_path / "absent.json"))
    import importlib

    import qbench_client
    importlib.reload(qbench_client)  # must not raise


def test_named_profile_overrides_the_default_pair(monkeypatch, tmp_path):
    """Different apps use different QBench OAuth clients; keep them separate."""
    monkeypatch.delenv("QBENCH_CLIENT_ID", raising=False)
    monkeypatch.delenv("QBENCH_CLIENT_SECRET", raising=False)
    store = tmp_path / "qbench.json"
    store.write_text(
        '{"client_id": "default-id", "client_secret": "default-secret",'
        ' "profiles": {"tools": {"client_id": "tools-id",'
        ' "client_secret": "tools-secret"}}}')
    monkeypatch.setenv("QBENCH_STORE_PATH", str(store))

    assert qbench_secrets.get_client_secret() == "default-secret"
    assert qbench_secrets.get_client_secret(profile="tools") == "tools-secret"
    assert qbench_secrets.get_client_id(profile="tools") == "tools-id"


def test_unknown_profile_is_an_error_not_a_silent_default(monkeypatch, tmp_path):
    """Falling back to the default pair would authenticate as the wrong client."""
    monkeypatch.delenv("QBENCH_CLIENT_SECRET", raising=False)
    store = tmp_path / "qbench.json"
    store.write_text('{"client_id": "d", "client_secret": "s", "profiles": {}}')
    monkeypatch.setenv("QBENCH_STORE_PATH", str(store))
    with pytest.raises(qbench_secrets.QBenchSecretMissing) as exc:
        qbench_secrets.get_client_secret(profile="nope")
    assert "nope" in str(exc.value)
