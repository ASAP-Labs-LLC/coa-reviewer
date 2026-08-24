#!/usr/bin/env python3
"""
COA Reviewer – Web Application
===============================
Flask-based web server that combines the COA Reviewer with inline test editing.
Serves on the local network so any machine can access it via browser.

Usage:
    python app.py
    -- then open http://<this-machine-ip>:5559 in any browser on the network --
"""

from __future__ import annotations

import atexit
import csv
import functools
import io
import json
import logging
import logging.handlers
import os
import queue
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests as _requests

from flask import Flask, Response, jsonify, render_template, request, send_file, session

from qbench_client import QBenchAPIClient, QBenchAPIError
from labcore_client import LabCoreClient, LabCoreUnavailable
from change_log import ChangeLog

# ── Playwright ──────────────────────────────────────────────────────────────
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    PLAYWRIGHT_AVAILABLE = True
    _playwright_error: Optional[str] = None
except Exception as _pw_exc:
    PLAYWRIGHT_AVAILABLE = False
    _playwright_error = str(_pw_exc)

# ── PyMuPDF ─────────────────────────────────────────────────────────────────
try:
    import fitz as _fitz
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False
    _fitz = None

# ── pyzbar (barcode detection for SIF page matching) ────────────────────────
try:
    from pyzbar import pyzbar as _pyzbar
    from PIL import Image as _PILImage
    PYZBAR_AVAILABLE = True
except Exception:
    # FileNotFoundError (libzbar-64.dll / libiconv.dll missing) or ImportError
    PYZBAR_AVAILABLE = False
    _pyzbar = None
    _PILImage = None

# ── Paths ───────────────────────────────────────────────────────────────────
# APP_DIR is where the *code* is; DATA_DIR is where everything this app writes
# lives. They used to be the same directory, which is exactly what stopped the
# app being deployable: a versioned release directory gets swapped underneath
# the process, and any state sitting inside it is destroyed with the old
# release. Under the deployment layout APP_DIR is
# ``C:\ASAPApps\coa\current`` (a junction onto an immutable release) while
# DATA_DIR is ``C:\ASAPApps\coa\data``, which deploys never touch.
#
# Resolution is deliberately import-time, not lazy: ARCHIVE_DIR.mkdir() and the
# rotating log handler below both run at module scope, so a DATA_DIR that only
# resolved on first use would already be too late.
APP_DIR = Path(__file__).resolve().parent


def _resolve_data_dir() -> Path:
    """Where this app keeps its state.

    ``COA_DATA_DIR`` when set, otherwise ``APP_DIR`` — so an existing
    shared-drive installation keeps behaving exactly as it did before.
    """
    raw = os.environ.get("COA_DATA_DIR", "").strip()
    return Path(raw).expanduser().resolve() if raw else APP_DIR


DATA_DIR = _resolve_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_FILE = DATA_DIR / "web_app_config.json"
CONFIG_TEMPLATE_FILE = APP_DIR / "web_app_config.default.json"
RE_REVIEW_STATE_FILE = DATA_DIR / "re_review_state.json"
ARCHIVE_DIR = DATA_DIR / "archive"
ARCHIVE_DIR.mkdir(exist_ok=True)


def _seed_config_if_absent() -> None:
    """Copy the shipped template into DATA_DIR on first boot — never over a
    real config. Overwriting here would cost the live QBench credentials, so
    the existence check is the whole point of the function.
    """
    if CONFIG_FILE.exists() or not CONFIG_TEMPLATE_FILE.is_file():
        return
    try:
        shutil.copyfile(CONFIG_TEMPLATE_FILE, CONFIG_FILE)
    except OSError:
        # A missing config is recoverable (load_config falls back to defaults);
        # failing to boot because the seed failed is not.
        pass


_seed_config_if_absent()

# ── Version stamp ───────────────────────────────────────────────────────────
# CI writes the release tag into a VERSION file next to the code, so the stamp
# travels with the release rather than with the state. The updater compares
# what /healthz reports against the tag it staged; if they don't match, the
# junction swap didn't take effect and it must roll back.
VERSION_FILE = APP_DIR / "VERSION"


def _read_version(path: Path | None = None) -> str:
    """The release tag, or ``"dev"`` for a working checkout.

    Never raises. A health check that 500s because VERSION is missing or
    corrupt would make a working release look broken and trigger a needless
    rollback — the exact failure this file exists to prevent.
    """
    target = VERSION_FILE if path is None else Path(path)
    try:
        stamp = target.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return "dev"
    return stamp.splitlines()[0].strip() if stamp else "dev"


APP_VERSION = _read_version()

QBENCH_BASE = "https://asaplabs.qbench.net"
REPORT_CONFIG_ID = "18"
REPORT_LEVEL = "sample"

# ── Portal Auth ─────────────────────────────────────────────────────────────
# Every way in resolves to a LabLink account through LabCore: tap a card, or
# type the same username and password. There is deliberately no local
# fallback credential — one that still worked while LabCore was down would be
# exactly the shared, unattributable login this replaced, and a session
# without LabCore can't flag, re-review, or sync anyway.
LOGIN_LOG_FILE = DATA_DIR / "login.log"
SESSION_TIMEOUT_SECONDS = 600   # 10 min – frontend timer threshold
SESSION_CLEANUP_SECONDS = 720   # 12 min – server-side cleanup (grace period for reauth)
AUTO_RESTART_HOUR = 3           # 3 AM – daily auto-restart target
AUTO_RESTART_IDLE_SECONDS = 300 # 5 min – must be idle this long before auto-restart
AUTO_RESTART_MIN_UPTIME_SECONDS = 3600  # 1 h – a just-respawned process must not re-restart (storm guard)


def _preview_workers() -> int:
    """Bounded worker count for preview generation.

    Preview POSTs serialize on the single COASession lock anyway; the parallel
    win is the per-sample attachment fetch + SIF work. Keeping this small
    bounds in-flight QBench calls (so we don't blow the rate limit) and caps
    thread/lock contention instead of spawning one OS thread per sample.
    """
    return min(8, max(4, os.cpu_count() or 4))


# Bounded pools replace the old thread-per-sample fan-out. PREVIEW_POOL runs
# generate_preview_for_sample; IO_POOL runs the downstream PDF-cache + SIF work.
PREVIEW_POOL = ThreadPoolExecutor(max_workers=_preview_workers(), thread_name_prefix="preview")
IO_POOL = ThreadPoolExecutor(max_workers=_preview_workers() * 2, thread_name_prefix="io")

# Stable secret key so cookies survive server restarts
_SECRET_KEY_FILE = DATA_DIR / ".secret_key"
if _SECRET_KEY_FILE.exists():
    APP_SECRET_KEY = _SECRET_KEY_FILE.read_text("utf-8").strip()
else:
    APP_SECRET_KEY = secrets.token_hex(32)
    _SECRET_KEY_FILE.write_text(APP_SECRET_KEY, encoding="utf-8")

# ── Logging with rotation ────────────────────────────────────────────────────
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_LOG_FILE = DATA_DIR / "app.log"
_log_formatter = logging.Formatter(_LOG_FORMAT)

# Rotating file handler: 2 MB per file, keep 5 backups
_file_handler = logging.handlers.RotatingFileHandler(
    _LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8",
)
_file_handler.setFormatter(_log_formatter)
_file_handler.setLevel(logging.INFO)

# Console handler (captured by Run.pyw into server.log)
_console_handler = logging.StreamHandler(sys.stderr)
_console_handler.setFormatter(_log_formatter)
_console_handler.setLevel(logging.INFO)

logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, handlers=[_file_handler, _console_handler])
logger = logging.getLogger(__name__)


