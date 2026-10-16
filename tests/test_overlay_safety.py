"""Regression: committed root TOMLs carry placeholder values, never secrets.

CLAUDE.md's "Dev overlay safety" rule — committed ``maestro.{env}.toml``
files contain only safe placeholder values, never real secrets, tokens, or
PII — shipped as prose with no fence, unlike every sibling invariant in that
document. A real credential pasted into an overlay passed ruff, mypy, pytest,
coverage, and the browser suite without a murmur; the only thing between a
committed token and ``origin/main`` was a human noticing it in a diff.

These tests turn the *hard-block credential* slice of that rule into a gate at
zero cost, reusing the in-tree scanner :mod:`maestro.secret_scan` already
backing the publication-approval path. They fence exactly what
``HARD_BLOCK_PATTERNS`` covers — Anthropic/OpenAI keys, GitHub tokens, AWS
access keys, private-key blocks, keyword-proximate base64. Credential shapes
outside that tuple (notably a Telegram bot token) and PII are *not* covered
and stay human-enforced; widening the pattern set touches the publication
path's own blast radius and belongs in its own change.

They read committed repo artifacts rather than exercising runtime file I/O,
so — like the repo-tree guard in ``tests/test_no_line_citations_in_docs`` —
they use a real ``Path`` read rather than ``mem_fs``.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from maestro.secret_scan import has_hard_block, scan_contents

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: The config loader sources ``maestro.toml`` plus an optional
#: ``maestro.{env}.toml`` overlay from the project root, so the glob fences
#: exactly the surface the invariant names. A plain glob (rather than
#: ``git ls-files``) keeps this a subprocess-free unit test and is strictly
#: safer: it also catches an untracked overlay sitting one ``git add`` away
#: from being committed.
_OVERLAY_GLOB = "maestro*.toml"

#: Overlays known to be committed today. The vacuity guard below pins these
#: so a repo-layout change or a test relocation cannot silently reduce the
#: scan to zero files.
_KNOWN_OVERLAYS = frozenset(
    {
        "maestro.toml",
        "maestro.development.toml",
        "maestro.production.toml",
    }
)

#: An AWS-key-shaped literal for the negative control below, split across a
#: concatenation so this file never carries a contiguous ``AKIA`` match for
#: an external scanner (or GitHub push protection) to flag as a real leak.
_SEEDED_AWS_KEY = "AKIA" + "EXAMPLEKEY123456"


def _committed_overlays() -> list[Path]:
    """Return every root-level ``maestro*.toml``, sorted by name.

    The non-empty assertion lives here rather than in one sibling test so
    that every caller inherits it: a glob resolving nothing would otherwise
    let the scan below pass having validated zero files whenever it runs
    alone (``pytest tests/test_overlay_safety.py::test_committed_...``).
    """
    paths = sorted(_REPO_ROOT.glob(_OVERLAY_GLOB))
    assert paths, (
        f"Expected at least one {_OVERLAY_GLOB} file at {_REPO_ROOT}; "
        "the glob matched nothing, so the secret scan would pass without "
        "validating anything."
    )
    return paths


def _hard_block_report(sources: Iterable[tuple[str, str]]) -> str:
    """Return a failure message for every hard-blocking source; ``""`` if none.

    Pure over ``(name, text)`` pairs rather than reading files itself, so the
    offender branch — dead for as long as the tree stays clean — is exercised
    by the seeded negative control below. Coverage measures only ``maestro/``
    and ``maestro_review/``, so an unreachable branch here is flagged by
    nothing else.
    """
    offenders: list[tuple[str, list[str]]] = []
    for name, text in sources:
        if has_hard_block(text):
            # Findings name the credential *kind* and a preview already
            # capped at 12 chars by _truncate_preview, so the failure
            # message identifies the leak without echoing a usable secret.
            offenders.append((name, scan_contents(text).hard_block_findings))
    if not offenders:
        return ""
    return (
        "Committed root TOMLs contain hard-block credentials: "
        + "; ".join(f"{name}: {findings}" for name, findings in offenders)
        + ". Replace each with a safe placeholder value and rotate the "
        "leaked credential; secrets belong in environment variables only."
    )


def test_committed_overlays_carry_no_hard_block_secrets() -> None:
    """No committed root TOML may contain a hard-block credential."""
    report = _hard_block_report(
        (path.name, path.read_text(encoding="utf-8")) for path in _committed_overlays()
    )
    assert report == "", report


def test_hard_block_report_fires_on_a_seeded_credential() -> None:
    """Negative control: the offender path must fire on a seeded key.

    All four committed overlays scan clean today, so the assertion above
    passes without ever building a report. Without this control a scanner
    regression — a pattern tuple emptied, an import rebound — would leave
    the fence green while checking nothing.
    """
    report = _hard_block_report(
        [("maestro.development.toml", f'aws_key = "{_SEEDED_AWS_KEY}"\n')]
    )
    assert "maestro.development.toml" in report
    assert "aws_access_key" in report


def test_hard_block_report_is_empty_for_placeholder_values() -> None:
    """Positive control: a placeholder-shaped overlay yields no report."""
    placeholder = 'api_key = "your-api-key-here"\nchat_id = "123456789"\n'
    assert _hard_block_report([("maestro.development.toml", placeholder)]) == ""


def test_overlay_safety_scan_covers_the_known_overlays() -> None:
    """The scan must still reach every overlay committed today.

    ``_committed_overlays`` already fails loudly on an empty glob, so this
    guards the remaining rot: an overlay dropping out of the scanned set
    while other files keep the glob non-empty.
    """
    scanned = {path.name for path in _committed_overlays()}
    missing = _KNOWN_OVERLAYS - scanned
    assert not missing, (
        f"Known committed overlays are missing from the scan: {sorted(missing)}. "
        "Either the file was removed (drop it from _KNOWN_OVERLAYS) or the "
        "glob no longer resolves the repo root."
    )
