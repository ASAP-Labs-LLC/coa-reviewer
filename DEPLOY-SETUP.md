# Instructions for Claude: self-updating deployment for LEM and COA Reviewer

You are running on the **ASAP Labs Windows server**. Your job is to replace the
current "edit code live on the shared drive" workflow with a versioned,
self-updating deployment that can roll back.

**These are production apps. The lab uses them during working hours.** Read the
Hard Rules before doing anything.

---

## 0. Hard rules

1. **Never delete or overwrite application state.** COA Reviewer's
   `re_review_state.json` and `archive/` hold real review work. If you are
   unsure whether a file is state or code, treat it as state and ask.
2. **Leave the existing installation on the shared drive untouched and
   working** until the new setup is proven. It is the fallback. Do not delete
   it at the end of this work either — ask the human first.
3. **Test-driven development.** Write the failing test, watch it fail, then
   write the code. This applies to every task below that changes behaviour.
4. **Verify before claiming done.** Run the command, read the output, paste it.
   Never report success you have not observed. If something is untested, say so.
5. **Do not do the cutover (Task 5) without the human present.** It stops live
   services.
6. **Ask before installing anything system-wide** (Python versions, services,
   firewall rules).

---

## 1. What exists today

Two Flask apps, both currently running **directly off the shared drive** over a
UNC path. Editing a file live causes a restart — which is fast, and dangerous.
That is what we are replacing.

### LEM Web Server
- Repo: `https://github.com/ASAP-Labs-LLC/lab-equipment-manager`
- Lives in the repo under `LEM Web Server/`
- Entry point: `web_server.pyw`, args `--host` (default `0.0.0.0`), `--port`
  (default `5557`), `--dev`, `--seed`, `--no-tray`, `--no-reload`
- Launched by `run.bat`, which bootstraps `.venv-win` (Windows needs its own
  venv — the `.venv` on the share is a macOS venv and will not work)
- `tray.py` is the launcher: a pystray icon (Open in Browser / Restart / Quit)
  **and** a file watcher that auto-restarts on `.py/.pyw/.html/.css/.js`
  changes. It already owns port handover (`port_is_free`,
  `wait_for_port_free`, `who_holds_port`).
- Config lives in **LabCore** (`lem_*` tables), not on disk. Local `data/` is
  regenerable cache — `tray.py` deliberately does not watch it.
- Talks to LabCore at `https://labvision.asaplabs.net` (`LABCORE_URL` overrides)

### COA Reviewer
- Repo: `https://github.com/ASAP-Labs-LLC/coa-reviewer`
- Entry point: `app.py` (Flask), launched via `Run.pyw`
- `supervisor.py` holds process/port primitives written after a real incident:
  `taskkill /F /T` timed out mid-tree, `proc.wait(5)` confirmed the process was
  still alive, and Windows' `SO_REUSEADDR` let a second server bind the still-
  occupied port and serve zero connections forever. It provides
  `port_has_listener`, `wait_until_free`, `wait_until_serving`,
  `stop_until_dead`, and `perform_restart`. **Use these. Do not write your own
  process-killing or port-probing logic — `stop_until_dead` and
  `perform_restart` already encode the failure modes this box actually hit.**
- `install.bat` installs deps: flask, PyJWT, requests, playwright (+ Chromium),
  pymupdf, pyzbar, pystray, and the VC++ 2013 runtime pyzbar needs.
- ⚠ **COA has no `requirements.txt`** — its dependency list exists only inside
  `install.bat`. See Task 2b; this must be fixed before the updater can build a
  venv for a COA release.

### Credentials (important)
Both apps resolve QBench credentials at runtime via `qbench_secrets.py` from a
local store — **they are no longer in the source**. On Windows the store is:

```
%APPDATA%\ASAPLabs\qbench.json
```

Shape, with four named OAuth client pairs:

```json
{
  "client_id": "...", "client_secret": "...",
  "profiles": {
    "legacy": {"client_id": "...", "client_secret": "..."},
    "tools":  {"client_id": "...", "client_secret": "..."},
    "batch":  {"client_id": "...", "client_secret": "..."}
  }
}
```

**If this file is missing, the apps will not start** — they raise
`QBenchSecretMissing` naming the key and path. See `QBENCH-CREDENTIALS.md` in
either repo. Ask the human for the values; do not invent them and do not commit
them anywhere.

---

## 2. Target layout

```
C:\ASAPApps\
  updater\
    updater.py
    .venv\
    config.json                 which apps, which repos, poll interval
    updater.log
  lem\
    releases\2026.08.21-1\      unpacked release — immutable
    releases\2026.08.20-3\      previous, retained
    current                     JUNCTION -> one of the releases
    data\                       state + venv — deploys NEVER touch this
  coa\
    releases\ ...
    current                     JUNCTION
    data\
```

A deploy never edits a release in place. It writes a new folder and re-points
the junction:

```bat
rmdir C:\ASAPApps\lem\current
mklink /J C:\ASAPApps\lem\current C:\ASAPApps\lem\releases\2026.08.21-1
```

Rollback is the same command aimed at the older folder. Because rollback and
deploy share one mechanism, rollback is exercised on every deploy.

