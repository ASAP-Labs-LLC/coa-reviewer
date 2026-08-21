"""HTTP client for LabCore's Command Center API.

Command Center lives inside **LabCore** (`apps/LabCore/src/LabCore.py` in the
LabLink repo), not LabStation. Its board is a tab in the LabVision web UI that
LabCore serves; the tables (`cc_tasks`, `cc_task_samples`) are in LabCore's own
database.

Two things shape this client:

* **Writes go through `/api/queue/write`,** LabCore's explicitly
  unauthenticated "any LabLink program can POST here" gateway, which dispatches
  named operations (`cc_create_task`, `cc_complete_task`) onto its serialized
  write queue. The alternative, `POST /api/cc/tasks`, requires a LabVision
  bearer token that a headless reviewer server has no way to obtain.
* **Reads are public** (`/api/cc/tasks`, `/api/cc/samples/search`, …), so they
  are plain GETs.

Timeouts are deliberately short: every one of these calls lands on a Flask
request thread, so a stalled LabCore should surface as a quick error rather
than a hung page. Idempotent reads get a bounded retry; writes get none — they
are not idempotent by nature, which is what `op_id` is for.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://labvision.asaplabs.net"

# Timeouts assume a real network hop, not loopback: LabCore is reached over
# the internet through Cloudflare, so a 2-second probe would report a healthy
# service as down on any slow link.
WRITE_TIMEOUT = 20      # POST /api/queue/write
READ_TIMEOUT = 12       # GET /api/cc/*
STATUS_TIMEOUT = 6      # is_available() probe

READ_RETRIES = 2
READ_RETRY_BACKOFF = 0.3

_TRANSIENT = (requests.ConnectionError, requests.Timeout)


class LabCoreUnavailable(RuntimeError):
    """LabCore could not be reached, or answered with a transport-level error.

    Raised rather than returning a falsy value on purpose: a bad sample that
    silently fails to reach the Command Center is worse than one that visibly
    refuses to be filed.
    """


class LabCoreClient:
    """Thin wrapper around the Command Center endpoints LabCore exposes."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL,
                 source: str = "COAReviewer") -> None:
        self._base_url = self._normalize(base_url)
        self._source = source
        # Last observed reachability, maintained as a side effect of real
        # traffic so ``/healthz`` can report it without making a call of its
        # own. ``None`` means nothing has been attempted yet — deliberately not
        # ``True``, because claiming a service is reachable before ever having
        # reached it is the kind of health check that reports green through an
        # outage.
        self._last_reachable: Optional[bool] = None

    @property
    def last_reachable(self) -> Optional[bool]:
        """``True``/``False`` from the last attempt, ``None`` if never tried."""
        return self._last_reachable

    def _mark_reachable(self, ok: bool) -> None:
        self._last_reachable = ok

    @staticmethod
    def _normalize(base_url: str) -> str:
        """Clean a configured base URL into something requests can use.

        Takes a whole URL rather than host/port because LabCore is served at
        https://labvision.asaplabs.net behind Cloudflare on 443 — a
        ``http://{host}:{port}`` template cannot express that, and would have
        produced a wrong URL for every real call.

        A bare hostname is assumed to be HTTPS: defaulting to plaintext would
        silently send credentials-bearing writes to a service that only
        answers on 443, and the failure would look like "LabCore is down".
        """
        url = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        if not url:
            url = DEFAULT_BASE_URL
        if "://" not in url:
            url = f"https://{url}"
        return url

    # ── plumbing ─────────────────────────────────────────────────────────

    @property
    def base_url(self) -> str:
        return self._base_url

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None,
             timeout: float = READ_TIMEOUT) -> Any:
        """GET a public read endpoint, with bounded retry on transient errors."""
        url = f"{self.base_url}{path}"
        last: Optional[BaseException] = None
        for attempt in range(READ_RETRIES):
            try:
                resp = requests.get(url, params=params, timeout=timeout)
                resp.raise_for_status()
                self._mark_reachable(True)
                return resp.json()
            except _TRANSIENT as exc:
                last = exc
                if attempt < READ_RETRIES - 1:
                    time.sleep(READ_RETRY_BACKOFF * (attempt + 1))
            except requests.RequestException as exc:
                self._mark_reachable(False)
                raise LabCoreUnavailable(f"LabCore GET {path} failed: {exc}") from exc
        self._mark_reachable(False)
        raise LabCoreUnavailable(f"LabCore unreachable at {self.base_url}: {last}")

    def _write(self, operation: str, params: Dict[str, Any],
               op_id: Optional[str] = None) -> Dict[str, Any]:
        """POST one named operation to LabCore's write gateway.

        ``op_id`` makes the call safe to retry: LabCore short-circuits a
        repeated op_id to the result it already recorded instead of performing
        the write twice. Without it a retried create would produce a second
        listing for the same sample.
        """
        body = {
            "operation": operation,
            "params": params,
            "source": self._source,
            "op_id": op_id or uuid.uuid4().hex,
        }
        try:
            resp = requests.post(
                f"{self.base_url}/api/queue/write", json=body, timeout=WRITE_TIMEOUT,
            )
        except requests.RequestException as exc:
            self._mark_reachable(False)
            raise LabCoreUnavailable(
                f"LabCore unreachable at {self.base_url}: {exc}"
            ) from exc
        self._mark_reachable(True)
        try:
            return resp.json()
        except ValueError as exc:
            raise LabCoreUnavailable(
                f"LabCore returned a non-JSON response to {operation} "
                f"(HTTP {resp.status_code})"
            ) from exc

    # ── availability ─────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Cheap liveness probe. Reports; never raises — it drives a banner."""
        try:
            resp = requests.get(
                f"{self.base_url}/api/queue/status", timeout=STATUS_TIMEOUT,
            )
            ok = resp.status_code == 200
            self._mark_reachable(ok)
            return ok
        except requests.RequestException:
            self._mark_reachable(False)
            return False

    # ── authentication ───────────────────────────────────────────────────

    def authenticate_card(self, code: str) -> Optional[str]:
        """Resolve a scanned keycard code to a LabLink username.

        LabLink's NFC readers are keyboard wedges: they type the card's code
        and press Enter. LabCore's ``/api/login`` treats a registered code in
        *either* field as a login, so the code goes in as the password (the
        field a wedge normally lands in) and LabCore returns the canonical
        account name.

        Returns None for a card LabCore doesn't recognise. Raises
        ``LabCoreUnavailable`` if LabCore can't be reached — the caller must
        be able to tell "this card isn't registered" from "we couldn't ask",
        because a card that silently does nothing is the worst outcome for
        someone standing at a terminal.
        """
        code = (code or "").strip()
        if not code:
            return None
        return self._login(code, code)

    def authenticate_user(self, username: str, password: str) -> Optional[str]:
        """Resolve a LabLink username and password to the canonical account.

        The alternative to tapping a card, not a separate identity system:
        it hits the same ``/api/login`` endpoint and yields the same thing —
        the LabLink account name that becomes ``created_by``/``completed_by``
        and the audit-log identity. LabCore resolves the stored casing, so a
        reviewer who types ``ryan c`` is still recorded as ``Ryan C``.

        Returns None for bad credentials. Raises ``LabCoreUnavailable`` if
        LabCore can't be reached, because "wrong password" and "we couldn't
        ask" send whoever is standing at the terminal different places.
        """
        username = (username or "").strip()
        if not username or not password:
            # LabCore 400s on a blank field; there is nothing to ask about.
            return None
        return self._login(username, password)

    def _login(self, username: str, password: str) -> Optional[str]:
        """POST /api/login and return the canonical username, or None."""
        try:
            resp = requests.post(
                f"{self.base_url}/api/login",
                json={"username": username, "password": password},
                timeout=WRITE_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise LabCoreUnavailable(
                f"LabCore unreachable at {self.base_url}: {exc}"
            ) from exc

        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        return data.get("username") or None

    # ── writes ───────────────────────────────────────────────────────────

    def create_task(self, params: Dict[str, Any],
                    op_id: Optional[str] = None) -> Dict[str, Any]:
        """Create a Command Center listing.

        Returns LabCore's response unchanged. That may be
        ``{"ok": True, "task_id": N}`` or, when an active listing already
        covers one of the lab_ids and ``force_create`` was not set,
        ``{"conflict": True, "existing_tasks": [...]}`` — the caller needs
        those listings to offer add/create-anyway/cancel, so nothing is
        swallowed or reshaped here.
        """
        return self._write("cc_create_task", params, op_id=op_id)

    def complete_task(self, task_id: int, notes: str,
                      completed_by: str = "",
                      op_id: Optional[str] = None) -> Dict[str, Any]:
        """Complete a listing. ``notes`` is required by LabCore, so an empty
        one is rejected here rather than round-tripped for a guaranteed error.
        """
        notes = (notes or "").strip()
        if not notes:
            raise ValueError("Completion notes are required to complete a listing.")
        return self._write(
            "cc_complete_task",
            {
                "task_id": int(task_id),
                "completion_notes": notes,
                "completed_by": completed_by,
            },
            op_id=op_id,
        )

    # ── reads ────────────────────────────────────────────────────────────

    def active_tasks(self) -> List[Dict[str, Any]]:
        """Every listing that is not completed, newest first."""
        data = self._get("/api/cc/tasks", params={"view": "active"})
        return data if isinstance(data, list) else []

    def check_duplicate(self, lab_ids: List[str]) -> Dict[str, Any]:
        """Active listings already covering any of ``lab_ids``."""
        clean = [str(x).strip() for x in lab_ids if str(x).strip()]
        if not clean:
            return {"conflict": False, "existing_tasks": []}
        data = self._get("/api/cc/tasks/check-duplicate", params={"lab_id": clean})
        return data if isinstance(data, dict) else {"conflict": False, "existing_tasks": []}

    def sample_info(self, lab_id: str) -> Dict[str, Any]:
        """LabCore's record for one sample: ``customer_name`` and ``fuel_type``.

        The underlying search is a LIKE, so querying ``073126-41552`` also
        matches ``073126-415520``. Only an exact lab_id match is returned —
        autofilling the wrong customer onto a listing is worse than
        autofilling nothing.
        """
        lab_id = str(lab_id).strip()
        if not lab_id:
            return {}
        rows = self._get("/api/cc/samples/search", params={"q": lab_id})
        if not isinstance(rows, list):
            return {}
        for row in rows:
            if str(row.get("lab_id", "")).strip() == lab_id:
                return row
        return {}

    def sample_data(self, lab_id: str) -> Dict[str, Any]:
        """LabCore's full record for one sample.

        Returns the sample-information fields (customer, fuel type, work
        order, collection details, …) plus a ``tests`` list. Only the
        sample-level fields are used by the sync — test results are
        deliberately out of scope.
        """
        lab_id = str(lab_id).strip()
        if not lab_id:
            return {}
        data = self._get("/api/sample", params={"id": lab_id})
        return data if isinstance(data, dict) else {}

    def customers(self) -> List[str]:
        """Customer names, for the listing form's datalist."""
        data = self._get("/api/cc/customers")
        return data if isinstance(data, list) else []
