"""Regression: a ``## What is missing`` section must not say nothing is missing.

A heading that asks *what is missing* answered by *nothing is missing* is
self-contradictory at a glance. The concrete failure this test kills (#1674):
the shipped proposal ``publication/030-artifact-persistence-foundation.md``
carried a ``## What is missing`` section whose body read "All slices have
landed. …" — making 030 the sole outlier in the 030-033 publication set, whose
siblings 031/032/033 each expose only a ``## What landed`` section. The
"everything landed" fact is already carried by ``| **Status** | shipped |`` and
the ``## What landed`` section, so the contradictory section is pure residue.

The guard is scoped to ``##``-level section headers under
``docs/proposals/publication/`` on purpose: the older ``core/`` table-row
convention (``| **What is missing** | Nothing material… |`` in
``016-sdk-call-optimization.md``) is a different, out-of-scope shape and must
stay unaffected. Same read-only checked-in-docs meta-test pattern as the
sibling ``tests/test_no_line_citations_in_docs.py`` — no ``mem_fs``, no
network, no subprocess.
"""

from __future__ import annotations

import re
from pathlib import Path

_PUBLICATION_DIR = (
    Path(__file__).resolve().parent.parent / "docs" / "proposals" / "publication"
)

# Captures a ``## What is missing`` section body: the ``##``-level header line
# (``### What is missing`` and the ``| **What is missing** |`` table row both
# fail to match — the former because ``##`` is not followed by whitespace, the
# latter because it is not at the ATX-header position) through to the next ATX
# header or end of file. DOTALL lets the body span lines; MULTILINE anchors the
# header and the next-header lookahead to line boundaries.
_MISSING_SECTION = re.compile(
    r"^##[ \t]+What is missing[ \t]*$"  # exactly a ``## What is missing`` header
    r"(?P<body>.*?)"  # its body (non-greedy)
    r"(?=^#{1,6}[ \t]|\Z)",  # up to the next ATX header or EOF
    re.MULTILINE | re.DOTALL,
)

# Signals that the section body claims nothing is actually missing — the
# self-contradiction. The specific phrase 030 shipped ("All slices have
# landed") plus the two generic escape hatches named in the issue.
_NOTHING_MISSING_SIGNAL = re.compile(
    r"all slices have landed|nothing (?:is )?missing|nothing material",
    re.IGNORECASE,
)


def _find_stale_missing_sections(text: str) -> list[str]:
    """Return the body of every ``## What is missing`` section that, despite the
    heading, signals nothing is missing (possibly empty)."""
    offenders: list[str] = []
    for match in _MISSING_SECTION.finditer(text):
        body = match.group("body")
        if _NOTHING_MISSING_SIGNAL.search(body):
            offenders.append(body.strip())
    return offenders


def test_no_stale_missing_section_in_publication_proposals() -> None:
    """No ``publication/**/*.md`` may carry a self-contradictory section."""
    md_files = sorted(_PUBLICATION_DIR.rglob("*.md"))
    # Python 3.12 rglob() silently yields nothing for a missing directory
    # instead of raising, so without this guard the scan below would pass
    # vacuously (zero files checked) if the publication/ tree ever moved.
    assert md_files, (
        f"Expected at least one .md file under {_PUBLICATION_DIR}; "
        "directory is missing or the path is wrong, so the scan would "
        "pass without validating anything."
    )
    offenders: list[str] = []
    for path in md_files:
        if _find_stale_missing_sections(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(_PUBLICATION_DIR.parent.parent)))
    assert not offenders, (
        "Publication proposals carry a '## What is missing' section that says "
        "nothing is missing (self-contradictory): " + ", ".join(offenders) + ". "
        "Remove the section — the 'everything landed' fact is already carried "
        "by '| **Status** | shipped |' and the '## What landed' section."
    )


def test_detector_fires_on_seeded_stale_section() -> None:
    """Negative control: the detector must catch a seeded contradiction."""
    text = (
        "## What landed\n\nEverything.\n\n"
        "## What is missing\n\nAll slices have landed. Nothing to do.\n"
    )
    hits = _find_stale_missing_sections(text)
    assert hits == ["All slices have landed. Nothing to do."]


def test_detector_ignores_genuine_missing_section() -> None:
    """A ``## What is missing`` section describing real remaining work is fine."""
    text = "## What is missing\n\nThe retry backoff is not yet wired up.\n"
    assert _find_stale_missing_sections(text) == []


def test_detector_ignores_table_row_convention() -> None:
    """The older ``core/`` table-row shape is out of scope and never flagged."""
    text = "| **What is missing** | Nothing material. It all shipped. |\n"
    assert _find_stale_missing_sections(text) == []


def test_detector_ignores_deeper_heading_level() -> None:
    """A ``### What is missing`` sub-header is not a ``##``-level section."""
    text = "### What is missing\n\nNothing missing here either.\n"
    assert _find_stale_missing_sections(text) == []