---

## 3. Ask the human before you start

Do not guess these.

1. **A GitHub fine-grained PAT** — read-only, `contents: read`, scoped to just
   `lab-equipment-manager` and `coa-reviewer`. Store it in Windows Credential
   Manager (`cmdkey` / `keyring`), **not** in `config.json` and never in a repo.
2. **Where both apps currently live on this box** (full paths, including the
   UNC path to the share).
3. **Where COA Reviewer's live state currently is** — `web_app_config.json`,
   `re_review_state.json`, `archive/`, `login.log`, `.secret_key`. The migration
   must move the *real* files, not create empty ones.
4. **The QBench credential values** for `%APPDATA%\ASAPLabs\qbench.json`, if
   that file does not already exist on this machine.
5. **A maintenance window** for Task 5. Roughly 30 minutes.

---

## 4. Tasks, in order

Each task has an acceptance check. Do not move on until it passes.

### Task 0 — Recon and backup

- Record what is running: `netstat -ano | findstr :5557`, the COA port, and the
  PIDs and command lines behind them.
- Copy the current COA state files somewhere safe outside both trees. Note the
  paths in your final report.
- Confirm `git`, `python`, and the GitHub PAT all work from this box:
  `git --version`, `python --version`,
  `curl -s -H "Authorization: Bearer <PAT>" https://api.github.com/repos/ASAP-Labs-LLC/lab-equipment-manager/releases/latest`

**Acceptance:** you can state the current version, port, and PID of each app,
and the API call returns JSON (a 404 with a valid token just means no release
exists yet — that is fine).

### Task 1 — COA Reviewer: move state out of the code directory ⚠ BLOCKING

This is the reason COA cannot be deployed today. In `app.py`:

```python
APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE          = APP_DIR / "web_app_config.json"
RE_REVIEW_STATE_FILE = APP_DIR / "re_review_state.json"
ARCHIVE_DIR          = APP_DIR / "archive"
LOGIN_LOG_FILE       = APP_DIR / "login.log"
```

State lives *inside* the code directory, so swapping the directory destroys it.

Note `ARCHIVE_DIR.mkdir(exist_ok=True)` runs at **import time** (line 80), as do
the other module-level path constants — so `DATA_DIR` must be resolvable at
import, not lazily. `app.py` also builds an `AppState` (and a QBench client) at
module scope, which is why importing it has side effects; the existing
`tests/conftest.py` documents this.

Introduce `DATA_DIR`, resolved as: `COA_DATA_DIR` env var → else `APP_DIR`
(so nothing breaks for anyone still running the old way). Point all five state
paths at `DATA_DIR`. Ship `web_app_config.default.json` in the repo and copy it
into `DATA_DIR` on first boot **only if absent** — never overwrite a real config.

TDD. Tests to write first, each watched failing:
- `DATA_DIR` follows `COA_DATA_DIR` when set
- `DATA_DIR` falls back to `APP_DIR` when unset
- first boot with an empty data dir creates config from the default template
- first boot with an existing config **does not** overwrite it
- `archive/` is created under `DATA_DIR`, not the code dir

**Acceptance:** the full COA suite passes, and with `COA_DATA_DIR` set to a temp
dir the app boots and writes its state there, leaving the code dir clean.

### Task 2 — `/healthz` and a version stamp in both apps

Each app gains `GET /healthz` returning `200` and:

```json
{"status": "ok", "version": "2026.08.21-1", "labcore": "reachable|unreachable"}
```

Version is read from a `VERSION` file written into the release at build time;
fall back to `"dev"` when absent. `/healthz` must not require auth (the updater
calls it before the app is live) and must not be slow — no LabCore round-trip
on the critical path; report last-known reachability.

TDD both apps.

**Acceptance:** `curl http://127.0.0.1:5557/healthz` returns 200 with a version.

### Task 2b — Give COA Reviewer a `requirements.txt`

The updater builds each release's venv from `requirements.txt`. LEM has one;
**COA does not** — its dependencies are hardcoded in `install.bat` (flask,
PyJWT, requests, playwright, pymupdf, pyzbar, pystray). Extract them into a
`requirements.txt`, pinning to the versions currently installed on this box
(`pip freeze` in the live COA environment) rather than to floating latest — an
unpinned deploy can pull a breaking dependency and fail health check for
reasons unrelated to the code change.

Playwright's Chromium download is a separate step from `pip install`; the
updater must run `playwright install chromium` when building a COA venv, or
reuse an existing browser cache. Decide which and write it down.

**Acceptance:** a fresh venv built from `requirements.txt` alone can boot COA
and pass `/healthz`.

### Task 3 — Release workflow in both repos

`.github/workflows/release.yml`, triggered on tag push `v*` or a date tag:

- Write the tag into a `VERSION` file
- Zip the app directory — **source only**, no `.venv`, no `__pycache__`, no
  state files
- Attach the zip to a GitHub Release
- Publish a SHA256 checksum alongside it

**Acceptance:** pushing a tag produces a downloadable release asset, and its
checksum verifies.

### Task 4 — The updater service

