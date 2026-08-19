# Lab Vision tests pane + Sync Data drag board

**Date:** 2026-08-03
**Status:** Approved, ready for implementation plan

Two related changes to how COA Reviewer surfaces LabCore's per-sample record:

1. In **Tests** review mode, the Lab Vision pane lists the sample's tests and
   results instead of its sample-information fields.
2. **Sync Data** is rebuilt as a two-column drag board showing *all* Lab Vision
   fields and *all* QBench sample fields, so any field can be dropped onto any
   other.

---

## Problem

### Tests are fetched and thrown away

LabCore's `GET /api/sample?id=<lab_id>` already returns a `tests` list —
`[{"test": ..., "result": ..., "operator": ...}]` — alongside the
sample-information fields (`LabCore.py:_serve_sample_detail`).

`LabCoreClient.sample_data()` returns the whole payload, but
`/api/sync-preview/<lab_id>` passes it through `pair_sample_fields()`, whose
`SYNC_SOURCE_EXCLUDE` set drops `tests`. The response contains only `pairs`, so
the Lab Vision pane has no way to show test results. A reviewer working in Tests
mode sees a pane full of customer/fuel/tank fields that are irrelevant to what
they are checking.

### The sync drag is impossible for most fields

`renderSyncRows()` builds one row per **Lab Vision** field. Each row's QBench
slot exists only if `pair_sample_fields()` auto-matched a target. Of the eleven
syncable Lab Vision fields, five auto-match:

| Lab Vision field | QBench target | Matched? |
|---|---|---|
| `fuel_type` | `fuel_type` | exact |
| `po_number` | `po_number` | exact |
| `work_order` | `work_order` | exact |
| `tank_capacity` | `tank_capacity` | exact |
| `site_location` | `site_location` | exact |
| `tank_number` | — | no |
| `sample_from` | — | no |
| `collection_date` | — | no |
| `rush_status` | — | no |
| `tracking_number` | — | no |
| `testing_package` | — | no |

The six unmatched fields render as "drop a field here". The only way to give one
a target is `repointSyncPair()`, which **steals** a target from another row.
A QBench field that nothing auto-matched — `tank`, `sample_taken_from`,
`time_of_collection`, `Rush`, `fw`, `generator`, `component_model`,
`point_of_collection`, `quantity_tank`, `accession_number`,
`customer_sample_id`, `package_size` — is never rendered anywhere, so it can
never be dropped onto.

`tank_number → tank` is the pairing `CLAUDE.md` calls "the whole point of the
drag". It is not reachable in the current UI. Neither is
`sample_from → sample_taken_from`, `collection_date → time_of_collection`, or
`rush_status → Rush`.

That is why the feature reads as "doesn't work at all": the auto-matched rows
work, and everything else is a dead end.

---

## Design

### Part 1 — Lab Vision pane shows tests in Tests mode

**Backend** (`app.py`, `/api/sync-preview/<lab_id>`)

Add a `tests` key to the response, taken from the LabCore payload:

```json
{
  "lab_id": "073126-41552",
  "pairs": [...],
  "targets": [...],
  "tests": [{"test": "Water & Sediment", "result": "0.02", "operator": "RC"}]
}
```

Normalisation: each entry is coerced to exactly `{test, result, operator}` with
string values (missing keys become `""`), so the frontend never has to defend
against an older LabCore that omits `operator` — `_serve_sample_detail` already
degrades to `""` there, and this keeps the contract explicit on our side too.

`SYNC_SOURCE_EXCLUDE` keeps excluding `tests` — test results remain out of scope
for the sync itself, per `CLAUDE.md`. This change only stops the data being
discarded on the way to the pane.

**Frontend** (`static/js/app.js`)

`loadLabVisionData()` stores the whole response, not just `pairs`.
`renderLabVisionData()` branches on review mode:

- `document.body.classList.contains("mode-tests")` → render the **test list
  only**: one `.lv-test-row` per test with `test name | result | operator`.
- otherwise → render the existing sample-information `.lv-row` list, unchanged.

Rules for the test list:

- An empty `result` renders as `—`, never as an empty cell. A missing result is
  a thing the reviewer needs to *see*; an empty cell reads as a rendering bug.
- An empty `operator` renders as blank (it is metadata, not a finding).
- No tests → `No Lab Vision tests for this sample.` in the existing
  `.labvision-empty` style.
- No comparison against QBench results. The list is what Lab Vision holds;
  matching test names between the two systems is fuzzy and a wrong pairing would
  raise a false mismatch — worse than no mismatch check.

The pane label (`templates/index.html`, `#labvision-pane .pane-label`) becomes a
mode-dependent string: `Lab Vision — Tests` in Tests mode, `Lab Vision — Sample
Info` in Info mode, so the pane's contents are never ambiguous. Implemented by
giving the label an id and setting it in `applyReviewMode()` and
`renderLabVisionData()`.

