# Marks follow the samples through a re-pull

**Date:** 2026-09-15
**Request:** "Which account has which samples checked off for 12 hours, so if
the samples get reloaded those checks (good and bad) pull up with them.
Account specific."

## What already existed

Since v2.2.0 the review is written to `review_state/<account>.json` on every
mark and tab load, and a `UserState` rebuilt for that account (login, card
login, re-auth after the reaper) is restored from it for 12 hours. That covers
a lost session and a process restart. It does **not** cover a re-pull:

- `POST /api/start` clears the records and results and persists the empty
  list, by design ("Start Pulling is the reviewer saying new day").
- Custom Day and Search delete their old records before fetching.
- A judged sample that comes back without a preview (any restore) can never
  render again: the window only renders `pending` samples, and Regenerate
  wipes the verdict.

So marks came back after a reaped session but were gone the moment the same
samples were pulled again.

## Design

### A verdict ledger per account

`UserState.verdicts: {(tab, lab_id): entry}` where an entry is
`{tab, lab_id, status, reason, cc_task_id, judged_at, date}`.

- `POST /api/mark` good/bad writes the entry; uncheck removes it.
- An entry is **fresh** for `REVIEW_STATE_MAX_AGE_SECONDS` (12 h) from
  `judged_at`. Stale entries are dropped when the snapshot is written and when
  they are looked up.
- `UserState.add_record()` is the one place every pull (Yesterday, Due Out,
  Intaked, Custom Day, Search, Re-review, and snapshot hydration) creates a
  record. When it adds a record whose key has a fresh entry it applies the
  verdict: status, reason, `cc_task_id`, and a `session_results` row carrying
  the original review date so Export and Good Samples see it.
- The ledger is keyed by `(tab, lab_id)`, like `session_results`, because the
  same lab ID on two tabs is two reviews (Yesterday vs Due Out are different
  review moments). The 12-hour window means a next-day pull sees no old marks.

### The snapshot carries the ledger

`snapshot()` writes `version: 2` with a `verdicts` list next to `records`.
`hydrate()` loads the ledger first, then the records. A version-1 snapshot
(what is on ASAPSV1 today) has no `verdicts`; its judged records seed the
ledger with `judged_at = saved_at` so nothing is lost across the upgrade.

`POST /api/start` still clears the list, results and caches and writes the
snapshot, but the ledger survives it. Rows come back as samples do.

### Judged samples still render

`_queue_preview()` renders a sample that is `pending` **or** judged with no
preview URL. `generate_preview_for_sample()` keeps a verdict through the
render: no `loading` status, no `error` status on failure (the mark is worth
more than the render), and on success it emits `sample_status` with the
verdict status and `has_preview: true`. The client loads the PDF on that flag
(it previously loaded only on `ready`), so a re-pulled Good sample shows its
COA without the reviewer touching Regenerate.

### Regenerate un-judges consistently

Regenerate (explicit, Sync & Regenerate) already dropped a sample's status to
`loading`, but left its `session_results` row, so Export disagreed with the
list. With a ledger that would also resurrect the mark on the next pull. So
`_reset_for_regenerate()` now clears the row and forgets the ledger entry: a
regenerated COA is a new document and gets a new verdict.

## Out of scope

- Carrying a mark from one tab to another (Yesterday -> Due Out next day).
- Any change to the 12-hour window or to what the snapshot excludes
  (preview URLs, PDF bytes).

## Versioning

MAJOR (v3.0.0). Start Pulling no longer resets marks, which is a visible
change to what a reviewer's click does, and the fix to Regenerate changes
what is recorded for export.