def log_login_event(event: str, name: str, ip: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {event}: {name} from {ip}\n"
    try:
        with open(LOGIN_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        logger.warning("Failed to write login log: %s", e)


class PreviewAttachmentError(RuntimeError):
    """Raised when COA preview fails due to a broken/missing attachment on QBench's S3."""
    pass


class SessionExpiredError(RuntimeError):
    """Raised when QBench answers an authed request with the login page.

    When the Playwright-scraped web session (cookies + CSRF token) expires,
    QBench returns HTTP 200 carrying the HTML login page instead of JSON.
    Detecting that and raising — rather than silently returning None — is what
    lets the caller re-login and recover instead of failing every preview
    until a manual restart.
    """
    pass


def _is_login_html(status_code: Any, body: Any) -> bool:
    """True when a 200 response body is actually the QBench login page.

    Anchored on the login form's stable element id so legitimate JSON never
    trips it.
    """
    if status_code != 200:
        return False
    text = body or ""
    if not isinstance(text, str):
        return False
    low = text.lstrip().lower()
    if not low.startswith("<!doctype") and not low.startswith("<html"):
        return False
    return "qbenchlimsloginemail" in text.lower()


def _preview_poll_delays(total_seconds: float = 120.0):
    """Yield sleep intervals for the preview render poll loop.

    The old loop slept a fixed 3s before the *first* check, adding a 3s floor
    to every preview even when QBench rendered in under a second. This ramps
    up from a sub-second first check and settles to a 3s cadence for the long
    tail, shaving up to ~2-3s off every fast preview. Finite by construction
    (bounded by ``total_seconds``).
    """
    elapsed = 0.0
    for d in (0.75, 1.0, 1.5, 2.0, 2.5):
        yield d
        elapsed += d
    while elapsed < total_seconds:
        yield 3.0
        elapsed += 3.0


def _lab_sort_key(lab_id: str):
    """Sort key that orders lab IDs numerically, smallest → largest.

    lab_id is MMDDYY-NNNNN. Within a tab the date prefix is constant, so the
    meaningful key is the trailing sample number — but we sort on
    (prefix, numeric tail, raw) so Search/Re-review tabs spanning multiple
    dates stay grouped by day then numbered. Malformed IDs (non-numeric tail)
    sort to the end deterministically instead of raising.
    """
    lab_id = lab_id or ""
    parts = lab_id.split("-")
    tail = parts[-1] if parts else lab_id
    prefix = parts[0] if len(parts) > 1 else ""
    return (prefix, int(tail) if tail.isdigit() else float("inf"), lab_id)


# ══════════════════════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_CONFIG = {
    "qbench_username": "",
    "qbench_password": "",
    "report_config_id": REPORT_CONFIG_ID,
    # LabCore hosts the Command Center API and serves the LabVision UI, at a
    # fixed public hostname behind Cloudflare (HTTPS/443). One whole URL
    # rather than host+port: a host/port pair cannot express the scheme, and
    # the same URL is used both server-side and as the browser's
    # "Open in Lab Vision" target. Point it at http://127.0.0.1:8080 to run
    # against a local LabCore instead.
    "labcore_url": "https://labvision.asaplabs.net",
    # Audit log of every reviewer change, one file per category. Defaults
    # inside APP_DIR, which already lives on the network share, so the record
    # outlives any single machine. Set an absolute path to point it elsewhere.
    "change_log_dir": "",
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return {**DEFAULT_CONFIG, **cfg}
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════════
# Business day utility
# ══════════════════════════════════════════════════════════════════════════════

def business_days_ago(n: int, from_date: Optional[date] = None) -> date:
    d = from_date or date.today()
    count = 0
    while count < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            count += 1
    return d


# ══════════════════════════════════════════════════════════════════════════════
# SIF discovery helpers
# ══════════════════════════════════════════════════════════════════════════════

def _sif_get_filename(a: dict) -> str:
    return (
        (a.get("asset") or {}).get("file_name")
        or a.get("file_name")
        or a.get("name")
        or f"Attachment {a.get('id', '?')}"
    )


def _sif_is_pdf(a: dict) -> bool:
    fname = _sif_get_filename(a).lower()
    ct = (a.get("content_type") or (a.get("asset") or {}).get("content_type") or "")
    return fname.endswith(".pdf") or "pdf" in ct.lower()


# ── Why a sample has no SIF ──────────────────────────────────────────────────
#
# "No SIF" used to be one state, so an order submitted through the customer
# portal (which never has a paper SIF) looked exactly like a paper order whose
# document had gone astray. QBench's order carries a positive signal for the
# former: `order_request_status` is set only when the order arrived as a
# customer order request.
#
# Measured against live QBench on 2026-07-31 across 45 orders:
#     has SIF | order_request_status | count
#       yes   | None                 |  36
#       no    | RECEIVED             |   6
#       yes   | RECEIVED             |   3
# Every order lacking a SIF was a portal request (6/6) and every order with a
# null status had one (36/36) — but portal orders can still carry a SIF, so
# the missing document alone was never proof of anything.

SIF_FOUND = "found"
SIF_ONLINE_ENTRY = "online_entry"   # portal order request — no SIF expected
SIF_MISSING = "missing"             # paper order, document genuinely absent


def classify_missing_sif(order: Optional[dict]) -> str:
    """Say *why* a sample has no SIF, given its QBench order.

    Returns ``SIF_ONLINE_ENTRY`` only when the order positively identifies as
    a customer-portal request. Anything else — including an order we could not
    read — reports ``SIF_MISSING``, because claiming "entered online" without
    evidence is the guess this exists to replace.
    """
    if not order:
        return SIF_MISSING
    status = str(order.get("order_request_status") or "").strip()
    return SIF_ONLINE_ENTRY if status else SIF_MISSING


def _sif_find_candidates(attachments: List[dict]) -> List[dict]:
    candidates = []
    for a in attachments:
        if not _sif_is_pdf(a):
            continue
        fname = _sif_get_filename(a).lower()
        if "coa" in fname or "certificate" in fname or "report" in fname:
            continue
        candidates.append(a)
    candidates.sort(key=lambda a: (0 if "sif" in _sif_get_filename(a).lower() else 1))
    return candidates


def _sif_download(attachment: dict, api_client: QBenchAPIClient) -> Optional[bytes]:
    url = attachment.get("url")
    if not url:
        asset = attachment.get("asset") or {}
        url = asset.get("url") or asset.get("file_url")
    if url:
        try:
            resp = _requests.get(url, timeout=60)
            if resp.status_code == 200:
                return resp.content
        except Exception as e:
            logger.warning("SIF direct download failed for %s: %s", attachment.get("id"), e)
    try:
        resp = api_client.request("GET", f"/attachments/{attachment['id']}/download", timeout=60)
        if resp.status_code == 200:
            return resp.content
    except Exception:
        pass
    return None


def _sif_count_pages(pdf_bytes: bytes) -> int:
    """Number of pages in a PDF, or 0 if it can't be read."""
    if not PYMUPDF_AVAILABLE or not pdf_bytes:
        return 0
    try:
        doc = _fitz.open(stream=pdf_bytes, filetype="pdf")
        n = len(doc)
        doc.close()
        return n
    except Exception:
        return 0


def _sif_find_page(pdf_bytes: bytes, lab_id: str) -> Optional[int]:
    if not PYMUPDF_AVAILABLE or not pdf_bytes:
        return None
    parts = lab_id.split("-")
    numeric_id = parts[-1] if parts else lab_id
    try:
        doc = _fitz.open(stream=pdf_bytes, filetype="pdf")
        # Text search first — it's milliseconds per page and is what actually
        # matched in production (barcode decode rarely hit). Only rasterize +
        # barcode-scan as a fallback, and at 150 DPI (barcodes decode fine and
        # it's ~4x less pixels/CPU than the old 300 DPI).
        for pg in range(len(doc)):
            text = doc[pg].get_text()
            if lab_id in text or numeric_id in text:
                doc.close()
                return pg
        if PYZBAR_AVAILABLE:
            mat = _fitz.Matrix(150 / 72, 150 / 72)
            for pg in range(len(doc)):
                pix = doc[pg].get_pixmap(matrix=mat)
                img = _PILImage.open(io.BytesIO(pix.tobytes("png")))
                for bc in _pyzbar.decode(img):
                    data = bc.data.decode("utf-8", errors="replace")
                    if numeric_id == data or lab_id == data:
                        doc.close()
                        return pg
        doc.close()
    except Exception as e:
        logger.warning("SIF page scan error for %s: %s", lab_id, e)
    return None


def _sif_load_order_pdf(order_id, api_client, cache: dict, lock) -> Optional[Tuple[bytes, int, dict]]:
    """Download + count the SIF PDF for an order, once, caching the result.

    Multiple samples share one order's SIF PDF, so this dedups the QBench
    fetch + download across them. A "no SIF found" result is cached as None so
    we don't re-query QBench for every sample in an order that has none.
    Returns (pdf_bytes, total_pages, candidate) or None.
    """
    oid = int(order_id)
    with lock:
        if oid in cache:
            entry = cache[oid]
            if not entry:
                return None
            return (entry["pdf_bytes"], entry["total_pages"], entry["candidate"])

    result: Optional[Tuple[bytes, int, dict]] = None
    failed = False
    try:
        order_atts = api_client.fetch_order_attachments(oid)
        for candidate in _sif_find_candidates(order_atts):
            pdf_bytes = _sif_download(candidate, api_client)
            if not pdf_bytes:
                continue
            result = (pdf_bytes, _sif_count_pages(pdf_bytes), candidate)
            break
    except Exception as exc:
        # A transient fetch/download error must NOT be cached as a permanent
        # "no SIF" — that would poison every other sample in the order until
        # the next restart. Leave the order uncached so it retries.
        failed = True
        logger.warning("SIF load failed for order %s: %s", oid, exc)

    if not failed:
        with lock:
            cache[oid] = (
                {"pdf_bytes": result[0], "total_pages": result[1],
                 "candidate": result[2], "pages_by_lab": {}}
                if result else None
            )
    return result


def _sif_extract_page(pdf_bytes: bytes, page_num: int) -> Optional[bytes]:
    if not PYMUPDF_AVAILABLE or not pdf_bytes:
        return None
    try:
        src = _fitz.open(stream=pdf_bytes, filetype="pdf")
        dst = _fitz.open()
        dst.insert_pdf(src, from_page=page_num, to_page=page_num)
        out = dst.tobytes()
        dst.close()
        src.close()
        return out
    except Exception as e:
        logger.warning("SIF page extract error (page %d): %s", page_num, e)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Re-review state persistence
# ══════════════════════════════════════════════════════════════════════════════

def load_re_review_state() -> Dict[str, dict]:
    if RE_REVIEW_STATE_FILE.exists():
        try:
            data = json.loads(RE_REVIEW_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {item["lab_id"]: item for item in data if item.get("lab_id")}
            return data
        except Exception:
            pass
    return {}


def save_re_review_state(state_data: Dict[str, dict]) -> None:
    RE_REVIEW_STATE_FILE.write_text(
        json.dumps(list(state_data.values()), indent=2), encoding="utf-8"
    )


# ── Shared field-visibility settings (Sample Info editor) ────────────────
# One config for ALL reviewers, hard-saved to disk so it survives server
# restarts and portal logins. `sample_info_hidden` lists field keys the
# Sample Info (SIF) panel should hide; `show_extra_fields` gates the
# auto-discovered QBench fields appended after the known 22.

# State, not config: /api/field-settings writes this at runtime when a reviewer
# hides a column, so it belongs in DATA_DIR. Bound to APP_DIR it would ship
# inside the release and be silently reverted to the shipped copy on the next
# deploy — the reviewer's setting would vanish with no error anywhere.
FIELD_SETTINGS_FILE = DATA_DIR / "field_settings.json"
FIELD_SETTINGS_TEMPLATE = APP_DIR / "field_settings.json"

DEFAULT_FIELD_SETTINGS: Dict[str, Any] = {
    "sample_info_hidden": [],
    "show_extra_fields": True,
}


def _seed_field_settings_if_absent() -> None:
    """Copy the shipped field settings into DATA_DIR on first boot only.

    No-op when DATA_DIR is APP_DIR (template and target are the same file).
    """
    if FIELD_SETTINGS_FILE.exists() or not FIELD_SETTINGS_TEMPLATE.is_file():
        return
    if FIELD_SETTINGS_TEMPLATE.resolve() == FIELD_SETTINGS_FILE.resolve():
        return
    try:
        shutil.copyfile(FIELD_SETTINGS_TEMPLATE, FIELD_SETTINGS_FILE)
    except OSError:
        pass  # defaults are fine; failing to boot over this is not


_seed_field_settings_if_absent()


def load_field_settings() -> Dict[str, Any]:
    if FIELD_SETTINGS_FILE.exists():
        try:
            data = json.loads(FIELD_SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                hidden = data.get("sample_info_hidden")
                return {
                    "sample_info_hidden": (
                        [str(k) for k in hidden] if isinstance(hidden, list) else []
                    ),
                    "show_extra_fields": bool(data.get("show_extra_fields", True)),
                }
        except Exception:
            logger.warning("field_settings.json unreadable; using defaults", exc_info=True)
    return dict(DEFAULT_FIELD_SETTINGS)


def save_field_settings(settings: Dict[str, Any]) -> None:
    FIELD_SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════════
# COASession (Playwright login + preview generation)
# ══════════════════════════════════════════════════════════════════════════════

class COASession:
    LOGIN_URL = f"{QBENCH_BASE}/lims"
    # When many preview threads hit a stale session at once they all want to
    # relogin. Only let one actually re-login per window; the rest reuse the
    # session it just refreshed.
    RELOGIN_THROTTLE_SECONDS = 30.0

    def __init__(self, username: str, password: str, report_config_id: str = REPORT_CONFIG_ID) -> None:
        self.username = username
        self.password = password
        self.report_config_id = report_config_id
        self.csrf_token: str = ""
        self.playwright_cookies: List[dict] = []
        self._session = _requests.Session()
        self._session_lock = threading.Lock()
        self._relogin_lock = threading.Lock()
        self._last_relogin: float = 0.0

    def login(self, headless: bool = True) -> None:
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("playwright not installed")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=headless)
            ctx = browser.new_context()
            page = ctx.new_page()
            logger.info("Opening QBench login page…")
            page.goto(self.LOGIN_URL, wait_until="load")
            try:
                page.wait_for_selector("#qbenchLimsLoginEmail", timeout=12_000)
            except PWTimeout:
                raise RuntimeError("Login form did not load (timeout).")

            page.fill("#qbenchLimsLoginEmail", self.username)
            page.fill("#qbenchLimsLoginPassword", self.password)
            page.click("button[type='submit']")

            try:
                page.wait_for_selector("#main-content, .navigation-menu, #assayTabsNav", timeout=15_000)
            except PWTimeout:
                pass

            if page.locator("#qbenchLimsLoginEmail").is_visible():
                err_loc = page.locator(".alert.alert-danger, .error, #loginErrorMessage").first
                msg = err_loc.text_content().strip() if err_loc.count() else "Invalid credentials."
                raise RuntimeError(f"Login failed: {msg}")

            try:
                self.csrf_token = page.evaluate("document.getElementById('csrfToken')?.textContent || ''")
                if not self.csrf_token:
                    self.csrf_token = page.evaluate(
                        "document.querySelector('[name=csrf-token]')?.getAttribute('content') || ''"
                    )
            except Exception as e:
                logger.warning("Could not extract CSRF token: %s", e)

            self.playwright_cookies = ctx.cookies()
            with self._session_lock:
                for c in self.playwright_cookies:
                    self._session.cookies.set(
                        c["name"], c["value"], domain=c.get("domain", "").lstrip(".")
                    )
                self._session.headers.update({
                    "X-Requested-With": "XMLHttpRequest",
                    "X-CSRF-Token": self.csrf_token,
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                })
            browser.close()

    def relogin(self) -> None:
        with self._relogin_lock:
            # A peer thread may have just refreshed the session — don't storm
            # QBench with a headless-browser login per failing preview.
            if time.monotonic() - self._last_relogin < self.RELOGIN_THROTTLE_SECONDS:
                logger.info("Skipping relogin — session refreshed %.0fs ago.",
                            time.monotonic() - self._last_relogin)
                return
            logger.info("Re-logging in to QBench…")
            self._session = _requests.Session()
            self.login(headless=True)
            self._last_relogin = time.monotonic()
            logger.info("Re-login complete.")

    def generate_preview(
        self,
        sample_id: int,
        test_ids: List[int],
        order_id: Optional[int] = None,
        attachment_ids: Optional[List[int]] = None,
        skip_attachments: bool = False,
        timeout_seconds: int = 120,
    ) -> Optional[str]:
        if not self.csrf_token and not self.playwright_cookies:
            return None

        data: List[Tuple[str, Any]] = [
            ("sample_id", sample_id),
            ("order_id", order_id or ""),
            ("test_id", test_ids[0] if test_ids else ""),
            ("signature_id", "0"),
            ("report_config_id", self.report_config_id),
            ("report_level", REPORT_LEVEL),
            ("use_default_report", "false"),
            ("printdoc_ids", ""),
            ("csrf_token", self.csrf_token),
        ]
        for tid in test_ids:
            data.append(("test_ids", tid))
        if skip_attachments:
            data.append(("use_default_attachments", "false"))
        elif attachment_ids:
            data.append(("use_default_attachments", "false"))
            for aid in attachment_ids:
                data.append(("attachment_ids", aid))
        else:
            data.append(("use_default_attachments", "true"))

        resp = None
        try:
            with self._session_lock:
                resp = self._session.post(
                    f"{QBENCH_BASE}/report/preview",
                    data=data,
                    headers={"Referer": f"{QBENCH_BASE}/sample?id={sample_id}"},
                    timeout=30,
                )
            resp.raise_for_status()
            preview = resp.json()
        except (ConnectionResetError, ConnectionAbortedError, ConnectionError, BrokenPipeError):
            raise
        except Exception as exc:
            if resp is not None:
                try:
                    status = resp.status_code
                    body_full = resp.text or ""
                except Exception:
                    status, body_full = "?", ""
                # Stale web session: QBench returns 200 + the login page
                # instead of JSON. Raise so the caller re-logins and retries
                # rather than silently failing every preview until restart.
                if _is_login_html(status, body_full):
                    logger.warning(
                        "QBench web session expired (login page returned) for sample %s — relogin needed.",
                        sample_id,
                    )
                    raise SessionExpiredError(f"login page returned for sample {sample_id}")
                logger.error(
                    "POST /report/preview failed for sample %s: %s (status=%s body=%r)",
                    sample_id, exc, status, body_full[:500],
                )
            else:
                logger.error("POST /report/preview failed for sample %s: %s", sample_id, exc)
            return None

        preview_id = preview.get("id")
        if not preview_id:
            return None

        deadline = time.time() + timeout_seconds
        for delay in _preview_poll_delays(timeout_seconds):
            if time.time() >= deadline:
                break
            time.sleep(delay)
            poll = None
            try:
                with self._session_lock:
                    poll = self._session.get(
                        f"{QBENCH_BASE}/report/preview/get",
                        params={"id": preview_id},
                        timeout=15,
                    )
                poll.raise_for_status()
                poll_data = poll.json()
                status = (
                    poll_data.get("render_status")
                    or poll_data.get("status")
                    or ""
                )
                render_error = poll_data.get("render_error") or ""
            except (ConnectionResetError, ConnectionAbortedError, ConnectionError, BrokenPipeError):
                raise
            except Exception:
                # If the session expired mid-render, QBench serves the login
                # page here too — surface it so the caller re-logins.
                if poll is not None:
                    try:
                        if _is_login_html(poll.status_code, poll.text or ""):
                            raise SessionExpiredError(f"login page during poll for preview {preview_id}")
                    except SessionExpiredError:
                        raise
                    except Exception:
                        pass
                continue

            if status == "SUCCESSFUL":
                return f"{QBENCH_BASE}/report/preview?id={preview_id}"

            if render_error:
                logger.warning("Preview %s render_error: %s", preview_id, render_error[:200])
                if "attachment" in render_error.lower() or "s3" in render_error.lower() or "asset" in render_error.lower():
                    raise PreviewAttachmentError(render_error[:300])
                return None

            if status == "FAILED":
                return None

        return None


# ══════════════════════════════════════════════════════════════════════════════
# Upload Queue (background QBench sync for test edits) – shared across users
# ══════════════════════════════════════════════════════════════════════════════

class UploadQueue:
    """Serializes QBench writes (test edits + comment saves) off the request
    thread so a 429 is retried server-side and a write survives the reviewer
    closing their browser. ``sse_broadcast`` (set after construction) lets the
    queue confirm comment writes back to every connected UI."""

    MAX_ATTEMPTS = 5

    def __init__(self, api_client: QBenchAPIClient, *, start_worker: bool = True) -> None:
        self.api_client = api_client
        self.queue: queue.Queue = queue.Queue()
        self.running = True
        self.last_synced_values: Dict[int, str] = {}
        self.results: List[dict] = []
        self._lock = threading.Lock()
        self.sse_broadcast: Optional[Callable[[dict], None]] = None
        self.thread = threading.Thread(target=self._worker_loop, daemon=True)
        if start_worker:
            self.thread.start()

    def enqueue(self, test_id: int, value: str) -> None:
        last = self.last_synced_values.get(test_id)
        if last == value:
            return
        self.queue.put({"kind": "test", "test_id": test_id, "value": value, "attempts": 0})

    def enqueue_comment(self, sample_id: int, comments: str, lab_id: str = "", uid: str = "") -> None:
        self.queue.put({
            "kind": "comment", "sample_id": sample_id, "comments": comments,
            "lab_id": lab_id, "uid": uid, "attempts": 0,
        })

    def stop(self) -> None:
        self.running = False
        self.queue.put(None)

    def _worker_loop(self) -> None:
        while self.running:
            try:
                payload = self.queue.get(timeout=1)
            except queue.Empty:
                continue
            if payload is None:
                break
            self._safe_process(payload)
            self.queue.task_done()
            time.sleep(0.2)

    def _safe_process(self, payload: dict) -> None:
        """Run _process so the worker thread can NEVER die.

        A dead worker would silently stall every future test edit and comment
        save until a process restart, so any unexpected error here must be
        logged and swallowed rather than propagated.
        """
        try:
            self._process(payload)
        except Exception:
            logger.exception("UploadQueue._process crashed on payload kind=%s", payload.get("kind"))

    def _schedule_retry(self, payload: dict, delay: float) -> None:
        threading.Timer(delay, lambda: self.queue.put(payload)).start()

    def _emit(self, data: dict) -> None:
        if self.sse_broadcast:
            try:
                self.sse_broadcast(data)
            except Exception:
                logger.exception("UploadQueue SSE broadcast failed")

    def _process(self, payload: dict) -> None:
        kind = payload.get("kind", "test")
        if kind == "comment":
            self._process_comment(payload)
        else:
            self._process_test(payload)

    def _process_test(self, payload: dict) -> None:
        test_id = payload["test_id"]
        value = payload["value"]
        attempts = payload.get("attempts", 0)
        try:
            self.api_client.update_test_result(int(test_id), value)
            self.last_synced_values[int(test_id)] = value
            with self._lock:
                self.results.append({"test_id": test_id, "value": value, "status": "ok"})
            logger.info("Uploaded test %s = '%s'", test_id, value)
        except (QBenchAPIError, _requests.exceptions.RequestException) as exc:
            if attempts < self.MAX_ATTEMPTS:
                payload["attempts"] = attempts + 1
                delay = min(5, 1 + attempts)
                logger.warning("Retrying test %s in %ss: %s", test_id, delay, exc)
                self._schedule_retry(payload, delay)
            else:
                with self._lock:
                    self.results.append({"test_id": test_id, "value": value, "status": "failed", "error": str(exc)})
                logger.error("Failed to upload test %s: %s", test_id, exc)

    def _process_comment(self, payload: dict) -> None:
        sample_id = payload["sample_id"]
        comments = payload["comments"]
        lab_id = payload.get("lab_id", "")
        attempts = payload.get("attempts", 0)
        try:
            self.api_client.update_sample_comments(int(sample_id), comments)
            logger.info("Saved comments for %s (sample %s)", lab_id or "?", sample_id)
            self._emit({"type": "comment_saved", "lab_id": lab_id,
                        "sample_id": sample_id, "comments": comments, "uid": payload.get("uid")})
        except (QBenchAPIError, _requests.exceptions.RequestException) as exc:
            if attempts < self.MAX_ATTEMPTS:
                payload["attempts"] = attempts + 1
                delay = min(8, 1 + attempts)
                logger.warning("Retrying comments for %s in %ss: %s", lab_id or sample_id, delay, exc)
                self._schedule_retry(payload, delay)
            else:
                logger.error("Failed to save comments for %s: %s", lab_id or sample_id, exc)
                self._emit({"type": "comment_failed", "lab_id": lab_id,
                            "sample_id": sample_id, "error": str(exc), "uid": payload.get("uid")})


# ══════════════════════════════════════════════════════════════════════════════
# SampleRecord
# ══════════════════════════════════════════════════════════════════════════════

STATUS_PENDING = "pending"
STATUS_LOADING = "loading"
STATUS_READY = "ready"
STATUS_GOOD = "good"
STATUS_BAD = "bad"
STATUS_ERROR = "error"


class SampleRecord:
    def __init__(
        self,
        lab_id: str,
        tab: str,
        sample_id: Optional[int] = None,
        test_ids: Optional[List[int]] = None,
        order_id: Optional[int] = None,
        cc_task: Optional[dict] = None,
        info: Optional[dict] = None,
    ) -> None:
        self.lab_id = lab_id
        self.tab = tab
        self.sample_id = sample_id
        self.test_ids = test_ids or []
        self.order_id = order_id
        self.preview_url: Optional[str] = None
        # The Command Center listing that put this sample in Re-review, if any.
        self.cc_task = cc_task
        # The listing this sample was filed under when flagged Bad here.
        self.cc_task_id: Optional[int] = None
        self.status: str = STATUS_PENDING
        self.reason: str = ""
        self.info = info or {}
        self.attachments: Optional[List[dict]] = None
        self.tests_data: Optional[List[dict]] = None
        self.sif_pdf_bytes: Optional[bytes] = None
        self.sif_page: Optional[int] = None
        self.sif_total_pages: Optional[int] = None
        self.sif_status: str = "pending"

    def to_dict(self) -> dict:
        return {
            "lab_id": self.lab_id,
            "tab": self.tab,
            "sample_id": self.sample_id,
            "test_ids": self.test_ids,
            "order_id": self.order_id,
            "status": self.status,
            "reason": self.reason,
            "has_preview": bool(self.preview_url),
            "cc_task": self.cc_task,
            "cc_task_id": self.cc_task_id,
            "has_sif": self.sif_status == "found",
            "sif_status": self.sif_status,
            "sif_page": self.sif_page,
            "sif_total_pages": self.sif_total_pages,
        }


# ══════════════════════════════════════════════════════════════════════════════
# Shared Application State (QBench connection + config)
# ══════════════════════════════════════════════════════════════════════════════

class AppState:
    """Shared server resources – QBench connection, config, Command Center.
    Per-user state (records, results, pdf cache) lives in UserState."""

    def __init__(self) -> None:
        self.config = load_config()
        self.api_client = QBenchAPIClient()
        self.coa_session: Optional[COASession] = None
        self.upload_queue: Optional[UploadQueue] = None
        self.logged_in = False
        # SIF PDFs are shared across the samples in an order; cache the
        # downloaded bytes + per-lab page lookups so we fetch/scan once per
        # order, not once per sample.
        self.sif_order_cache: Dict[int, Optional[dict]] = {}
        # Why an order has no SIF (portal entry vs genuinely missing). Shared
        # by every sample in the order, so it is answered once.
        self.sif_absence_cache: Dict[int, str] = {}
        self._sif_cache_lock = threading.Lock()

        self.labcore = LabCoreClient(
            base_url=self.config.get("labcore_url", "https://labvision.asaplabs.net"),
        )

        # The audit trail is state, not code: it defaults into DATA_DIR so a
        # release swap cannot take the change log with it. An explicit
        # ``change_log_dir`` in config still wins (that is how it gets pointed
        # at the network share).
        self.change_log = ChangeLog(
            self.config.get("change_log_dir") or (DATA_DIR / "changelog")
        )

    def broadcast_sse(self, data: dict) -> None:
        """Send an SSE event to ALL currently connected users."""
        with _sessions_lock:
            all_states = list(user_sessions.values())
        for ustate in all_states:
            ustate.emit_sse(data)

    def emit_to_user(self, uid: Optional[str], data: dict) -> None:
        """Send an SSE event to one session by uid (falls back to broadcast)."""
        if not uid:
            self.broadcast_sse(data)
            return
        with _sessions_lock:
            ustate = user_sessions.get(uid)
        if ustate is not None:
            ustate.emit_sse(data)

    def _queue_sse(self, data: dict) -> None:
        """Broadcaster handed to UploadQueue: route per-session when the job
        carries a uid (comment confirmations), else broadcast."""
        self.emit_to_user(data.get("uid"), data)


state = AppState()


# ══════════════════════════════════════════════════════════════════════════════
# Per-User Session State
# ══════════════════════════════════════════════════════════════════════════════

class UserState:
    """Holds COA review state for one browser session (one person)."""

    def __init__(self, uid: str, name: str) -> None:
        self.uid = uid
        self.name = name
        self.last_active = time.time()
        self.records: Dict[Tuple[str, str], SampleRecord] = {}
        self.session_results: List[dict] = []
        self.pdf_cache: Dict[str, bytes] = {}
        self.pdf_loading: set = set()
        self.status_log: List[str] = []
        self._lock = threading.Lock()
        self._sse_queues: List[queue.Queue] = []

    def emit_status(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        with self._lock:
            self.status_log.append(entry)
            if len(self.status_log) > 500:
                self.status_log = self.status_log[-250:]
        self.emit_sse({"type": "status", "message": msg})

    def emit_sse(self, data: dict) -> None:
        dead = []
        for q in self._sse_queues:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
        for q in dead:
            try:
                self._sse_queues.remove(q)
            except ValueError:
                pass

    def add_record(self, rec: SampleRecord) -> None:
        key = (rec.tab, rec.lab_id)
        if key not in self.records:
            self.records[key] = rec

    def record_result(self, rec: SampleRecord, outcome: str, reason: str = "") -> None:
        """Record this sample's review outcome, replacing any earlier one.

        session_results feeds Export CSV and Good Samples. It used to be
        append-only, so a reviewer who changed their mind exported the sample
        twice with contradictory outcomes. Keyed on (tab, lab_id): the same
        lab_id on Yesterday and Search are genuinely separate reviews.
        """
        row = {
            "lab_id": rec.lab_id,
            "sample_id": rec.sample_id or "",
            "tab": rec.tab,
            "outcome": outcome,
            "reason": reason,
            "reviewer": self.name,
            "date": date.today().isoformat(),
        }
        key = (rec.tab, rec.lab_id)
        for i, existing in enumerate(self.session_results):
            if (existing.get("tab"), existing.get("lab_id")) == key:
                self.session_results[i] = row
                return
        self.session_results.append(row)

    def clear_result(self, tab: str, lab_id: str) -> bool:
        """Drop a sample's outcome so it leaves the exports. True if removed."""
        before = len(self.session_results)
        self.session_results = [
            r for r in self.session_results
            if (r.get("tab"), r.get("lab_id")) != (tab, lab_id)
        ]
        return len(self.session_results) != before

    def get_tab_records(self, tab: str) -> List[SampleRecord]:
        recs = [r for (t, _), r in self.records.items() if t == tab]
        recs.sort(key=lambda r: _lab_sort_key(r.lab_id))
        return recs


# ── Session registry ─────────────────────────────────────────────────────────
user_sessions: Dict[str, UserState] = {}
_sessions_lock = threading.Lock()


def get_user_state() -> Optional[UserState]:
    """Return the UserState for the current request, or None if not authenticated."""
    uid = session.get("uid")
    if not uid:
        return None
    with _sessions_lock:
        ustate = user_sessions.get(uid)
    if ustate is None:
        return None
    ustate.last_active = time.time()
    return ustate


def require_portal(f):
    """Decorator: return 401 if the request has no valid portal session."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if get_user_state() is None:
            return jsonify({"error": "Session expired", "portal_auth": False}), 401
        return f(*args, **kwargs)
    return wrapper


def _session_cleanup_worker() -> None:
    """Background thread: remove sessions idle longer than SESSION_CLEANUP_SECONDS."""
    while True:
        time.sleep(30)
        now = time.time()
        with _sessions_lock:
            expired = [
                uid for uid, us in user_sessions.items()
                if now - us.last_active > SESSION_CLEANUP_SECONDS
            ]
            for uid in expired:
                us = user_sessions.pop(uid)
                log_login_event("TIMEOUT", us.name, "server")
                logger.info("Session timed out: %s", us.name)


threading.Thread(target=_session_cleanup_worker, daemon=True).start()


# ── Global activity tracking (for auto-restart) ──────────────────────────────
_last_request_time = time.time()
_server_start_time = time.time()
_auto_restart_done_today: Optional[str] = None  # date string of last auto-restart


def _should_auto_restart(
    *,
    hour: int,
    today_str: str,
    done_today: Optional[str],
    uptime_seconds: float,
    active_count: int,
    idle_seconds: float,
) -> bool:
    """Decide whether the daily auto-restart should fire right now.

    The uptime guard is the storm fix: a process that has only just respawned
    (within the 3 AM hour) must not immediately restart again — its in-memory
    once-a-day flag was reset by the respawn, so without this it would loop
    every ~30s through the whole hour.
    """
    if hour != AUTO_RESTART_HOUR:
        return False
    if done_today == today_str:
        return False
    if uptime_seconds < AUTO_RESTART_MIN_UPTIME_SECONDS:
        return False
    if active_count > 0 and idle_seconds < AUTO_RESTART_IDLE_SECONDS:
        return False
    return True


def _auto_restart_worker() -> None:
    """Background thread: restart the server daily at AUTO_RESTART_HOUR if idle."""
    global _auto_restart_done_today
    while True:
        time.sleep(30)
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")

        idle_seconds = time.time() - _last_request_time
        uptime_seconds = time.time() - _server_start_time
        with _sessions_lock:
            active_count = len(user_sessions)

        if not _should_auto_restart(
            hour=now.hour,
            today_str=today_str,
            done_today=_auto_restart_done_today,
            uptime_seconds=uptime_seconds,
            active_count=active_count,
            idle_seconds=idle_seconds,
        ):
            continue

        # Idle enough (or no sessions) and long-running — restart
        _auto_restart_done_today = today_str
        logger.info("Auto-restart triggered (idle %.0fs, %d sessions, uptime %.0fs). Exiting for restart.",
                     idle_seconds, active_count, uptime_seconds)
        _graceful_shutdown("auto-restart")


threading.Thread(target=_auto_restart_worker, daemon=True).start()


def _graceful_shutdown(reason: str = "unknown") -> None:
    """Shut down cleanly: flush logs, close sockets, then exit."""
    logger.info("Graceful shutdown initiated (reason: %s)", reason)
    # Flush all log handlers
    for h in logging.root.handlers:
        try:
            h.flush()
        except Exception:
            pass

    # If not being watched by Run.pyw, self-respawn so the server comes back up.
    # Run.pyw sets COA_WATCHER_ACTIVE=1 in the environment; if it's absent we're
    # running standalone and must restart ourselves.
    if reason in ("auto-restart", "manual restart") and not os.environ.get("COA_WATCHER_ACTIVE"):
        logger.info("No Run.pyw watcher — self-respawning...")
        try:
            subprocess.Popen(
                [sys.executable, "-u", str(Path(__file__).resolve())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                cwd=str(APP_DIR),
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except Exception as _spawn_err:
            logger.error("Self-respawn failed: %s", _spawn_err)

    time.sleep(0.5)
    # Use os._exit to avoid hanging on daemon threads
    os._exit(0)


def _cleanup_at_exit() -> None:
    logger.info("Server process exiting — atexit cleanup.")
    for h in logging.root.handlers:
        try:
            h.flush()
        except Exception:
            pass


atexit.register(_cleanup_at_exit)


# ══════════════════════════════════════════════════════════════════════════════
# Background Workers
# ══════════════════════════════════════════════════════════════════════════════

def fetch_samples_for_tab(tab_name: str, target_date: date, ustate: UserState) -> None:
    """Fetch samples + tests for a given date (runs in background thread)."""
    try:
        prefix = target_date.strftime("%m%d%y")
        ustate.emit_status(f"[{tab_name}] Fetching samples with prefix {prefix}…")

        samples_raw = state.api_client.fetch_samples_by_lab_id_prefix(prefix)
        if not samples_raw:
            ustate.emit_status(f"[{tab_name}] No samples found.")
            ustate.emit_sse({"type": "tab_loaded", "tab": tab_name, "count": 0})
            return

        sample_test_map: Dict[int, Dict[str, Any]] = {}
        for s in samples_raw:
            sid = s.get("id")
            if not sid:
                continue
            sid = int(sid)
            sample_test_map[sid] = {
                "sample_id": sid,
                "test_ids": [],
                "order_id": s.get("order_id"),
                "lab_id": s.get("lab_id", ""),
            }

        if not sample_test_map:
            ustate.emit_sse({"type": "tab_loaded", "tab": tab_name, "count": 0})
            return

        ustate.emit_status(f"[{tab_name}] Fetching tests for {len(sample_test_map)} samples…")
        tests = state.api_client.fetch_tests_for_sample_ids(list(sample_test_map.keys()))
        for t in tests:
            sid = t.get("sample_id") or (t.get("sample") or {}).get("id")
            if not sid:
                continue
            sid = int(sid)
            if sid in sample_test_map:
                sample_test_map[sid]["test_ids"].append(int(t["id"]))
                if not sample_test_map[sid]["order_id"]:
                    sample_test_map[sid]["order_id"] = t.get("order_id")

        samples = [info for info in sample_test_map.values() if info["test_ids"]]
        ustate.emit_status(f"[{tab_name}] Found {len(samples)} samples.")

        for info in samples:
            lab_id = info.get("lab_id") or str(info.get("sample_id", "?"))
            rec = SampleRecord(
                lab_id=lab_id,
                tab=tab_name,
                sample_id=info.get("sample_id"),
                test_ids=info.get("test_ids", []),
                order_id=info.get("order_id"),
                info=info,
            )
            ustate.add_record(rec)

        ustate.emit_sse({"type": "tab_loaded", "tab": tab_name, "count": len(samples)})

        if state.coa_session and state.logged_in:
            for info in samples:
                lab_id = info.get("lab_id") or str(info.get("sample_id", "?"))
                PREVIEW_POOL.submit(generate_preview_for_sample, tab_name, lab_id, ustate)

    except Exception as exc:
        ustate.emit_status(f"[{tab_name}] Error: {exc}")
        logger.exception("fetch_samples_for_tab failed for %s", tab_name)


CC_RE_REVIEW_TYPE = "double_check"


def cc_tasks_to_re_review_entries(tasks: List[dict]) -> List[dict]:
    """Turn Command Center listings into the Re-review tab's work list.

    Kept separate from the QBench resolution loop below so the selection rules
    are testable without the network.

    Rules:
      * only ``double_check`` listings — maintenance and customer-clarification
        listings are real work, but not COA re-reviews;
      * not completed (the caller asks for view=active, but a listing finished
        between read and render must not resurface);
      * one entry per attached sample, since each COA is reviewed on its own;
      * a lab_id on two active listings appears once, keeping the first —
        LabCore returns newest-first, and force_create or consolidation can
        legitimately produce overlapping listings.

    Malformed listings are skipped rather than raised on: the payload shape is
    LabCore's, not ours, and one bad row must not empty the tab.
    """
    entries: List[dict] = []
    seen: set = set()
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        if task.get("type") != CC_RE_REVIEW_TYPE:
            continue
        if task.get("status") == "completed":
            continue
        samples = task.get("samples") or []
        if not isinstance(samples, list):
            continue
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            lab_id = str(sample.get("lab_id", "")).strip()
            if not lab_id or lab_id in seen:
                continue
            seen.add(lab_id)
            entries.append({
                "lab_id": lab_id,
                "customer_name": sample.get("customer_name", ""),
                "fuel_type": sample.get("fuel_type", ""),
                "task": {
                    "id": task.get("id"),
                    "type": task.get("type", ""),
                    "status": task.get("status", ""),
                    "initial_problem": task.get("initial_problem", ""),
                    "context": task.get("context", ""),
                    "customer": task.get("customer", ""),
                    "department": task.get("department", ""),
                    "created_by": task.get("created_by", ""),
                    "source_program": task.get("source_program", ""),
                    "date_created": task.get("date_created", ""),
                    "latest_update": task.get("latest_update", ""),
                    "latest_update_by": task.get("latest_update_by", ""),
                },
            })
    return entries


def fetch_re_review_samples(ustate: UserState) -> None:
    """Fetch re-review samples from the Command Center board.

    Shows every open double_check listing, whoever filed it — including ones
    raised in LabVision. Those may name a lab_id QBench cannot resolve or that
    has no renderable COA; each is skipped with a status line rather than
    failing the tab.
    """
    try:
        ustate.emit_status("[Re-review] Reading Command Center…")

        try:
            tasks = state.labcore.active_tasks()
        except LabCoreUnavailable as exc:
            # Surfaced as an error, not an empty tab: "no re-reviews" and
            # "couldn't ask" must not look the same to a reviewer.
            logger.warning("Re-review: LabCore unavailable: %s", exc)
            ustate.emit_status(f"[Re-review] Command Center unreachable: {exc}")
            ustate.emit_sse({
                "type": "tab_loaded", "tab": "Re-review", "count": 0,
                "error": "Command Center unreachable",
            })
            return

        cc_entries = cc_tasks_to_re_review_entries(tasks)
        # re_review_state.json is now purely a QBench-resolution cache
        # (lab_id -> sample/test/order ids, which never change). Command
        # Center is the source of truth for *which* samples are in the tab.
        prev_state = load_re_review_state()
        new_state: Dict[str, dict] = {}
        count = 0

        for cc_entry in cc_entries:
            lab_id = cc_entry["lab_id"]

            cached = prev_state.get(lab_id, {})
            sample_id = cached.get("sample_id")
            test_ids = cached.get("test_ids", [])
            order_id = cached.get("order_id")

            if not sample_id or not test_ids:
                try:
                    matching = state.api_client.fetch_samples_by_lab_id(lab_id)
                    if not matching:
                        continue
                    s = matching[0]
                    sample_id = int(s.get("id") or s.get("sample_id", 0))
                    order_id = s.get("order_id")
                    if sample_id:
                        tests = state.api_client.fetch_tests_for_sample_ids([sample_id])
                        test_ids = [int(t["id"]) for t in tests if t.get("id")]
                        if not order_id and tests:
                            order_id = tests[0].get("order_id")
                except Exception as exc:
                    ustate.emit_status(f"[Re-review] Skipping {lab_id}: {exc}")
                    continue

            if not sample_id or not test_ids:
                continue

            # Cached fields only — the CC task is deliberately not persisted,
            # so a stale file can never contradict the live board.
            new_state[lab_id] = {
                "lab_id": lab_id,
                "sample_id": sample_id,
                "test_ids": test_ids,
                "order_id": order_id,
            }

            entry = dict(new_state[lab_id])
            entry["customer_name"] = cc_entry.get("customer_name", "")
            entry["fuel_type"] = cc_entry.get("fuel_type", "")

            rec = SampleRecord(
                lab_id=lab_id,
                tab="Re-review",
                sample_id=sample_id,
                test_ids=test_ids,
                order_id=order_id,
                cc_task=cc_entry["task"],
                info=entry,
            )
            ustate.add_record(rec)
            count += 1

        save_re_review_state(new_state)
        ustate.emit_status(f"[Re-review] Found {count} entries.")
        ustate.emit_sse({"type": "tab_loaded", "tab": "Re-review", "count": count})

        if state.coa_session and state.logged_in:
            for lab_id in new_state:
                PREVIEW_POOL.submit(generate_preview_for_sample, "Re-review", lab_id, ustate)

    except Exception as exc:
        ustate.emit_status(f"[Re-review] Error: {exc}")


def generate_preview_for_sample(tab: str, lab_id: str, ustate: UserState) -> None:
    """Generate a COA preview for a single sample (runs in background thread)."""
    key = (tab, lab_id)
    rec = ustate.records.get(key)
    if not rec or not state.coa_session:
        return
    if rec.status not in (STATUS_PENDING, STATUS_ERROR, STATUS_LOADING):
        return

    rec.status = STATUS_LOADING
    ustate.emit_sse({"type": "sample_status", "tab": tab, "lab_id": lab_id, "status": STATUS_LOADING})

    sample_id = rec.sample_id
    test_ids = rec.test_ids
    order_id = rec.order_id

    if not sample_id or not test_ids:
        rec.status = STATUS_ERROR
        ustate.emit_sse({"type": "sample_status", "tab": tab, "lab_id": lab_id, "status": STATUS_ERROR})
        return

    all_attachments: List[dict] = []
    attachment_ids: List[int] = []
    try:
        all_attachments = state.api_client.fetch_all_attachments_for_sample(int(sample_id))
        _report_fields = ("attach_to_report", "include_in_report", "include_on_coa", "print_with_report")
        report_tagged = [a for a in all_attachments if any(a.get(f) for f in _report_fields)]
        attachment_ids = [int(a["id"]) for a in report_tagged if a.get("id")]
    except Exception:
        pass

    rec.attachments = all_attachments

    current_att_ids = attachment_ids or None
    skip_atts = False

    for attempt in range(3):
        try:
            url = state.coa_session.generate_preview(
                sample_id=int(sample_id),
                test_ids=[int(t) for t in test_ids],
                order_id=int(order_id) if order_id else None,
                attachment_ids=None if skip_atts else current_att_ids,
                skip_attachments=skip_atts,
            )
            if not url:
                rec.status = STATUS_ERROR
                ustate.emit_sse({"type": "sample_status", "tab": tab, "lab_id": lab_id, "status": STATUS_ERROR})
                return

            viewable_url = url
            try:
                resp = state.coa_session._session.get(url, timeout=30, allow_redirects=True, stream=True)
                resp.close()
                if resp.url and resp.url != url:
                    viewable_url = resp.url
            except Exception:
                pass

            rec.preview_url = viewable_url
            rec.status = STATUS_READY
            ustate.emit_sse({"type": "sample_status", "tab": tab, "lab_id": lab_id, "status": STATUS_READY})
            ustate.emit_status(f"Preview ready: {lab_id}")

            IO_POOL.submit(cache_pdf, lab_id, viewable_url, ustate)
            IO_POOL.submit(fetch_sif_for_sample, tab, lab_id, ustate)
            return

        except PreviewAttachmentError as exc:
            ustate.emit_status(f"Attachment error for {lab_id} — retrying without attachments…")
            logger.warning("Attachment error for %s: %s", lab_id, str(exc)[:200])
            skip_atts = True
            continue

        except SessionExpiredError:
            # The QBench web session expired. Re-login (throttled so concurrent
            # preview threads don't storm) and retry on the next attempt, on
            # ANY attempt index — this is the recovery path that was missing.
            if attempt < 2:
                ustate.emit_status(f"QBench session expired — re-logging in for {lab_id}…")
                try:
                    state.coa_session.relogin()
                except Exception:
                    rec.status = STATUS_ERROR
                    ustate.emit_sse({"type": "sample_status", "tab": tab, "lab_id": lab_id, "status": STATUS_ERROR})
                    return
                continue
            rec.status = STATUS_ERROR
            ustate.emit_sse({"type": "sample_status", "tab": tab, "lab_id": lab_id, "status": STATUS_ERROR})
            ustate.emit_status(f"Preview error for {lab_id}: session expired")
            return

        except Exception as exc:
            if attempt == 0:
                ustate.emit_status(f"Preview failed for {lab_id} — re-logging in…")
                try:
                    state.coa_session.relogin()
                except Exception:
                    rec.status = STATUS_ERROR
                    ustate.emit_sse({"type": "sample_status", "tab": tab, "lab_id": lab_id, "status": STATUS_ERROR})
                    return
            else:
                rec.status = STATUS_ERROR
                ustate.emit_sse({"type": "sample_status", "tab": tab, "lab_id": lab_id, "status": STATUS_ERROR})
                ustate.emit_status(f"Preview error for {lab_id}: {exc}")
                return


def _sif_absence_reason(order_id: Optional[int]) -> str:
    """Classify a missing SIF, fetching the order only when one is missing.

    Deliberately not fetched up front: the overwhelming majority of orders do
    have a SIF (36 of 45 sampled), and paying an extra QBench call on every
    sample to answer a question that rarely arises would slow the common path
    for nothing. Results are memoised per order, since every sample in an
    order shares the answer.
    """
    if not order_id:
        return SIF_MISSING

    key = int(order_id)
    with state._sif_cache_lock:
        cached = state.sif_absence_cache.get(key)
    if cached:
        return cached

    order = None
    try:
        order = state.api_client.fetch_order(key)
    except Exception as exc:
        # Unreadable order → SIF_MISSING, never a claim of portal entry.
        logger.warning("SIF: could not read order %s to classify a missing SIF: %s",
                       key, exc)

    reason = classify_missing_sif(order)
    with state._sif_cache_lock:
        state.sif_absence_cache[key] = reason
    return reason


def fetch_sif_for_sample(tab: str, lab_id: str, ustate: UserState) -> None:
    """Fetch the SIF PDF for a sample, find the matching page via barcode scanning."""
    key = (tab, lab_id)
    rec = ustate.records.get(key)
    if not rec:
        return
    if rec.sif_status == "found":
        return

    rec.sif_status = "loading"
    ustate.emit_sse({"type": "sif_status", "tab": tab, "lab_id": lab_id, "status": "loading"})

    def _no_sif(order_id_for_lookup):
        """Report *why* there is no SIF, not merely that there isn't one."""
        status = _sif_absence_reason(order_id_for_lookup)
        rec.sif_status = status
        ustate.emit_sse({"type": "sif_status", "tab": tab, "lab_id": lab_id, "status": status})

    order_id = rec.order_id
    if not order_id:
        _no_sif(None)
        return

    try:
        # Download + count the order's SIF PDF once (shared across all samples
        # in the order); only the per-lab page lookup is done per sample.
        doc_info = _sif_load_order_pdf(
            order_id, state.api_client, state.sif_order_cache, state._sif_cache_lock
        )
        if not doc_info:
            _no_sif(order_id)
            return

        pdf_bytes, total_pages, candidate = doc_info

        # Per-lab page index, cached on the order entry so we never re-scan
        # the same PDF for a lab_id we've already located.
        with state._sif_cache_lock:
            entry = state.sif_order_cache.get(int(order_id)) or {}
            pages_by_lab = entry.get("pages_by_lab", {})
            page_num = pages_by_lab.get(lab_id, "MISS")
        if page_num == "MISS":
            page_num = _sif_find_page(pdf_bytes, lab_id)
            with state._sif_cache_lock:
                entry = state.sif_order_cache.get(int(order_id))
                if entry:
                    entry.setdefault("pages_by_lab", {})[lab_id] = page_num

        rec.sif_pdf_bytes = pdf_bytes
        rec.sif_page = page_num
        rec.sif_total_pages = total_pages
        rec.sif_status = "found"
        ustate.emit_sse({
            "type": "sif_status", "tab": tab, "lab_id": lab_id,
            "status": "found", "sif_page": page_num, "sif_total_pages": total_pages,
        })
        logger.info("SIF found for %s: %s (page %s/%s)",
                    lab_id, _sif_get_filename(candidate),
                    page_num + 1 if page_num is not None else "?", total_pages)

    except Exception as exc:
        logger.warning("SIF fetch error for %s: %s", lab_id, exc)
        rec.sif_status = "error"
        ustate.emit_sse({"type": "sif_status", "tab": tab, "lab_id": lab_id, "status": "error"})


def cache_pdf(lab_id: str, url: str, ustate: UserState) -> None:
    """Download and cache PDF bytes for a user session."""
    if lab_id in ustate.pdf_cache or lab_id in ustate.pdf_loading:
        return
    ustate.pdf_loading.add(lab_id)
    try:
        resp = _requests.get(url, timeout=60)
        resp.raise_for_status()
        ustate.pdf_cache[lab_id] = resp.content
    except Exception as exc:
        logger.warning("Failed to cache PDF for %s: %s", lab_id, exc)
    finally:
        ustate.pdf_loading.discard(lab_id)


# ══════════════════════════════════════════════════════════════════════════════
# Flask Application
# ══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)
app.secret_key = APP_SECRET_KEY
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
# Serve template edits from disk immediately, matching static-file
# behavior. With the default (cache until restart), editing index.html +
# app.js together deploys only the JS half — the resulting HTML/JS skew
# bricked the boot screen on 2026-07-10.
app.config["TEMPLATES_AUTO_RELOAD"] = True


def _qbench_error_response(exc):
    """Build a clean, retryable JSON response for a QBench failure.

    QBench rate-limit (429) and transient errors used to escape /api/search as
    a raw HTML 500 that the frontend couldn't parse ("Search failed"). This
    gives every route a uniform, actionable 503 instead.
    """
    return jsonify({
        "error": "QBench is rate-limiting or temporarily unavailable; please retry in a moment.",
        "detail": str(exc)[:200],
    }), 503


@app.errorhandler(QBenchAPIError)
def _handle_qbench_error(exc):
    logger.warning("QBenchAPIError surfaced to client: %s", exc)
    return _qbench_error_response(exc)


# Health endpoints are machines asking after the app, not people using it.
# Counting them as activity would make the app look permanently busy: the
# updater polls /healthz to decide whether anyone would be interrupted by a
# deploy, and the frontend polls /api/health while waiting for a restart.
# Either would also suppress the 3 AM auto-restart, which is gated on the same
# timestamp — so a monitoring call would silently disable a token refresh.
_NON_ACTIVITY_PATHS = frozenset({"/healthz", "/api/health"})


@app.before_request
def track_activity():
    """Record that a *reviewer* did something.

    Deliberately gated on holding a session rather than on the path. Something
    on this network has requested ``GET /`` every ~2.2 minutes since long
    before this deployment (it is all over the old server.log), and counting
    that kept the app permanently "busy" — which both blocks an idle-gated
    deploy and suppresses the 3 AM auto-restart. Excluding ``/`` instead is
    wrong, because ``/`` is also exactly what a real reviewer opens. A session
    separates them: a monitor carries no cookie, and anyone with work in
    progress always does.
    """
    global _last_request_time
    if request.path not in _NON_ACTIVITY_PATHS and session.get("uid"):
        _last_request_time = time.time()
    # Log non-static, non-polling requests for debugging
    if not request.path.startswith("/static/") and request.path != "/api/events":
        logger.debug("REQUEST %s %s from %s", request.method, request.path, request.remote_addr)


@app.after_request
def no_cache(response):
    """Prevent Cloudflare (or any proxy) from caching API responses."""
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    """Lightweight health check — no auth required. Used by frontend restart polling."""
    return jsonify({"ok": True, "pid": os.getpid(), "t": time.time()})


@app.route("/healthz")
def healthz():
    """Deployment health check — **no auth**, no outbound calls.

    Distinct from ``/api/health`` on purpose: that one answers the frontend's
    "is my server back yet" poll, this one is the updater's contract. It runs
    against a release started on a scratch port before that release is live,
    so it must answer without a session and must answer fast.

    ``labcore`` is deliberately last-known state rather than a probe. LabCore
    is a real internet hop behind Cloudflare; probing it here would let a
    momentary blip on their side fail a health check and roll back a release
    that was never broken. Reporting it at all is still useful — the updater
    logs it, and a release that comes up with LabCore unreachable is worth a
    human look even though it is not a rollback trigger.
    """
    last = getattr(getattr(state, "labcore", None), "last_reachable", None)
    labcore = "unknown" if last is None else ("reachable" if last else "unreachable")
    with _sessions_lock:
        active_sessions = len(user_sessions)
    return jsonify({
        "status": "ok",
        "version": APP_VERSION,
        "labcore": labcore,
        "pid": os.getpid(),
        # Whether a deploy would interrupt anyone. A session is a reviewer with
        # records and a PDF cache in memory; restarting loses that and makes
        # them re-pull, so sessions matter more here than raw request age.
        "active_sessions": active_sessions,
        "idle_seconds": round(time.time() - _last_request_time, 1),
    })


# ── Portal Auth ──────────────────────────────────────────────────────────────

@app.route("/api/portal-session", methods=["GET", "POST"])
def portal_session_check():
    ustate = get_user_state()
    if ustate is None:
        return jsonify({"logged_in": False})
    return jsonify({"logged_in": True, "name": ustate.name})


def _resolve_identity(body):
    """Turn a login body into a LabLink account name.

    Accepts either form the UI can produce — a scanned ``code`` or a typed
    ``username``/``password`` — and returns ``(name, error_response)`` with
    exactly one of the two set. Both go to the same LabCore endpoint, so the
    identity a card produces and the identity a password produces are the
    same thing and can never drift apart.

    A deliberately supplied ``name`` is ignored: who did the review is
    LabCore's answer, not the client's claim.
    """
    code = str(body.get("code", "")).strip()
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))

    try:
        if code:
            name = state.labcore.authenticate_card(code)
            return (name, None) if name else (
                None, (jsonify({"error": "Card not recognised."}), 401))
        if not username or not password:
            return None, (jsonify(
                {"error": "Username and password are required."}), 400)
        name = state.labcore.authenticate_user(username, password)
    except LabCoreUnavailable as exc:
        # Distinct from a bad password on purpose — one means try again, the
        # other means go find someone.
        return None, _labcore_down(exc)

    if not name:
        return None, (jsonify({"error": "Invalid username or password."}), 401)
    return name, None


@app.route("/api/portal-login", methods=["POST"])
def portal_login():
    """Log in with a LabLink username and password.

    The alternative to tapping a card, not a separate account system: the
    credentials are the reviewer's LabLink ones, checked by LabCore, and the
    session takes the canonical account name it returns. There used to be a
    shared portal password plus a typed "Your Name" box, which meant the
    audit log and Command Center recorded a self-declared string.
    """
    name, error = _resolve_identity(request.json or {})
    if error:
        return error

    uid = str(uuid.uuid4())
    ustate = UserState(uid, name)
    with _sessions_lock:
        user_sessions[uid] = ustate
    session["uid"] = uid
    session.permanent = True
    log_login_event("LOGIN", name, request.remote_addr)
    state.change_log.session("login", user=name, method="password",
                             ip=request.remote_addr)
    logger.info("Portal login: %s", name)
    return jsonify({"ok": True, "name": name})


@app.route("/api/portal-card-login", methods=["POST"])
def portal_card_login():
    """Log in by tapping a LabLink keycard.

    The card is the whole login — no portal password, no separately typed
    initials. The session takes the LabLink account name LabCore resolves the
    card to, which is what makes Command Center's created_by/completed_by and
    the audit log attributable to a real person instead of hand-typed
    letters. Password login remains for a lost or unregistered card.
    """
    code = str((request.json or {}).get("code", "")).strip()
    if not code:
        # Wedge readers emit stray Enters; an empty scan is not an attempt.
        return jsonify({"error": "No card code received."}), 400

    try:
        username = state.labcore.authenticate_card(code)
    except LabCoreUnavailable as exc:
        # Distinct from a bad card on purpose: the reviewer needs to know
        # whether to re-tap or go find someone.
        return _labcore_down(exc)

    if not username:
        return jsonify({"error": "Card not recognised."}), 401

    uid = str(uuid.uuid4())
    ustate = UserState(uid, username)
    with _sessions_lock:
        user_sessions[uid] = ustate
    session["uid"] = uid
    session.permanent = True

    log_login_event("LOGIN(card)", username, request.remote_addr)
    # The code itself is deliberately not logged — it is a credential, and
    # anyone with read access to the share could otherwise clone a login.
    state.change_log.session("login", user=username, method="card",
                             ip=request.remote_addr)
    logger.info("Portal card login: %s", username)
    return jsonify({"ok": True, "name": username})


@app.route("/api/portal-logout", methods=["POST"])
def portal_logout():
    uid = session.get("uid")
    if uid:
        with _sessions_lock:
            ustate = user_sessions.pop(uid, None)
        if ustate:
            log_login_event("LOGOUT", ustate.name, request.remote_addr)
            state.change_log.session("logout", user=ustate.name,
                                     ip=request.remote_addr)
            logger.info("Portal logout: %s", ustate.name)
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/portal-reauth", methods=["POST"])
def portal_reauth():
    """Re-authenticate after an inactivity timeout, by card or password.

    Restores the existing session — with its records, results and PDF cache —
    only for the account that owns it. A different LabLink user is refused
    rather than handed the screen, because taking over an in-progress review
    would attribute every listing filed afterwards to the wrong person.
    """
    name, error = _resolve_identity(request.json or {})
    if error:
        return error

    uid = session.get("uid")
    if uid:
        with _sessions_lock:
            ustate = user_sessions.get(uid)
        if ustate:
            if ustate.name.strip().lower() != name.strip().lower():
                return jsonify({
                    "error": f"This session belongs to {ustate.name}. "
                             "Sign out to switch accounts.",
                    "wrong_user": True,
                }), 403
            ustate.last_active = time.time()
            log_login_event("REAUTH", ustate.name, request.remote_addr)
            return jsonify({"ok": True, "restored": True, "name": ustate.name})

    # Session was already cleaned up server-side — create a fresh one under
    # whoever just authenticated. There is nothing left to match against, but
    # the identity is still LabCore's answer rather than a client-sent name.
    new_uid = str(uuid.uuid4())
    ustate = UserState(new_uid, name)
    with _sessions_lock:
        user_sessions[new_uid] = ustate
    session["uid"] = new_uid
    session.permanent = True
    log_login_event("RELOGIN", ustate.name, request.remote_addr)
    return jsonify({"ok": True, "restored": False, "name": ustate.name})


@app.route("/api/heartbeat", methods=["POST"])
@require_portal
def heartbeat():
    return jsonify({"ok": True})


@app.route("/api/restart", methods=["POST"])
@require_portal
def restart_server():
    """Restart the server process. Run.pyw will auto-restart it."""
    ustate = get_user_state()
    logger.info("Manual restart requested by %s", ustate.name if ustate else "unknown")

    def _do_restart():
        time.sleep(1)  # let the response reach the client
        _graceful_shutdown("manual restart")

    threading.Thread(target=_do_restart, daemon=True).start()
    return jsonify({"ok": True, "message": "Server is restarting...", "old_pid": os.getpid()})


@app.route("/api/server-info", methods=["GET"])
@require_portal
def server_info():
    """Return server uptime and active user count (for restart button tooltip)."""
    with _sessions_lock:
        active = len(user_sessions)
    uptime_s = time.time() - _server_start_time
    hours, remainder = divmod(int(uptime_s), 3600)
    minutes, _ = divmod(remainder, 60)
    return jsonify({
        "uptime": f"{hours}h {minutes}m",
        "active_users": active,
    })


# ── QBench Login & Config ────────────────────────────────────────────────────

@app.route("/api/config", methods=["GET"])
@require_portal
def get_config():
    ustate = get_user_state()
    cfg = state.config
    return jsonify({
        "username": cfg.get("qbench_username", ""),
        "has_password": bool(cfg.get("qbench_password")),
        "logged_in": state.logged_in,
        "has_data": len(ustate.records) > 0,
    })


@app.route("/api/login", methods=["POST"])
@require_portal
def login():
    """Login to QBench via Playwright."""
    if not PLAYWRIGHT_AVAILABLE:
        msg = f"Playwright not available: {_playwright_error}" if _playwright_error else "Playwright not installed"
        return jsonify({"error": msg}), 500

    ustate = get_user_state()
    body = request.json or {}
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    save_creds = body.get("save", False)

    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400

    if save_creds:
        state.config["qbench_username"] = username
        state.config["qbench_password"] = password
        save_config(state.config)

    state.coa_session = COASession(
        username=username,
        password=password,
        report_config_id=state.config.get("report_config_id", REPORT_CONFIG_ID),
    )

    try:
        ustate.emit_status("Logging in to QBench…")
        state.coa_session.login(headless=True)
        state.logged_in = True
        state.upload_queue = UploadQueue(state.api_client)
        state.upload_queue.sse_broadcast = state._queue_sse
        ustate.emit_status("Logged in successfully.")
        return jsonify({"ok": True})
    except Exception as exc:
        ustate.emit_status(f"Login failed: {exc}")
        return jsonify({"error": str(exc)}), 401


# ── Tab / Sample Data ────────────────────────────────────────────────────────

@app.route("/api/start", methods=["POST"])
@require_portal
def start_pulling():
    if not state.logged_in:
        return jsonify({"error": "Not logged in to QBench"}), 401

    ustate = get_user_state()
    ustate.records.clear()
    ustate.session_results.clear()
    ustate.pdf_cache.clear()
    ustate.pdf_loading.clear()
    # Drop cached SIF PDFs on each daily pull so the shared cache can't grow
    # unbounded across the process lifetime.
    with state._sif_cache_lock:
        state.sif_order_cache.clear()

    body = request.get_json(silent=True) or {}
    mode = body.get("mode") if isinstance(body, dict) else None
    if mode not in ("info", "tests"):
        mode = "tests"

    yesterday = business_days_ago(2)
    due_out   = business_days_ago(3)

    # Dispatch the mode's PRIORITY tab first so its samples populate
    # ahead of the others. The shared QBench rate-limiter is FIFO; the
    # first thread to call .acquire() gets the first slots, which
    # translates to "samples appearing first in the UI." Info opens on
    # Intaked, Tests opens on Due Out — match the default tab.
    if mode == "info":
        intaked = business_days_ago(1)
        threading.Thread(target=fetch_samples_for_tab, args=("Intaked", intaked, ustate), daemon=True).start()
        threading.Thread(target=fetch_samples_for_tab, args=("Yesterday", yesterday, ustate), daemon=True).start()
        threading.Thread(target=fetch_samples_for_tab, args=("Due Out", due_out, ustate), daemon=True).start()
    else:
        threading.Thread(target=fetch_samples_for_tab, args=("Due Out", due_out, ustate), daemon=True).start()
        threading.Thread(target=fetch_samples_for_tab, args=("Yesterday", yesterday, ustate), daemon=True).start()
        threading.Thread(target=fetch_re_review_samples, args=(ustate,), daemon=True).start()

    ustate.emit_status("Started pulling from QBench…")
    return jsonify({"ok": True})


@app.route("/api/tabs/<tab_name>")
@require_portal
def get_tab(tab_name: str):
    ustate = get_user_state()
    records = ustate.get_tab_records(tab_name)
    return jsonify({
        "tab": tab_name,
        "samples": [r.to_dict() for r in records],
    })


def _parse_search_query(query: str) -> List[str]:
    """Parse a search query that may contain comma-separated values and/or ranges.

    Examples:
        "32217"           → ["32217"]
        "32217,32219"     → ["32217", "32219"]
        "32217-32222"     → ["32217", "32218", "32219", "32220", "32221", "32222"]
        "32217-32219,32225" → ["32217", "32218", "32219", "32225"]
    Returns an empty list if the query doesn't look like a multi-ID query.
    """
    import re
    if "," not in query and not re.match(r"^\d+\s*-\s*\d+$", query):
        return []

    parts = [p.strip() for p in query.split(",") if p.strip()]
    result: List[str] = []
    for part in parts:
        range_match = re.match(r"^(\d+)\s*-\s*(\d+)$", part)
        if range_match:
            start, end = int(range_match.group(1)), int(range_match.group(2))
            if start > end:
                start, end = end, start
            if end - start > 500:
                continue
            for i in range(start, end + 1):
                result.append(str(i))
        else:
            result.append(part)
    return result


@app.route("/api/search", methods=["POST"])
@require_portal
def search_samples():
    if not state.logged_in:
        return jsonify({"error": "Not logged in to QBench"}), 401

    ustate = get_user_state()
    body = request.json or {}
    query = body.get("query", "").strip()
    if not query:
        return jsonify({"error": "Query required"}), 400

    keys_to_del = [k for k in ustate.records if k[0] == "Search"]
    for k in keys_to_del:
        del ustate.records[k]

    multi_ids = _parse_search_query(query)
    samples_raw: List[Dict[str, Any]] = []

    if multi_ids:
        ustate.emit_status(f"Searching for {len(multi_ids)} lab IDs…")
        seen_ids: set = set()
        for lab_id_q in multi_ids:
            results = state.api_client.fetch_samples_by_lab_id(lab_id_q)
            for s in results:
                sid = s.get("id")
                if sid and sid not in seen_ids:
                    seen_ids.add(sid)
                    samples_raw.append(s)
    else:
        ustate.emit_status(f"Searching for '{query}'…")
        samples_raw = state.api_client.fetch_samples_by_lab_id(query)
        if not samples_raw:
            samples_raw = state.api_client.fetch_samples_by_lab_id_prefix(query)
        if not samples_raw and query.isdigit():
            try:
                s = state.api_client.fetch_sample(int(query))
                if s:
                    samples_raw = [s]
            except Exception:
                pass

    if not samples_raw:
        ustate.emit_status(f"No samples found for '{query}'.")
        return jsonify({"tab": "Search", "samples": []})

    sample_test_map: Dict[int, Dict[str, Any]] = {}
    for s in samples_raw:
        sid = s.get("id")
        if not sid:
            continue
        sid = int(sid)
        sample_test_map[sid] = {
            "sample_id": sid,
            "test_ids": [],
            "order_id": s.get("order_id"),
            "lab_id": s.get("lab_id", str(sid)),
        }

    tests = state.api_client.fetch_tests_for_sample_ids(list(sample_test_map.keys()))
    for t in tests:
        sid = t.get("sample_id") or (t.get("sample") or {}).get("id")
        if not sid:
            continue
        sid = int(sid)
        if sid in sample_test_map:
            sample_test_map[sid]["test_ids"].append(int(t["id"]))

    samples = [info for info in sample_test_map.values() if info["test_ids"]]

    for info in samples:
        lab_id = info.get("lab_id") or str(info.get("sample_id", "?"))
        rec = SampleRecord(
            lab_id=lab_id,
            tab="Search",
            sample_id=info.get("sample_id"),
            test_ids=info.get("test_ids", []),
            order_id=info.get("order_id"),
            info=info,
        )
        ustate.add_record(rec)

    ustate.emit_status(f"Found {len(samples)} sample(s) for '{query}'.")

    if state.coa_session and state.logged_in:
        for info in samples:
            lab_id = info.get("lab_id") or str(info.get("sample_id", "?"))
            PREVIEW_POOL.submit(generate_preview_for_sample, "Search", lab_id, ustate)

    records = ustate.get_tab_records("Search")
    return jsonify({
        "tab": "Search",
        "samples": [r.to_dict() for r in records],
    })


@app.route("/api/custom-day", methods=["POST"])
@require_portal
def load_custom_day():
    if not state.logged_in:
        return jsonify({"error": "Not logged in to QBench"}), 401

    ustate = get_user_state()
    body = request.json or {}
    date_str = body.get("date", "")
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Invalid date format"}), 400

    keys_to_del = [k for k in ustate.records if k[0] == "Custom Day"]
    for k in keys_to_del:
        lab_id = ustate.records[k].lab_id
        ustate.pdf_cache.pop(lab_id, None)
        del ustate.records[k]

    threading.Thread(target=fetch_samples_for_tab, args=("Custom Day", target, ustate), daemon=True).start()
    return jsonify({"ok": True})


# ── PDF ──────────────────────────────────────────────────────────────────────

_RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)")


def _inline_pdf_response(
    pdf_bytes: bytes,
    *,
    filename: str,
    etag: Optional[str] = None,
    cache: str = "private, max-age=3600",
) -> Response:
    """Serve a PDF inline with proper Range support.

    Safari's iframe PDF viewer (QuickLook) probes with a Range request before
    rendering. If the server advertises `Accept-Ranges: bytes` but answers with
    a 200 OK full body, Safari renders blank. This helper:
      - parses the incoming Range header and returns 206 Partial Content,
      - sets Content-Disposition: inline; filename="..." so the URL has a
        visible PDF identity even though the path has no .pdf extension,
      - emits a stable ETag so 304 short-circuits the second request.
    """
    total = len(pdf_bytes)
    etag_val = f'"{etag or filename}-{total}"'
    common_headers = {
        "Content-Type": "application/pdf",
        "Content-Disposition": f'inline; filename="{filename}"',
        "Accept-Ranges": "bytes",
        "Cache-Control": cache,
        "ETag": etag_val,
        # Safari respects this for cross-frame PDF; allow same-origin only.
        "X-Content-Type-Options": "nosniff",
    }

    # If-None-Match → 304
    if request.headers.get("If-None-Match") == etag_val:
        resp = Response(status=304)
        for k, v in common_headers.items():
            resp.headers[k] = v
        return resp

    range_header = request.headers.get("Range")
    if range_header:
        m = _RANGE_RE.match(range_header.strip())
        if m:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else total - 1
            if start >= total or start < 0:
                resp = Response(status=416)
                resp.headers["Content-Range"] = f"bytes */{total}"
                return resp
            end = min(end, total - 1)
            chunk = pdf_bytes[start : end + 1]
            resp = Response(chunk, status=206)
            for k, v in common_headers.items():
                resp.headers[k] = v
            resp.headers["Content-Range"] = f"bytes {start}-{end}/{total}"
            resp.headers["Content-Length"] = str(len(chunk))
            return resp

    resp = Response(pdf_bytes, status=200)
    for k, v in common_headers.items():
        resp.headers[k] = v
    resp.headers["Content-Length"] = str(total)
    return resp


def _pdf_response(pdf_bytes: bytes, lab_id: str) -> Response:
    return _inline_pdf_response(
        pdf_bytes,
        filename=f"COA_{lab_id}.pdf",
        etag=lab_id,
        cache="private, max-age=3600, immutable",
    )


@app.route("/api/pdf/<lab_id>")
@require_portal
def get_pdf(lab_id: str):
    ustate = get_user_state()
    if lab_id in ustate.pdf_cache:
        return _pdf_response(ustate.pdf_cache[lab_id], lab_id)

    rec = None
    for r in ustate.records.values():
        if r.lab_id == lab_id and r.preview_url:
            rec = r
            break

    if rec and rec.preview_url:
        try:
            resp = _requests.get(rec.preview_url, timeout=60)
            resp.raise_for_status()
            ustate.pdf_cache[lab_id] = resp.content
            return _pdf_response(resp.content, lab_id)
        except Exception as exc:
            return jsonify({"error": f"Failed to fetch PDF: {exc}"}), 502

    return jsonify({"error": "PDF not available"}), 404


@app.route("/api/pdf/<lab_id>/download")
@require_portal
def download_pdf(lab_id: str):
    ustate = get_user_state()
    if lab_id in ustate.pdf_cache:
        pdf_bytes = ustate.pdf_cache[lab_id]
    else:
        rec = None
        for r in ustate.records.values():
            if r.lab_id == lab_id and r.preview_url:
                rec = r
                break
        if not rec or not rec.preview_url:
            return jsonify({"error": "PDF not available"}), 404
        try:
            resp = _requests.get(rec.preview_url, timeout=60)
            resp.raise_for_status()
            pdf_bytes = resp.content
            ustate.pdf_cache[lab_id] = pdf_bytes
        except Exception as exc:
            return jsonify({"error": f"Failed to fetch PDF: {exc}"}), 502

    resp = Response(pdf_bytes, mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f'attachment; filename="COA_{lab_id}.pdf"'
    resp.headers["Content-Length"] = len(pdf_bytes)
    return resp


@app.route("/api/sif/<lab_id>")
@require_portal
def get_sif(lab_id: str):
    ustate = get_user_state()
    rec = None
    for r in ustate.records.values():
        if r.lab_id == lab_id:
            rec = r
            break

    if not rec or not rec.sif_pdf_bytes:
        return jsonify({"error": "SIF not available"}), 404

    pdf_bytes = rec.sif_pdf_bytes
    if rec.sif_page is not None:
        single = _sif_extract_page(pdf_bytes, rec.sif_page)
        if single:
            pdf_bytes = single

    return _inline_pdf_response(
        pdf_bytes,
        filename=f"SIF_{lab_id}.pdf",
        etag=f"sif-{lab_id}-{rec.sif_page or 0}",
    )


# ── Tests (for the bottom editor) ───────────────────────────────────────────

@app.route("/api/tests/<lab_id>")
@require_portal
def get_tests(lab_id: str):
    ustate = get_user_state()
    rec = None
    for r in ustate.records.values():
        if r.lab_id == lab_id:
            rec = r
            break

    if not rec or not rec.sample_id:
        return jsonify({"tests": []})

    if rec.tests_data is not None:
        return jsonify({"tests": rec.tests_data})

    try:
        raw_tests = state.api_client.fetch_tests_for_sample_ids([rec.sample_id])
        tests_out = []
        for t in raw_tests:
            assay = t.get("assay") or {}
            assay_data = assay.get("data") or {}
            test_name = (
                assay_data.get("title")
                or assay.get("custom_formatted_id")
                or assay.get("name")
                or f"Assay {t.get('assay_id', '?')}"
            )
            tests_out.append({
                "test_id": t.get("id"),
                "test_name": str(test_name).strip(),
                "results": t.get("results") or "",
                "sample_id": t.get("sample_id"),
            })
        rec.tests_data = tests_out
        return jsonify({"tests": tests_out})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/tests/<int:test_id>", methods=["PATCH"])
@require_portal
def update_test(test_id: int):
    if not state.upload_queue:
        return jsonify({"error": "Not logged in to QBench"}), 401

    ustate = get_user_state()
    body = request.json or {}
    value = body.get("value", "")
    state.upload_queue.enqueue(test_id, str(value))

    # Capture what it was *before* the cached copy is overwritten below —
    # "who changed this result and what was it before" is the question this
    # log category exists to answer.
    old_value = None
    edited_lab_id = None
    assay = None
    for rec in ustate.records.values():
        if rec.tests_data:
            for t in rec.tests_data:
                if t["test_id"] == test_id:
                    old_value = t.get("results")
                    edited_lab_id = rec.lab_id
                    assay = t.get("assay") or t.get("test_name")
                    t["results"] = str(value)
                    break

    state.change_log.qbench_edit(
        "test_result",
        user=ustate.name, test_id=test_id, lab_id=edited_lab_id, assay=assay,
        old_value=old_value, new_value=str(value), ip=request.remote_addr,
    )

    return jsonify({"ok": True, "test_id": test_id, "value": value})


# ── Attachments & Comments ───────────────────────────────────────────────────

@app.route("/api/attachments/<lab_id>")
@require_portal
def get_attachments(lab_id: str):
    ustate = get_user_state()
    rec = None
    for r in ustate.records.values():
        if r.lab_id == lab_id:
            rec = r
            break

    if not rec or not rec.sample_id:
        return jsonify({"attachments": []})

    if rec.attachments is not None:
        atts = []
        for a in rec.attachments:
            fname = (
                (a.get("asset") or {}).get("file_name")
                or a.get("file_name")
                or a.get("name")
                or f"Attachment {a.get('id', '?')}"
            )
            is_report = bool(
                a.get("attach_to_report") or a.get("include_in_report") or a.get("include_on_coa")
            )
            atts.append({"id": a.get("id"), "filename": fname, "is_report": is_report})
        return jsonify({"attachments": atts})

    try:
        raw = state.api_client.fetch_all_attachments_for_sample(int(rec.sample_id))
        rec.attachments = raw
        atts = []
        for a in raw:
            fname = (
                (a.get("asset") or {}).get("file_name")
                or a.get("file_name")
                or a.get("name")
                or f"Attachment {a.get('id', '?')}"
            )
            is_report = bool(
                a.get("attach_to_report") or a.get("include_in_report") or a.get("include_on_coa")
            )
            atts.append({"id": a.get("id"), "filename": fname, "is_report": is_report})
        return jsonify({"attachments": atts})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/attachments/<int:attachment_id>", methods=["DELETE"])
@require_portal
def delete_attachment(attachment_id: int):
    ustate = get_user_state()
    # Grab the filename before the record is dropped, so the log names what
    # was destroyed rather than just an id.
    filename, owner_lab_id = None, None
    for rec in ustate.records.values():
        for att in (rec.attachments or []):
            if att.get("id") == attachment_id:
                filename = att.get("filename") or att.get("name")
                owner_lab_id = rec.lab_id
                break

    ok = state.api_client.delete_attachment(attachment_id)
    if ok:
        for rec in ustate.records.values():
            if rec.attachments:
                rec.attachments = [a for a in rec.attachments if a.get("id") != attachment_id]
        # Irreversible in QBench — it has to be attributable.
        state.change_log.qbench_edit(
            "attachment_deleted",
            user=ustate.name, attachment_id=attachment_id,
            filename=filename, lab_id=owner_lab_id, ip=request.remote_addr,
        )
        return jsonify({"ok": True})
    return jsonify({"error": "Failed to delete"}), 500


@app.route("/api/comments/<lab_id>")
@require_portal
def get_comments(lab_id: str):
    ustate = get_user_state()
    rec = None
    for r in ustate.records.values():
        if r.lab_id == lab_id:
            rec = r
            break

    if not rec or not rec.sample_id:
        return jsonify({"comments": ""})

    try:
        detail = state.api_client.fetch_sample(int(rec.sample_id))
        raw = detail.get("comments") or ""
        if isinstance(raw, list):
            texts = []
            for c in raw:
                if isinstance(c, dict):
                    texts.append(c.get("text") or c.get("comment") or c.get("body") or str(c))
                else:
                    texts.append(str(c))
            text = "\n\n".join(texts)
        else:
            text = str(raw).strip()
        return jsonify({"comments": text})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/comments/<lab_id>", methods=["PATCH"])
@require_portal
def update_comments(lab_id: str):
    ustate = get_user_state()
    body = request.json or {}
    new_comments = body.get("comments", "")

    rec = None
    for r in ustate.records.values():
        if r.lab_id == lab_id:
            rec = r
            break

    if not rec or not rec.sample_id:
        return jsonify({"error": "Sample not found"}), 404

    if not state.upload_queue:
        return jsonify({"error": "Not logged in to QBench"}), 401

    # Hand the write to the server-side queue so a QBench rate-limit is
    # retried in the background and the comment is never lost if the reviewer
    # closes the window. The UI confirms via the comment_saved SSE event.
    state.upload_queue.enqueue_comment(int(rec.sample_id), new_comments, lab_id=lab_id, uid=ustate.uid)

    state.change_log.qbench_edit(
        "comments",
        user=ustate.name, lab_id=lab_id, sample_id=rec.sample_id,
        new_value=new_comments, ip=request.remote_addr,
    )
    return jsonify({"ok": True, "queued": True})


# ── Sample-info editor (Info mode right panel) ───────────────────────────────

# Whitelist of editable sample-level fields, derived from the QBench
# sample-info screen (2026-05-19 screenshot). The reviewer edits all 22
# of these; structural QBench keys (id, audit fields, dates QBench manages)
# are NOT in this set so PATCH never accepts them.
SAMPLE_EDITABLE_FIELDS = frozenset({
    "lab_id", "fw", "work_order", "accession_number", "fuel_type",
    "package_size", "po_number", "time_of_collection", "customer_sample_id",
    "sample_taken_from", "tank", "generator", "component_model",
    "tank_capacity", "point_of_collection", "quantity_tank", "site_location",
    "tags", "attachments", "comments", "Rush",
    # The source field is intentionally absent: QBench returns it as a
    # structured SOURCE object, so it renders read-only in the editor and
    # PATCH rejects it (2026-07-10). It stays in TOP_LEVEL_SAMPLE_FIELDS
    # below so a future re-enable nests it correctly.
})

# QBench's PATCH /samples endpoint expects standard sample fields at the
# top level of each row, but anything custom must be nested under
# `custom_fields`. This set enumerates the fields that LIVE at the top
# level so the PATCH handler can split incoming edits correctly.
#
# Sourced from a real QBench sample response (2026-05-20, sample 33383) —
# every key here was observed at the root of the QBench Sample object.
# Mis-nesting one of these under custom_fields makes QBench return 200 OK
# while silently dropping the value, which is exactly the "edit goes green
# but doesn't save" bug reported on 2026-05-20.
TOP_LEVEL_SAMPLE_FIELDS = frozenset({
    "lab_id", "source", "tags", "attachments", "comments", "accession_number",
    "fw", "work_order", "fuel_type", "package_size", "po_number",
    "time_of_collection", "customer_sample_id", "sample_taken_from",
    "tank", "generator", "component_model", "tank_capacity",
    "point_of_collection", "quantity_tank", "site_location", "Rush",
})


# ── LabVision → QBench sample-information sync ───────────────────────────────
#
# Sample information only. Test results are deliberately out of scope: they
# are the lab's actual measurements, and a mis-paired assay would overwrite
# one silently.

#: LabVision fields that describe the sample but can never be written to
#: QBench — showing them would promise a sync that quietly does nothing.
SYNC_SOURCE_EXCLUDE = frozenset({
    "lab_id", "sample_id", "customer_name", "order_id", "tests",
    "photo_count", "photo_has_blob",
})


def normalize_field_name(name: str) -> str:
    """Reduce a field name for matching: case, spaces, and punctuation go.

    ``Work Order``, ``work_order`` and ``WORK-ORDER`` are the same field.
    Deliberately no stemming or fuzziness — ``tank_number`` must NOT collapse
    onto ``tank``, because writing a value into the wrong QBench field is
    worse than making the reviewer drag it.
    """
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def pair_sample_fields(source: Dict[str, Any], targets, current: Dict[str, Any]) -> List[dict]:
    """Guess a LabVision-field → QBench-field pairing for the sync dialog.

    Exact name match first, then normalized. Anything still unmatched is
    returned with ``target=None`` for the reviewer to drag onto a destination.

    ``current`` is QBench's present value per field, used to mark:
      * ``unchanged`` — QBench already holds this value, nothing to do;
      * ``clash``     — QBench holds something *different*. Reported, never
                        written unless the reviewer opts in, because
                        overwriting a released value is the one thing that
                        cannot be undone from here.
    """
    targets = set(targets)
    by_norm = {normalize_field_name(t): t for t in targets}

    pairs: List[dict] = []
    for src, raw_value in (source or {}).items():
        if src in SYNC_SOURCE_EXCLUDE:
            continue
        value = "" if raw_value is None else str(raw_value).strip()
        if not value:
            # Never sync a blank over a populated QBench field.
            continue

        target = None
        match = None
        if src in targets:
            target, match = src, "exact"
        else:
            hit = by_norm.get(normalize_field_name(src))
            if hit:
                target, match = hit, "normalized"

        existing = ""
        if target is not None:
            existing = "" if current.get(target) is None else str(current.get(target)).strip()

        pairs.append({
            "source": src,
            "value": value,
            "target": target,
            "match": match,
            "current": existing,
            "unchanged": bool(target and existing == value),
            "clash": bool(target and existing and existing != value),
        })
    return pairs


def _display_value(raw: Any) -> str:
    """One line of text for a field value, whatever shape it arrived in.

    QBench returns lists for `tags`/`attachments` and a dict for `source`;
    LabVision returns a list for `tests`. Rendering those raw would fill a
    column with JSON, so a collection reports its size instead.
    """
    if raw is None:
        return ""
    if isinstance(raw, (list, tuple, dict)):
        n = len(raw)
        return f"{n} item{'' if n == 1 else 's'}" if n else ""
    return str(raw).strip()


def lab_vision_field_rows(source: Dict[str, Any]) -> List[dict]:
    """Every field LabVision holds for the sample, in LabVision's own order.

    ``syncable`` is False for the fields QBench will not take — the sync board
    still shows them (they are part of the record the reviewer is reading) but
    does not let them be dragged, because offering a drag the route would
    refuse with a 400 promises a write that cannot happen. The set is
    ``SYNC_SOURCE_EXCLUDE``, so the column and ``pair_sample_fields`` can never
    disagree about what is syncable.
    """
    return [
        {"name": name,
         "value": _display_value(raw),
         "syncable": name not in SYNC_SOURCE_EXCLUDE}
        for name, raw in (source or {}).items()
    ]


def qbench_field_rows(current: Dict[str, Any]) -> List[dict]:
    """Every QBench field the board can drop onto, then everything else.

    Editable rows are the whole of ``SAMPLE_EDITABLE_FIELDS`` — *including*
    the ones no LabVision name matched. Listing only auto-matched targets is
    what made `tank_number -> tank` impossible to express in the UI.

    The read-only tail is the rest of what QBench returned, so the reviewer
    sees the full record without the board implying it can be written.
    """
    editable = [
        {"name": name,
         "current": _display_value((current or {}).get(name)),
         "editable": True}
        for name in sorted(SAMPLE_EDITABLE_FIELDS)
    ]
    read_only = [
        {"name": name, "current": _display_value(raw), "editable": False}
        # `custom_fields` itself is skipped: the route flattens it, so its
        # keys already appear on their own and the container would just be
        # a duplicate "N items" row.
        for name, raw in sorted((current or {}).items())
        if name not in SAMPLE_EDITABLE_FIELDS and name != "custom_fields"
    ]
    return editable + read_only


def lab_vision_tests(raw: Any) -> List[dict]:
    """LabVision's test list, normalised to exactly test/result/operator.

    LabCore drops the `operator` key entirely against an older database, so
    the shape is pinned here rather than left for the pane to defend against.
    """
    if not isinstance(raw, list):
        return []
    out: List[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        out.append({
            "test": _display_value(entry.get("test")),
            "result": _display_value(entry.get("result")),
            "operator": _display_value(entry.get("operator")),
        })
    return out


def _normalize_panels(raw: Any) -> List[str]:
    """Coerce QBench's per-sample `panels` data into a list of display
    strings. QBench may return any of: None, a string, a list of strings,
    a list of dicts ({title|name|id}), or a singleton dict."""
    if not raw:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return [str(raw)]
    out: List[str] = []
    for p in raw:
        if isinstance(p, dict):
            name = p.get("title") or p.get("name") or p.get("id")
            if name is not None:
                out.append(str(name))
        elif p:
            out.append(str(p))
    return out


def _find_sample_id_for_lab(ustate: "UserState", lab_id: str) -> Optional[int]:
    """Resolve a sample_id for `lab_id` using the user's in-memory records
    first, falling back to a QBench lookup if nothing is cached."""
    for rec in ustate.records.values():
        if rec.lab_id == lab_id and rec.sample_id:
            return int(rec.sample_id)
    try:
        matches = state.api_client.fetch_samples_by_lab_id(lab_id)
    except Exception as exc:
        logger.warning("fetch_samples_by_lab_id failed for %s: %s", lab_id, exc)
        return None
    for s in matches:
        sid = s.get("id") or s.get("sample_id")
        if sid:
            return int(sid)
    return None


@app.route("/api/field-settings", methods=["GET", "PUT"])
@require_portal
def field_settings():
    """Shared Sample Info field-visibility settings — one config for all
    reviewers, persisted to FIELD_SETTINGS_FILE across restarts."""
    if request.method == "PUT":
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"error": "Expected a JSON object"}), 400
        hidden = body.get("sample_info_hidden", [])
        if not isinstance(hidden, list):
            return jsonify({"error": "sample_info_hidden must be a list"}), 400
        settings = {
            "sample_info_hidden": sorted({str(k) for k in hidden}),
            "show_extra_fields": bool(body.get("show_extra_fields", True)),
        }
        save_field_settings(settings)
        logger.info("Field settings saved: %s", settings)
        return jsonify({"ok": True, "settings": settings})
    return jsonify(load_field_settings())


@app.route("/api/sample-info/<lab_id>", methods=["GET", "PATCH"])
@require_portal
def sample_info(lab_id: str):
    if not state.logged_in:
        return jsonify({"error": "Not logged in to QBench"}), 401

    ustate = get_user_state()
    sid = _find_sample_id_for_lab(ustate, lab_id)
    if sid is None:
        return jsonify({"error": f"Sample not found: {lab_id}"}), 404

    if request.method == "PATCH":
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"error": "Expected a JSON object"}), 400
        fields = {k: v for k, v in body.items() if k in SAMPLE_EDITABLE_FIELDS}
        if not fields:
            return jsonify({"error": "No editable fields in payload"}), 400
        # Split: anything that lives at the QBench Sample root goes flat;
        # everything else nests under custom_fields. Both halves of the
        # split are merged into a single payload for one PATCH call.
        payload: Dict[str, Any] = {}
        custom_fields: Dict[str, Any] = {}
        for k, v in fields.items():
            if k in TOP_LEVEL_SAMPLE_FIELDS:
                payload[k] = v
            else:
                custom_fields[k] = v
        if custom_fields:
            payload["custom_fields"] = custom_fields
        # Diagnostic: log the exact payload being sent to QBench so server
        # logs make "did the edit propagate?" debuggable post-hoc.
        logger.info(
            "PATCH /samples for lab_id=%s sid=%s payload=%s",
            lab_id, sid, payload,
        )
        try:
            qbench_response = state.api_client.update_sample(sid, payload)
        except Exception as exc:
            logger.exception("update_sample failed for %s payload=%s", lab_id, payload)
            return jsonify({"error": str(exc), "payload": payload}), 500
        logger.info("QBench accepted update for lab_id=%s response=%s", lab_id, qbench_response)
        state.change_log.qbench_edit(
            "sample_info",
            user=(get_user_state().name if get_user_state() else ""),
            lab_id=lab_id, sample_id=sid, fields=fields,
            ip=request.remote_addr,
        )
        return jsonify({
            "ok": True,
            "updated": list(fields.keys()),
            # Echo what we sent + what QBench returned so DevTools can
            # confirm propagation when troubleshooting "didn't save" reports.
            "payload": payload,
            "qbench_response": qbench_response,
        })

    # GET — fetch the full sample dict, flatten custom_fields onto top
    # level for uniform client rendering, and surface the panels list.
    try:
        sample = state.api_client.fetch_sample(sid)
    except Exception as exc:
        logger.exception("fetch_sample failed for %s", lab_id)
        return jsonify({"error": str(exc)}), 500
    custom = sample.get("custom_fields") if isinstance(sample, dict) else None
    if isinstance(custom, dict):
        for k, v in custom.items():
            sample.setdefault(k, v)
    panels = _normalize_panels(
        sample.get("panels") or sample.get("panel_ids") or sample.get("panel_names")
    )
    return jsonify({
        "lab_id": lab_id,
        "sample_id": sid,
        "sample": sample,
        "panels": panels,
        "editable_fields": sorted(SAMPLE_EDITABLE_FIELDS),
    })


