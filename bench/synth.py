"""Synthetic samples, tests, attachments and *real* PDFs for a benchmark run.

Two things here are load-bearing and easy to get quietly wrong:

* **Every sample must have at least one test.** ``fetch_samples_for_tab``
  filters with ``if info["test_ids"]`` (app.py:1362) and drops the rest
  without a word, so a gap in the test map makes the run measure fewer
  samples than it reports. ``served_count`` reproduces that filter so the
  run can assert on it.

* **The PDFs must be real.** The repo's existing fakes are ``b"%PDF-fake"``,
  which Chromium renders as an error page. Timing that measures the error
  page, not a COA. These are built with PyMuPDF and padded to a
  configurable size, because a COA is plausibly 0.5-2 MB and the byte count
  is most of what the viewer pane is waiting for.

The SIF pages carry each member sample's lab_id **as text**, because
``_sif_find_page`` does a text search before it rasterises and barcode-scans
(app.py:451). A SIF without the text would make every run measure pyzbar.
"""

from __future__ import annotations

import random
from datetime import date
from typing import Any, Iterable, Optional, Sequence

import fitz


# Six well-separated marker colours. The COA's masthead band is painted in
# the colour for its sample's position in the list, and "this COA is on
# screen" is decided by looking for that colour in the viewer pane. Six is
# enough that neighbours never collide (see MARKERS usage below) while each
# stays trivially separable in a low-resolution screencast frame: the closest
# pair is ~140 units apart in RGB, so a match tolerance of 55 cannot confuse
# two of them even after JPEG compression.
MARKERS: tuple[tuple[float, float, float], ...] = (
    (0.85, 0.10, 0.10),   # red
    (0.10, 0.10, 0.90),   # blue
    (0.05, 0.60, 0.15),   # green
    (0.95, 0.65, 0.00),   # amber
    (0.80, 0.10, 0.65),   # magenta
    (0.00, 0.65, 0.70),   # teal
)


def served_count(samples: Sequence[dict], tests: Iterable[dict]) -> int:
    """How many samples app.py would actually put on a tab.

    A faithful copy of the map-then-filter in ``fetch_samples_for_tab``: a
    sample with no test is silently dropped, which is the failure mode this
    exists to catch.
    """
    by_sample: dict[int, list] = {int(s["id"]): [] for s in samples if s.get("id")}
    for t in tests:
        sid = t.get("sample_id") or (t.get("sample") or {}).get("id")
        if sid is None:
            continue
        sid = int(sid)
        if sid in by_sample:
            by_sample[sid].append(t)
    return sum(1 for v in by_sample.values() if v)


def _pad_to(doc: fitz.Document, target: int) -> bytes:
    """Serialise ``doc``, padded with an embedded incompressible blob so the
    result lands at ``target`` bytes.

    Padding rather than adding pages on purpose: page count drives PDFium's
    render work, and we want the *transfer* size to be the parameter without
    silently changing how much there is to draw.
    """
    body = doc.tobytes()
    if len(body) >= target:
        return body
    # Two passes: the first estimate is off by the embedded-file dictionary
    # overhead, which depends on the blob length.
    for _ in range(2):
        gap = target - len(body)
        if gap <= 0:
            break
        blob = random.Random(len(body)).randbytes(gap)
        try:
            doc.embfile_del("pad")
        except Exception:
            pass
        doc.embfile_add("pad", blob)
        body = doc.tobytes()
    return body


