"""Regression: the proposal ``Status`` axis is a closed, typed vocabulary, and
the README **Open Proposals** column stays bound to each linked doc's own
``Status`` cell.

The status of an open proposal lives in two places — the ``| **Status** |``
metadata row of the proposal doc, and the Status column of that proposal's row
in the ``docs/proposals/README.md`` **Open Proposals** table. Those two must
agree, and both must draw from a closed set of tokens. The concrete drift this
kills: a burn-in proposal's README column read a bare ``burn-in`` while its rollout
position (ready / observing / graduated) was recoverable only from a
hand-maintained prose bullet that had already gone stale (one bullet said a
proposal's flags were "default-off" while ``maestro.production.toml`` had that
canary live). A typed vocabulary plus a number-keyed README↔doc binding converts
that silent drift into a loud CI failure.

This is a deterministic, zero-LLM-cost doc-lint in the same family as
``tests/test_no_line_citations_in_docs.py`` and
``tests/test_proposals_readme_shipped_order.py``: read-only over
``docs/proposals/``, no ``mem_fs``, no network, no subprocess, no ``sleep``.

Scope note: the lint covers the **Open Proposals** table and the docs it links.
Those cells are clean single tokens and are the live drift surface. Shipped and
superseded docs are removed from Open Proposals and carry free-text-augmented
metadata values (e.g. ``shipped (all 15 steps…)``) that a closed-vocabulary
check would need token normalization to admit — deliberately out of scope here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

_PROPOSALS_DIR = Path(__file__).resolve().parent.parent / "docs" / "proposals"
_README = _PROPOSALS_DIR / "README.md"

_OPEN_HEADING = "## Open Proposals"

# The prose section beneath the Open Proposals table. Its membership — which
# proposals get a bullet — must track the typed rows, so a graduated proposal
# cannot leave an orphan bullet behind and a newly-typed one cannot go unlisted.
_BURN_IN_HEADING = "### Burn-in — pending graduation & decommission"

# The closed status vocabulary — the first machine-readable enumeration of it.
# Mirrors the README "Status Key": the base tokens plus the three burn-in
# substates. A ``burn-in`` token MUST carry a substate suffix, so bare
# ``burn-in`` is deliberately absent (see ``test_no_open_proposal_uses_bare_burn_in``).
_CLOSED_STATUS_VOCABULARY = frozenset(
    {
        "proposed",
        "approved",
        "in-progress",
        "partial",
        "shipped",
        "superseded",
        "deferred",
        "burn-in:ready",
        "burn-in:observing",
        "burn-in:graduated",
    }
)

_BARE_BURN_IN = "burn-in"

# Order-of-magnitude non-vacuity floor: the Open Proposals table holds dozens of
# rows, so a parse yielding fewer than this many means the row regex or the
# section slice broke and the checks below would pass without validating
# anything. Well below the current population (~50) to tolerate proposals
# shipping out of the table over time.
_MIN_EXPECTED_ROWS = 20

# Non-vacuity floor for the burn-in bullet parse, same purpose as
# ``_MIN_EXPECTED_ROWS``: if the bullet regex or the section slice breaks, the
# membership check would compare two empty sets and pass. Set well below the
# current population (7) so proposals graduating out of burn-in over time do not
# trip it.
_MIN_EXPECTED_BURN_IN_BULLETS = 3

# An Open Proposals data row: ``| [NNN](path) | Title | Impact | Status |``.
# Group 1 is the proposal number (the binding key), group 2 the linked doc path.
# The ``[NNN](...)`` link is what scopes the match to Open Proposals rows — the
# Shipped table uses bare ``| NNN |`` numbers and the burn-in prose section uses
# ``- **[NNN]**`` bullets, neither of which matches this ``^\| \[NNN\](...)``
# shape.
_ROW_RE = re.compile(r"^\|\s*\[(\d{3})\]\(([^)]+)\)\s*\|")

# A burn-in section bullet: ``- **[NNN](path)** — remaining work…``. Anchored at
# line start on the ``- **[`` bullet shape, so it matches only genuine bullets —
# never an Open Proposals table row (``| [NNN](...)``), never a Shipped-style
# bare-number row, and never a prose mention of ``[NNN](...)`` mid-sentence.
_BURN_IN_BULLET_RE = re.compile(r"^- \*\*\[(\d{3})\]\(")

# Any markdown heading (``# `` … ``###### ``) at line start ends the Open
# Proposals table region.
_NEXT_HEADING_RE = re.compile(r"\n#{1,6} ")

# A proposal doc's metadata ``| **Status** | <token> … |`` row. ``(\S+)`` is the
# leading whitespace-delimited token, so a free-text-augmented cell still yields
# its leading token. ``re.MULTILINE`` anchors ``^`` to line start; ``.search``
# returns the first match, which is the metadata row near the top of the doc.
_DOC_STATUS_RE = re.compile(r"^\|\s*\*\*Status\*\*\s*\|\s*(\S+)", re.MULTILINE)


class _Row(NamedTuple):
    """One parsed Open Proposals row."""

    number: int
    doc_path: str
    status: str


def _open_proposals_section(text: str) -> str:
    """Slice the Open Proposals table region: from its heading to the next
    markdown heading (or end of document if none follows)."""
    start = text.index(_OPEN_HEADING)
    after = start + len(_OPEN_HEADING)
    m = _NEXT_HEADING_RE.search(text, after)
    end = m.start() if m is not None else len(text)
    return text[start:end]


def _split_cells(row: str) -> list[str]:
    """Split one ``| a | b | c |`` table row into stripped cells."""
    inner = row.strip()
    return [cell.strip() for cell in inner[1:-1].split("|")]


def _parse_open_proposals_rows(readme: str) -> list[_Row]:
    """Parse the Open Proposals table into ``_Row`` records, keyed on the
    proposal number in each row's ``[NNN](...)`` link."""
    section = _open_proposals_section(readme)
    rows: list[_Row] = []
    for line in section.splitlines():
        m = _ROW_RE.match(line)
        if m is None:
            continue
        status = _split_cells(line)[-1]
        rows.append(_Row(number=int(m.group(1)), doc_path=m.group(2), status=status))
    return rows