# ── Good / Bad / Regenerate ──────────────────────────────────────────────────

@app.route("/api/mark", methods=["POST"])
@require_portal
def mark_sample():
    ustate = get_user_state()
    body = request.json or {}
    tab = body.get("tab", "")
    lab_id = body.get("lab_id", "")
    outcome = body.get("outcome", "")
    reason = body.get("reason", "")

    key = (tab, lab_id)
    rec = ustate.records.get(key)
    if not rec:
        return jsonify({"error": "Sample not found"}), 404

    if outcome == "good":
        # Deliberately does not close anything. The old code auto-completed
        # the Double Check row under hardcoded initials; whether to complete a
        # Command Center listing is now the reviewer's explicit choice, made
        # in the complete / continue / back-out prompt before this call.
        rec.status = STATUS_GOOD
        rec.reason = ""
        ustate.record_result(rec, "Good")

    elif outcome == "bad":
        reason = reason.strip()
        if not reason:
            return jsonify({"error": "Reason required"}), 400
        rec.status = STATUS_BAD
        rec.reason = reason
        # The listing itself is created by /api/cc/tasks before this call, so
        # a conflict can be resolved while the sample is still unmarked.
        cc_task_id = body.get("cc_task_id")
        if cc_task_id is not None:
            try:
                rec.cc_task_id = int(cc_task_id)
            except (TypeError, ValueError):
                rec.cc_task_id = None
        ustate.record_result(rec, "Bad", reason)

    elif outcome == "uncheck":
        # Back to "rendered, awaiting review" — NOT pending. The frontend
        # derives has_preview from status (see updateSampleStatus in app.js),
        # so pending would make an already-rendered COA display as though it
        # had never rendered: the preview visibly disappears even though the
        # PDF is still cached here. Nothing about changing your mind on a
        # verdict requires re-rendering, so the preview and cache are left
        # untouched.
        rec.status = STATUS_READY if rec.preview_url else STATUS_PENDING
        rec.reason = ""
        rec.cc_task_id = None
        ustate.clear_result(tab, lab_id)

    else:
        return jsonify({"error": "Invalid outcome"}), 400

    state.change_log.review(
        "unmark" if outcome == "uncheck" else "mark",
        user=ustate.name,
        lab_id=rec.lab_id,
        sample_id=rec.sample_id,
        tab=rec.tab,
        outcome={"good": "Good", "bad": "Bad", "uncheck": "Unchecked"}[outcome],
        reason=rec.reason,
        cc_task_id=rec.cc_task_id,
        ip=request.remote_addr,
    )

    ustate.emit_sse({"type": "sample_status", "tab": tab, "lab_id": lab_id, "status": rec.status})
    labels = {"good": "Good", "bad": "Bad", "uncheck": "un-marked"}
    ustate.emit_status(f"{lab_id} {labels[outcome]}")
    return jsonify({"ok": True, "status": rec.status})


