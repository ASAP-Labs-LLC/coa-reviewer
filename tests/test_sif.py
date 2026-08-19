"""Tests for SIF per-order caching and page scanning.

Root cause (diagnosed 2026-06-24): fetch_sif_for_sample ran once per sample
with no per-order cache, so a SIF PDF shared by an order's samples was
re-downloaded and re-barcode-scanned once per sample (the same file appeared
65× in the logs), each scan rasterizing every page at 300 DPI. The fix caches
the downloaded PDF per order and scans text-first at a lower DPI.
"""

from __future__ import annotations

import threading

import pytest

pytest.importorskip("flask")

import app  # noqa: E402


class _FakeClient:
    def __init__(self):
        self.order_attachment_calls = 0

    def fetch_order_attachments(self, order_id):
        self.order_attachment_calls += 1
        return [{"id": 1, "file_name": "SIF.pdf"}]


def test_load_order_pdf_downloads_once_per_order(monkeypatch) -> None:
    client = _FakeClient()
    downloads = {"n": 0}

    def fake_download(candidate, api_client):
        downloads["n"] += 1
        return b"%PDF-fake"

    monkeypatch.setattr(app, "_sif_find_candidates", lambda atts: [{"id": 1}])
    monkeypatch.setattr(app, "_sif_download", fake_download)
    monkeypatch.setattr(app, "_sif_count_pages", lambda b: 3)

    cache = {}
    lock = threading.Lock()

    a = app._sif_load_order_pdf(99, client, cache, lock)
    b = app._sif_load_order_pdf(99, client, cache, lock)

    assert a is not None and a[0] == b"%PDF-fake" and a[1] == 3
    assert b == a
    # The second sample in the same order must NOT hit the network again.
    assert client.order_attachment_calls == 1
    assert downloads["n"] == 1


def test_load_order_pdf_caches_not_found(monkeypatch) -> None:
    client = _FakeClient()
    # No usable candidates → result is "no SIF", and that negative result is
    # cached so we don't re-query QBench for every sample in the order.
    monkeypatch.setattr(app, "_sif_find_candidates", lambda atts: [])
    cache = {}
    lock = threading.Lock()

    assert app._sif_load_order_pdf(7, client, cache, lock) is None
    assert app._sif_load_order_pdf(7, client, cache, lock) is None
    assert client.order_attachment_calls == 1


@pytest.mark.skipif(not app.PYMUPDF_AVAILABLE, reason="PyMuPDF not installed")
def test_sif_find_page_matches_by_text(monkeypatch) -> None:
    import fitz  # type: ignore

    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "cover page")
    doc.new_page().insert_text((72, 72), "Lab ID 061226-35266 details")
    pdf_bytes = doc.tobytes()
    doc.close()

    # Page index 1 contains the lab id.
    assert app._sif_find_page(pdf_bytes, "061226-35266") == 1
