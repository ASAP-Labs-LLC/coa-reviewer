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

## COA Reviewer's QBench web login

Separate from the OAuth client above: COA Reviewer also signs in to QBench's
**website** (Playwright, for COA previews) with a person's username and
password. When a reviewer ticks "Save credentials", that login is kept by
`qbench_login.py` — outside the release, so it survives every update.

**The app picks the location itself. Nobody needs to create, copy or fix
anything.** At startup and before every save it tries, in order:

| Where | When |
|---|---|
| `%APPDATA%\ASAPLabs\coa-qbench-login.json` (next to `qbench.json`) | the app can create the folder, write, read and delete there, **and** Windows DPAPI works for this account |
| `qbench_login.json` in the app's data folder (`C:\ASAPApps\coa\data` when deployed) | the first place can't be used |

Loading looks in both (chosen place first), so a login saved in either is found
after an update. A successful save removes any older copy in the other place.
`app.log` records which place was chosen and why (logger `coa.credentials`);
the browser never sees paths or storage errors.

- **Encryption** — on Windows the password is encrypted with DPAPI
  (`CryptProtectData`, current-user scope, fixed entropy). Only the **same
  Windows account on the same machine** can decrypt it. If DPAPI fails on the
  account, the fallback file is written base64-only (logged as a WARNING,
  once per run). On macOS/Linux (dev boxes) it is base64-only with file mode
  `0600`.
- **What "base64-only" means on Windows** — if DPAPI is unavailable for the
  account the app runs as, the copy in the data folder is **not encrypted**:
  base64 is an encoding, not protection. It is exactly as private as
  `C:\ASAPApps\coa\data` itself — anyone who can read that folder can read
  the password, as they could when it sat in `web_app_config.json`. Look for
  "Keeping QBench login unencrypted" in `app.log`.
- **Same-user caveat** — the app runs as whichever Windows account launched it
  (under the updater: the account its scheduled task runs as). A login saved by
  a different account can't be decrypted; the app logs "saved by a different
  Windows account?" and simply shows the manual login. Signing in again with
  "Save credentials" ticked replaces it.
- **If nothing can be saved** — the login still works for that session and the
  form says, in plain words, that it will be asked for again after a restart.
- **Moving off `web_app_config.json`** — older releases kept the password in
  plain text in `web_app_config.json`. On first start the app moves it into the
  store and blanks `qbench_password` there (the username stays, for display) —
  only once the store has actually kept it. If a config password and a stored
  login both exist, the app decides by evidence which is newer: the config
  wins only if the file was modified more than 2 s after the stored login's
  `saved_at` (an older release wrote it after a rollback) and is then moved
  over the stored login; otherwise the stored login wins and the old config
  password is ignored until the next successful save removes it.
- **Reset** — "Forget saved login" under the QBench login form (shown when a
  login is saved) clears every copy. By hand: stop the app and delete both
  files above (and blank `qbench_password` in `web_app_config.json` if it still
  has one). `COA_QBENCH_LOGIN_PATH` overrides the first location (the tests
  point it at a temp file).
- **Health checks never sign in** — the updater starts each staged release with
  `COA_HEALTH_CHECK=1`, and the app skips the automatic QBench login then.

## Guard

`tests/test_no_hardcoded_credentials.py` fails if any `CLIENT_ID` /
`CLIENT_SECRET` is assigned a literal, including as an `os.getenv()` fallback.
That second shape is how four of these leaked past earlier review.