def _extract_doc_status_token(doc_body: str) -> str | None:
    """Return the leading token of a doc's ``| **Status** |`` cell, or ``None``
    when the doc has no such metadata row."""
    m = _DOC_STATUS_RE.search(doc_body)
    if m is None:
        return None
    return m.group(1)


def _load_open_proposals_rows() -> list[_Row]:
    """Read and parse the real README, failing loudly if the parse is vacuous."""
    rows = _parse_open_proposals_rows(_README.read_text(encoding="utf-8"))
    assert len(rows) >= _MIN_EXPECTED_ROWS, (
        f"Parsed only {len(rows)} Open Proposals rows from {_README}; expected "
        f"at least {_MIN_EXPECTED_ROWS} (the table holds dozens). The row regex "
        "or the section slice changed, so the checks below would otherwise pass "
        "vacuously."
    )
    return rows


def _burn_in_section(text: str) -> str:
    """Slice the burn-in prose section: from its heading to the next markdown
    heading (or end of document if none follows). Same shape as
    ``_open_proposals_section``."""
    start = text.index(_BURN_IN_HEADING)
    after = start + len(_BURN_IN_HEADING)
    m = _NEXT_HEADING_RE.search(text, after)
    end = m.start() if m is not None else len(text)
    return text[start:end]


def _parse_burn_in_bullet_numbers(readme: str) -> list[int]:
    """Parse the proposal numbers bulleted in the burn-in section.

    Scans only the section's **contiguous bullet run** — from its first
    ``- **[NNN](`` bullet to the blank line that terminates the markdown list —
    rather than the whole slice. The slice reaches from the burn-in heading to
    the next markdown heading, which in the real README is ~120 lines: seven
    bullets followed by the track-narrative paragraphs ("Proposals 068-074 form
    the architecture-review track…"). Those cite proposal numbers heavily, so a
    future ``- **[NNN](path)**`` bullet written *there* would otherwise be read
    as burn-in membership and fail this lint spuriously. Bounding to the run
    keeps membership meaning "listed in the burn-in list", not "mentioned
    somewhere under the burn-in heading".

    Only the terminating blank line ends the run — a non-blank, non-bullet line
    (a wrapped bullet continuation) does not — so a reflowed bullet stays inside.
    """
    numbers: list[int] = []
    started = False
    for line in _burn_in_section(readme).splitlines():
        m = _BURN_IN_BULLET_RE.match(line)
        if m is not None:
            started = True
            numbers.append(int(m.group(1)))
        elif started and not line.strip():
            break
    return numbers


