"""QBench credentials, resolved from a local store instead of the source tree.

Resolution order for each value:

1. ``QBENCH_CLIENT_ID`` / ``QBENCH_CLIENT_SECRET`` in the environment
   (default profile only -- a named profile always comes from the store, since
   one process may need two different QBench clients at once).
2. The local JSON store, whose location is ``QBENCH_STORE_PATH`` or
   :func:`default_store_path`.
3. Nothing -- raise :class:`QBenchSecretMissing`, never return ``None``.

Store shape::

    {
      "client_id": "...",           # the default pair
      "client_secret": "...",
      "profiles": {                 # optional, for apps on a different client
        "tools": {"client_id": "...", "client_secret": "..."}
      }
    }

The store is deliberately outside every repository. Nothing here writes it.
"""
import json
import os

__all__ = [
    "QBenchSecretMissing",
    "default_store_path",
    "get_client_id",
    "get_client_secret",
]


class QBenchSecretMissing(RuntimeError):
    """Raised when a credential is in neither the environment nor the store."""


def default_store_path():
    """Where the credential store lives when nothing overrides it."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "ASAPLabs", "qbench.json")
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".config", "asaplabs", "qbench.json")


def _store_path():
    return os.environ.get("QBENCH_STORE_PATH") or default_store_path()


def _load_store():
    path = _store_path()
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _resolve(key, env_var, profile=None):
    store = _load_store()
    if profile is None:
        value = os.environ.get(env_var) or store.get(key)
    else:
        profiles = store.get("profiles") or {}
        if profile not in profiles:
            raise QBenchSecretMissing(
                f"QBench profile {profile!r} is not defined in the local store "
                f"at {_store_path()}. Add it under \"profiles\" rather than "
                f"falling back to the default client."
            )
        value = profiles[profile].get(key)
    if not value:
        where = f"profile {profile!r}" if profile else "the default pair"
        raise QBenchSecretMissing(
            f"QBench {key} is not configured for {where}. Set the {env_var} "
            f"environment variable, or add {key!r} to the local store at "
            f"{_store_path()}."
        )
    return value


def get_client_secret(profile=None):
    """The QBench OAuth client secret. Never returns ``None``."""
    return _resolve("client_secret", "QBENCH_CLIENT_SECRET", profile)


def get_client_id(profile=None):
    """The QBench OAuth client id. Never returns ``None``."""
    return _resolve("client_id", "QBENCH_CLIENT_ID", profile)
