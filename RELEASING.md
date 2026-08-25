# Releasing

How a version number becomes a running app, and what an agent needs to know
before cutting one.

**Read the warning in §5 before your first release.** Deploys here are
unattended: tagging ships to the lab without anyone clicking anything.

---

## 1. The pipeline, end to end

```
you: git tag -a v1.2.3 && git push origin v1.2.3
        │
        ▼
.github/workflows/release.yml   (triggers on tags matching v* )
        │  writes the tag into a VERSION file
        │  builds a source-only zip + a SHA256 checksum
        ▼
GitHub Release  v1.2.3   ← this is now "latest"
        │
        ▼
C:\ASAPApps\updater\updater.py   (polls every 5 min, on ASAPSV1)
        │  compares latest tag with C:\ASAPApps\<app>\current\VERSION
        │  downloads, VERIFIES THE CHECKSUM, unpacks to releases\v1.2.3\
        │  builds that release's venv from its requirements.txt
        │  starts it on a scratch port and polls /healthz
        │  if unhealthy: records the failure and stops. Never deployed.
        ▼
        │  if healthy: waits until nobody is using the app
        ▼
   repoints the `current` junction, restarts, re-checks /healthz
        │  if unhealthy after the switch: ROLLS ITSELF BACK
        ▼
   live on the normal port
```

Nothing in that chain needs a credential — both repos are public.

## 2. Choosing the number

`MAJOR.MINOR.PATCH`, tag prefixed with `v`.

| Bump | When | Examples here |
|---|---|---|
| **PATCH** `v1.2.3 → v1.2.4` | A fix that changes nothing about how the app is used | A wrong QC band, a crash on a missing SIF, a typo in a label |
| **MINOR** `v1.2.3 → v1.3.0` | New behaviour, existing behaviour unchanged | A new tab, a new API route, a new export column |
| **MAJOR** `v1.2.3 → v2.0.0` | Something a person or another program must know about | A LabCore table shape change, a config key renamed, an API route removed, a change to what a reviewer's click does |

Two extra rules that matter more than the letter of semver, because these are
lab tools that deploy themselves:

- **Bump MAJOR for anything a reviewer would be surprised by**, even if it is
  technically a minor feature. The number is how a human decides whether to
  read the release notes before the lab hits it.
- **If a change touches how results are calculated, recorded, or displayed,
  treat it as MAJOR** regardless of size. Those are the changes an idle-gated
  health check cannot catch (§5).

The two repos version independently. `coa-reviewer` at `v1.0.6` and
`lab-equipment-manager` at `v1.0.6` is coincidence, not coupling.

## 3. Cutting a release

From a clean checkout on `main`, with the full suite passing:

```bash
python -m pytest -q                 # must be green (see CLAUDE.md for known env failures)
git tag -a v1.2.3 -m "One line saying what changed and why"
git push origin v1.2.3
```

That is the whole thing. Do **not**:

- create a `VERSION` file by hand — CI writes it, and it is gitignored
- edit a release directory on the server — releases are immutable
- push the tag before the commit it points at (`git push origin main` first)

Then confirm CI built it:

```bash
gh run list --workflow=release.yml --limit 1        # expect: completed  success
gh release view v1.2.3                              # expect: 2 assets, .zip and .zip.sha256
```

## 4. Confirming it reached the lab

On ASAPSV1 (or via the **Lab Apps Status** desktop shortcut):

```
python C:\ASAPApps\updater\updater.py status --config C:\ASAPApps\updater\config.json
```

```
coa: SERVING on 5559  current=v1.2.3  junction->v1.2.3  staged=v1.2.3 healthy=True
```

`current` is what is running. `staged` ahead of `current` means it is built and
waiting for the app to go quiet. `C:\ASAPApps\updater\updater.log` says why in
plain words, e.g. `holding v1.2.3 back: 2 active session(s)`.

Expect up to ~5 minutes for the poll, plus however long the app stays busy.

