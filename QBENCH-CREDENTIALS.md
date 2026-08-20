# QBench credentials

QBench OAuth credentials are **never** stored in this repository. They are read
at runtime by `qbench_secrets.py` from a local store outside every checkout.

## Where the store lives

| Platform | Path |
|---|---|
| Windows | `%APPDATA%\ASAPLabs\qbench.json` |
| macOS / Linux | `~/.config/asaplabs/qbench.json` |

Override with `QBENCH_STORE_PATH`. Individual values can also be overridden by
the `QBENCH_CLIENT_ID` / `QBENCH_CLIENT_SECRET` environment variables (default
profile only).

## Shape

```json
{
  "client_id": "...",
  "client_secret": "...",
  "profiles": {
    "legacy": { "client_id": "...", "client_secret": "..." },
    "tools":  { "client_id": "...", "client_secret": "..." },
    "batch":  { "client_id": "...", "client_secret": "..." }
  }
}
```

ASAP Labs uses more than one QBench OAuth client, so the store carries named
profiles. Code asks for the one it needs — `get_client_secret("legacy")` — and
an unknown profile raises rather than silently falling back to the default,
because falling back would authenticate as the wrong client.

Create the file with `0600` permissions. Nothing in any repo writes it.

## If a credential is missing

`qbench_secrets.QBenchSecretMissing` is raised, naming the key, the profile and
the store path. It never returns `None` — a `None` secret would surface much
later as a confusing auth failure.

## Guard

`tests/test_no_hardcoded_credentials.py` fails if any `CLIENT_ID` /
`CLIENT_SECRET` is assigned a literal, including as an `os.getenv()` fallback.
That second shape is how four of these leaked past earlier review.