# ══════════════════════════════════════════════════════════════════════════════
# Command Center (LabCore) proxy
#
# LabCore sends no CORS headers and implements no OPTIONS handler, so a page
# served from :5559 cannot call LabCore on :8080 from the browser. Every
# Command Center call is proxied through here.
# ══════════════════════════════════════════════════════════════════════════════

def _lab_vision_base_url() -> str:
    """The LabVision URL to hand the *browser*.

    LabVision is served by LabCore itself, so this is simply LabCore's base
    URL — a fixed public hostname (https://labvision.asaplabs.net, behind
    Cloudflare on 443) that resolves the same from the server and from every
    reviewer's browser.

    An earlier version derived this from ``request.host`` plus a LabCore port.
    That was only correct while LabCore ran on this machine; against a public
    hostname it produces ``http://<coa-reviewer-host>:8080``, which does not
    exist, and "Open in Lab Vision" would open a dead page for everyone.
    """
    return state.labcore.base_url


def _labcore_down(exc: Exception):
    """Uniform 503 so the frontend can show one 'LabCore unreachable' banner."""
    logger.warning("LabCore unavailable: %s", exc)
    return jsonify({"error": str(exc), "labcore_down": True}), 503


@app.route("/api/cc/config")
@require_portal
def cc_config():
    return jsonify({
        "lab_vision_url": _lab_vision_base_url(),
        "available": state.labcore.is_available(),
    })


