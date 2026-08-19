# Command Center integration — design

**Date:** 2026-07-31
**Status:** Implemented

## Deviations from the approved design

Two changes made during implementation, both deliberate:

1. **`re_review_state.json` was kept, not deleted.** The spec called for
   removing it. It was serving two roles: the Re-review queue itself (now
   Command Center's job) *and* a cache of QBench lookups per lab_id
   (`sample_id` / `test_ids` / `order_id`). Deleting it would have made every
   Re-review load re-query QBench once per sample for values that never
   change. It is now the cache only — the CC listing is deliberately not
   persisted, so a stale file cannot contradict the live board.

2. **`credentials.json` was left on disk.** Nothing reads it, and the code and
   config that referenced it are gone. Deleting a credential file is the
   owner's call, not an implementation detail; it is safe to remove whenever.

Replace COA Reviewer's Google Sheets "Double Check" workflow with LabLink's
Command Center, add an "Open in Lab Vision" link, an un-mark button, and a
"Regenerate Pending" action.

## Background

Command Center is **not** in `apps/LabStation` as first assumed. It is built
into **LabCore** (`apps/LabCore/src/LabCore.py`) — tables `cc_tasks`,
`cc_task_samples`, `cc_task_updates` — and its board is a tab in the
**LabVision** web UI (`apps/LabCore/src/labvision.html`) that LabCore serves.

Verified facts the design depends on:

- LabCore's `lab_id` is the same `MMDDYY-NNNNN` form COA Reviewer uses, so
  dedup, read-back, and matrix flagging line up with no translation.
- `POST /api/queue/write` is an explicitly unauthenticated gateway ("any
  LabLink program can POST here") and dispatches `cc_create_task` /
  `cc_complete_task`. `POST /api/cc/tasks` would instead require a LabVision
  bearer token.
- `GET /api/cc/tasks`, `/api/cc/tasks/check-duplicate`,
  `/api/cc/samples/search`, `/api/cc/customers` are public reads.
- **LabCore sends no CORS headers and implements no `OPTIONS` handler**, so a
  browser on `http://host:5559` cannot call `http://host:8080` directly.
  Everything proxies through Flask.
- `cc_create_task` dedups on `lab_id`, returning
  `{"conflict": true, "existing_tasks": [...]}` unless `force_create` is set.
- `cc_complete_task` **requires** non-empty `completion_notes`.
- LabCore is at `https://labvision.asaplabs.net`, behind Cloudflare on 443
  (corrected 2026-07-31 after the initial "same machine" assumption).

## Decisions

| Question | Decision |
|---|---|
| Re-review source | All open `type == "double_check"` listings, any origin |
| Mark Good / Uncheck on a sample with a listing | Modal: complete / continue anyway / back out |
| Flag modal | Full LabVision listing form, autofilled from the sample |
| Open in Lab Vision | `#/sample/<lab_id>` |
| LabCore location | Remote: `https://labvision.asaplabs.net` (HTTPS/443) |
| Google Sheets | Full removal |

## Architecture

### `labcore_client.py` (new)

Thin HTTP client following LabStation's `labcore_client.py` conventions:
short timeouts (these calls land on request threads), bounded retry on
idempotent reads, **no** retry on writes.

| Method | Call |
|---|---|
| `create_task(params)` | `POST /api/queue/write` → `cc_create_task` |
| `complete_task(task_id, notes, by)` | `POST /api/queue/write` → `cc_complete_task` |
| `active_tasks()` | `GET /api/cc/tasks?view=active` |
| `check_duplicate(lab_ids)` | `GET /api/cc/tasks/check-duplicate` |
| `sample_info(lab_id)` | `GET /api/cc/samples/search?q=` |
| `customers()` | `GET /api/cc/customers` |
| `is_available()` | `GET /api/queue/status` |

Writes carry an `op_id`; LabCore's fast idempotency path returns the recorded
result for a repeated `op_id` instead of creating a second listing.

### Two base URLs

`web_app_config.json` gains a single `labcore_url`
(default `https://labvision.asaplabs.net`). One whole URL rather than
host+port: a host/port pair cannot express the scheme, and LabCore is reached
over HTTPS on 443.

Server and browser use the **same** URL, because LabVision is served by
LabCore at a public hostname that resolves identically from both.

This replaced an earlier design in which the browser URL was derived from
`request.host` plus a LabCore port. That was correct only under the original
"LabCore is on the COA Reviewer machine" assumption; against a public
hostname it yields `http://<coa-reviewer-host>:8080`, which does not exist,
and "Open in Lab Vision" would have opened a dead page for every reviewer.

### Flask proxy routes

`GET /api/cc/config`, `GET /api/cc/lookup/<lab_id>` (autofill + existing
listings in one round trip), `POST /api/cc/tasks`,
`POST /api/cc/tasks/<id>/complete`, `GET /api/cc/customers`.

CC writes go direct, **not** through `UploadQueue`: LabCore already serializes
via its own write queue, and the reviewer needs the conflict answer
synchronously. `UploadQueue` remains for QBench test/comment edits.

## Features

### Flag Bad → full listing form

The reason modal becomes the LabVision new-listing form: problem, type,
context, customer, samples, status, department. Autofilled — customer and
fuel type from `samples/search`, current `lab_id` as a removable chip (more
can be added), type `double_check`, status `open`, context stamped with tab,
reviewer initials, and date. On `conflict`, offer add-to-existing / create
anyway / cancel.

### Mark Good or Uncheck with an active listing

Both consult `check-duplicate` first. If a listing exists, a modal shows it
(id, type, status, problem, latest update) with three actions:

1. **Complete the listing** — collects the completion notes LabCore requires,
   completes it, then applies the mark.
2. **Continue anyway** — applies the mark, listing stands.
3. **Back out** — nothing changes.

### Uncheck

Clears status to pending and clears the reason. `session_results` becomes
keyed by `(tab, lab_id)` rather than append-only — that is what makes uncheck
affect Export CSV and Good Samples. (The append-only list also means marking a
sample twice currently double-counts it; keying fixes that too.)

### Regenerate Pending

Current tab only. Resets preview state for every record not marked good/bad —
including `error` — and resubmits to `PREVIEW_POOL`. Returns immediately with
a count; results stream over the existing `sample_status` SSE events so the
reviewer keeps working.

### Re-review from Command Center

`fetch_re_review_samples()` reads `GET /api/cc/tasks?view=active`, keeps
`type == "double_check"`, expands `samples[]` to lab_ids, and resolves each
through the existing QBench path. The CC task replaces `double_check_row` on
`SampleRecord`.

Because this includes listings COA Reviewer did not create, some lab_ids will
not resolve or will have no renderable COA. Those are skipped with a status
message, as the sheet path already does.

### Open in Lab Vision

Button in the existing `.bottom-bar` beside "Open in QBench". Opens
`<labVisionBase>/#/sample/<lab_id>` in a new tab.

## Google Sheets removal

Delete `google_sheet.py`, `re_review_state.json`, `DoubleCheckRow`,
`/api/check-sheet`, and the Sheets members of `AppState`. `credentials.json`
is no longer needed. Remove the Google deps from `install.bat`, `install.sh`,
and `REQUIRED_PACKAGES`.

Coupled test files needing updates: `test_helpers.py`, `test_consolidation.py`
(asserts on `google_sheet.py`'s contents — breaks on deletion),
`test_install_scripts.py`, `conftest.py`.

## Error handling

LabCore unreachable: banner, CC actions disabled, and flagging Bad **fails
loudly** — a silently lost flag is worse than a blocked one. Re-review shows
an error state, not an empty tab.

## Testing

TDD throughout.

- `test_labcore_client.py` — against a real local stub HTTP server (real
  sockets, not mocks), covering conflict, error, and unreachable paths.
- `test_cc_routes.py` — Flask test client with the client monkeypatched.
- Uncheck-affects-export, regenerate-pending selection, Re-review mapping.
- Updates to the four Sheets-coupled test files.

## Accepted risks

- Re-review is wider than the sheet was: listings raised in LabVision for
  non-COA reasons enter the reviewer's queue, and some will have no COA.
- Sheets removal is one-way. The Master Logbook stops receiving rows on ship.

## Implementation order

Dependency-ordered so nothing is broken between steps:

1. `labcore_client.py`
2. Config + Flask proxy routes
3. Re-review from Command Center (replaces the Sheets read)
4. Flag Bad → Command Center (replaces the Sheets write)
5. Complete / continue / back-out modal
6. Uncheck + `session_results` keying
7. Regenerate Pending
8. Open in Lab Vision
9. Remove Google Sheets (nothing depends on it by now)
