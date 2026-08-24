# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

Flask web app that lets ASAP Labs reviewers QC QBench Certificates of Analysis (COAs) in a browser. The server logs into QBench (REST API + Playwright web session), fetches samples whose `lab_id` starts with `MMDDYY` for "2 business days ago" (Yesterday tab) and "3 business days ago" (Due Out tab), renders the COA preview PDFs inline, optionally fetches the matching SIF (Sample Information Form) page next to it, and lets the reviewer flag issues as **Command Center listings** in LabLink's LabCore. Open `double_check` listings appear in a Re-review tab and can be completed from here.

**Google Sheets is gone** (removed 2026-07-31). The "Double Check" Master Logbook sheet, `google_sheet.py`, `GoogleSheetsManager`, `DoubleCheckRow`, `/api/check-sheet`, and the `google-auth*` dependencies were all removed when Command Center replaced them. `credentials.json` is no longer read. Don't reintroduce any of it — `tests/test_sheets_removed.py` fails loudly if you do.

The server is multi-user: one Flask process serves the LAN, each browser session gets its own `UserState` (records, results, PDF cache, SSE queue), while shared resources (QBench API client, Playwright session, LabCore client, upload queue) live in a single `AppState`.

## Releasing

**See `RELEASING.md`.** Short version: push a `v*` tag and CI builds a release;
the updater on ASAPSV1 stages it, health-checks it, and **deploys it by itself
once no reviewer is using the app**. Nothing else is needed and no credential is
involved — the repo is public.

Two things to know before tagging: the updater tracks whatever GitHub calls
*latest* (equality, not ordering — publishing an older tag deploys it, which is
how a rollback-by-release works), and an idle-gated health check proves the app
starts, never that it renders a COA correctly. `updater.py rollback --app coa`
is the counterweight.

## Run / develop

Windows — one-time setup, then launch separately:

```
install.bat                    # installs deps + Playwright Chromium + pytest into system Python
python app.py                  # or double-click Run.pyw for the system-tray launcher
```

macOS / Linux — one-shot bootstrap (creates `.venv`, installs deps into it, launches the app):

```
./install.sh                   # honors $PYTHON, defaults to python3; safe to re-run
```

Re-running `install.sh` reuses the existing `.venv` and is essentially the daily launcher. To run the app without re-running pip, do `.venv/bin/python app.py` directly.

Dependencies live in **three** files that must stay in lock-step, and
`tests/test_install_scripts.py` fails loudly when they drift:

- `install.bat` / `install.sh` — the "set up my machine" path, floating `>=`
  floors, because a person setting up a laptop wants current packages.
- `requirements.txt` — **runtime only, exactly pinned (`==`)**. This is what the
  updater builds each release venv from. A deploy must install exactly what was
  tested; an unpinned deploy can pull a new transitive release between staging
  and switch, fail its health check, and trigger an automatic rollback that
  blames the wrong commit.
- `requirements-dev.txt` — `-r requirements.txt` plus `pytest`. A release venv
  deliberately does not carry the test runner.

If you add or upgrade a package, update all three plus the `REQUIRED_PACKAGES`
list in `tests/test_install_scripts.py`.

**Playwright's Chromium is not per-venv.** It caches at
`%LOCALAPPDATA%\ms-playwright` (655 MB as of 2026-08-21), outside any virtual
environment, so retaining five release venvs does *not* mean five Chromiums —
only the pip packages are duplicated. `playwright install chromium` still runs
on every release venv build: it is a ~15s no-op when the matching build is
already cached, and it is what downloads a new browser when the `playwright`
pin is bumped. Set `PLAYWRIGHT_BROWSERS_PATH` to one machine-wide path for both
the app and the updater — otherwise a scheduled task running as a different
account gets its own 655 MB copy and the first post-deploy COA render pays for
the download.

`Run.pyw` is the production launcher — a pystray system-tray icon that spawns `app.py`, captures stdout/stderr to `server.log` (rotated at 1 MB), watches every `*.py` in the directory and auto-restarts the server when any change. Tray menu: Open Browser, Restart Server, Show/Hide Log, Quit.

