#!/usr/bin/env python3
"""
SIF Viewer Test App
====================
Standalone Flask app to test fetching and displaying the SIF (Sample Information Form)
alongside the COA for a given lab_id.

Workflow:
  1. Enter a lab_id
  2. App fetches the sample → gets order_id
  3. Fetches order-level attachments (and sample-level for comparison)
  4. Identifies the SIF PDF (usually named after the company)
  5. Downloads the SIF PDF, scans pages for the lab_id barcode using pyzbar
  6. Displays COA + SIF side by side, SIF extracted to the matching page

Usage:
    python sif_test_app.py
    → open http://localhost:5050
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests as _requests
from flask import Flask, Response, jsonify, render_template_string, request, send_file

from qbench_client import QBenchAPIClient, QBenchAPIError

# ── PyMuPDF ─────────────────────────────────────────────────────────────────
try:
    import fitz as _fitz
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False
    _fitz = None

# ── pyzbar (barcode detection) ──────────────────────────────────────────────
try:
    from pyzbar import pyzbar as _pyzbar
    from PIL import Image as _PILImage
    PYZBAR_AVAILABLE = True
except ImportError:
    PYZBAR_AVAILABLE = False
    _pyzbar = None
    _PILImage = None

# ── Playwright (for COA preview) ────────────────────────────────────────────
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# ── Config ──────────────────────────────────────────────────────────────────
APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "web_app_config.json"
QBENCH_BASE = "https://asaplabs.qbench.net"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("sif_test")


def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return {}


# ── Global state ────────────────────────────────────────────────────────────
api_client = QBenchAPIClient()
config = load_config()

# COA session (Playwright login for preview generation)
coa_session_cookies: Optional[_requests.Session] = None
csrf_token: str = ""

# Cache: lab_id → {sif_pdf_bytes, sif_page_num, sif_attachment_info, order_attachments, sample_attachments}
cache: Dict[str, dict] = {}

app = Flask(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Playwright COA login (reused from main app)
# ══════════════════════════════════════════════════════════════════════════════

def do_playwright_login() -> _requests.Session:
    """Log in via Playwright and return a requests.Session with cookies."""
    global csrf_token
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError("playwright not installed")
    username = config.get("qbench_username", "")
    password = config.get("qbench_password", "")
    if not username or not password:
        raise RuntimeError("Missing qbench_username / qbench_password in config")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(f"{QBENCH_BASE}/lims", wait_until="load")
        try:
            page.wait_for_selector("#qbenchLimsLoginEmail", timeout=12_000)
        except PWTimeout:
            raise RuntimeError("Login form did not load")

        page.fill("#qbenchLimsLoginEmail", username)
        page.fill("#qbenchLimsLoginPassword", password)
        page.click("button[type='submit']")

        try:
            page.wait_for_selector("#main-content, .navigation-menu, #assayTabsNav", timeout=15_000)
        except PWTimeout:
            pass

        if page.locator("#qbenchLimsLoginEmail").is_visible():
            raise RuntimeError("Login failed – check credentials")

        try:
            csrf_token = page.evaluate("document.getElementById('csrfToken')?.textContent || ''")
        except Exception:
            csrf_token = ""

        cookies = ctx.cookies()
        session = _requests.Session()
        for c in cookies:
            session.cookies.set(c["name"], c["value"], domain=c.get("domain", "").lstrip("."))
        session.headers.update({
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRF-Token": csrf_token,
            "Accept": "application/json, text/javascript, */*; q=0.01",
        })
        browser.close()
    return session


def generate_coa_preview(session: _requests.Session, sample_id: int, test_ids: List[int],
                         order_id: Optional[int], attachment_ids: Optional[List[int]]) -> Optional[str]:
    """Generate COA preview and return the final URL."""
    report_config_id = config.get("report_config_id", "18")
    data = [
        ("sample_id", sample_id),
        ("report_config_id", report_config_id),
        ("report_level", "sample"),
        ("order_id", order_id or ""),
    ]
    for tid in test_ids:
        data.append(("test_ids", tid))
    if attachment_ids:
        data.append(("use_default_attachments", "false"))
        for aid in attachment_ids:
            data.append(("attachment_ids", aid))
    else:
        data.append(("use_default_attachments", "true"))
    data.append(("csrf_token", csrf_token))

    resp = session.post(f"{QBENCH_BASE}/report/preview", data=data, timeout=30)
    if resp.status_code != 200:
        logger.error("Preview POST failed: %s %s", resp.status_code, resp.text[:200])
        return None

    preview = resp.json()
    preview_id = preview.get("id") or preview.get("preview_id")
    if not preview_id:
        return None

    # Poll for completion
    for _ in range(40):
        time.sleep(3)
        poll = session.get(f"{QBENCH_BASE}/report/preview/get", params={"id": preview_id}, timeout=30)
        if poll.status_code == 200:
            pdata = poll.json()
            status = (pdata.get("status") or "").upper()
            if status == "SUCCESSFUL":
                url = f"{QBENCH_BASE}/report/preview?id={preview_id}"
                # Resolve redirect
                try:
                    r = session.get(url, timeout=30, allow_redirects=True, stream=True)
                    r.close()
                    if r.url and r.url != url:
                        return r.url
                except Exception:
                    pass
                return url
            if status == "FAILED":
                return None
    return None


# ══════════════════════════════════════════════════════════════════════════════
# SIF discovery logic
# ══════════════════════════════════════════════════════════════════════════════

def download_attachment_file(attachment: dict) -> Optional[bytes]:
    """Download the actual file bytes for an attachment via the QBench API."""
    att_id = attachment.get("id")
    if not att_id:
        return None

    # Try the direct URL first (S3 signed link — lives at top level or in asset)
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
            logger.warning("Direct download failed for attachment %s: %s", att_id, e)

    # Fallback: try QBench attachment download endpoint
    try:
        resp = api_client.request("GET", f"/attachments/{att_id}/download", timeout=60)
        if resp.status_code == 200:
            return resp.content
    except Exception as e:
        logger.warning("API download failed for attachment %s: %s", att_id, e)

    return None


def get_attachment_filename(a: dict) -> str:
    """Extract the filename from an attachment object."""
    return (
        (a.get("asset") or {}).get("file_name")
        or a.get("file_name")
        or a.get("name")
        or f"Attachment {a.get('id', '?')}"
    )


def is_pdf(a: dict) -> bool:
    """Check if an attachment is a PDF."""
    fname = get_attachment_filename(a).lower()
    content_type = (a.get("content_type") or a.get("mime_type") or
                    (a.get("asset") or {}).get("content_type") or "")
    return fname.endswith(".pdf") or "pdf" in content_type.lower()


def find_sif_candidates(attachments: List[dict]) -> List[dict]:
    """Filter attachments to find likely SIF PDFs.

    SIF PDFs are typically named after the company name and may contain 'SIF'
    in the filename. They are PDFs that are NOT the COA.
    """
    candidates = []
    for a in attachments:
        if not is_pdf(a):
            continue
        fname = get_attachment_filename(a).lower()
        # Skip obvious non-SIF files (COA reports, etc.)
        if "coa" in fname or "certificate" in fname or "report" in fname:
            continue
        # Prioritize files with 'sif' in the name
        candidates.append(a)
    # Sort: files with 'sif' in name first
    candidates.sort(key=lambda a: (0 if "sif" in get_attachment_filename(a).lower() else 1))
    return candidates


def find_lab_id_page(pdf_bytes: bytes, lab_id: str) -> Optional[int]:
    """Scan PDF pages for the lab_id via barcode detection, then text fallback.

    Returns 0-based page number or None.
    """
    if not PYMUPDF_AVAILABLE or not pdf_bytes:
        return None

    # Extract the numeric portion of the lab_id (e.g., "32217" from "040226-32217")
    parts = lab_id.split("-")
    numeric_id = parts[-1] if parts else lab_id

    try:
        doc = _fitz.open(stream=pdf_bytes, filetype="pdf")

        # Strategy 1: Barcode scanning with pyzbar (most reliable for scanned SIFs)
        if PYZBAR_AVAILABLE:
            for page_num in range(len(doc)):
                page = doc[page_num]
                mat = _fitz.Matrix(300 / 72, 300 / 72)  # 300 DPI
                pix = page.get_pixmap(matrix=mat)
                img = _PILImage.open(io.BytesIO(pix.tobytes("png")))
                barcodes = _pyzbar.decode(img)
                for bc in barcodes:
                    data = bc.data.decode("utf-8", errors="replace")
                    if numeric_id == data or lab_id == data:
                        doc.close()
                        logger.info("Barcode match for %s on page %d: %s", lab_id, page_num + 1, data)
                        return page_num

        # Strategy 2: Text search (for PDFs with selectable text)
        for page_num in range(len(doc)):
            page = doc[page_num]
            text = page.get_text()
            if lab_id in text or numeric_id in text:
                doc.close()
                return page_num

        doc.close()
    except Exception as e:
        logger.warning("Error scanning for lab_id %s: %s", lab_id, e)
    return None


def extract_single_page(pdf_bytes: bytes, page_num: int) -> Optional[bytes]:
    """Extract a single page from a PDF as a new PDF."""
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
        logger.warning("Error extracting page %d: %s", page_num, e)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Flask routes
# ══════════════════════════════════════════════════════════════════════════════

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SIF Viewer Test</title>
<style>
  :root {
    --bg: #1a1a2e;
    --surface: #16213e;
    --border: #0f3460;
    --accent: #e94560;
    --text: #eee;
    --muted: #888;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    height: 100vh;
    display: flex;
    flex-direction: column;
  }

  /* ── Top bar ─────────────────────────────────────────── */
  .topbar {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 20px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .topbar h1 { font-size: 16px; font-weight: 600; }
  .topbar input[type="text"] {
    padding: 6px 12px;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--bg);
    color: var(--text);
    font-size: 14px;
    width: 160px;
  }
  .topbar button {
    padding: 6px 16px;
    border: none;
    border-radius: 4px;
    background: var(--accent);
    color: white;
    font-size: 14px;
    cursor: pointer;
    font-weight: 500;
  }
  .topbar button:hover { opacity: 0.9; }
  .topbar button:disabled { opacity: 0.5; cursor: not-allowed; }
  #status { color: var(--muted); font-size: 13px; flex: 1; text-align: right; }

  /* ── Main area ───────────────────────────────────────── */
  .main {
    flex: 1;
    display: flex;
    overflow: hidden;
  }

  /* Info panel (left) */
  .info-panel {
    width: 320px;
    min-width: 280px;
    background: var(--surface);
    border-right: 1px solid var(--border);
    overflow-y: auto;
    padding: 16px;
    flex-shrink: 0;
  }
  .info-panel h3 {
    font-size: 13px;
    text-transform: uppercase;
    color: var(--accent);
    margin: 16px 0 8px 0;
    letter-spacing: 0.5px;
  }
  .info-panel h3:first-child { margin-top: 0; }
  .info-panel .item {
    padding: 6px 8px;
    margin: 2px 0;
    border-radius: 4px;
    font-size: 13px;
    background: var(--bg);
    border: 1px solid transparent;
    word-break: break-all;
  }
  .info-panel .item.pdf { border-color: var(--border); cursor: pointer; }
  .info-panel .item.pdf:hover { border-color: var(--accent); }
  .info-panel .item.pdf.selected { border-color: var(--accent); background: #1e2a4a; }
  .info-panel .item .tag {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 11px;
    margin-left: 4px;
    font-weight: 600;
  }
  .info-panel .item .tag.sif { background: #e94560; color: white; }
  .info-panel .item .tag.report { background: #0f3460; color: #8ab4f8; }
  .info-panel .item .tag.page { background: #2e7d32; color: white; }

  .info-panel .detail { color: var(--muted); font-size: 12px; padding: 2px 8px; }

  /* PDF viewers (center + right) */
  .pdf-container {
    flex: 1;
    display: flex;
    gap: 0;
  }
  .pdf-pane {
    flex: 1;
    display: flex;
    flex-direction: column;
    border-left: 1px solid var(--border);
    min-width: 0;
  }
  .pdf-pane .pane-header {
    padding: 8px 16px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    font-size: 13px;
    font-weight: 600;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .pdf-pane .pane-header .badge {
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 3px;
    background: var(--accent);
    color: white;
  }
  .pdf-pane iframe, .pdf-pane embed, .pdf-pane .placeholder {
    flex: 1;
    width: 100%;
    border: none;
    background: #111;
  }
  .pdf-pane .placeholder {
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--muted);
    font-size: 14px;
  }

  /* Loading spinner */
  .spinner {
    display: inline-block;
    width: 14px; height: 14px;
    border: 2px solid var(--muted);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin-right: 6px;
    vertical-align: middle;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* View toggle */
  .view-toggle {
    display: flex;
    gap: 4px;
  }
  .view-toggle button {
    padding: 3px 10px;
    font-size: 12px;
    border: 1px solid var(--border);
    border-radius: 3px;
    background: transparent;
    color: var(--text);
    cursor: pointer;
  }
  .view-toggle button.active {
    background: var(--accent);
    border-color: var(--accent);
  }
</style>
</head>
<body>

<div class="topbar">
  <h1>SIF Viewer Test</h1>
  <input type="text" id="labIdInput" placeholder="Lab ID (e.g. 32217)" autofocus>
  <button id="fetchBtn" onclick="fetchSample()">Fetch</button>
  <div class="view-toggle">
    <button class="active" onclick="setView('split', this)">Split</button>
    <button onclick="setView('sif', this)">SIF Only</button>
    <button onclick="setView('coa', this)">COA Only</button>
  </div>
  <span id="status">Ready</span>
</div>

<div class="main">
  <div class="info-panel" id="infoPanel">
    <h3>Sample Info</h3>
    <div class="item" id="sampleInfo" style="color: var(--muted);">Enter a Lab ID and click Fetch</div>
  </div>

  <div class="pdf-container" id="pdfContainer">
    <div class="pdf-pane" id="coaPane">
      <div class="pane-header">
        COA Preview
      </div>
      <div class="placeholder" id="coaPlaceholder">No COA loaded</div>
    </div>
    <div class="pdf-pane" id="sifPane">
      <div class="pane-header">
        SIF Document
        <span class="badge" id="sifPageBadge" style="display:none;">Page ?</span>
      </div>
      <div class="placeholder" id="sifPlaceholder">No SIF loaded</div>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let currentLabId = '';
let currentView = 'split';

// Enter key triggers fetch
$('labIdInput').addEventListener('keydown', e => { if (e.key === 'Enter') fetchSample(); });

function setStatus(msg) { $('status').innerHTML = msg; }

function setView(mode, btn) {
  currentView = mode;
  document.querySelectorAll('.view-toggle button').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');

  const coa = $('coaPane');
  const sif = $('sifPane');
  coa.style.display = (mode === 'sif') ? 'none' : 'flex';
  sif.style.display = (mode === 'coa') ? 'none' : 'flex';
}

async function fetchSample() {
  const labId = $('labIdInput').value.trim();
  if (!labId) return;
  currentLabId = labId;

  $('fetchBtn').disabled = true;
  setStatus('<span class="spinner"></span>Fetching sample info…');

  // Clear previous
  $('sampleInfo').textContent = 'Loading…';
  $('coaPlaceholder')?.remove();
  $('sifPlaceholder')?.remove();

  // Reset PDF panes
  resetPane('coaPane', 'Loading COA…');
  resetPane('sifPane', 'Loading SIF…');

  try {
    const resp = await fetch(`/api/lookup/${labId}`);
    const data = await resp.json();

    if (data.error) {
      setStatus('Error: ' + data.error);
      $('fetchBtn').disabled = false;
      return;
    }

    // Show sample info
    renderSampleInfo(data);

    // Load COA
    if (data.has_coa) {
      setStatus('<span class="spinner"></span>COA ready, loading SIF…');
      showPdf('coaPane', `/api/coa/${labId}`);
    } else {
      setStatus('<span class="spinner"></span>Generating COA preview…');
      generateCOA(labId);
    }

    // Load SIF
    if (data.sif_found) {
      const pageParam = data.sif_page !== null ? `#page=${data.sif_page + 1}` : '';
      showPdf('sifPane', `/api/sif/${labId}${data.sif_page !== null ? '?page=' + data.sif_page : ''}`);
      $('sifPageBadge').style.display = 'inline';
      $('sifPageBadge').textContent = data.sif_page !== null
        ? `Page ${data.sif_page + 1} of ${data.sif_total_pages}`
        : 'Full PDF';
    } else {
      resetPane('sifPane', data.sif_message || 'No SIF found');
      $('sifPageBadge').style.display = 'none';
      setStatus(data.sif_message || 'No SIF found');
    }

    if (data.sif_found) setStatus('Done — COA + SIF loaded');

  } catch (err) {
    setStatus('Fetch error: ' + err.message);
  }
  $('fetchBtn').disabled = false;
}

async function generateCOA(labId) {
  try {
    const resp = await fetch(`/api/generate-coa/${labId}`, { method: 'POST' });
    const data = await resp.json();
    if (data.ok) {
      showPdf('coaPane', `/api/coa/${labId}`);
    } else {
      resetPane('coaPane', 'COA generation failed: ' + (data.error || 'unknown'));
    }
  } catch (err) {
    resetPane('coaPane', 'COA error: ' + err.message);
  }
}

function renderSampleInfo(data) {
  const panel = $('infoPanel');
  let html = '';

  html += `<h3>Sample</h3>`;
  html += `<div class="item"><b>Lab ID:</b> ${data.lab_id}</div>`;
  html += `<div class="item"><b>Sample ID:</b> ${data.sample_id || 'N/A'}</div>`;
  html += `<div class="item"><b>Order ID:</b> ${data.order_id || 'N/A'}</div>`;
  if (data.client_name) html += `<div class="item"><b>Client:</b> ${data.client_name}</div>`;

  // Order attachments
  if (data.order_attachments && data.order_attachments.length > 0) {
    html += `<h3>Order Attachments (${data.order_attachments.length})</h3>`;
    for (const a of data.order_attachments) {
      let tags = '';
      if (a.is_sif) tags += '<span class="tag sif">SIF</span>';
      if (a.is_pdf) tags += '<span class="tag report">PDF</span>';
      if (a.matched_page !== null && a.matched_page !== undefined) {
        tags += `<span class="tag page">Page ${a.matched_page + 1}</span>`;
      }
      const cls = a.is_pdf ? 'item pdf' + (a.is_sif ? ' selected' : '') : 'item';
      html += `<div class="${cls}" title="ID: ${a.id}">${a.filename}${tags}</div>`;
    }
  } else {
    html += `<h3>Order Attachments</h3>`;
    html += `<div class="item" style="color:var(--muted);">${data.order_attach_error || 'None found'}</div>`;
  }

  // Sample attachments
  if (data.sample_attachments && data.sample_attachments.length > 0) {
    html += `<h3>Sample Attachments (${data.sample_attachments.length})</h3>`;
    for (const a of data.sample_attachments) {
      let tags = '';
      if (a.is_report) tags += '<span class="tag report">Report</span>';
      if (a.is_pdf) tags += '<span class="tag report">PDF</span>';
      html += `<div class="item">${a.filename}${tags}</div>`;
    }
  }

  // SIF status
  html += `<h3>SIF Detection</h3>`;
  if (data.sif_found) {
    html += `<div class="item" style="color:#4caf50;"><b>Found!</b> ${data.sif_filename}</div>`;
    if (data.sif_page !== null) {
      html += `<div class="detail">Lab ID "${data.lab_id}" found on page ${data.sif_page + 1} of ${data.sif_total_pages}</div>`;
    } else {
      html += `<div class="detail">Lab ID not found in text — showing full PDF</div>`;
    }
  } else {
    html += `<div class="item" style="color:var(--accent);">${data.sif_message || 'Not found'}</div>`;
  }

  panel.innerHTML = html;
}

function resetPane(paneId, message) {
  const pane = $(paneId);
  // Remove any existing iframe/embed
  const existing = pane.querySelector('iframe, embed, .placeholder');
  if (existing) existing.remove();
  const ph = document.createElement('div');
  ph.className = 'placeholder';
  ph.textContent = message || '';
  pane.appendChild(ph);
}

function showPdf(paneId, url) {
  const pane = $(paneId);
  const existing = pane.querySelector('iframe, embed, .placeholder');
  if (existing) existing.remove();
  const iframe = document.createElement('iframe');
  iframe.src = url;
  iframe.style.flex = '1';
  iframe.style.width = '100%';
  iframe.style.border = 'none';
  pane.appendChild(iframe);
}
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/lookup/<lab_id>")
def lookup_sample(lab_id: str):
    """
    Main lookup: fetch sample, order, attachments, identify SIF, find matching page.
    Returns all info needed for the UI in one call.
    """
    setattr(lookup_sample, '_last_lookup', {})
    result: Dict[str, Any] = {
        "lab_id": lab_id,
        "sample_id": None,
        "order_id": None,
        "client_name": None,
        "has_coa": False,
        "order_attachments": [],
        "order_attach_error": None,
        "sample_attachments": [],
        "sif_found": False,
        "sif_filename": None,
        "sif_page": None,
        "sif_total_pages": None,
        "sif_message": None,
    }

    # 1. Fetch sample
    try:
        samples = api_client.fetch_samples_by_lab_id(lab_id)
        if not samples:
            result["error"] = f"No sample found for lab_id {lab_id}"
            return jsonify(result)
        sample = samples[0]
        result["sample_id"] = sample.get("id")
        result["order_id"] = sample.get("order_id")
        result["client_name"] = sample.get("client_name") or sample.get("customer_name")
    except Exception as e:
        result["error"] = f"API error fetching sample: {e}"
        return jsonify(result)

    sample_id = result["sample_id"]
    order_id = result["order_id"]

    # 2. If no order_id from sample, try fetching tests to get it
    if not order_id and sample_id:
        try:
            tests = api_client.fetch_tests_for_sample_ids([sample_id])
            if tests:
                order_id = tests[0].get("order_id")
                result["order_id"] = order_id
        except Exception:
            pass

    # 3. Fetch sample-level attachments
    if sample_id:
        try:
            raw = api_client.fetch_all_attachments_for_sample(int(sample_id))
            report_fields = ("attach_to_report", "include_in_report", "include_on_coa", "print_with_report")
            for a in raw:
                fname = get_attachment_filename(a)
                result["sample_attachments"].append({
                    "id": a.get("id"),
                    "filename": fname,
                    "is_report": bool(any(a.get(f) for f in report_fields)),
                    "is_pdf": is_pdf(a),
                })
        except Exception as e:
            logger.warning("Error fetching sample attachments: %s", e)

    # 4. Fetch order-level attachments
    if order_id:
        try:
            order_atts = api_client.fetch_order_attachments(int(order_id))
            sif_candidates = find_sif_candidates(order_atts)

            for a in order_atts:
                fname = get_attachment_filename(a)
                is_candidate = a in sif_candidates
                result["order_attachments"].append({
                    "id": a.get("id"),
                    "filename": fname,
                    "is_pdf": is_pdf(a),
                    "is_sif": is_candidate,
                    "matched_page": None,
                })

            # 5. Try to find the SIF and the right page
            for candidate in sif_candidates:
                logger.info("Trying SIF candidate: %s", get_attachment_filename(candidate))
                pdf_bytes = download_attachment_file(candidate)
                if not pdf_bytes:
                    logger.warning("Could not download attachment %s", candidate.get("id"))
                    continue

                page_num = find_lab_id_page(pdf_bytes, lab_id)
                total_pages = 0
                if PYMUPDF_AVAILABLE:
                    try:
                        doc = _fitz.open(stream=pdf_bytes, filetype="pdf")
                        total_pages = len(doc)
                        doc.close()
                    except Exception:
                        pass

                fname = get_attachment_filename(candidate)

                # Update the order_attachments entry with page info
                for oa in result["order_attachments"]:
                    if oa["id"] == candidate.get("id"):
                        oa["matched_page"] = page_num

                result["sif_found"] = True
                result["sif_filename"] = fname
                result["sif_page"] = page_num
                result["sif_total_pages"] = total_pages

                # Cache the PDF bytes
                cache[lab_id] = {
                    "sif_pdf_bytes": pdf_bytes,
                    "sif_page": page_num,
                    "sif_total_pages": total_pages,
                    "sif_filename": fname,
                    "sif_attachment_id": candidate.get("id"),
                }
                break  # Use the first good candidate

            if not result["sif_found"]:
                if sif_candidates:
                    result["sif_message"] = "Found PDF candidates but couldn't download them"
                else:
                    result["sif_message"] = "No SIF-like PDFs found in order attachments"

        except QBenchAPIError as e:
            error_str = str(e)
            if "404" in error_str:
                result["order_attach_error"] = "Order attachments endpoint returned 404 — may not be supported"
                # Fallback: try sample attachments for SIF
                result["sif_message"] = "Order-level attachments not available; check sample attachments"
            else:
                result["order_attach_error"] = f"Error: {error_str}"
                result["sif_message"] = f"Could not fetch order attachments: {error_str}"
        except Exception as e:
            result["order_attach_error"] = str(e)
    else:
        result["order_attach_error"] = "No order_id found for this sample"
        result["sif_message"] = "Cannot look for SIF without order_id"

    # Check if COA is cached
    result["has_coa"] = lab_id in cache and "coa_pdf_bytes" in cache[lab_id]

    return jsonify(result)


@app.route("/api/sif/<lab_id>")
def serve_sif(lab_id: str):
    """Serve the SIF PDF, optionally extracted to a single page."""
    entry = cache.get(lab_id)
    if not entry or not entry.get("sif_pdf_bytes"):
        return "SIF not found", 404

    page_param = request.args.get("page")
    pdf_bytes = entry["sif_pdf_bytes"]

    if page_param is not None:
        try:
            page_num = int(page_param)
            single_page = extract_single_page(pdf_bytes, page_num)
            if single_page:
                pdf_bytes = single_page
        except (ValueError, TypeError):
            pass

    return Response(pdf_bytes, mimetype="application/pdf",
                    headers={"Content-Disposition": "inline"})


@app.route("/api/sif-full/<lab_id>")
def serve_sif_full(lab_id: str):
    """Serve the complete SIF PDF (all pages)."""
    entry = cache.get(lab_id)
    if not entry or not entry.get("sif_pdf_bytes"):
        return "SIF not found", 404
    return Response(entry["sif_pdf_bytes"], mimetype="application/pdf",
                    headers={"Content-Disposition": "inline"})


@app.route("/api/coa/<lab_id>")
def serve_coa(lab_id: str):
    """Serve the cached COA PDF."""
    entry = cache.get(lab_id, {})
    if entry.get("coa_pdf_bytes"):
        return Response(entry["coa_pdf_bytes"], mimetype="application/pdf",
                        headers={"Content-Disposition": "inline"})
    return "COA not cached yet", 404


@app.route("/api/generate-coa/<lab_id>", methods=["POST"])
def generate_coa(lab_id: str):
    """Generate a COA preview and cache it."""
    global coa_session_cookies

    # Need login first
    if not coa_session_cookies:
        try:
            coa_session_cookies = do_playwright_login()
        except Exception as e:
            return jsonify({"ok": False, "error": f"Login failed: {e}"})

    # Get sample info
    try:
        samples = api_client.fetch_samples_by_lab_id(lab_id)
        if not samples:
            return jsonify({"ok": False, "error": "Sample not found"})
        sample = samples[0]
        sample_id = sample.get("id")
        order_id = sample.get("order_id")

        tests = api_client.fetch_tests_for_sample_ids([sample_id])
        test_ids = [t["id"] for t in tests if t.get("id")]
        if not order_id and tests:
            order_id = tests[0].get("order_id")

        # Get report attachments
        atts = api_client.fetch_attachments_for_sample(int(sample_id))
        att_ids = [int(a["id"]) for a in atts if a.get("id")]

        url = generate_coa_preview(coa_session_cookies, int(sample_id), test_ids,
                                   int(order_id) if order_id else None,
                                   att_ids or None)
        if not url:
            return jsonify({"ok": False, "error": "Preview generation failed"})

        # Download the PDF
        try:
            resp = coa_session_cookies.get(url, timeout=60)
            if resp.status_code == 200 and resp.content:
                if lab_id not in cache:
                    cache[lab_id] = {}
                cache[lab_id]["coa_pdf_bytes"] = resp.content
                return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": f"Download failed: {e}"})

        return jsonify({"ok": False, "error": "Could not download COA PDF"})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/debug/order/<int:order_id>")
def debug_order(order_id: int):
    """Debug endpoint: show raw order data and attachments."""
    result = {"order_id": order_id}
    try:
        order = api_client.fetch_order(order_id)
        result["order"] = order
    except Exception as e:
        result["order_error"] = str(e)

    try:
        atts = api_client.fetch_order_attachments(order_id)
        result["attachments"] = atts
        result["attachment_count"] = len(atts)
    except Exception as e:
        result["attachments_error"] = str(e)

    try:
        samples = api_client.fetch_order_samples(order_id)
        result["samples"] = [{"id": s.get("id"), "lab_id": s.get("lab_id")} for s in samples]
    except Exception as e:
        result["samples_error"] = str(e)

    return jsonify(result)


@app.route("/api/debug/sample-attachments/<lab_id>")
def debug_sample_attachments(lab_id: str):
    """Debug endpoint: show ALL raw attachment data for a sample."""
    try:
        samples = api_client.fetch_samples_by_lab_id(lab_id)
        if not samples:
            return jsonify({"error": "Sample not found"})
        sample = samples[0]
        sample_id = sample.get("id")
        raw = api_client.fetch_all_attachments_for_sample(int(sample_id))
        return jsonify({
            "lab_id": lab_id,
            "sample_id": sample_id,
            "order_id": sample.get("order_id"),
            "attachment_count": len(raw),
            "attachments": raw,
        })
    except Exception as e:
        return jsonify({"error": str(e)})


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  SIF Viewer Test App")
    print("  → http://localhost:5050")
    print("=" * 60 + "\n")
    print(f"PyMuPDF available: {PYMUPDF_AVAILABLE}")
    print(f"pyzbar available: {PYZBAR_AVAILABLE}")
    print(f"Playwright available: {PLAYWRIGHT_AVAILABLE}")
    print(f"Config loaded: {bool(config.get('qbench_username'))}")
    print()
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
