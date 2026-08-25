# Updater CLI reference

Every command and flag `deploy/updater/updater.py` accepts, what it actually
does, and what it deliberately refuses to do.

`RELEASING.md` is the narrative: how a tag becomes a running app. This is the
reference. `tests/test_updater_flags_documented.py` fails if a command or flag
exists in `updater.py` but is missing here, so the two cannot drift apart.

Invocation on the lab server (ASAPSV1):

```
python C:\ASAPApps\updater\updater.py <command> [--config PATH] [--app NAME] [--tag TAG] [--force] [--verbose]
```

---

## Commands

### `run`

The daemon. This is what the scheduled task runs and the only long-lived mode.

It interleaves two independent timers, deliberately decoupled: a **release
check** every `poll_seconds` (default **300s**) and a **supervision** pass every
`supervise_seconds` (default **20s**). Supervision is a local port probe that
costs nothing, so noticing a dead app does not have to wait on a GitHub call.
Supervision runs first each tick — keeping the lab's apps up outranks checking
for releases.

### `poll`

One release check for the selected apps, then exit. The same work `run` does on
its release timer. Use it to force an immediate check instead of waiting up to
five minutes.

### `status`

Prints one line per app and exits. Nothing is changed.

```
coa: SERVING on 5559  current=v1.2.3  junction->v1.2.3  staged=v1.2.3 healthy=True
```

- `SERVING on <port>` / `DOWN (port <port>)` — whether anything is listening.
  `no port configured` if the app has no port in config.
- `[PAUSED]` — a `paused` marker exists in the app's data dir.
- `current` — the `VERSION` the running release reports.
- `junction->` — what the `current` junction actually points at.
- `staged` / `healthy` — the release built and waiting, and whether it passed
  its health check. `notes:` on the next line says why it failed.

`staged` ahead of `current` is normal: it means the release is built and
waiting for the app to go quiet.

### `start`

Starts and supervises the selected apps now. **Refuses if the app is paused** —
run `resume` first. Prints the supervisor's result per app.

### `switch`

Repoints an app at a specific release. **Requires `--tag`**; exits `2` without
it.

Guarded by `may_switch`, which refuses when:

- nothing is staged;
- the staged tag is not the tag you asked for — in case a newer release was
  staged since you looked;
- the staged release failed its health check.

`--force` skips *that* guard only. After the switch the health check and the
automatic rollback still run, and are not bypassable.

### `rollback`

Switches to the most recent release that is not the current one. This is the
immediate undo for a bad deploy, and the counterweight to unattended deploys —
an idle-gated health check proves an app starts, never that it renders a COA
correctly.

Internally a forced switch, so the staged/healthy guard does not block it.
Fails and logs if there is no other release to fall back to.

Prefer this over the prerelease flag for pulling a bad release. Marking the
*running* release as a prerelease moves GitHub's "latest" backwards and invites
a silent downgrade.

### `pause`

Writes a `paused` marker into the app's data dir. The updater will not start,
switch, or restart a paused app.

**Pause does not stop a running app.** If it is up, it stays up — stop it
yourself. Pause prevents the updater from acting, nothing more.

### `resume`

Removes the `paused` marker. The app is started within one supervision pass.

---

## Flags

### `--config` PATH

Path to the updater's JSON config. Defaults to `config.json` **next to
`updater.py`**, not the working directory. Exits `2` if the file does not
exist. See `deploy/updater/config.example.json`.

### `--app` NAME

Restricts the command to one app by its config name (`coa`,
`lem`). Omit it and the command applies to **every** configured app — which for
`switch`, `rollback`, `pause` and `start` means all of them at once. Exits `2`
if no configured app has that name.

### `--tag` TAG

The release tag to act on. Required by `switch`; ignored by every other
command. The tag is also the release directory name, so keep tags to letters,
digits, dots and hyphens — a `/` breaks the unpack.

### `--force`

Only meaningful with `switch`. Skips the staged-and-healthy guard and logs a
warning saying so. The post-switch health check and automatic rollback are
**not** bypassed by it.

### `--verbose`

Turns up log verbosity. Logging goes to the configured `log_file`
(`updater.log`) with propagation disabled, so a deploy is not logged twice.

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | The operation ran and failed (a `switch` or `rollback` that did not take) |
| `2` | Usage error — config file missing, unknown `--app`, or `switch` without `--tag` |

## Notes that bite

- **No credential is needed for the normal path** — both repos are public. The
  updater reads a GitHub token from the credential store named by
  `credential_target` (default `asaplabs-github`) only to raise the rate limit.
  Without it, it polls anonymously at 60 requests/hour and warns.
- **Idleness is configurable per app.** `min_idle_seconds` defaults to **600**.
  A staged release waits for real idleness before deploying itself.
- **`/healthz` must keep reporting `active_sessions` and `idle_seconds`.**
  Without them the updater will not deploy unattended — it refuses to guess
  that nobody is there.
