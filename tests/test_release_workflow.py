"""The release workflow must not ship state, and must stamp the tag it built.

A release zip is unpacked into ``releases\\<tag>\\`` and the ``current``
junction is pointed at it. Two things therefore matter more than anything else
about the workflow file:

* **No state in the zip.** ``web_app_config.json`` holds live QBench
  credentials; ``re_review_state.json``, ``archive/`` and ``changelog/`` hold
  real review work. Shipping any of them would publish credentials to a public
  repo's release assets and hand every deploy a stale copy of the audit trail.
  Note that ``changelog/*.jsonl`` **is** tracked in git, so ".gitignore covers
  it" is not true and the exclusion has to be explicit.
* **VERSION equals the tag.** The updater compares ``/healthz`` against the tag
  it staged; if the stamp disagrees, a successful swap looks like a failed one
  and gets rolled back.

These are static checks on the workflow text. They cannot prove the zip is
correct — only a real tag push does that — but they fail fast on the mistakes
that are easy to make while editing it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "release.yml"

# Anything that is state rather than source. Each must be excluded from the
# release archive by name.
STATE_PATHS = [
    "web_app_config.json",
    "re_review_state.json",
    ".secret_key",
    "archive",
    "changelog",
    "login.log",
]


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_release_workflow_exists() -> None:
    assert WORKFLOW.is_file(), "no .github/workflows/release.yml"


def test_workflow_is_valid_yaml() -> None:
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(_text())
    assert isinstance(doc, dict) and "jobs" in doc


def test_triggers_on_tag_push() -> None:
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(_text())
    # PyYAML parses a bare ``on:`` key as the boolean True.
    trigger = doc.get("on", doc.get(True))
    assert trigger, "workflow has no trigger"
    tags = trigger["push"]["tags"]
    assert any("v" in t or "*" in t for t in tags), f"no tag pattern in {tags}"


@pytest.mark.parametrize("state", STATE_PATHS)
def test_state_is_excluded_from_the_archive(state: str) -> None:
    assert state in _text(), (
        f"{state} is not named in release.yml; a release zip that contains it "
        "would ship live state (and, for web_app_config.json, credentials) as "
        "a public release asset"
    )


def test_pycache_and_venvs_are_excluded() -> None:
    text = _text()
    for junk in ("__pycache__", ".venv"):
        assert junk in text, f"{junk} is not excluded from the archive"


def test_version_file_is_written_from_the_tag() -> None:
    """The stamp has to come from the tag, not be hand-edited."""
    text = _text()
    assert "VERSION" in text
    assert "github.ref_name" in text, (
        "VERSION should be written from github.ref_name so the stamp and the "
        "tag cannot disagree"
    )


def test_checksum_is_published() -> None:
    text = _text().lower()
    assert "sha256" in text, "no SHA256 checksum step"


def test_workflow_declares_contents_write_permission() -> None:
    """Creating a release needs it, and the default token is read-only on
    repos configured that way — the failure is a late, confusing 403."""
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(_text())
    perms = doc.get("permissions") or next(
        (j.get("permissions") for j in doc["jobs"].values() if j.get("permissions")),
        None,
    )
    assert perms and perms.get("contents") == "write"