`C:\ASAPApps\updater\updater.py`, its own venv, `requests` only.

Loop, every `poll_seconds` (default 300), per configured app:

1. `GET https://api.github.com/repos/ASAP-Labs-LLC/<repo>/releases/latest`
   with the PAT from Credential Manager
2. Compare `tag_name` with `current\VERSION`. Same → sleep.
3. Different → download the asset, **verify the checksum**, unpack to
   `releases\<tag>\`
4. Build that release's venv from its `requirements.txt`
5. **Health check before offering it:** start it on a scratch port (e.g.
   `--port 15557`) with `COA_DATA_DIR` / LEM data pointed at a *temp* dir, poll
   `/healthz` for up to 60s, require 200 and a matching version, then stop it
6. Write `data\staged.json`: `{"tag", "staged_at", "healthy", "notes"}`
7. **Stop. Do not switch.** Wait for a human click.

On switch request:
- Stop the app using COA's `supervisor.stop_until_dead` / `perform_restart` —
  they confirm the process actually died rather than trusting `taskkill`'s exit
  code, and probe the port by connecting rather than binding
- Re-point the junction
- Start the app
- Poll `/healthz` up to 60s
- **If unhealthy: re-point the junction to the previous release, restart,
  and record the failure in `staged.json`.** Automatic rollback is required,
  not optional.

Also: retain the last 5 releases and prune older ones; never prune `current` or
the release it rolled back from; log every action to `updater.log` with
timestamps; survive a reboot (Task 5 registers it).

TDD the logic that can be tested off-Windows — version comparison, checksum
verification, retention/pruning, the decision table for
staged/healthy/switch/rollback. The junction and process work must be verified
live on this box.

**Acceptance:** with a fake release served locally, the updater stages it,
health-checks it, and waits. On switch it swaps and verifies. Given a
deliberately broken release, it rolls back on its own.

### Task 5 — Install and migrate ⚠ WITH THE HUMAN PRESENT

1. Create `C:\ASAPApps\` and the per-app subtrees
2. Clone each repo, check out the current release tag, and place it as the
   first entry in `releases\`
3. **Move** (do not copy-and-hope) the live COA state into `coa\data\`; set
   `COA_DATA_DIR` for the service
4. Create `%APPDATA%\ASAPLabs\qbench.json` if absent (Task 3 of section 3)
5. Point `current` at the first release; start both apps; confirm they serve on
   their normal ports and that COA sees its **real** history and archive
6. Register the updater as a Scheduled Task at boot, running as a user that can
   write `C:\ASAPApps` and read Credential Manager
7. Leave the shared-drive installation in place, stopped, as the fallback

**Acceptance:** both apps serve from `C:\ASAPApps\*\current`, COA shows its real
prior review state, and the updater is running and logging.

### Task 6 — Prove it end to end

With the human:
1. Tag a trivial release (a comment change). Confirm it appears as staged within
   one poll interval, health-checked, not switched.
2. Click Update. Confirm the swap, and that the app comes back on the same port
   with the new version at `/healthz`.
3. **Roll back to the previous release and confirm the app returns.**
4. Deliberately publish a broken release. Confirm it is caught at the staging
   health check and never offered.
5. Confirm COA's state survived every step — same review history, same archive.

**Acceptance:** all five, observed, with output pasted into your report.

---

## 5. What "done" looks like

- Both apps run from `C:\ASAPApps\<app>\current`
- New releases appear as *staged* within ~5 minutes, health-checked, never
  auto-switched
- A human click deploys; a click rolls back
- A bad release either never gets offered, or rolls itself back
- **No application state has been lost at any point**
- The old shared-drive setup still exists as a fallback

## 6. Report back

State plainly what you did, what you verified with observed output, and what you
did **not** test. If you ran out of time or hit something Windows-specific that
did not behave as written here, say so explicitly rather than leaving it
implied. A partially-working deployment that is honestly described is far more
useful than one reported as finished.

---

## Appendix: things that will bite you

- **`.venv` from the share is a macOS venv.** Windows needs its own
  (`run.bat` already handles this as `.venv-win`). Never reuse the other.
- **Junctions, not symlinks.** `mklink /J` needs no admin rights; `/D` does.
- **A junction cannot be re-pointed while files under it are open.** Stop the
  app first, and confirm it actually stopped.
- **`SO_REUSEADDR` on Windows** lets a second process bind a port that is
  already being served, producing a silent zombie. Probe by *connecting*, never
  by binding — `supervisor.port_has_listener` does this correctly.
- **LEM's `tray.py` auto-restarts on file change.** Unpacking a release into a
  directory it is watching would trigger restarts mid-write. Stage into
  `releases\`, which is outside the watched tree — and check `SKIP_DIRS` before
  assuming.
- **COA's install pulls Playwright Chromium** — hundreds of MB per venv. Five
  retained releases is multiple GB. If disk is tight, keep the venv beside
  `data\` and rebuild only when `requirements.txt` changes; note the trade-off
  in your report.
- **`cmd.exe` cannot `cd` to a UNC path** and will silently fall back to
  `C:\Windows`. `run.bat` uses `pushd` for this reason.
