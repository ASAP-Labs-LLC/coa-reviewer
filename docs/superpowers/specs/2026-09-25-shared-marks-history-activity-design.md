# Shared marks, sample history, time online, update-on-restart — design

Date: 2026-09-25 · Target release: **v4.0.0** (MAJOR — a reviewer's click now
changes what every other reviewer sees; RELEASING.md §2)

## Why

Today a mark belongs to one reviewer for 12 hours. Two people working the same
day cannot see each other's work, so samples get reviewed twice or not at all.
The lab also cannot answer "who changed this sample, and what was it before"
without grepping JSONL on the share, or "who was on today, and when".

Use case over literal request: the point is **coordination** (don't re-review a
cleared sample), **accountability** (every change to a sample, before/after,
by whom, when), and **staffing visibility** (who worked when). None of this may
slow the core task — reviewing a COA.

## Decisions (approved 2026-09-25)

| Question | Decision |
|---|---|
| What other reviewers get from a mark | **Tags + shared verdict.** The verdict belongs to the sample (per mode), not the reviewer. It shows as that sample's status for everyone in that mode, and in everyone's Good Samples / Export, with the marker named as reviewer. |
| Restart and updates | **Only if already staged.** Restart switches to a newer release only when the updater has staged it and it passed its health check. |
| Where Time Online lives | **Header icon (◷) → `/activity` in a new browser tab.** Never takes space from the COA panes. |

## 1. Shared store — `shared_store.py`, `DATA_DIR/coa_shared.db`

SQLite (stdlib, WAL, `synchronous=NORMAL`, `busy_timeout=5000`). One
`SharedStore` object on `AppState`, one connection per thread
(`threading.local`), all writes under one lock. `DATA_DIR`, so it survives
restarts, logins and release swaps. `ChangeLog` JSONL keeps being written
unchanged — it remains the audit trail of record; the DB is the queryable view.

Tables:

- `verdicts(lab_id, mode, outcome, reason, cc_task_id, sample_id, by_user, at)`
  — PK `(lab_id, mode)`; `mode ∈ {info, tests}`, `outcome ∈ {good, bad}`.
  Uncheck deletes the row.
- `sample_events(id, lab_id, at, user, kind, field, before, after, detail)` —
  index `(lab_id, at)`, `(at)`. `kind` is a closed set: `mark`, `unmark`,
  `test_result`, `sample_info`, `sample_sync`, `comments`,
  `attachment_deleted`, `listing_created`, `listing_completed`.
- `presence(id, user, started_at, last_seen, ended_at, end_reason)` — index
  `(started_at)`, `(user, last_seen)`.

Rules (NASA Power-of-10 applied to Python):

- Every query is bounded: `LIMIT` on every read (history ≤ 500, activity per
  day ≤ 5 000 events, lab-id batches ≤ 900 bound parameters, chunked).
- Every public method validates its inputs (assert-style guard → `ValueError`
  for programming errors) and returns a checked value; **I/O errors never
  propagate to a reviewer's request** — logged at WARNING with context and
  degraded (reads → empty, writes → `False`).
- Functions ≤ ~60 lines, no recursion, no unbounded loops.
- `logger = logging.getLogger("coa.shared_store")`; DEBUG logs each write
  (table, key, duration ms) and slow queries (> 50 ms) at INFO.

## 2. Shared verdicts and tags

- `/api/start` already receives `mode`; it is stored on `UserState.mode`.
  `/api/mark` accepts `mode` (falls back to `UserState.mode`, default `tests`).
- Mark Good/Bad → `store.set_verdict`; Uncheck → `store.clear_verdict`.
  Then **fan-out**: every live session whose mode matches and which holds that
  lab_id (any tab) gets the verdict applied (status, reason, cc_task_id,
  `record_result` with `reviewer` = marker) and a `sample_status` SSE; every
  session gets a `tags` SSE `{lab_id, tags}` so the pills update live.
- Loading samples (tab pull, custom day, search, re-review, restore): one
  batched `store.verdicts_for(lab_ids)` per tab load applies the session-mode
  verdict to unjudged records and attaches `tags` to `to_dict()`. The shared
  verdict supersedes the per-account 12 h ledger when both exist; the ledger is
  kept only as the offline fallback (store unreadable).
- `tags` shape: `{"info": {"by","at"} | null, "tests": {"by","at"} | null}` —
  Good only. Bad shows through status, not a tag.
- UI: in the sample list, two small pills after the lab id: `INFO`, `TEST`.
  Outlined neutral when absent (hidden, to avoid noise), filled soft-green with
  green text when Good. Tooltip: "Info checked by Dana P · 9:42 AM".