Mode changes mid-session must re-render the pane: `applyReviewMode()` calls
`loadLabVisionData(state.currentSample.lab_id)` when a sample is selected and
the pane is visible.

### Part 2 — Sync Data as a two-column drag board

**Backend** (`app.py`, `/api/sync-preview/<lab_id>`)

Two new arrays alongside `pairs`. `pairs` and `pair_sample_fields()` are kept
exactly as they are — `pair_sample_fields()` remains the auto-pairing engine and
its existing tests stay green.

```json
{
  "lv_fields": [
    {"name": "tank_number", "value": "2A",   "syncable": true},
    {"name": "customer_name","value": "Acme", "syncable": false},
    {"name": "sample_id",    "value": "8821", "syncable": false}
  ],
  "qb_fields": [
    {"name": "tank",     "current": "",     "editable": true},
    {"name": "fuel_type","current": "ULSD", "editable": true},
    {"name": "source",   "current": "...",  "editable": false}
  ]
}
```

`lv_fields` — **every** field LabCore returns for the sample, in the order
LabCore returns them, so the reviewer sees the whole record. `syncable: false`
for `lab_id`, `sample_id`, `tests`, `photo_count`, `photo_has_blob`,
`customer_name`, `order_id`:

- `tests`, `photo_count`, `photo_has_blob` are not sample-information fields.
- `sample_id`, `customer_name`, `order_id` are real Lab Vision data worth
  *seeing*, but QBench will not accept them (`customer_name` and `order_id` are
  not in `SAMPLE_EDITABLE_FIELDS`; `sample_id` is structural).
- `lab_id` is technically in `SAMPLE_EDITABLE_FIELDS` but is the sample's
  identity — syncing it would rename the sample. Not draggable.

A non-syncable entry is rendered greyed and is not draggable, because offering a
drag that the backend would refuse promises a write that cannot happen.

`qb_fields` — every name in `SAMPLE_EDITABLE_FIELDS` with QBench's current
value, sorted, **including the ones nothing auto-matched**. That is the fix.
Followed by a read-only tail: the remaining keys QBench returned for the sample
(flattened `custom_fields` included, same flattening the route already does),
marked `editable: false`. The tail makes "all QBench sample data" honest — the
reviewer can see the full record while the board is unambiguous about what is
writable.

If QBench cannot be read (no `sample_id` resolved, or `fetch_sample` raised —
the route already logs and continues with `current = {}`), `qb_fields` still
lists every editable field with `current: ""` and the modal shows a warning line
that QBench's current values are unknown. The board must stay usable; it just
cannot claim "unchanged" or "clash" for anything.

**Frontend** (`static/js/app.js`, `templates/index.html`, `static/css/app.css`)

The modal body becomes two columns:

```
LAB VISION                      QBENCH
────────────────────────        ─────────────────────────────────
 tank_number     2A     ──▶      tank              (empty)   [x]
 sample_from     Tank            sample_taken_from Tank      [ ]
 collection_date 7/29            time_of_collection —        [ ]
 fuel_type       ULSD   ──▶      fuel_type         ULSD    ✓same
 work_order      WO-91  ──▶      work_order        WO-88   ⚠ [ ]
 rush_status     Rush            Rush              (empty)  [ ]
 customer_name   Acme  (grey)    fw                (empty)
                                 generator         CAT-400
                                 ─── read-only ───
                                 source            {...}
```

State lives in a rebuilt `SYNC` object: `{labId, lvFields, qbFields, links}`
where `links` maps a QBench field name to the Lab Vision field name paired to
it. Auto-pairs from `pairs` seed `links` on load.

Interaction rules:

- Every **editable** QBench row is a drop target. Read-only rows are not.
- Only `syncable` Lab Vision cards are draggable.
- Dropping onto an already-paired QBench row displaces the previous source back
  to unpaired rather than reassigning it silently — the same principle as
  today's `repointSyncPair()`.
- A Lab Vision field pairs to at most one QBench field. Dragging a card that is
  already paired **moves** the pairing; it does not duplicate it.
- Unpair by dropping the card back on the left column, or by clicking the `×` on
  the link.
- On a manual pair, clash/unchanged are recomputed locally from
  `qb_fields[target].current` versus the card's value. Today
  `repointSyncPair()` clears the clash flag and shows nothing, so a manual drag
  onto a populated QBench field can overwrite a released value with no warning.
  That is the second real defect in the current implementation.
- Tick state per pair: auto-paired and blank-target → ticked; `unchanged` →
  disabled and unticked (nothing to do); `clash` → **unticked**, the reviewer
  must opt in. A freshly dragged pair onto a blank field is ticked; onto a
  populated field it is unticked and flagged.