def _duplicate_numbers(numbers: list[int]) -> list[int]:
    """Return the proposal numbers appearing more than once, ascending.

    ``_parse_burn_in_bullet_numbers`` deliberately returns document order *with*
    repeats, but the membership check compares *sets* — so a proposal bulleted
    twice collapses to one member and still compares equal to its single typed
    row. Two bullets for one proposal are two carriers of the same remaining
    work, which is precisely the hand-maintenance drift this section's lint
    exists to convert into a loud failure, so the repeat is reported before the
    set comparison can hide it.
    """
    seen: set[int] = set()
    duplicates: set[int] = set()
    for number in numbers:
        if number in seen:
            duplicates.add(number)
        seen.add(number)
    return sorted(duplicates)


# ---------------------------------------------------------------------------
# (a) closed vocabulary
# ---------------------------------------------------------------------------


def test_open_proposals_statuses_are_in_closed_vocabulary() -> None:
    """Every Open Proposals status column value and every linked doc's ``Status``
    token must be in the closed vocabulary."""
    offenders: list[tuple[int, str, str | None]] = []
    for row in _load_open_proposals_rows():
        if row.status not in _CLOSED_STATUS_VOCABULARY:
            offenders.append((row.number, "README", row.status))
        doc = _PROPOSALS_DIR / row.doc_path
        assert doc.is_file(), (
            f"Open Proposals row {row.number} links {row.doc_path}, which does "
            f"not resolve under {_PROPOSALS_DIR}; the binding cannot be checked."
        )
        token = _extract_doc_status_token(doc.read_text(encoding="utf-8"))
        if token not in _CLOSED_STATUS_VOCABULARY:
            offenders.append((row.number, "doc", token))
    assert not offenders, (
        "Proposal status tokens outside the closed vocabulary "
        f"{sorted(_CLOSED_STATUS_VOCABULARY)}: "
        + "; ".join(f"#{n} ({where}): {tok!r}" for n, where, tok in offenders)
    )


def test_closed_vocabulary_detector_catches_seeded_typo() -> None:
    """Negative control: a status typo is caught on both the README and doc side."""
    typo_readme = (
        "## Open Proposals\n"
        "| [201](core/201-alpha.md) | Alpha | HIGH | propsoed |\n"
        "### End\n"
    )
    rows = _parse_open_proposals_rows(typo_readme)
    assert rows[0].status == "propsoed"
    assert rows[0].status not in _CLOSED_STATUS_VOCABULARY
    doc_token = _extract_doc_status_token("| **Status** | observng |\n")
    assert doc_token == "observng"
    assert doc_token not in _CLOSED_STATUS_VOCABULARY


# ---------------------------------------------------------------------------
# (b) no bare burn-in
# ---------------------------------------------------------------------------


def test_no_open_proposal_uses_bare_burn_in() -> None:
    """No Open Proposals status token — README column or linked doc — may be the
    bare ``burn-in``; it must carry a substate suffix."""
    offenders: list[tuple[int, str]] = []
    for row in _load_open_proposals_rows():
        if row.status == _BARE_BURN_IN:
            offenders.append((row.number, "README"))
        doc = _PROPOSALS_DIR / row.doc_path
        assert doc.is_file(), (
            f"Open Proposals row {row.number} links {row.doc_path}, which does "
            f"not resolve under {_PROPOSALS_DIR}; the binding cannot be checked."
        )
        token = _extract_doc_status_token(doc.read_text(encoding="utf-8"))
        if token == _BARE_BURN_IN:
            offenders.append((row.number, "doc"))
    assert not offenders, (
        "Bare 'burn-in' status tokens (must carry a :ready/:observing/:graduated "
        "substate): " + "; ".join(f"#{n} ({where})" for n, where in offenders)
    )