@app.route("/api/cc/check/<path:lab_id>")
@require_portal
def cc_check(lab_id: str):
    """Just: does this sample already have an active listing?

    Split out from /api/cc/lookup for the hot path. Marking Good (and
    un-marking) must ask before closing a sample out, and that happens on
    every single sample a reviewer clears — measured against live LabCore,
    check_duplicate is ~150ms while the full lookup was ~272ms because it also
    fetched customer/fuel autofill that only the flag form needs.
    """
    try:
        dup = state.labcore.check_duplicate([lab_id])
    except LabCoreUnavailable as exc:
        return _labcore_down(exc)
    return jsonify({
        "lab_id": lab_id,
        "conflict": bool(dup.get("conflict")),
        "existing_tasks": dup.get("existing_tasks", []),
    })


@app.route("/api/cc/lookup/<path:lab_id>")
@require_portal
def cc_lookup(lab_id: str):
    """Everything the flag modal needs to open pre-filled, in one round trip:
    LabCore's customer/fuel for the sample plus any listing already covering
    it.

    The two calls are independent, so they run concurrently — in sequence the
    modal cost the sum of both (~272ms) instead of the slower one (~173ms).
    """
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            info_f = pool.submit(state.labcore.sample_info, lab_id)
            dup_f = pool.submit(state.labcore.check_duplicate, [lab_id])
            info = info_f.result()
            dup = dup_f.result()
    except LabCoreUnavailable as exc:
        return _labcore_down(exc)
    return jsonify({
        "lab_id": lab_id,
        "customer_name": info.get("customer_name", ""),
        "fuel_type": info.get("fuel_type", ""),
        "conflict": bool(dup.get("conflict")),
        "existing_tasks": dup.get("existing_tasks", []),
    })