- The modal's hint text is rewritten to describe the board.

`applySyncData()` posts `{mappings: [{source, target}]}` exactly as it does now,
built from ticked links. `/api/sync-sample-info/<lab_id>` is **unchanged** — it
already validates every target against `SAMPLE_EDITABLE_FIELDS` and returns 400
on anything else, splits top-level versus `custom_fields`, writes the audit log,
and re-renders the COA.

Sync Data stays gated to `body.mode-info` (`static/css/app.css:2407`). Tests
mode gets the read-only test pane only.

---

## Data flow

```
LabCore  GET /api/sample?id=<lab_id>
   │        → {sample fields…, tests: [...]}
   ▼
LabCoreClient.sample_data()            (unchanged)
   ▼
app.py  /api/sync-preview/<lab_id>
   │   ├─ pair_sample_fields(source, SAMPLE_EDITABLE_FIELDS, current) → pairs   (unchanged)
   │   ├─ lv_fields  ← every LabCore key + syncable flag                        (new)
   │   ├─ qb_fields  ← SAMPLE_EDITABLE_FIELDS + current, then read-only tail    (new)
   │   └─ tests      ← normalised LabCore tests                                 (new)
   ▼
static/js/app.js
   ├─ loadLabVisionData()  → renderLabVisionData()   → tests list (Tests mode)
   │                                                 → lv-row list (Info mode)
   └─ openSyncData()       → renderSyncBoard()       → two-column drag board
                                                     → applySyncData()
                                                          ▼
                                       POST /api/sync-sample-info/<lab_id>  (unchanged)
```

---

## Error handling

| Condition | Behaviour |
|---|---|
| LabCore unreachable | Existing `_labcore_down()` path. Pane: "Lab Vision unreachable." Modal: existing error line. |
| LabCore has no record of the sample | Existing 404 + `error` string, rendered in the pane and the modal. |
| QBench sample not resolvable | `qb_fields` still lists every editable field with `current: ""`; modal shows "QBench's current values could not be read — clashes are not detected." Board stays usable. |
| LabCore returns no `tests` | Tests-mode pane shows "No Lab Vision tests for this sample." |
| A test has no result | Renders `—`, never blank. |
| Reviewer pairs onto a populated QBench field | Row flagged as a clash and left unticked; the write only happens if they tick it. |
| Reviewer ticks a field QBench will not accept | Impossible from the board (read-only rows are not drop targets), and the route still returns 400 as a backstop. |

---

## Testing

TDD, per `CLAUDE.md`: failing test first, then implement.

**`tests/test_sample_sync.py`** (extend)
- `/api/sync-preview` returns `tests`, normalised to `{test, result, operator}`.
- A LabCore payload with no `operator` key still yields `operator: ""`.
- `qb_fields` includes every name in `SAMPLE_EDITABLE_FIELDS`, including
  `tank`, `sample_taken_from`, `time_of_collection` and `Rush`, which nothing
  auto-matches. This is the regression test for the actual bug.
- `qb_fields` carries QBench's current value per field.
- The read-only tail is marked `editable: false` and contains no name from
  `SAMPLE_EDITABLE_FIELDS`.
- `lv_fields` lists every LabCore key, with `syncable: false` for `lab_id`,
  `sample_id`, `tests`, `photo_count`, `photo_has_blob`, `customer_name`,
  `order_id`.
- When QBench cannot be read, `qb_fields` is still complete with empty
  `current`.
- Existing `pair_sample_fields` tests must remain green untouched.

**`tests/test_view_panes.py`** (extend)
- The Lab Vision pane renders tests when `mode-tests` is set and sample info
  when `mode-info` is set (source-level assertions on `renderLabVisionData`,
  matching the file's existing source-guard style).
- The pane label is mode-dependent.

**`tests/test_sync_frontend.py`** (rewrite the drag assertions)
- The modal contains two labelled columns.
- Every editable QBench row is a drop target — not only auto-matched ones.
- A non-syncable Lab Vision field is not draggable.
- Dropping onto a paired row displaces the previous source to unpaired.
- A manual pair onto a populated QBench field is flagged and left unticked.
- Only ticked, paired rows are posted.
- Existing guards that survive: the button is Info-mode only, the dialog
  exists, the preview is regenerated after a sync.

No new network is contacted in tests. LabCore stays mocked or stubbed exactly as
the existing sync tests do.

---

## Out of scope

- Comparing Lab Vision test results against QBench test results.
- Syncing test results in either direction (`CLAUDE.md`: a mis-paired assay
  would silently overwrite a measurement).
- Editing Lab Vision from COA Reviewer. The board is one-directional:
  Lab Vision → QBench.
- Making Sync Data available in Tests mode.