Auxiliary:
```
python sif_test_app.py        # standalone Flask on port 5050 — debugging SIF discovery for one lab_id
```

Testing:
```
pytest                          # full suite (54 tests; pytest is installed by install.bat / install.sh)
pytest tests/test_helpers.py    # one file
pytest -k business_days         # by name
```

`pytest.ini` sets `testpaths = tests` and `addopts = -q`, so bare `pytest` is the canonical invocation. On macOS, pytest lives in the venv only — use `.venv/bin/pytest` if it's not on PATH.

The project uses **pytest** and follows TDD: write a failing test first, then implement until it passes.

- `tests/test_consolidation.py` — source-level invariants that lock in the post-consolidation layout (no `COA_DIR`, no `Past Data Manager`, no `APP_DIR.parent` traversals, no `Path(__file__).parent.parent` in `labcore_client.py`). These guard against silently reintroducing cross-folder dependencies.
- `tests/test_labcore_client.py` — the Command Center client, run against a real stub HTTP server on a real socket. Deliberately not mocked: what matters is the exact wire shape LabCore expects (the `/api/queue/write` envelope, the conflict response, required `completion_notes`), and a mock would happily accept a wrong one.
- `tests/test_cc_routes.py` — the Flask CC proxy routes with the client mocked (route behavior: auth, defaults, stamping, failure codes).
- `tests/test_cc_integration.py` — routes → **real** `LabCoreClient` → stub LabCore. Catches mismatches between the two layers that neither of the above can.
- `tests/test_re_review_cc.py` — `cc_tasks_to_re_review_entries` selection rules, without the network.
- `tests/test_mark_flow.py` — marking, un-marking, `session_results` keying, and `/api/regenerate-pending`.
- `tests/test_cc_frontend.py` — source-level frontend guards. The enum tests matter most: type/status/department options must match LabCore's `CC_TASK_TYPES`, `CC_TASK_STATUSES`, and `CC_DEPTS`, because a value outside those sets is **silently coerced to a default** by LabCore's writer.
- `tests/test_sheets_removed.py` — fails if any part of the Google Sheets path is reintroduced.
- `tests/test_supervisor.py` + `tests/test_port_guard.py` — launcher supervision and the `app.py` port guard (see `supervisor.py` below).
- `tests/test_helpers.py` — pure-function tests for `business_days_ago`, `load_config`/`save_config`, and `load_re_review_state`/`save_re_review_state`. Uses the `isolated_app_paths` fixture from `conftest.py` to redirect `app.CONFIG_FILE` and `app.RE_REVIEW_STATE_FILE` into `tmp_path` so real config/state is never touched.
- `tests/test_install_scripts.py` — keeps `install.bat` and `install.sh` in lock-step. Parametrized over every required package; both scripts must list every dep and run `playwright install chromium`. Fails noisily when one script forgets a dep the other has.
- `tests/conftest.py` — adds project root to `sys.path` and exposes `isolated_app_paths`.

What is mocked / never touched in tests:
- No tests exercise QBench API calls or Playwright login. If you add a feature that touches them, monkeypatch `state.api_client` / `state.coa_session` rather than letting real requests escape.
- LabCore is never contacted for real: either `state.labcore` is a `MagicMock` (route tests) or a real client pointed at a local stub server (client + integration tests).
- The real `web_app_config.json` and `re_review_state.json` are never read or written by the helper tests (use `isolated_app_paths`).
- `tests/conftest.py` also exposes `LocalServer` (a TCP server that binds, listens, **and accepts**) and `free_port()` for port-probe tests. Accepting is not incidental: a socket that only listens fills its backlog after a handful of probes, after which connects fail and the port looks free.

## Architecture