def test_bare_burn_in_detector_catches_seeded_token() -> None:
    """Negative control: a bare ``burn-in`` is caught on both sides."""
    bare_readme = (
        "## Open Proposals\n"
        "| [201](core/201-alpha.md) | Alpha | HIGH | burn-in |\n"
        "### End\n"
    )
    rows = _parse_open_proposals_rows(bare_readme)
    assert rows[0].status == _BARE_BURN_IN
    assert _extract_doc_status_token("| **Status** | burn-in |\n") == _BARE_BURN_IN


# ---------------------------------------------------------------------------
# (c) README ↔ doc binding, keyed on proposal number
# ---------------------------------------------------------------------------


def test_readme_status_column_matches_linked_doc() -> None:
    """For every Open Proposals row, the README status column must equal the
    linked doc's own ``Status`` token, keyed on proposal number (never title)."""
    mismatches: list[tuple[int, str, str | None]] = []
    for row in _load_open_proposals_rows():
        doc = _PROPOSALS_DIR / row.doc_path
        assert doc.is_file(), (
            f"Open Proposals row {row.number} links {row.doc_path}, which does "
            f"not resolve under {_PROPOSALS_DIR}; the binding cannot be checked."
        )
        token = _extract_doc_status_token(doc.read_text(encoding="utf-8"))
        if row.status != token:
            mismatches.append((row.number, row.status, token))
    assert not mismatches, (
        "README Open Proposals status column disagrees with the linked doc's "
        "Status: "
        + "; ".join(
            f"#{n}: README {col!r} vs doc {tok!r}" for n, col, tok in mismatches
        )
    )


def test_binding_detector_catches_seeded_mismatch() -> None:
    """Negative control: a README-vs-doc status disagreement is detected."""
    readme_status = "burn-in:observing"
    doc_status = _extract_doc_status_token("| **Status** | burn-in:ready |\n")
    assert doc_status == "burn-in:ready"
    assert readme_status != doc_status


# ---------------------------------------------------------------------------
# (d) burn-in section membership ↔ typed rows
# ---------------------------------------------------------------------------


def test_burn_in_section_membership_matches_typed_rows() -> None:
    """The burn-in section bullets and the ``burn-in``-typed Open Proposals rows
    must name exactly the same proposals.

    The token is the authoritative carrier of rollout position; the bullets carry
    only remaining work and tripwires. Binding the two sets keeps that division
    honest in both directions: a proposal that graduates out of burn-in cannot
    leave an orphan bullet behind, and a newly-typed one cannot go unlisted.
    """
    readme = _README.read_text(encoding="utf-8")
    bulleted = _parse_burn_in_bullet_numbers(readme)
    assert len(bulleted) >= _MIN_EXPECTED_BURN_IN_BULLETS, (
        f"Parsed only {len(bulleted)} burn-in bullets from {_README}; expected at "
        f"least {_MIN_EXPECTED_BURN_IN_BULLETS}. The bullet regex or the section "
        "slice changed, so the membership check below would otherwise compare two "
        "empty sets and pass vacuously."
    )
    repeats = _duplicate_numbers(bulleted)
    assert not repeats, (
        f"Proposal(s) {repeats} carry more than one bullet in the burn-in "
        f"section of {_README}. Each burn-in proposal gets exactly one "
        "remaining-work bullet; a second is a duplicate carrier that drifts "
        "from the first, and the set comparison below cannot see it."
    )
    typed = {
        row.number
        for row in _load_open_proposals_rows()
        if row.status.startswith(_BARE_BURN_IN)
    }
    orphans = sorted(set(bulleted) - typed)
    unlisted = sorted(typed - set(bulleted))
    assert not orphans and not unlisted, (
        "Burn-in section membership disagrees with the typed Open Proposals rows. "
        f"Bulleted but not typed burn-in (remove the bullet): {orphans}; "
        f"typed burn-in but not bulleted (add a remaining-work bullet): {unlisted}."
    )