class SyntheticLab:
    """A day's worth of fake samples, with the documents to go with them."""

    def __init__(
        self,
        count: int,
        *,
        seed: int = 1,
        coa_bytes: int = 750_000,
        sif_bytes: int = 400_000,
        samples_per_order: int = 5,
        coa_pages: int = 4,
        prefix: Optional[str] = None,
        base_url: str = "",
    ) -> None:
        if count < 1:
            raise ValueError("count must be >= 1")
        if samples_per_order < 1:
            raise ValueError("samples_per_order must be >= 1")
        self.count = count
        self.seed = seed
        self.coa_bytes = coa_bytes
        self.sif_bytes = sif_bytes
        self.samples_per_order = samples_per_order
        self.coa_pages = coa_pages
        self.base_url = base_url.rstrip("/")
        self.prefix = prefix or date.today().strftime("%m%d%y")

        rng = random.Random(seed)
        first = rng.randrange(10_000, 80_000)

        self.samples: list[dict] = []
        self._index_by_lab: dict[str, int] = {}
        self._members: dict[int, list[dict]] = {}
        for i in range(count):
            lab_id = f"{self.prefix}-{first + i}"
            order_id = 900_000 + seed * 1_000 + (i // samples_per_order)
            s = {"id": 100_000 + seed * 1_000 + i, "lab_id": lab_id, "order_id": order_id}
            self.samples.append(s)
            self._index_by_lab[lab_id] = i
            self._members.setdefault(order_id, []).append(s)

        # Generated lazily and memoised by marker, not by lab_id: two samples
        # sharing a marker can share bytes on the wire without affecting what
        # is being measured (the app copies them into its own per-sample
        # cache either way), and holding N x 750 KB in the harness would
        # dwarf the thing under test.
        self._coa_cache: dict[int, bytes] = {}
        self._sif_cache: dict[int, bytes] = {}

    # ── ids ──────────────────────────────────────────────────────────────

    def index_of(self, lab_id: str) -> int:
        return self._index_by_lab[lab_id]

    def marker_index(self, lab_id: str) -> int:
        """Which masthead colour this sample's COA is painted in.

        Position modulo len(MARKERS), so two adjacent samples never share
        one — otherwise an ArrowDown would look instantaneous because the
        *previous* COA already matched the colour being waited for.
        """
        return self._index_by_lab[lab_id] % len(MARKERS)

    def members_of_order(self, order_id: int) -> list[dict]:
        return self._members[int(order_id)]

    # ── QBench-shaped payloads ───────────────────────────────────────────

    def tests_for(self, sample_ids: Iterable[int]) -> list[dict]:
        """>= 1 test per sample id, in the shape ``fetch_tests_for_sample_ids``
        returns. Two tests for most samples so the preview carries a realistic
        test_ids list."""
        wanted = {int(s) for s in sample_ids}
        out: list[dict] = []
        for s in self.samples:
            if s["id"] not in wanted:
                continue
            n = 1 + (self._index_by_lab[s["lab_id"]] % 2)
            for k in range(n):
                out.append({
                    "id": s["id"] * 10 + k,
                    "sample_id": s["id"],
                    "order_id": s["order_id"],
                    "results": "PASS",
                    "assay": {"id": 5 + k, "data": {"title": f"Assay {5 + k}"}},
                })
        return out

    def sample_attachments(self, sample_id: int) -> list[dict]:
        """Report-tagged COA attachments for one sample.

        Named so ``_sif_find_candidates`` rejects them (they contain "coa" /
        "report"): a sample attachment must never be mistaken for the order's
        SIF.
        """
        sid = int(sample_id)
        lab_id = next((s["lab_id"] for s in self.samples if s["id"] == sid), str(sid))
        return [
            {"id": sid * 10 + 1, "file_name": f"COA_{lab_id}.pdf",
             "content_type": "application/pdf", "attach_to_report": True,
             "url": f"{self.base_url}/coa/{lab_id}.pdf"},
            {"id": sid * 10 + 2, "file_name": f"report_notes_{lab_id}.pdf",
             "content_type": "application/pdf", "attach_to_report": False,
             "url": f"{self.base_url}/coa/{lab_id}.pdf"},
        ]

    def order_attachments(self, order_id: int) -> list[dict]:
        """The order's SIF, named to survive app.py:399-410.

        ``SIF_<order>.pdf``: ends in .pdf, and contains none of "coa",
        "certificate" or "report". The "sif" in the name also sorts it first
        among candidates.
        """
        oid = int(order_id)
        return [
            {"id": oid * 10 + 1, "file_name": f"SIF_{oid}.pdf",
             "content_type": "application/pdf",
             "url": f"{self.base_url}/sif/{oid}.pdf"},
        ]

    def order(self, order_id: int) -> dict:
        """A paper order — no ``order_request_status`` — so a missing SIF
        would classify as genuinely missing rather than 'entered online'."""
        return {"id": int(order_id), "order_request_status": None}

    def sample(self, sample_id: int) -> dict:
        sid = int(sample_id)
        return {"id": sid, "comments": f"Reviewed by the bench harness ({sid})."}

    # ── documents ────────────────────────────────────────────────────────

    def coa_pdf(self, lab_id: str) -> bytes:
        marker = self.marker_index(lab_id)
        cached = self._coa_cache.get(marker)
        if cached is not None:
            return cached
        colour = MARKERS[marker]
        doc = fitz.open()
        for p in range(self.coa_pages):
            page = doc.new_page()
            w, h = page.rect.width, page.rect.height
            # Full-width masthead across the top quarter of page 1 and a
            # slimmer repeat on later pages. This is the paint marker: it
            # has to be large enough to survive a 320px screencast frame.
            band = fitz.Rect(0, 0, w, h * (0.28 if p == 0 else 0.06))
            page.draw_rect(band, color=colour, fill=colour, width=0)
            page.insert_text((40, h * 0.36), "CERTIFICATE OF ANALYSIS", fontsize=18)
            page.insert_text((40, h * 0.40), f"Lab ID {lab_id}   page {p + 1}", fontsize=11)
            for row in range(24):
                y = h * 0.45 + row * 12
                if y > h - 40:
                    break
                page.insert_text((40, y), f"Analyte {row:02d} ..... 0.00 mg/kg     PASS",
                                 fontsize=9)
        body = _pad_to(doc, self.coa_bytes)
        doc.close()
        self._coa_cache[marker] = body
        return body

    def sif_pdf(self, order_id: int) -> bytes:
        oid = int(order_id)
        cached = self._sif_cache.get(oid)
        if cached is not None:
            return cached
        members = self._members[oid]
        doc = fitz.open()
        for i, s in enumerate(members):
            page = doc.new_page()
            page.insert_text((40, 60), "SAMPLE INFORMATION FORM", fontsize=16)
            # _sif_find_page text-searches for the lab_id (and for the part
            # after the last dash) before it rasterises; putting it here is
            # what keeps the run off the pyzbar path.
            page.insert_text((40, 90), f"Lab ID: {s['lab_id']}", fontsize=12)
            page.insert_text((40, 110), f"Order: {oid}   sheet {i + 1} of {len(members)}",
                             fontsize=10)
        body = _pad_to(doc, self.sif_bytes)
        doc.close()
        self._sif_cache[oid] = body
        return body
