# LabLink username/password login — design

**Date:** 2026-08-03
**Status:** Implemented

## Deviations from the approved design

None. Written after implementation from the design approved in conversation;
what shipped matches it.

Replace COA Reviewer's shared portal password and typed "Your Name" box with
the reviewer's own LabLink account, so that password login and keycard login
produce the same identity.

## Background

Keycard login (2026-07-31) made the card the whole login: `authenticate_card`
posts the scanned code to LabCore's `POST /api/login`, and the LabLink account
name LabCore returns becomes the session identity, `created_by` /
`completed_by` on Command Center listings, and the audit-log user.

The password path was left behind. It still checked two hardcoded constants in
`app.py` — `APP_LOGIN_USERNAME = "Administrator"` and
`APP_LOGIN_PASSWORD = "A$aprocks!1"` — and then asked for a free-text name. So
one login was a real account and the other was a shared secret plus a
self-declared string: anyone who knew the password could file work under any
name, and the audit trail recorded whatever was typed.

Verified facts the design depends on, read from LabCore's `_handle_login`
(`apps/LabCore/src/LabCore.py:11213`, working checkout at
`/Volumes/Labsharedrive/Ryan C/LabLink`):

- The endpoint already serves both proofs. It tries the NFC lookup on the
  password field and then the username field; failing that, it authenticates
  the pair as real credentials. Card login needed no new endpoint, and neither
  does password login.
- On success it returns `{"token": ..., "username": ...}` where `username` is
  the **stored casing**, resolved by `_resolve_username`. A reviewer who types
  `ryan c` is recorded as `Ryan C`, so attribution is stable across logins.
- Bad credentials are a 401; a blank field is a 400.

## Decisions

**No local fallback credential.** The hardcoded pair is deleted outright
rather than kept as a break-glass path for when LabCore is unreachable. A
credential that still worked while LabCore was down would be exactly the
shared, unattributable login being removed — and a session without LabCore
cannot flag, re-review, or sync, so it could not do the job anyway. Login
answers 503 `labcore_down` and says so.

**The timeout overlay re-authenticates the session's owner only.** Restoring a
session hands back its records, results, and PDF cache. If any valid LabLink
credential unlocked it, a colleague tapping in at a shared terminal would
inherit the in-progress review and every listing filed afterwards would name
the wrong person.

## Design

### `labcore_client.py`

Add `authenticate_user(username, password) -> Optional[str]`, alongside
`authenticate_card`. Both delegate to a new private `_login(username,
password)` that owns the single `POST /api/login` call, so the two proofs
cannot drift into different request shapes or different error handling.

Contract, matching `authenticate_card` exactly:

| Outcome | Result |
| --- | --- |
| 200 | canonical username from LabCore |
| 401 | `None` |
| blank username or password | `None`, without calling LabCore |
| network failure | raises `LabCoreUnavailable` |

The raise is the point: "wrong password" and "we couldn't ask" send whoever is
standing at the terminal to different places, so they must not collapse into
one falsy return.

### `app.py`

Delete `APP_LOGIN_USERNAME` and `APP_LOGIN_PASSWORD`.

Add `_resolve_identity(body) -> (name, error_response)`, with exactly one of
the two set. It accepts either form the UI can produce — `{code}` or
`{username, password}` — and routes each to the matching client call. Both
login routes go through it, which is what keeps card identity and password
identity the same thing. It ignores any `name` in the body: who did the review
is LabCore's answer, not the client's claim.

`POST /api/portal-login` resolves the identity, then creates the `UserState`
under the returned account name. Unchanged otherwise — same session cookie,
same `log_login_event`, same `change_log.session(method="password")`.

`POST /api/portal-reauth` resolves the identity the same way, then:

1. If a live `UserState` exists for the session cookie and its name differs
   from the resolved account (compared case-insensitively, since LabCore
   returns canonical casing), return **403** with `wrong_user: true`.
2. If it matches, touch `last_active` and restore — `restored: true`.
3. If no `UserState` survives, there is nothing to match against, so create a
   fresh session under the resolved account — `restored: false`. Still
   LabCore's answer, never a client-supplied name.

### Frontend

`templates/index.html` — the password view drops `#portal-name` and relabels
its fields as LabLink credentials. The timeout overlay drops `#reauth-name`
and gains `#reauth-card-input`, a visually-hidden capture field reusing the
existing `.portal-card-input` style.

`static/js/app.js` — `handlePortalLogin` stops sending `name` and surfaces
`labcore_down` as its own message. `handleReauth` sends the signed-in
username with the typed password; a scan on the overlay sends `{code}`; both
call a shared `submitReauth(body)`. `setupReauthCard` / `refocusReauthCard`
keep the capture field focused whenever the overlay is up and the reviewer is
not typing a password, because a wedge reader types into whatever holds focus.

## Error handling

| Condition | Response | What the reviewer sees |
| --- | --- | --- |
| Bad username or password | 401 | "Invalid username or password." |
| Missing field | 400 | "Username and password are required." |
| Unregistered card | 401 | "Card not recognised." |
| LabCore unreachable | 503 `labcore_down` | "Can't reach LabCore to check your sign-in. Try again shortly." |
| Reauth by a different account | 403 `wrong_user` | "This session belongs to *N*. Sign out to switch accounts." |

## Testing

`tests/test_user_login.py`, written before the implementation. The client
tests stub `requests.post` for the response cases and point at a dead port
(via `free_port()`) for the unreachable case; the route tests use a
`MagicMock` LabCore, matching how `tests/test_card_login.py` already works.

Coverage: canonical casing wins over typed casing; no LabCore call on a blank
field; a supplied `name` cannot set the identity; the password never reaches
the audit log; the login is logged under the LabCore account with
`method="password"`; reauth by card and by password both restore the same
session; a different account gets 403 and leaves the existing `UserState`
untouched; a collected session yields a fresh one under the authenticated
account; and source-level guards that the hardcoded constants, `#portal-name`,
and `#reauth-name` stay gone.

Two existing tests posted the old hardcoded pair and were updated:
`tests/test_card_login.py::test_password_login_still_works` and
`tests/test_change_logging_wiring.py::test_login_and_logout_are_logged`.

## Known limitation

LabCore's `_handle_login` tries the NFC lookup on both fields before checking
passwords, so a username that happened to equal a registered card code would
sign in as that card's owner. Card codes are hex strings and usernames are
not, so it is not reachable in practice — and it is LabCore-side behavior,
not something COA Reviewer can fix from here.