- **`app.py`** (~2.2k lines) — the whole Flask app. Notable internals:
  - `COASession` — Playwright login to `https://asaplabs.qbench.net/lims`, scrapes the CSRF token and cookies, then POSTs `/report/preview` and polls `/report/preview/get` until `render_status == "SUCCESSFUL"` to get a renderable preview URL. Thread-safe re-login.
  - `AppState` (singleton, `state`) — shared QBench `QBenchAPIClient`, `COASession`, `GoogleSheetsManager`, and `UploadQueue`. Built at import time; auto-logs into QBench in a background thread if `web_app_config.json` has saved credentials.
  - `UserState` — per-browser-session: `records: Dict[(tab, lab_id), SampleRecord]`, `session_results`, in-memory `pdf_cache`, `_sse_queues` for live updates. Keyed off Flask session cookie `uid`. 10-min idle timeout (frontend) / 12-min cleanup (server, grace period for re-auth).
  - `SampleRecord` — one row in a tab; tracks status, preview URL, tests, attachments, SIF state.
  - `UploadQueue` — serializes Google Sheets writes so concurrent reviewers don't trample each other.
  - **Why a sample has no SIF** — `classify_missing_sif(order)` returns `online_entry` or `missing`, never a guess. The signal is QBench's **`order_request_status` on the order**, which is set only when the order arrived as a customer-portal request. Measured against live QBench (2026-07-31, 45 orders): every order lacking a SIF was a portal request (6/6) and every order with a null status had one (36/36) — but 3 portal orders *did* carry a SIF, so a missing document was never proof by itself. An order that can't be read reports `missing`, because claiming "entered online" without evidence is the guess this replaced. The order is fetched **only when a SIF is absent** (most have one) and memoised per order in `state.sif_absence_cache`, which `_reset_for_regenerate` clears so a later-attached SIF is found.
  - SIF helpers (`_sif_find_candidates`, `_sif_download`, `_sif_find_page`, `_sif_extract_page`) — pick a non-COA PDF from order-level attachments, find the page containing `lab_id` via pyzbar barcode scan (falls back to text search), and extract that single page.
  - Background workers: `_session_cleanup_worker` (idle session GC), `_auto_restart_worker` (graceful self-restart at 3 AM if idle ≥ 5 min so Run.pyw respawns a fresh process).
  - Routes are grouped by feature: `/api/portal-*` (portal auth), `/api/login` + `/api/start` (QBench login + begin pulling), `/api/tabs/<tab_name>`, `/api/pdf/<lab_id>`, `/api/sif/<lab_id>`, `/api/tests/<lab_id>` (GET) + `/api/tests/<test_id>` (PATCH inline edits), `/api/attachments/<lab_id>`, `/api/comments/<lab_id>`, `/api/mark` (`good` / `bad` / `uncheck`), `/api/regenerate`, `/api/regenerate-pending`, `/api/search`, `/api/custom-day`, `/api/export`, `/api/good-links`, `/api/events` (SSE), `/api/heartbeat`, `/api/restart`.
  - **`/healthz`** is the deployment contract, distinct from `/api/health` (which serves the frontend's restart poll). It takes **no auth** — the updater calls it on a scratch port before the release is live — and makes **no outbound call**: `labcore` reports the last-known reachability recorded by real traffic, because probing LabCore here would let a blip on their side roll back a release that was never broken. Do not add `@require_portal` to it; `tests/test_healthz.py` asserts its absence.
  - **`/healthz` also reports `active_sessions` and `idle_seconds`, and releases deploy themselves when both say nobody is here.** Two rules in `track_activity` keep that honest, and both are easy to break by accident:
    1. **`/healthz` and `/api/health` never count as activity.** Otherwise asking "is anyone using this?" answers itself — and because the 3 AM `_auto_restart_worker` reads the same `_last_request_time`, a monitoring poll would also silently disable the QBench/Playwright token refresh.
    2. **Activity requires a session.** Something on this network `GET`s `/` every ~2.2 minutes and always has (it is all over the old `server.log`); counting it kept the app permanently "busy". Excluding `/` by path is wrong, because `/` is exactly what a reviewer opens — the session cookie is what separates a monitor from a person with records and a PDF cache in memory.
  - **Command Center proxy**: `/api/cc/config`, `/api/cc/check/<lab_id>`, `/api/cc/lookup/<lab_id>`, `POST /api/cc/tasks`, `POST /api/cc/tasks/<id>/complete`, `/api/cc/customers`. These exist because **LabCore sends no CORS headers and has no OPTIONS handler**, so the browser cannot call it directly — never have the frontend address LabCore itself.
  - **Sync + auth**: `/api/portal-card-login` (LabLink keycard), `/api/sync-preview/<lab_id>`, `POST /api/sync-sample-info/<lab_id>`, `/api/regenerate-selected`.
  - `check` vs `lookup` is a latency split, not a style choice. Marking Good and un-marking must ask before closing a sample out, and that runs on *every* sample a reviewer clears; `check` does the one call that answers it (~150ms against live LabCore) instead of `lookup`'s two (~272ms). `lookup` additionally fetches customer/fuel autofill for the flag form, and runs its two calls concurrently. Don't point the marking path back at `lookup`.
  - `_lab_vision_base_url()` returns LabCore's own base URL — LabVision is served by LabCore, at one public hostname that resolves the same from the server and from every browser. It briefly derived this from `request.host`, which was only correct while LabCore ran on this machine; don't reintroduce that.
  - SSE (`/api/events`) is the only push channel — frontend listens for `sample_status`, `status`, `auto_login_done`, etc.

- **`qbench_client.py`** — `QBenchAPIClient` for QBench REST API v2 at `asaplabs.qbench.net`. OAuth2 JWT Bearer. Credentials are **not in this file**: they resolve at construction time via `qbench_secrets.py` from `%APPDATA%\ASAPLabs\qbench.json` (or `QBENCH_CLIENT_ID` / `QBENCH_CLIENT_SECRET`). Resolution is lazy, so importing the module never requires a configured machine — but **the app will not start without the store**, raising `QBenchSecretMissing` naming the key and path. See `QBENCH-CREDENTIALS.md`. Thread-safe token cache with clock-skew recovery, a module-level `GLOBAL_RATE_LIMITER` (270 calls/min — below QBench's real 300/60s ceiling; override with `QBENCH_MAX_CALLS_PER_MIN` env), with 429 backoff that honors the server's `Retry-After`/"Retry in N seconds" hint, 401 token refresh and 429 backoff. Helpers used by `app.py`: `fetch_samples_by_lab_id_prefix`, `fetch_tests_for_sample_ids`, `fetch_attachments_for_sample`, `fetch_order_attachments`, `fetch_order_comments`, `update_test`, `delete_attachment`.

- **`labcore_client.py`** — `LabCoreClient` for LabLink's Command Center. Command Center lives in **LabCore** (`apps/LabCore/src/LabCore.py` in the LabLink repo), *not* LabStation; its board is a tab in the LabVision web UI that LabCore serves. Writes go through `POST /api/queue/write` — LabCore's explicitly unauthenticated "any LabLink program can POST here" gateway, dispatching `cc_create_task` / `cc_complete_task` onto its serialized write queue. (`POST /api/cc/tasks` is the alternative but needs a LabVision bearer token this headless server cannot obtain.) Reads are public GETs. Every write carries an `op_id` so a retry can't create a duplicate listing. Raises `LabCoreUnavailable` rather than returning falsy — a flag that silently fails to file is worse than one that visibly refuses.

  Things that will bite you: LabCore's `lab_id` is the same `MMDDYY-NNNNN` form COA Reviewer uses (no translation needed); `cc_create_task` dedups per lab_id and returns `{"conflict": true, "existing_tasks": [...]}` unless `force_create` is set; `cc_complete_task` **requires** non-empty `completion_notes`; and `sample_info()` filters to an exact lab_id match because the underlying search is a `LIKE` (querying `41552` also matches `415520`).

- **`change_log.py`** — `ChangeLog`, the audit trail of every reviewer change, written to the network share. One file per category (`reviews`, `command_center`, `qbench_edits`, `sessions`), month-partitioned (`reviews-2026-07.jsonl`), JSON Lines. Categories are a closed set on purpose — a typo'd category would silently create an orphan file nobody reads. **Logging must never break reviewing**: every method swallows its own I/O and serialisation errors, so a dropped share mount costs a log line, not a reviewer's mark. `category`/`event` are positional-only so a caller's stray `category=` field can't forge a record. Location: `change_log_dir` in config, defaulting to `APP_DIR/changelog` (APP_DIR is already on the share, so no traversal is needed — `tests/test_consolidation.py` forbids one).

- **`supervisor.py`** — process/port supervision primitives used by `Run.pyw` (no GUI imports, so it is importable and unit-tested). `port_has_listener` / `wait_until_free` / `wait_until_serving` probe by **connecting**, never by binding — a bind probe with `SO_REUSEADDR` reports an occupied port as free on Windows, which is how the 2026-07-31 inert-duplicate incident happened. `stop_until_dead` kills and *verifies* death with escalating attempts; `perform_restart` encodes the restart ordering (confirm death → sweep strays → refuse to spawn while the port is held → verify the new child serves). Change launcher restart behavior here, not in `Run.pyw`, so it stays covered by `tests/test_supervisor.py`.

- **`sif_test_app.py`** — separate Flask app (port 5050) for iterating on SIF discovery/page-matching in isolation. Inlines its HTML via `render_template_string`. Not used by the main app at runtime, but imports `qbench_client.QBenchAPIClient`.

- **`templates/index.html`** + **`static/js/app.js`** + **`static/css/app.css`** — single-page UI. JS owns tab state, PDF iframe rendering, SSE listener, portal session timer, and modals (mark sample, export, custom day, search).

## External dependencies

- **QBench API v2** at `https://asaplabs.qbench.net` — OAuth2 JWT-bearer. The client_id/client_secret come from the local store (`qbench_secrets.py`), never from the source tree.
- **QBench web session** — Playwright Chromium for COA preview rendering (no public API for `/report/preview`).
- **LabCore / Command Center** at `https://labvision.asaplabs.net` — **remote, behind Cloudflare on HTTPS/443** (not a LAN host:port). Serves both the Command Center API and the LabVision UI, so the same URL is used server-side and as the browser's "Open in Lab Vision" target. Verified 2026-07-31: no CORS headers and no `OPTIONS`/`HEAD` handler (a HEAD returns 501), which is why every call is proxied through Flask. Because it is a real network hop rather than loopback, the client's timeouts are sized for internet latency — don't drop them back to loopback values. Source of truth: the LabLink repo (`https://github.com/ASAP-Labs-LLC/LabLink`, private — working checkout at `/Volumes/Labsharedrive/Ryan C/LabLink`).
- **`web_app_config.json`** — QBench username/password, `report_config_id`, `labcore_url`. One whole URL, not host+port: a host/port pair cannot express the scheme. A bare hostname is normalized to `https://`. Point it at `http://127.0.0.1:8080` to develop against a local LabCore. Contains secrets; do not commit real values.
- **Portal auth** — every way in is a **LabLink account**, resolved by LabCore's `POST /api/login`: tap a card (`authenticate_card`) or type the same username and password (`authenticate_user`). The canonical account name LabCore returns becomes the session identity, `created_by` / `completed_by` on Command Center listings, and the audit-log user. There is deliberately **no local fallback credential** — the old hardcoded `Administrator` password plus a typed "Your Name" box let anyone who knew one string file work under any name, and a credential that still worked while LabCore was down would reintroduce exactly that. If LabCore is unreachable, login returns 503 `labcore_down` and says so; a session without LabCore couldn't flag, re-review, or sync anyway.

## Persistent state files (`DATA_DIR`, **not** the project root)

`DATA_DIR` is `COA_DATA_DIR` when set, otherwise `APP_DIR` — so a shared-drive
install behaves exactly as it always did, while a deployed install keeps state
outside the release. Under the deployment layout `APP_DIR` is
`C:\ASAPApps\coa\current` (a junction onto an immutable release) and `DATA_DIR`
is `C:\ASAPApps\coa\data`, which deploys never touch.

**Never bind a state path to `APP_DIR`.** With `COA_DATA_DIR` unset the two are
equal, so the mistake passes every test on a dev box and only destroys data in
production, on the first release swap. `tests/test_consolidation.py` has a
source-level guard against it and `tests/test_data_dir.py` covers the
resolution.

Resolution is import-time, not lazy: `ARCHIVE_DIR.mkdir()` and the rotating log
handler both run at module scope.


- `web_app_config.json` — QBench + LabCore config (above).
- `re_review_state.json` — **a QBench-resolution cache only**, keyed by lab_id → `sample_id` / `test_ids` / `order_id` (values that never change). It used to hold the Re-review queue itself; Command Center is the source of truth for *which* samples are in the tab, and the listing is deliberately not persisted here so a stale file can never contradict the live board.
- `credentials.json` — leftover Google service-account key. **No longer read by anything**; safe to delete whenever you like.
- `.secret_key` — stable Flask `app.secret_key` so session cookies survive restarts; auto-generated on first launch.
- `archive/` — CSV exports written by `/api/export` (named `review_<reviewer>_<timestamp>.csv`). Auto-created.
- `app.log` (rotating, 2 MB × 5 backups via `logging.handlers.RotatingFileHandler`) — application log.
- `server.log` — stdout/stderr from `app.py` when launched by `Run.pyw`; the launcher trims it past 1 MB.
- `launcher.log` — `Run.pyw`'s own lifecycle log.
- `login.log` — portal login/logout events with timestamps and IPs.

## Things worth knowing before editing

- `business_days_ago(n)` skips weekends only — it does **not** account for holidays.
- `REPORT_CONFIG_ID = "18"` and `REPORT_LEVEL = "sample"` are the QBench preview parameters used everywhere; change them in the config, not in code.
- The auto-restart at 3 AM is a deliberate way to refresh long-lived Playwright/QBench tokens without manual intervention — Run.pyw respawns the process.
- Both the QBench OAuth token flow and the Google service-account JWT include automatic clock-skew detection and retry (see `_ClockAdjustedCredentials` and the 401-with-`invalid_grant` path in `qbench_client.py`). Don't paper over a clock issue elsewhere; the existing recovery paths handle it.
- `UploadQueue` is for **QBench** test/comment edits only. Command Center writes deliberately go straight out through `state.labcore`: LabCore already serializes them on its own write queue, and the reviewer needs the conflict answer synchronously to resolve it in the modal.
- Flagging Bad is a **two-step** flow: the frontend creates the listing via `POST /api/cc/tasks` first (so a conflict can be resolved while the sample is still unmarked), then calls `/api/mark` with the resulting `cc_task_id`. Don't collapse them.
- Marking Good and un-marking never close a listing on their own. If the sample has an active listing the reviewer is prompted — complete / continue anyway / back out. The old code auto-completed the sheet row under hardcoded initials (`"CR"`); don't reintroduce that.
- `session_results` is keyed by `(tab, lab_id)` via `UserState.record_result()` / `clear_result()`. Appending to it directly reintroduces the bug where re-marking a sample exported it twice with contradictory outcomes.
- **Un-checking restores `STATUS_READY`, never `STATUS_PENDING`** (when a preview exists). The frontend derives `has_preview` from status in `updateSampleStatus()` — `ready`/`good`/`bad` mean "has a preview". Setting `pending` made an already-rendered COA display as if it had never rendered: the preview visibly vanished even though the PDF was still cached server-side. Un-marking never re-renders anything.
- Button state has two scopes. `updateActionButtons()` is sample-level and runs from `selectSample()`; `updateTabActionButtons()` is tab-level (Regenerate Pending) and must run from `renderSampleList()` and `updateSampleStatus()` too, because it has to be live *before* any sample is clicked. Folding the tab-level logic back into the sample-level function leaves Regenerate Pending greyed out on a freshly loaded tab.
- **Keycard login is the default way in.** LabLink readers are keyboard wedges (type the code, press Enter), so `#portal-card-input` stays focused while the scan view is up and consumes Enter itself. The code is verified by `LabCoreClient.authenticate_card()` against LabCore's `/api/login`, and the returned **LabLink username becomes the reviewer identity** — that is what makes `created_by`/`completed_by` and the audit log attributable. The card code is never logged; it is a credential. Password login is the fallback for a lost or unregistered card, and it is the *same* login — `authenticate_user()` hits the same endpoint and yields the same account name, so the two can't drift into different identities. Both routes funnel through `_resolve_identity()` in `app.py`, which ignores any `name` the client sends: who did the review is LabCore's answer, not the browser's claim.

- **The timeout overlay re-authenticates the session's owner, not just anyone.** It accepts a card tap or the LabLink password, then `/api/portal-reauth` compares the resolved account against the live `UserState.name` (case-insensitively) and returns **403** on a mismatch rather than handing over the screen — otherwise a colleague unlocking a terminal would inherit the in-progress review, and every listing filed afterwards would name the wrong person. If the `UserState` was already GC'd there is nothing to match, so a fresh session is created under whoever authenticated.
- **Sync Data is sample-information only** — test results are deliberately out of scope, because a mis-paired assay would silently overwrite a measurement. The button is CSS-gated to `body.mode-info`. `normalize_field_name()` matches on case/punctuation only; it must never collapse `tank_number` onto `tank`, since writing into the wrong QBench field is worse than making the reviewer drag it. A QBench field that already holds a *different* value is reported as a `clash` and left unticked. A sync re-renders the COA, because sample info feeds it.
- **The sync board renders both sides in full** (`lv_fields` / `qb_fields` from `/api/sync-preview`): every LabVision field on the left, **every** name in `SAMPLE_EDITABLE_FIELDS` on the right, whether or not auto-pairing matched it, plus a read-only tail of the rest of the QBench record. Rendering only auto-matched targets is what made `tank_number → tank` — the pairing the drag exists for — impossible to express, since the sole way to acquire a target was to steal one from another row. Fields QBench cannot accept (`SYNC_SOURCE_EXCLUDE`) are shown greyed and not draggable rather than hidden; a drag the route answers with a 400 promises a write that can't happen. `syncPairState()` recomputes clash/unchanged on every manual pair — the old code cleared the flag instead, so a hand-made pairing could overwrite a released value in silence. `qbench_read: false` means QBench's values couldn't be read, so nothing can be called a clash and the board says so instead of showing every field as empty.
- **The Lab Vision pane's contents follow the review mode.** Tests mode lists LabCore's tests (`test` / `result` / `operator`, from the same `/api/sync-preview` call); Info mode lists the sample-information pairs. A test with no result renders an em dash and a highlight, never a blank cell — a missing result is the thing the reviewer is looking for, and an empty cell reads as a rendering fault. `applyReviewMode()` re-renders the pane, otherwise a mid-session switch strands the other mode's contents.
- Pane visibility is three independent checkboxes (`panesVisible`, persisted in localStorage), not a three-way slider — the slider could not express COA + Lab Vision without the SIF. **Layout: COA is the left half at full height; SIF and Lab Vision stack inside `#side-panes` on the right, top and bottom.** So all three showing makes SIF and Lab Vision a quarter of the *area* each — they are not three columns. `#side-panes` needs `flex-direction: column`, and the generic `.pdf-pane + .pdf-pane` left-border is overridden to a top border inside it. `setPaneVisible()` refuses to hide the last visible pane.
- Marking relies on the SSE `sample_status` event to update local state — `markGood()` does not set `sample.status` itself. That is pre-existing, and it means a dead SSE connection makes marking look like it did nothing.
- Re-review shows **all** open `double_check` listings, whoever filed them — including ones raised in LabVision. Some of those name a lab_id QBench cannot resolve or that has no renderable COA; those are skipped with a status line, by design.
- Per-user state lives in `UserState` keyed by Flask session `uid`. Anything that should persist across browser refreshes but not across server restarts belongs there. Anything that should persist across restarts belongs in `re_review_state.json` or a config file.
- SSE events drive UI updates — when adding a new async path, broadcast a typed event (`{"type": "...", ...}`) and update the JS handler in `static/js/app.js` to consume it, rather than polling.
- The port (5559) is hardcoded in `Run.pyw` and defaults in `app.py` (override with `PORT` env var). Frontend assumes the same origin so it doesn't need to know.
- `V3/Web App/` is a legacy directory from a previous layout — it only holds stale `app.log` / `server.log` and is not referenced by any code. Ignore it; don't add anything there.