def test_burn_in_membership_detector_catches_orphan_bullet() -> None:
    """Negative control (orphan side): a bullet for a proposal whose row is
    ``partial`` is reported as bulleted-but-not-typed."""
    fixture = (
        "## Open Proposals\n"
        "| [201](core/201-alpha.md) | Alpha | HIGH | partial |\n"
        "| [202](core/202-beta.md) | Beta | MEDIUM | burn-in:ready |\n"
        "\n"
        f"{_BURN_IN_HEADING}\n"
        "\n"
        "- **[201](core/201-alpha.md)** — an orphan: its row is `partial`.\n"
        "- **[202](core/202-beta.md)** — what remains is the flag graduation.\n"
        "\n"
        "### Status Key\n"
    )
    bulleted = set(_parse_burn_in_bullet_numbers(fixture))
    typed = {
        row.number
        for row in _parse_open_proposals_rows(fixture)
        if row.status.startswith(_BARE_BURN_IN)
    }
    assert bulleted == {201, 202}
    assert typed == {202}
    assert sorted(bulleted - typed) == [201]


def test_burn_in_membership_detector_catches_missing_bullet() -> None:
    """Negative control (unlisted side): a ``burn-in:observing`` row with no
    bullet is reported as typed-but-not-bulleted."""
    fixture = (
        "## Open Proposals\n"
        "| [201](core/201-alpha.md) | Alpha | HIGH | burn-in:observing |\n"
        "| [202](core/202-beta.md) | Beta | MEDIUM | burn-in:ready |\n"
        "\n"
        f"{_BURN_IN_HEADING}\n"
        "\n"
        "- **[202](core/202-beta.md)** — what remains is the flag graduation.\n"
        "\n"
        "### Status Key\n"
    )
    bulleted = set(_parse_burn_in_bullet_numbers(fixture))
    typed = {
        row.number
        for row in _parse_open_proposals_rows(fixture)
        if row.status.startswith(_BARE_BURN_IN)
    }
    assert bulleted == {202}
    assert typed == {201, 202}
    assert sorted(typed - bulleted) == [201]


def test_burn_in_membership_detector_catches_duplicate_bullet() -> None:
    """Negative control (duplicate side): a proposal bulleted twice is reported.

    The membership assertion differences *sets*, which collapses the repeat, so
    a README carrying two bullets for one proposal satisfies it unchanged — the
    duplicate check is the only thing that sees the second carrier. The parser
    preserving document order and repeats is what makes that possible.
    """
    fixture = (
        "## Open Proposals\n"
        "| [201](core/201-alpha.md) | Alpha | HIGH | burn-in:ready |\n"
        "\n"
        f"{_BURN_IN_HEADING}\n"
        "\n"
        "- **[201](core/201-alpha.md)** — what remains is the decommission.\n"
        "- **[201](core/201-alpha.md)** — a second, drifting copy.\n"
        "\n"
        "### Status Key\n"
    )
    bulleted = _parse_burn_in_bullet_numbers(fixture)
    typed = {
        row.number
        for row in _parse_open_proposals_rows(fixture)
        if row.status.startswith(_BARE_BURN_IN)
    }
    assert bulleted == [201, 201]
    assert _duplicate_numbers(bulleted) == [201]
    # The set comparison the duplicate check guards is blind to the repeat: it
    # agrees with the typed rows in both directions despite the extra bullet.
    assert set(bulleted) == typed

    # And the detector stays quiet on a section whose bullets are distinct.
    assert _duplicate_numbers([201, 202, 203]) == []


