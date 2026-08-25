"""The four module attributes a benchmark run swaps into an imported ``app``.

Every one of these replaces something that would otherwise talk to QBench or
LabCore over the real network. ``POST /api/login`` is never called for the
same reason — it drives a real Playwright login against QBench.

Two of these have bitten before and are commented where they are set:

* ``labcore.base_url`` must be a real string. It is returned straight through
  ``jsonify`` by ``/api/cc/config``; a bare MagicMock attribute serialises
  into something the frontend cannot use.
* ``fetch_tests_for_sample_ids`` must cover every sample. A sample with no
  tests is dropped without a word at app.py:1362, so a hole here makes the
  run quietly measure fewer samples than it claims.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from .synth import SyntheticLab

REVIEWER_NAME = "Bench Reviewer"


class _FakeResponse:
    """What ``coa_session._session.get(...)`` returns.

    app.py only reads ``.url`` (to follow a redirect the preview may issue)
    and calls ``.close()``.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self.status_code = 200
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeCoaSession:
    """Stands in for ``COASession`` — the Playwright-driven QBench preview.

    ``generate_preview`` returns a URL on the loopback PDF fixture server, so
    everything downstream of it (the redirect probe, ``cache_pdf``, and the
    browser's own fetch of ``/api/pdf/<lab_id>``) is real HTTP carrying real
    PDF bytes.
    """

    def __init__(self, lab: SyntheticLab, base_url: str) -> None:
        self.lab = lab
        self.base_url = base_url.rstrip("/")
        self.calls = 0
        self.relogins = 0
        self._by_sample = {s["id"]: s for s in lab.samples}
        self._session = SimpleNamespace(get=self._get)

    def _get(self, url, timeout=None, allow_redirects=True, stream=False):
        return _FakeResponse(url)

    def generate_preview(self, sample_id=None, test_ids=None, order_id=None,
                         attachment_ids=None, skip_attachments=False):
        self.calls += 1
        sample = self._by_sample.get(int(sample_id)) if sample_id else None
        if sample is None:
            return None
        return f"{self.base_url}/coa/{sample['lab_id']}.pdf"

    def relogin(self):
        self.relogins += 1
        return True

    def close(self):
        return None


def build_api_client(lab: SyntheticLab, prefix: str) -> MagicMock:
    """A QBenchAPIClient double backed by the synthetic lab.

    ``prefix`` is the lab-id prefix of the tab we want populated (Due Out).
    Every other prefix returns nothing on purpose: ``/api/start`` fans out
    across three tabs, and answering all of them would triple the work while
    the UI still only shows one tab's worth.
    """
    client = MagicMock(name="api_client")

    def by_prefix(p):
        return list(lab.samples) if str(p) == prefix else []

    client.fetch_samples_by_lab_id_prefix.side_effect = by_prefix
    client.fetch_tests_for_sample_ids.side_effect = lambda ids: lab.tests_for(ids)
    client.fetch_all_attachments_for_sample.side_effect = lab.sample_attachments
    client.fetch_order_attachments.side_effect = lab.order_attachments
    client.fetch_order.side_effect = lab.order
    client.fetch_sample.side_effect = lab.sample
    client.fetch_samples_by_lab_id.side_effect = (
        lambda lab_id: [s for s in lab.samples if s["lab_id"] == lab_id]
    )
    return client


def build_labcore(name: str = REVIEWER_NAME) -> MagicMock:
    core = MagicMock(name="labcore")
    core.last_reachable = True
    # A real string, not a MagicMock attribute: /api/cc/config jsonifies it.
    core.base_url = "https://labvision.invalid"
    core.authenticate_card.return_value = name
    core.authenticate_user.return_value = name
    core.is_available.return_value = True
    core.check_duplicate.return_value = None
    core.sample_data.return_value = {}
    # Empty board: Re-review must not add a second tab's worth of previews to
    # a run whose N is meant to be exactly the Due Out tab.
    core.active_tasks.return_value = []
    core.customers.return_value = []
    return core


def install(app_module, lab: SyntheticLab, prefix: str, pdf_base_url: str,
            session=None):
    """Point an already-imported ``app`` at the synthetic lab.

    ``session`` is the COA session to install. Left as None it is the fully
    faked one above, which is what every run before the real-preview work
    used and which stays available behind ``--fake-preview`` so those results
    remain reproducible. ``bench/server.py`` passes the *real* ``COASession``
    (see ``bench/realpreview.py``) for the default mode.

    Returns the installed session so a caller can assert on how many previews
    were generated — and, for the real one, on what its lock cost.
    """
    if session is None:
        session = FakeCoaSession(lab, pdf_base_url)
    app_module.state.api_client = build_api_client(lab, prefix)
    app_module.state.labcore = build_labcore()
    app_module.state.coa_session = session
    app_module.state.logged_in = True
    return session