@app.route("/api/cc/tasks", methods=["POST"])
@require_portal
def cc_create_task():
    ustate = get_user_state()
    body = request.json or {}

    problem = str(body.get("initial_problem", "")).strip()
    if not problem:
        return jsonify({"error": "A problem description is required."}), 400

    params = {
        "type": body.get("type") or "double_check",
        "customer": body.get("customer", ""),
        "initial_problem": problem,
        "context": body.get("context", ""),
        "next_action": body.get("next_action", ""),
        "status": body.get("status") or "open",
        "department": body.get("department", ""),
        "sample_ids": body.get("sample_ids") or [],
        "created_by": ustate.name,
        "source_program": "COAReviewer",
    }
    # Only set when the reviewer explicitly chose "create anyway" at the
    # conflict prompt — otherwise LabCore's per-lab_id dedup should win.
    if body.get("force_create"):
        params["force_create"] = True

    try:
        result = state.labcore.create_task(params)
    except LabCoreUnavailable as exc:
        return _labcore_down(exc)

    # Only a real creation is logged — a conflict means nothing was created,
    # and the log must not claim otherwise.
    if result.get("ok") and result.get("task_id") is not None:
        state.change_log.command_center(
            "listing_created",
            user=ustate.name,
            task_id=result.get("task_id"),
            lab_ids=[s.get("lab_id") for s in params["sample_ids"] if s.get("lab_id")],
            initial_problem=params["initial_problem"],
            type=params["type"],
            status=params["status"],
            department=params["department"],
            customer=params["customer"],
            forced=bool(params.get("force_create")),
            ip=request.remote_addr,
        )
    return jsonify(result)