def test_burn_in_bullet_parser_ignores_table_rows_and_track_prose() -> None:
    """The bullet parser reads only the section's contiguous bullet run.

    The section slice runs to the next markdown heading, so it sweeps in the
    track-narrative paragraphs that follow the bullets in the real README. Those
    cite proposal numbers freely — inline links, bold ranges, and Shipped-style
    bare-number rows — and none may be read as section membership. The last
    narrative line is the sharp case: a genuine ``- **[NNN](path)**`` bullet
    written *after* the list is prose, not membership, and counting it would
    fail the lint spuriously against a README that is perfectly in sync.
    """
    fixture = (
        "## Open Proposals\n"
        "| [201](core/201-alpha.md) | Alpha | HIGH | burn-in:ready |\n"
        "\n"
        f"{_BURN_IN_HEADING}\n"
        "\n"
        "- **[201](core/201-alpha.md)** — what remains is the decommission,\n"
        "  which this wrapped continuation line keeps inside the run.\n"
        "\n"
        "| [301](core/301-table.md) | A table row inside the slice | LOW | partial |\n"
        "| 302 | A shipped-style bare-number row | Landed as |\n"
        "Proposals **303-304** form a track; see [303](core/303-gamma.md) in prose.\n"
        "  - **[305](core/305-indented.md)** — an indented sub-bullet, not a member.\n"
        "- **[306](core/306-narrative.md)** — a bullet past the run, not a member.\n"
        "\n"
        "### Status Key\n"
    )
    assert _parse_burn_in_bullet_numbers(fixture) == [201]


def test_burn_in_section_slice_runs_to_end_without_trailing_heading() -> None:
    """Section slicer's no-following-heading branch: the slice runs to the end of
    the document when no heading follows the burn-in section."""
    text = (
        "## Open Proposals\n"
        f"{_BURN_IN_HEADING}\n"
        "- **[201](core/201-alpha.md)** — what remains is the decommission.\n"
    )
    section = _burn_in_section(text)
    assert section.startswith(_BURN_IN_HEADING)
    assert _parse_burn_in_bullet_numbers(text) == [201]


# ---------------------------------------------------------------------------
# parser / extractor negative controls
# ---------------------------------------------------------------------------


def test_row_parser_scopes_to_open_proposals_and_ignores_noise() -> None:
    """The row parser reads only genuine linked-NNN rows inside the Open
    Proposals section — never Shipped-style bare-number rows or prose mentions."""
    fixture = (
        "## Open Proposals\n"
        "| # | Title | Impact | Status |\n"
        "|---|-------|--------|--------|\n"
        "| [201](core/201-alpha.md) | Alpha | HIGH | partial |\n"
        "| [202](core/202-beta.md) | Beta | MEDIUM | burn-in:ready |\n"
        "\n"
        "### Burn-in — pending graduation & decommission\n"
        "| 119 | Shipped Row | Landed as |\n"
        "See proposal [203](core/203-gamma.md) in prose, not a table row.\n"
        "- **[204](core/204-delta.md)** — a burn-in bullet, not a row.\n"
    )
    rows = _parse_open_proposals_rows(fixture)
    assert [(r.number, r.doc_path, r.status) for r in rows] == [
        (201, "core/201-alpha.md", "partial"),
        (202, "core/202-beta.md", "burn-in:ready"),
    ]


def test_open_proposals_section_slice_runs_to_end_without_trailing_heading() -> None:
    """Section slicer's no-following-heading branch: the slice runs to the end of
    the document when no heading follows Open Proposals."""
    text = (
        "intro prose\n"
        "## Open Proposals\n"
        "| [201](core/201-alpha.md) | Alpha | HIGH | partial |\n"
    )
    section = _open_proposals_section(text)
    assert section.startswith(_OPEN_HEADING)
    assert "201" in section
    assert [r.number for r in _parse_open_proposals_rows(text)] == [201]


def test_extract_doc_status_token_reads_leading_token() -> None:
    """The extractor returns the leading token, dropping any augmenting text."""
    assert _extract_doc_status_token("| **Status** | partial |\n") == "partial"
    assert _extract_doc_status_token("| **Status** | partial (Phase 2) |\n") == "partial"


def test_extract_doc_status_token_handles_missing_row() -> None:
    """The extractor returns ``None`` for a body with no ``Status`` metadata row."""
    assert _extract_doc_status_token("no metadata table here") is None
    assert _extract_doc_status_token("| **Impact** | HIGH |\n") is None
