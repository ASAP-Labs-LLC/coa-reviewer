"""QBench OAuth2 API client – merged for COA Reviewer Web App.

Combines functionality from both the COA Reviewer and Past Data Manager clients:
- OAuth2 JWT Bearer token authentication
- Rate limiting (270 calls/min default — below QBench's real 300/60s ceiling)
- All sample, test, attachment, comment, and order endpoints
- Test result updating (PATCH /tests)
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import deque
from datetime import date
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Sequence

import jwt
import requests
import requests.exceptions
from requests.adapters import HTTPAdapter

CLIENT_ID = "75e7e304-792f-47f0-ace0-831b11bf1774"
CLIENT_SECRET = "***PURGED-QBENCH-SECRET***"
TOKEN_URL = "https://asaplabs.qbench.net/qbench/oauth2/v1/token"
API_BASE_URL = "https://asaplabs.qbench.net/qbench/api/v2"
DEFAULT_TIMEOUT = int(os.getenv("QBENCH_TIMEOUT_SECONDS", "30"))
# QBench enforces 300 requests / 60 seconds (confirmed verbatim in every 429
# body). The local cap MUST sit below that ceiling or the limiter cannot
# prevent 429s. 270 leaves headroom for clock jitter and the rolling window.
MAX_CALLS_PER_MINUTE = int(os.getenv("QBENCH_MAX_CALLS_PER_MIN", "270"))
# Cap the TOTAL time one request may spend sleeping on 429 backoff. Preview
# work runs in a small bounded pool, so an unbounded per-request backoff could
# park every worker for minutes during a throttle episode. Give up past this
# and let the caller's own retry/regenerate path handle it.
MAX_TOTAL_BACKOFF_SECONDS = float(os.getenv("QBENCH_MAX_TOTAL_BACKOFF", "25"))

logger = logging.getLogger(__name__)


class QBenchAPIError(RuntimeError):
    pass


def _parse_retry_after(response) -> Optional[float]:
    """Extract how long to wait before retrying a 429, in seconds.

    Prefers the standard ``Retry-After`` header, then falls back to QBench's
    body hint ("…Retry in N seconds"). Returns ``None`` when no hint is found
    so the caller can use its own backoff.
    """
    try:
        ra = response.headers.get("Retry-After")
    except Exception:
        ra = None
    if ra:
        try:
            return float(str(ra).strip())
        except (TypeError, ValueError):
            pass
    try:
        body = response.text or ""
    except Exception:
        body = ""
    m = re.search(r"retry in\s+(\d+(?:\.\d+)?)\s*second", body, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None


class RateLimiter:
    def __init__(self, max_calls: int, period_seconds: float) -> None:
        self.max_calls = max_calls
        self.period = period_seconds
        self.calls: deque = deque()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                while self.calls and now - self.calls[0] > self.period:
                    self.calls.popleft()
                if len(self.calls) < self.max_calls:
                    self.calls.append(now)
                    return
                wait_seconds = self.period - (now - self.calls[0]) + 0.01
            time.sleep(max(0.01, min(wait_seconds, 0.25)))


GLOBAL_RATE_LIMITER = RateLimiter(MAX_CALLS_PER_MINUTE, 60.0)


class QBenchAPIClient:
    def __init__(
        self,
        client_id: str = CLIENT_ID,
        client_secret: str = CLIENT_SECRET,
        token_url: str = TOKEN_URL,
        api_base_url: str = API_BASE_URL,
        timeout: int = DEFAULT_TIMEOUT,
        max_calls_per_minute: int = MAX_CALLS_PER_MINUTE,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_url = token_url
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout = timeout
        self._access_token: Optional[str] = None
        self._token_expires_at: int = 0
        self.api_calls: int = 0
        self._token_lock = threading.Lock()
        self.time_offset: float = 0.0

        adapter = HTTPAdapter(pool_connections=12, pool_maxsize=32)
        self.session = requests.Session()
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        if max_calls_per_minute == MAX_CALLS_PER_MINUTE:
            self.rate_limiter = GLOBAL_RATE_LIMITER
        else:
            self.rate_limiter = RateLimiter(max_calls_per_minute, 60.0)

    def _generate_jwt(self) -> str:
        now = int(time.time() + self.time_offset)
        claims = {"sub": self.client_id, "iat": now, "exp": now + 3600}
        token = jwt.encode(claims, self.client_secret, algorithm="HS256")
        if isinstance(token, bytes):
            token = token.decode("utf-8")
        return token

    def get_access_token(self, *, force: bool = False) -> str:
        now = int(time.time() + self.time_offset)
        with self._token_lock:
            if not force and self._access_token and now < (self._token_expires_at - 15):
                return self._access_token
            data = {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": self._generate_jwt(),
            }
            self.rate_limiter.acquire()
            try:
                response = self.session.post(self.token_url, data=data, timeout=self.timeout)
                self.api_calls += 1
                response.raise_for_status()
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 400:
                    server_date = e.response.headers.get("Date")
                    if server_date:
                        try:
                            server_ts = parsedate_to_datetime(server_date).timestamp()
                            self.time_offset = (server_ts - time.time()) - 10
                            logger.warning("Clock skew detected, offset adjusted to %.2fs", self.time_offset)
                            data["assertion"] = self._generate_jwt()
                            self.rate_limiter.acquire()
                            response = self.session.post(self.token_url, data=data, timeout=self.timeout)
                            self.api_calls += 1
                            response.raise_for_status()
                        except Exception:
                            raise e
                    else:
                        raise e
                else:
                    raise e
            payload = response.json()
            token = payload.get("access_token")
            if not token:
                raise QBenchAPIError(f"Token response missing access_token: {payload}")
            self._access_token = token
            self._token_expires_at = int(time.time() + self.time_offset) + int(payload.get("expires_in", 3600))
            return token

    def _auth_headers(self, *, force: bool = False) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.get_access_token(force=force)}"}

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        params_list: Optional[Sequence[tuple]] = None,
        json_body: Optional[Any] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> requests.Response:
        url = f"{self.api_base_url}/{path.lstrip('/')}"
        headers = self._auth_headers()
        if extra_headers:
            headers.update(extra_headers)
        query_params = params_list if params_list is not None else params

        max_retries = 6
        attempt = 0
        force_refresh = False
        total_slept = 0.0

        while True:
            self.rate_limiter.acquire()
            response = self.session.request(
                method, url,
                headers=headers,
                params=query_params,
                json=json_body,
                timeout=timeout or self.timeout,
            )
            self.api_calls += 1

            if response.status_code == 401 and not force_refresh:
                headers = self._auth_headers(force=True)
                force_refresh = True
                attempt += 1
                continue

            if response.status_code == 429 and attempt < max_retries:
                hint = _parse_retry_after(response)
                if hint is not None:
                    wait = min(30.0, max(1.0, hint))
                else:
                    wait = min(30.0, float(2 ** attempt))
                if total_slept + wait <= MAX_TOTAL_BACKOFF_SECONDS:
                    logger.warning("Rate limit hit at %s, retrying in %ss", path, wait)
                    time.sleep(wait)
                    total_slept += wait
                    attempt += 1
                    continue
                logger.warning("Giving up on 429 at %s after %.0fs total backoff", path, total_slept)
                # fall through to raise so the caller can retry later

            if response.status_code >= 400:
                raise QBenchAPIError(f"{response.status_code} error at {path}: {response.text}")

            return response

    # ── Sample endpoints ───────────────────────────────────────────────────

    def fetch_sample(self, sample_id: int) -> Dict[str, Any]:
        payload = self.request("GET", f"/samples/{sample_id}").json()
        return payload.get("data", payload)

    def fetch_samples_by_lab_id_prefix(self, prefix: str, *, page_size: int = 50) -> List[Dict[str, Any]]:
        if not prefix:
            return []
        params: Dict[str, Any] = {
            "lab_id": prefix.strip(),
            "page_size": min(max(page_size, 1), 50),
            "page_num": 1,
        }
        results: List[Dict[str, Any]] = []
        while True:
            payload = self.request("GET", "/samples", params=params).json()
            for s in payload.get("data", []):
                lab_id = s.get("lab_id") or ""
                if lab_id.startswith(prefix):
                    results.append(s)
            if params["page_num"] >= int(payload.get("total_pages", 1) or 1):
                break
            params["page_num"] += 1
        return results

    def fetch_samples_by_lab_id(self, lab_id: str, *, page_size: int = 50) -> List[Dict[str, Any]]:
        if not lab_id:
            return []
        params: Dict[str, Any] = {
            "lab_id": lab_id.strip(),
            "page_size": min(max(page_size, 1), 50),
            "page_num": 1,
        }
        results: List[Dict[str, Any]] = []
        while True:
            payload = self.request("GET", "/samples", params=params).json()
            results.extend(payload.get("data", []))
            if params["page_num"] >= int(payload.get("total_pages", 1) or 1):
                break
            params["page_num"] += 1
        return results

    # ── Test endpoints ─────────────────────────────────────────────────────

    def fetch_tests_for_sample_ids(self, sample_ids: Sequence[int], *, page_size: int = 50) -> List[Dict[str, Any]]:
        ids = [int(x) for x in sample_ids if str(x).strip()]
        if not ids:
            return []
        page_size = min(max(page_size, 1), 50)
        results: List[Dict[str, Any]] = []
        page = 1
        while True:
            params_list = [("page_num", page), ("page_size", page_size)]
            for sid in ids:
                params_list.append(("sample_ids", sid))
            payload = self.request("GET", "/tests", params_list=params_list).json()
            results.extend(payload.get("data", []))
            if page >= int(payload.get("total_pages", 1) or 1):
                break
            page += 1
        return results

    def fetch_tests_by_start_date(self, start_date: date, *, page_size: int = 50) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {
            "page_size": min(max(page_size, 1), 50),
            "start_date_start": start_date.strftime("%m/%d/%Y"),
            "start_date_end": start_date.strftime("%m/%d/%Y"),
        }
        results: List[Dict[str, Any]] = []
        page = 1
        while True:
            params["page_num"] = page
            payload = self.request("GET", "/tests", params=params).json()
            results.extend(payload.get("data", []))
            if page >= int(payload.get("total_pages", 1) or 1):
                break
            page += 1
        return results

    def update_test_result(self, test_id: int, value: str) -> Dict[str, Any]:
        """Update a single test result in QBench."""
        body = [{"id": test_id, "results": value}]
        return self.request("PATCH", "/tests", json_body=body).json()

    # ── Attachment endpoints ───────────────────────────────────────────────

    def fetch_attachments_for_sample(self, sample_id: int) -> List[Dict[str, Any]]:
        try:
            payload = self.request("GET", f"/samples/{sample_id}/attachments").json()
            rows = payload.get("data", payload) if isinstance(payload, dict) else payload
            if not isinstance(rows, list):
                return []
            include_fields = ("attach_to_report", "include_in_report", "include_on_coa", "print_with_report")
            return [r for r in rows if any(r.get(f) for f in include_fields)]
        except Exception as exc:
            logger.warning("Could not fetch attachments for sample %s: %s", sample_id, exc)
            return []

    def fetch_all_attachments_for_sample(self, sample_id: int) -> List[Dict[str, Any]]:
        try:
            payload = self.request("GET", f"/samples/{sample_id}/attachments").json()
            rows = payload.get("data", payload) if isinstance(payload, dict) else payload
            return rows if isinstance(rows, list) else []
        except Exception as exc:
            logger.warning("Could not fetch attachments for sample %s: %s", sample_id, exc)
            return []

    def delete_attachment(self, attachment_id: int) -> bool:
        try:
            self.request("DELETE", f"/attachments/{attachment_id}")
            return True
        except Exception as exc:
            logger.warning("Could not delete attachment %s: %s", attachment_id, exc)
            return False

    # ── Comment / order endpoints ──────────────────────────────────────────

    def update_sample(self, sample_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
        """Update one or more sample fields via PATCH /samples.

        Used by the Info-mode editor where the reviewer edits sample-level
        fields (sample_type, matrix, batch, source, description, …). The
        caller is responsible for whitelisting which keys to send.
        """
        payload = {"id": int(sample_id), **dict(fields)}
        return self.request("PATCH", "/samples", json_body=[payload]).json()

    def update_sample_comments(self, sample_id: int, comments: str) -> Dict[str, Any]:
        """Update the comments field on a sample."""
        body = [{"id": sample_id, "comments": comments}]
        return self.request("PATCH", "/samples", json_body=body).json()

    def fetch_order(self, order_id: int) -> Dict[str, Any]:
        """Fetch a single order by ID."""
        payload = self.request("GET", f"/orders/{order_id}").json()
        return payload.get("data", payload)

    def fetch_order_attachments(self, order_id: int) -> List[Dict[str, Any]]:
        """Fetch all attachments at the order level."""
        try:
            payload = self.request("GET", f"/orders/{order_id}/attachments").json()
            rows = payload.get("data", payload) if isinstance(payload, dict) else payload
            return rows if isinstance(rows, list) else []
        except Exception as exc:
            logger.warning("Could not fetch attachments for order %s: %s", order_id, exc)
            return []

    def fetch_order_samples(self, order_id: int, *, page_size: int = 50) -> List[Dict[str, Any]]:
        """Fetch all samples belonging to an order."""
        results: List[Dict[str, Any]] = []
        page = 1
        while True:
            params = {"page_size": min(max(page_size, 1), 50), "page_num": page}
            payload = self.request("GET", f"/orders/{order_id}/samples", params=params).json()
            results.extend(payload.get("data", []))
            if page >= int(payload.get("total_pages", 1) or 1):
                break
            page += 1
        return results

    def fetch_order_comments(self, order_id: int) -> List[Dict[str, Any]]:
        try:
            payload = self.request("GET", f"/orders/{order_id}/comments").json()
            rows = payload.get("data", payload) if isinstance(payload, dict) else payload
            return rows if isinstance(rows, list) else []
        except Exception as exc:
            logger.warning("Could not fetch comments for order %s: %s", order_id, exc)
            return []
