"""How COA and SIF PDFs are delivered to a browser's PDF viewer.

Chrome's PDFium is much stricter than Safari's QuickLook about the HTTP
contract, and this app serves every PDF from a path under ``/api/``. Two
consequences are covered here:

* A blanket ``after_request`` hook rewrites ``Cache-Control`` on everything
  under ``/api/`` to ``no-store`` so Cloudflare cannot cache an API response.
  It also catches ``/api/pdf`` and ``/api/sif``, which silently discards the
  ``private, max-age, immutable`` policy the PDF helper builds and makes its
  own ETag/304 fast path unreachable. Every COA is then refetched in full on
  every pane switch — and Chrome, which re-requests the bytes for its viewer
  after the iframe navigation commits, pays that cost twice.

* ``_RANGE_RE`` only matches ``bytes=N-``. A *suffix* range (``bytes=-N``) is
  how a PDF reader grabs the trailer before deciding whether it can stream a
  document. The route advertises ``Accept-Ranges: bytes`` and then answers the
  suffix probe with a full ``200``, which is the blank-render trap the helper's
  own docstring describes.

The Cloudflare protection is the reason the hook exists, so it is asserted
here too: exempting the PDF routes must not turn it off for the rest of the API.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")

LAB = "073126-41552"
# Big enough that a range request is a genuinely partial read.
PDF_BYTES = b"%PDF-1.7\n" + (b"x" * 40_000) + b"\n%%EOF\n"


@pytest.fixture
def viewer(monkeypatch):
    """(client, ustate) with one sample whose COA is already cached."""
    import app as app_module
    from app import SampleRecord, UserState

    monkeypatch.setattr(app_module.state, "labcore", MagicMock())

    uid = "test-uid-pdf-delivery"
    ustate = UserState(uid, "RC")
    ustate.add_record(
        SampleRecord(lab_id=LAB, tab="Yesterday", sample_id=1, test_ids=[9])
    )
    ustate.pdf_cache[LAB] = PDF_BYTES
    with app_module._sessions_lock:
        app_module.user_sessions[uid] = ustate

    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = uid

    yield client, ustate

    with app_module._sessions_lock:
        app_module.user_sessions.pop(uid, None)


# ── caching ──────────────────────────────────────────────────────────────

def test_pdf_keeps_its_private_cache_policy(viewer) -> None:
    """The helper's Cache-Control must survive as far as the browser.

    `private` already forbids a shared cache (Cloudflare) from storing it,
    which is the whole reason the blanket no-store hook exists.
    """
    client, _ = viewer
    resp = client.get(f"/api/pdf/{LAB}")
    assert resp.status_code == 200
    cc = resp.headers.get("Cache-Control", "")
    assert "no-store" not in cc, f"blanket hook clobbered the PDF policy: {cc!r}"
    assert "private" in cc
    assert "max-age=3600" in cc


def test_pdf_revalidates_instead_of_resending(viewer) -> None:
    """A second view of the same COA must be a 304, not the whole file again."""
    client, _ = viewer
    first = client.get(f"/api/pdf/{LAB}")
    etag = first.headers.get("ETag")
    assert etag, "no ETag to revalidate with"

    second = client.get(f"/api/pdf/{LAB}", headers={"If-None-Match": etag})
    assert second.status_code == 304, (
        "the ETag fast path is unreachable, so every pane switch refetches "
        "the entire PDF"
    )
    assert second.get_data() == b""


def test_the_rest_of_the_api_is_still_uncacheable(viewer) -> None:
    """Exempting PDFs must not let a proxy cache real API responses."""
    client, _ = viewer
    resp = client.get("/api/config")
    assert "no-store" in resp.headers.get("Cache-Control", "")


# ── range requests ───────────────────────────────────────────────────────

def test_a_normal_range_is_partial(viewer) -> None:
    """Baseline: the supported form already works."""
    client, _ = viewer
    resp = client.get(f"/api/pdf/{LAB}", headers={"Range": "bytes=0-99"})
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == f"bytes 0-99/{len(PDF_BYTES)}"
    assert len(resp.get_data()) == 100


def test_a_suffix_range_is_partial_too(viewer) -> None:
    """`bytes=-N` asks for the last N bytes — how a PDF reader finds the
    trailer. Answering 200 with the whole file breaks streaming."""
    client, _ = viewer
    total = len(PDF_BYTES)
    resp = client.get(f"/api/pdf/{LAB}", headers={"Range": "bytes=-1024"})
    assert resp.status_code == 206, (
        "suffix range fell through to a full 200 while Accept-Ranges "
        "advertises byte ranges"
    )
    assert resp.headers["Content-Range"] == f"bytes {total - 1024}-{total - 1}/{total}"
    assert resp.get_data() == PDF_BYTES[-1024:]


def test_an_oversized_suffix_range_clamps_to_the_whole_body(viewer) -> None:
    """`bytes=-99999` on a smaller file means 'all of it', not an error."""
    client, _ = viewer
    total = len(PDF_BYTES)
    resp = client.get(f"/api/pdf/{LAB}", headers={"Range": f"bytes=-{total + 5000}"})
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == f"bytes 0-{total - 1}/{total}"
    assert resp.get_data() == PDF_BYTES