## 3. Sample history — right pane tab

- Every place that writes the change log also calls
  `store.record_event(...)` with `before`/`after`. New before-values:
  - `sample_info` PATCH and `sample_sync`: read the QBench sample once before
    the PATCH (the same `get_sample` the GET branch uses); per-field before.
  - `comments`: read current order/sample comments before writing.
  - A failed before-read records `before = null` ("unknown") and **never**
    blocks the edit.
- `GET /api/sample-history/<lab_id>?limit=200` → newest first.
- UI: the Lab Vision pane header becomes a segmented control
  `Lab Vision | History`. History is a vertical timeline: initials avatar,
  one plain sentence ("**Dana P** changed *moisture* from `11.2` → `12.0`"),
  relative time with exact timestamp on hover, day separators. Loaded when the
  tab is shown or the selected sample changes while shown; refreshed on a
  `sample_event` SSE for the selected lab_id. Empty state explains itself.

## 4. Time online — `/activity`

- Presence: `PresenceTracker` in memory (`user → open span id, last_seen`),
  touched by heartbeat (60 s) and any authenticated API request, **written to
  the DB at most once per 30 s per user**. A gap > 180 s closes the span at
  `last_seen` and opens a new one; logout closes it immediately
  (`end_reason=logout`); the idle reaper closes it (`timeout`); spans still
  open at process start are closed at their `last_seen` (`restart`).
- `GET /api/activity?date=YYYY-MM-DD` → `{date, users:[{user, spans:[{start,
  end, open}], changes:[{at, kind}], totals}], bounds}`; `GET
  /api/activity/range` → first/last recorded day. Changes per user are
  bucketed into 5-minute bins for rendering ("active editing" blocks).
- Page: separate template + `static/js/activity.js` + `static/css/activity.css`
  sharing app.css tokens and the theme pips. Day chart: time on the y-axis
  (6 AM–10 PM, auto-extends), one column per reviewer on the x-axis; soft grey
  bar = online, solid bar = editing blocks. Hover card: name, exact
  start–end, duration, change count and breakdown. Summary strip: online now,
  people today, total hours, total changes. Day stepper ‹ › + date input.
  Polls every 120 s only while `document.visibilityState === "visible"`, and
  only for today. Requires a portal session (redirects to `/` otherwise).
- History starts at this release — nothing before recorded presence.

## 5. Restart checks for staged updates

- App: `staged_update(DATA_DIR, APP_VERSION)` reads the updater's
  `staged.json`; returns the tag iff `healthy` and different from the running
  version. `request_restart()`: if a tag is returned, write
  `DATA_DIR/switch-requested` (`{tag, by, at}`) and start a watchdog; if the
  marker still exists after 60 s (updater not upgraded / not running), delete
  it and restart normally. `/api/restart` returns `{update: tag|null}`; the UI
  says "Installing vX…" and reloads when `/api/health` reports a new version.
  The 3 AM auto-restart and the tray use the same path.
- Updater: each supervision tick, `honour_switch_request(app)` consumes the
  marker (delete first, so a crash cannot loop) and, if `may_switch` allows
  the staged tag and it differs from current, calls `switch()` (health check +
  auto-rollback unchanged). Otherwise logs why and does nothing — the app's
  watchdog restarts normally.
- Deploy note: `updater.py` is not deployed by tags; copy it to
  `C:\ASAPApps\updater\` once. Until then Restart behaves as today (after 60 s).

## Performance budget

- Mark: +1 indexed upsert + 1 insert (< 5 ms) + in-memory fan-out.
- Tab load: +1 batched SELECT per ≤ 900 lab_ids.
- Requests: presence is an in-memory dict touch; DB write ≤ 1/30 s/user.
- `/activity` is a separate page; nothing on the review screen polls for it.
- A bench/pytest guard asserts mark + tab-load overhead stays bounded.

## Error handling & logging

- Store failures degrade, never raise into a route; the UI shows a quiet
  inline notice ("History unavailable — the shared store could not be read").
- New loggers: `coa.shared_store`, `coa.presence`, `coa.restart`; DEBUG
  traces with timings; INFO for state transitions (span open/close, switch
  requested/honoured/timed out); WARNING for degraded paths.

## Testing (TDD)

`tests/test_shared_store.py`, `test_shared_verdicts.py`,
`test_sample_history.py`, `test_presence.py`, `test_activity_api.py`,
`test_restart_update.py`, updater cases in `test_updater.py`, and source-level
frontend guards in `test_shared_ui_frontend.py`. Real SQLite in `tmp_path`,
no mocks of the store.