## 5. ⚠ What deploying automatically does and does not protect you from

A release **is** blocked from ever going live if it fails to start, crashes on
import, has a bad checksum, or cannot build its venv. A release that goes live
and then fails to answer `/healthz` is **rolled back automatically**.

A release that **starts perfectly and renders something wrong** is not caught by
anything. It reaches reviewers unattended, and the first person to notice will
be whoever is using it. `/healthz` proves an app is alive, never that it is
correct.

So: if a change could produce a wrong number on a COA, do not rely on the
pipeline to catch it. Either test it properly first, or use the prerelease
escape hatch below.

**Rolling back** (immediate, one command):

```
python C:\ASAPApps\updater\updater.py rollback --app coa
```

**Publishing without deploying** — mark the GitHub release as a prerelease.
`/releases/latest` skips prereleases, so the updater never sees it:

```bash
gh release edit v1.2.3 --prerelease          # updater ignores it
gh release edit v1.2.3 --prerelease=false    # hand it over when ready
```

Two things about that, both learned the hard way:

- **It takes ~20 seconds to take effect.** Flip the flag and query
  `/releases/latest` immediately and you will still see the old answer and
  conclude it does not work. It does; wait.
- **Only use it on a release that is not yet deployed.** Marking the
  *currently running* release as a prerelease moves "latest" **backwards** to
  the previous tag — the updater will stage that older release and, because
  staged ≠ current, try to deploy it. That is a silent downgrade. The updater
  now refuses (`staged release 'v1.0.5' is no longer the latest`), but do not
  rely on that as a workflow: if you want to pull a bad release, use
  `rollback`, not the prerelease flag.

Every command and flag the updater accepts is catalogued in
[`docs/UPDATER-FLAGS.md`](docs/UPDATER-FLAGS.md).

**Stopping deploys entirely** while you work:

```
python C:\ASAPApps\updater\updater.py pause  --app coa
python C:\ASAPApps\updater\updater.py resume --app coa
```

## 6. Gotchas that will bite an agent

- **The updater tracks whatever GitHub calls *latest*, not the highest
  version.** The comparison is equality (`differs_from`), not ordering, and
  GitHub's "latest" is the most recently *created* non-prerelease tag. So
  publishing `v1.0.9` after `v1.2.0` **deploys `v1.0.9`**. That is deliberate —
  it is how you re-release a known-good older build to undo a bad one — but it
  means a mistyped tag ships. Check `gh release list` before assuming.
- **The tag becomes a directory name** (`releases\v1.2.3\`). Keep tags to
  letters, digits, dots and hyphens. A `/` in a tag breaks the unpack.
- **Only the tag decides the version.** `VERSION` is written from
  `github.ref_name`; the commit, the branch and the release title are ignored.
  A release created by hand in the GitHub UI, with no tag push, produces no
  `VERSION` and will not deploy correctly.
- **Retention keeps 5 releases plus `current` and its rollback target**, so
  seeing 6–7 directories is correct, not a leak. Protecting an old release does
  not evict a recent one.
- **`/healthz` must keep reporting `active_sessions` and `idle_seconds`.** If a
  release stops reporting them the updater refuses to deploy it unattended and
  says so — it will not guess that nobody is there.
- **A release that only changes docs still deploys**, restarting the app. That
  is usually fine, but it is not a no-op.

## 7. If something goes wrong

| Symptom | Where to look |
|---|---|
| Tag pushed, no release | `gh run list --workflow=release.yml` — the workflow fails the build if state files leaked into the archive |
| Release exists, never staged | `updater.log` — checksum mismatch, or the repo/tag is unreachable |
| Staged but never deployed | `updater.py status`, then `updater.log` — it prints the exact reason it is holding back |
| Deployed and broken | `updater.py rollback --app <name>`, then investigate `C:\ASAPApps\<app>\data\app.log` |
| App keeps restarting | `updater.log` — after 3 starts in 15 min the supervisor gives up and logs CRITICAL rather than hiding a crashloop |