@app.route("/api/cc/tasks/<int:task_id>/complete", methods=["POST"])
@require_portal
def cc_complete_task(task_id: int):
    ustate = get_user_state()
    notes = str((request.json or {}).get("notes", "")).strip()
    if not notes:
        return jsonify({"error": "Completion notes are required."}), 400
    try:
        result = state.labcore.complete_task(
            task_id, notes=notes, completed_by=ustate.name,
        )
    except LabCoreUnavailable as exc:
        return _labcore_down(exc)

    if result.get("ok"):
        state.change_log.command_center(
            "listing_completed",
            user=ustate.name, task_id=task_id, notes=notes,
            ip=request.remote_addr,
        )
    return jsonify(result)


@app.route("/api/sync-preview/<path:lab_id>")
@require_portal
def sync_preview(lab_id: str):
    """Everything the Lab Vision pane and the sync board need for one sample.

    * ``pairs``     — the auto-guessed LabVision → QBench pairing (one row per
                      syncable LabVision field, ``target=None`` where nothing
                      matched).
    * ``lv_fields`` — the whole LabVision record, for the board's left column.
    * ``qb_fields`` — every droppable QBench field plus a read-only tail, for
                      the right column.
    * ``tests``     — LabVision's test list, for the Tests-mode pane.

    ``qbench_read`` says whether QBench's current values were actually read;
    when it is false, ``current`` is blank everywhere and nothing can be
    called a clash, so the board must say so rather than imply every field is
    empty.
    """
    ustate = get_user_state()
    try:
        source = state.labcore.sample_data(lab_id)
    except LabCoreUnavailable as exc:
        return _labcore_down(exc)
    if not source:
        return jsonify({"error": f"Lab Vision has no record of {lab_id}."}), 404

    sid = _find_sample_id_for_lab(ustate, lab_id)
    current: Dict[str, Any] = {}
    qbench_read = False
    if sid is not None:
        try:
            sample = state.api_client.fetch_sample(sid)
            if isinstance(sample, dict):
                current = dict(sample)
                # QBench nests custom fields; flatten so a target name looks
                # the same here as it does on the way back in.
                nested = sample.get("custom_fields")
                if isinstance(nested, dict):
                    current.update(nested)
                qbench_read = True
        except Exception as exc:
            logger.warning("sync-preview: could not read QBench sample %s: %s", sid, exc)

    return jsonify({
        "lab_id": lab_id,
        "pairs": pair_sample_fields(source, SAMPLE_EDITABLE_FIELDS, current),
        "targets": sorted(SAMPLE_EDITABLE_FIELDS),
        "lv_fields": lab_vision_field_rows(source),
        "qb_fields": qbench_field_rows(current),
        "tests": lab_vision_tests(source.get("tests")),
        "qbench_read": qbench_read,
    })


@app.route("/api/sync-sample-info/<path:lab_id>", methods=["POST"])
@require_portal
def sync_sample_info(lab_id: str):
    """Apply the reviewer's chosen field pairing to QBench, then re-render.

    Only the pairs sent are written — anything the reviewer left unpaired or
    declined (a clash they didn't tick) is not touched.
    """
    ustate = get_user_state()
    if not state.logged_in:
        return jsonify({"error": "Not logged in to QBench"}), 401

    mappings = (request.json or {}).get("mappings") or []
    if not mappings:
        return jsonify({"error": "Nothing selected to sync."}), 400

    try:
        source = state.labcore.sample_data(lab_id)
    except LabCoreUnavailable as exc:
        return _labcore_down(exc)

    fields: Dict[str, Any] = {}
    for m in mappings:
        target = str((m or {}).get("target", "")).strip()
        src = str((m or {}).get("source", "")).strip()
        if target not in SAMPLE_EDITABLE_FIELDS:
            # QBench answers 200 and silently drops an unknown field, so a
            # bad target has to be refused here or the sync would look like
            # it worked.
            return jsonify({"error": f"QBench cannot accept the field '{target}'."}), 400
        value = source.get(src)
        if value is None or not str(value).strip():
            continue
        fields[target] = str(value).strip()

    if not fields:
        return jsonify({"error": "Nothing selected to sync."}), 400

    sid = _find_sample_id_for_lab(ustate, lab_id)
    if sid is None:
        return jsonify({"error": f"Sample not found: {lab_id}"}), 404

    # Same top-level/custom_fields split the Sample Info editor uses —
    # mis-nesting makes QBench accept the write and drop the value.
    payload: Dict[str, Any] = {}
    custom_fields: Dict[str, Any] = {}
    for k, v in fields.items():
        if k in TOP_LEVEL_SAMPLE_FIELDS:
            payload[k] = v
        else:
            custom_fields[k] = v
    if custom_fields:
        payload["custom_fields"] = custom_fields

    try:
        qbench_response = state.api_client.update_sample(sid, payload)
    except Exception as exc:
        logger.exception("sync-sample-info failed for %s payload=%s", lab_id, payload)
        return jsonify({"error": str(exc), "payload": payload}), 500

    state.change_log.qbench_edit(
        "sample_sync",
        user=ustate.name, lab_id=lab_id, sample_id=sid,
        fields=fields, source="LabVision", ip=request.remote_addr,
    )

    # Sample info feeds the COA, so the rendered preview is stale the moment
    # this lands. Re-render the one sample that changed.
    regenerated = False
    for (tab, lid), rec in list(ustate.records.items()):
        if lid != lab_id:
            continue
        _reset_for_regenerate(ustate, rec)
        ustate.emit_sse({"type": "sample_status", "tab": tab,
                         "lab_id": lab_id, "status": STATUS_LOADING})
        if state.coa_session and state.logged_in:
            PREVIEW_POOL.submit(generate_preview_for_sample, tab, lab_id, ustate)
        regenerated = True

    return jsonify({
        "ok": True, "updated": fields, "regenerated": regenerated,
        "qbench_response": qbench_response,
    })


@app.route("/api/cc/customers")
@require_portal
def cc_customers():
    try:
        return jsonify(state.labcore.customers())
    except LabCoreUnavailable as exc:
        return _labcore_down(exc)


@app.route("/api/regenerate", methods=["POST"])
@require_portal
def regenerate_preview():
    ustate = get_user_state()
    body = request.json or {}
    tab = body.get("tab", "")
    lab_id = body.get("lab_id", "")

    key = (tab, lab_id)
    rec = ustate.records.get(key)
    if not rec:
        return jsonify({"error": "Sample not found"}), 404

    _reset_for_regenerate(ustate, rec)
    ustate.emit_sse({"type": "sample_status", "tab": tab, "lab_id": lab_id, "status": STATUS_LOADING})
    PREVIEW_POOL.submit(generate_preview_for_sample, tab, lab_id, ustate)
    return jsonify({"ok": True})


def _reset_for_regenerate(ustate: UserState, rec: SampleRecord) -> None:
    """Clear every cached artefact for one sample so it re-renders from scratch."""
    rec.status = STATUS_LOADING
    rec.preview_url = None
    rec.attachments = None
    rec.tests_data = None
    rec.sif_pdf_bytes = None
    rec.sif_page = None
    rec.sif_total_pages = None
    rec.sif_status = "pending"
    ustate.pdf_cache.pop(rec.lab_id, None)
    ustate.pdf_loading.discard(rec.lab_id)
    # Drop the cached order SIF so a regenerate re-downloads it fresh, and the
    # cached "why is there no SIF" answer with it — a document attached since
    # the last look should be found rather than reported missing again.
    if rec.order_id is not None:
        with state._sif_cache_lock:
            state.sif_order_cache.pop(int(rec.order_id), None)
            state.sif_absence_cache.pop(int(rec.order_id), None)


@app.route("/api/regenerate-pending", methods=["POST"])
@require_portal
def regenerate_pending():
    """Re-render every sample on one tab that hasn't been judged yet.

    Preview URLs are short-lived (the underlying Amazon links expire) and the
    render can fail outright, which previously meant regenerating each stale
    sample by hand. Samples already marked Good or Bad are left alone — the
    reviewer is done with those, and re-rendering would discard what they are
    looking at for no reason.

    Returns immediately; each sample reports back over the existing
    sample_status SSE events as it finishes, so the reviewer keeps working.
    """
    ustate = get_user_state()
    tab = (request.json or {}).get("tab", "")

    stale = [r for r in ustate.get_tab_records(tab)
             if r.status not in (STATUS_GOOD, STATUS_BAD)]

    for rec in stale:
        _reset_for_regenerate(ustate, rec)
        ustate.emit_sse({
            "type": "sample_status", "tab": tab,
            "lab_id": rec.lab_id, "status": STATUS_LOADING,
        })

    if stale and state.coa_session and state.logged_in:
        for rec in stale:
            PREVIEW_POOL.submit(generate_preview_for_sample, tab, rec.lab_id, ustate)

    ustate.emit_status(f"[{tab}] Regenerating {len(stale)} pending sample(s)…")
    return jsonify({"ok": True, "count": len(stale)})


@app.route("/api/regenerate-selected", methods=["POST"])
@require_portal
def regenerate_selected():
    """Re-render an explicit set of samples the reviewer picked in the list.

    Unlike /api/regenerate-pending this does **not** skip samples already
    marked Good or Bad. Pending is a blanket sweep, so leaving judged samples
    alone is the safe default; selecting one by hand is a direct instruction
    and gets obeyed.
    """
    ustate = get_user_state()
    body = request.json or {}
    tab = body.get("tab", "")
    lab_ids = body.get("lab_ids") or []

    picked = []
    for lab_id in lab_ids:
        rec = ustate.records.get((tab, str(lab_id)))
        if rec is not None:
            picked.append(rec)

    for rec in picked:
        _reset_for_regenerate(ustate, rec)
        ustate.emit_sse({
            "type": "sample_status", "tab": tab,
            "lab_id": rec.lab_id, "status": STATUS_LOADING,
        })

    if picked and state.coa_session and state.logged_in:
        for rec in picked:
            PREVIEW_POOL.submit(generate_preview_for_sample, tab, rec.lab_id, ustate)

    if picked:
        ustate.emit_status(f"[{tab}] Regenerating {len(picked)} selected sample(s)…")
    return jsonify({"ok": True, "count": len(picked)})


# ── Good Samples Links ───────────────────────────────────────────────────────

@app.route("/api/good-links", methods=["POST"])
@require_portal
def good_links():
    ustate = get_user_state()
    body = request.json or {}
    selected_tabs = body.get("tabs", ["Yesterday", "Due Out", "Re-review", "Search", "Custom Day"])
    link_eligible = {"Yesterday", "Due Out", "Search", "Custom Day"}
    links = []

    for tab in selected_tabs:
        tab_rows = [r for r in ustate.session_results if r["tab"] == tab]
        if not tab_rows or tab not in link_eligible:
            continue
        good_ids = [str(r["sample_id"]) for r in tab_rows if r["outcome"] == "Good" and r["sample_id"]]
        if good_ids:
            params = "&".join(f"sample_ids={sid}" for sid in good_ids)
            url = f"https://asaplabs.qbench.net/tests?{params}&sort_order=DESC&view_config_id=17&page_size=700"
            links.append({"tab": tab, "count": len(good_ids), "url": url})

    return jsonify({"links": links})


# ── Export ───────────────────────────────────────────────────────────────────

@app.route("/api/export", methods=["POST"])
@require_portal
def export_csv():
    ustate = get_user_state()
    if not ustate.session_results:
        return jsonify({"error": "Nothing to export"}), 400

    body = request.json or {}
    selected_tabs = body.get("tabs", ["Yesterday", "Due Out", "Re-review", "Search", "Custom Day"])
    include_links = body.get("include_links", True)

    buf = io.StringIO()
    fieldnames = ["lab_id", "sample_id", "tab", "outcome", "reason", "reviewer", "date"]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    link_urls = []

    first = True
    for tab in selected_tabs:
        tab_rows = [r for r in ustate.session_results if r["tab"] == tab]
        if not tab_rows:
            continue
        if not first:
            buf.write("\n")
        first = False
        buf.write(f"# -- {tab} ({len(tab_rows)} reviewed) --\n")
        writer.writeheader()
        writer.writerows(tab_rows)

        if include_links and tab in ("Yesterday", "Due Out", "Search", "Custom Day"):
            good_ids = [str(r["sample_id"]) for r in tab_rows if r["outcome"] == "Good" and r["sample_id"]]
            if good_ids:
                params = "&".join(f"sample_ids={sid}" for sid in good_ids)
                url = f"https://asaplabs.qbench.net/tests?{params}&sort_order=DESC&view_config_id=17&page_size=700"
                buf.write(f"# QBench link: {url}\n")
                link_urls.append(url)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_path = ARCHIVE_DIR / f"review_{ustate.name}_{timestamp}.csv"
    out_path.write_text(buf.getvalue(), encoding="utf-8")

    return jsonify({
        "ok": True,
        "csv": buf.getvalue(),
        "links": link_urls,
        "filename": out_path.name,
    })


# ── Server-Sent Events ───────────────────────────────────────────────────────

@app.route("/api/events")
@require_portal
def sse_stream():
    ustate = get_user_state()
    q: queue.Queue = queue.Queue(maxsize=200)
    ustate._sse_queues.append(q)

    def generate():
        try:
            while True:
                try:
                    data = q.get(timeout=30)
                    yield f"data: {json.dumps(data)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            try:
                ustate._sse_queues.remove(q)
            except ValueError:
                pass

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


# ── Status log ───────────────────────────────────────────────────────────────

@app.route("/api/status")
@require_portal
def get_status():
    ustate = get_user_state()
    return jsonify({"log": ustate.status_log[-50:]})


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def auto_login_from_saved_creds() -> None:
    """If credentials are saved in config, log in to QBench automatically."""
    cfg = state.config
    username = cfg.get("qbench_username", "").strip()
    password = cfg.get("qbench_password", "").strip()
    if not username or not password:
        logger.info("No saved credentials — waiting for browser login.")
        return
    if not PLAYWRIGHT_AVAILABLE:
        msg = f"Playwright not available: {_playwright_error}" if _playwright_error else "Playwright not installed"
        logger.error("Cannot auto-login — %s", msg)
        state.broadcast_sse({"type": "auto_login_done", "ok": False, "error": msg})
        return

    def _status(msg: str) -> None:
        logger.info(msg)
        state.broadcast_sse({"type": "status", "message": msg})

    _status(f"Connecting to QBench as {username}…")
    state.coa_session = COASession(
        username=username,
        password=password,
        report_config_id=cfg.get("report_config_id", REPORT_CONFIG_ID),
    )
    try:
        _status("Launching headless browser…")
        state.coa_session.login(headless=True)
        state.logged_in = True
        state.upload_queue = UploadQueue(state.api_client)
        state.upload_queue.sse_broadcast = state._queue_sse
        _status("QBench login successful!")
        state.broadcast_sse({"type": "auto_login_done", "ok": True})
    except Exception as exc:
        logger.error("Auto-login failed: %s", exc, exc_info=True)
        state.coa_session = None
        err = str(exc)
        _status(f"Login failed: {err}")
        state.broadcast_sse({"type": "auto_login_done", "ok": False, "error": err})


def _port_has_listener(port: int, probe_timeout: float = 0.5) -> bool:
    """True if something is accepting connections on ``port`` right now.

    Deliberately probes by *connecting*, not by binding. A bind probe cannot
    answer this question reliably:

    * On Windows ``SO_REUSEADDR`` permits binding a port that another socket
      is already listening on — the bind succeeds, both sockets exist, and
      the OS keeps delivering connections to the original. That is exactly
      how the 2026-07-31 inert-duplicate incident happened.
    * On BSD/macOS a wildcard (``0.0.0.0``) bind succeeds over a listener
      bound to a specific address such as ``127.0.0.1``.

    Dropping ``SO_REUSEADDR`` from a bind probe is not the fix either: that
    would report "busy" for a port merely sitting in TIME_WAIT, which the
    real server (Werkzeug sets ``SO_REUSEADDR``) would bind without trouble.
    A successful connect is the only signal that means a server is really
    there.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=probe_timeout):
            return True
    except OSError:
        return False


def _wait_for_port(port: int, timeout: float = 10.0) -> bool:
    """Wait until no server is listening on `port`, up to `timeout` seconds.

    Returns True once the port is clear, False if a listener is still there
    when the timeout expires.
    """
    deadline = time.time() + timeout
    attempt = 0
    while True:
        if not _port_has_listener(port):
            return True
        attempt += 1
        if attempt == 1:
            logger.warning("Port %d still in use, waiting for release...", port)
        if time.time() >= deadline:
            logger.error(
                "Port %d still has a live listener after %.0fs.", port, timeout
            )
            return False
        time.sleep(1)


if __name__ == "__main__":
    try:
        if sys.stdout is not None:
            sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    try:
        if sys.stderr is not None:
            sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass

    ip = get_local_ip()
    port = int(os.environ.get("PORT", 5559))

    logger.info("="*60)
    logger.info("  COA Reviewer Web App starting (PID %d)", os.getpid())
    logger.info("="*60)

    print(f"\n{'='*60}")
    print(f"  COA Reviewer Web App")
    print(f"  Local:   http://127.0.0.1:{port}")
    print(f"  Network: http://{ip}:{port}")
    print(f"{'='*60}\n")
    sys.stdout.flush()

    logging.getLogger("werkzeug").setLevel(logging.INFO)

    # Wait for port to be free (handles restart race condition)
    if not _wait_for_port(port, timeout=15.0):
        logger.error("Port %d is still in use after 15s — cannot start. Exiting.", port)
        print(f"  ERROR: Port {port} is still in use. Cannot start.")
        sys.exit(1)

    if not PLAYWRIGHT_AVAILABLE:
        logger.warning("Playwright unavailable: %s", _playwright_error)
        print(f"  WARNING: Playwright unavailable — {_playwright_error}")
        print("  Run: pip install playwright && playwright install chromium")

    cfg = state.config
    if cfg.get("qbench_username") and cfg.get("qbench_password"):
        print("  Saved credentials found — will auto-login in background.")
        threading.Thread(target=auto_login_from_saved_creds, daemon=True).start()
    else:
        print("  No saved credentials — waiting for browser login.")
    print()
    sys.stdout.flush()

    try:
        logger.info("Starting Flask server on 0.0.0.0:%d", port)
        app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
    except OSError as e:
        logger.error("Failed to start server: %s", e)
        print(f"  ERROR: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error("Unexpected error during server startup: %s", e, exc_info=True)
        sys.exit(1)
