"""Regression: proposal docs cite symbols/sections, never ``file.py:line``.

Line numbers rot the instant code shifts. A ``foo.py:123`` citation in a
checked-in Markdown doc is correct only until the next edit above line 123,
after which it silently lies. The concrete failure this test kills: five-plus
remediation tasks (#2376, #2359, #2443, the #2362 chain) all dead-ended at the
same phantom doc-only conflict — a stale ``file.py:<line>`` citation under
``docs/proposals/`` that every ``origin/main`` merge re-spawned and a human
hand-resolved while calling it "success."

Banning the token deterministically self-extinguishes the entire conflict
class at zero LLM cost. Cite a stable symbol or section anchor instead
(``Memory.archive()`` rather
than ``agent.py:5094``); ``foo.py:run_loop()`` is fine because it carries no
line number.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from maestro.flag_lifecycle import CUTOVER_FLAGS, CutoverFlag

_PROPOSALS_DIR = Path(__file__).resolve().parent.parent / "docs" / "proposals"

# Matches a Python-file path immediately followed by ``:<digits>`` — the
# rotting line-number citation shape. ``foo.py:42`` matches; ``foo.py``,
# ``foo.py:run_loop()``, and prose like "the .py file" do not.
_LINE_CITATION = re.compile(r"\b[\w./-]+\.py:\d+\b")


def _find_line_citations(text: str) -> list[str]:
    """Return every ``file.py:<line>`` token in ``text`` (possibly empty)."""
    return _LINE_CITATION.findall(text)


def test_no_line_citations_in_proposal_docs() -> None:
    """No ``docs/proposals/**/*.md`` file may cite ``file.py:<line>``."""
    md_files = sorted(_PROPOSALS_DIR.rglob("*.md"))
    # Python 3.12 rglob() silently yields nothing for a missing directory
    # instead of raising, so without this guard the scan below would pass
    # vacuously (zero files checked) if docs/proposals/ ever went missing.
    assert md_files, (
        f"Expected at least one .md file under {_PROPOSALS_DIR}; "
        "directory is missing or the path is wrong, so the scan would "
        "pass without validating anything."
    )
    offenders: list[tuple[str, list[str]]] = []
    for path in md_files:
        hits = _find_line_citations(path.read_text(encoding="utf-8"))
        if hits:
            rel = path.relative_to(_PROPOSALS_DIR.parent.parent)
            offenders.append((str(rel), hits))
    assert not offenders, (
        "Proposal docs cite rotting file.py:line locations: "
        + "; ".join(f"{rel}: {hits}" for rel, hits in offenders)
        + ". Replace each with a stable symbol or section anchor "
        "(e.g. Memory.archive() instead of memory.py:53)."
    )


def test_detector_fires_on_seeded_citation() -> None:
    """Negative control: the detector must catch a seeded line citation.

    Without this, a detector regression (e.g. a broken regex matching
    nothing) would make the clean-tree assertion above pass vacuously.
    """
    hits = _find_line_citations("see maestro/foo.py:123 for the call site")
    assert hits == ["maestro/foo.py:123"]


def test_detector_ignores_symbol_citations() -> None:
    """Symbol/section citations carrying no line number are allowed."""
    text = "resolved in Memory.archive(); see agent.py:run_loop() and foo.py"
    assert _find_line_citations(text) == []


# ``_emit_for`` is a phantom method name: it exists nowhere in ``maestro/``,
# yet proposal 032 cited it as the publication-emission gate and framed it as
# "extended from 031" — where no such method exists either. The real shipped
# method is ``ArtifactReviewer._emit_publication_if_eligible``. Banning the
# token stops a reader from being pointed at a symbol that never existed, the
# same deterministic zero-LLM-cost family as the line-citation guard above.
# ``\b`` boundaries keep a future legitimate ``_emit_format``-style name from
# false-matching on the ``_emit_for`` prefix.
_EMIT_FOR = re.compile(r"\b_emit_for\b")


def _find_emit_for(text: str) -> list[str]:
    """Return every ``_emit_for`` token in ``text`` (possibly empty)."""
    return _EMIT_FOR.findall(text)


def test_no_emit_for_symbol_in_proposal_docs() -> None:
    """No ``docs/proposals/**/*.md`` file may cite the phantom ``_emit_for``."""
    md_files = sorted(_PROPOSALS_DIR.rglob("*.md"))
    # Same missing-directory guard as the line-citation scan: rglob() yields
    # nothing (not an error) for an absent directory, so without this the scan
    # would pass vacuously if docs/proposals/ ever moved.
    assert md_files, (
        f"Expected at least one .md file under {_PROPOSALS_DIR}; "
        "directory is missing or the path is wrong, so the scan would "
        "pass without validating anything."
    )
    offenders: list[str] = []
    for path in md_files:
        if _find_emit_for(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(_PROPOSALS_DIR.parent.parent)))
    assert not offenders, (
        "Proposal docs cite the phantom symbol _emit_for, which exists "
        "nowhere in maestro/: " + ", ".join(offenders) + ". The shipped "
        "publication-emission method is "
        "ArtifactReviewer._emit_publication_if_eligible."
    )


def test_emit_for_detector_fires_on_seeded_symbol() -> None:
    """Negative control: the detector must catch a seeded ``_emit_for``."""
    hits = _find_emit_for("see ArtifactReviewer._emit_for for the gate")
    assert hits == ["_emit_for"]


def test_emit_for_detector_ignores_similar_symbols() -> None:
    """The real method name and ``_emit_format``-style siblings are allowed."""
    text = "_emit_publication_if_eligible runs; _emit_format is unrelated"
    assert _find_emit_for(text) == []


# ``#014`` is proposal 016's own stale self-reference: deferred item D1 opened
# with "Once #014 lands" for its ``usage.jsonl`` dependency, but ``usage.jsonl``
# is delivered by 016's own A1 persistence step — not by proposal 014, which is
# the unrelated self-model semantic curation work. The stale token dates to the
# renumber that moved sdk-call-optimization to 016 without updating D1's
# internal reference. This ban is scoped to the 016 file alone: a global
# ``#014`` ban would wrongly flag legitimate references to proposal 014
# elsewhere. The ``(?!\d)`` lookahead keeps a future ``#0140``-style number from
# false-matching the token, mirroring the ``\b`` boundaries above.
_016_PROPOSAL = _PROPOSALS_DIR / "core" / "016-sdk-call-optimization.md"
_STALE_014_REF = re.compile(r"#014(?!\d)")


def _find_stale_014_ref(text: str) -> list[str]:
    """Return every ``#014`` token (not part of a longer number) in ``text``."""
    return _STALE_014_REF.findall(text)


def test_no_stale_014_ref_in_proposal_016() -> None:
    """Proposal 016 must not cite ``#014``.

    D1's dependency (``usage.jsonl``) is delivered by 016's own A1 step, not by
    the unrelated proposal 014 (self-model semantic curation), so the reference
    sends a reader to the wrong proposal.
    """
    # Direct-file guard analogous to the rglob missing-directory guards above:
    # if 016 is ever moved or renamed, ``read_text`` below would raise, but the
    # explicit check gives a clearer failure than a bare FileNotFoundError.
    assert _016_PROPOSAL.is_file(), (
        f"Expected {_016_PROPOSAL} to exist; the file was moved or renamed, "
        "so this guard would not validate anything."
    )
    hits = _find_stale_014_ref(_016_PROPOSAL.read_text(encoding="utf-8"))
    assert not hits, (
        "Proposal 016 cites the stale cross-reference #014, but its D1 "
        "dependency usage.jsonl is delivered by 016's own A1 step, not by "
        "proposal 014 (unrelated self-model semantic curation). Point D1 at "
        "016's A1 usage.jsonl persistence layer instead."
    )


def test_stale_014_detector_fires_on_seeded_ref() -> None:
    """Negative control: the detector must catch a seeded ``#014``."""
    hits = _find_stale_014_ref("Once #014 lands, read usage.jsonl")
    assert hits == ["#014"]


def test_stale_014_detector_ignores_longer_numbers() -> None:
    """A future ``#0140``-style number must not false-match the ban."""
    assert _find_stale_014_ref("see #0140 and #01499 for the follow-ups") == []


# ``docs/self-evolution.md`` twice restated the total size of the audit-rule
# registry in prose — "the registry now lists **sixteen** rules" and "the
# registry now lists **twenty** rules" — and both were wrong *and* mutually
# contradictory: ``_registered_audit_checks`` returns nineteen rules under
# default config, and ``format_audit_checks_summary()`` already renders the live
# set. Each spelled count is a code-derived constant with nothing tying it back
# to the registry, so every rule added since silently invalidated it. This guard
# bans the drift *shape* — a clause that says the registry *lists* some number of
# *rules* — not a specific value, so the test cannot itself drift as rules are
# added or removed. It is scoped to the one file issue #2964 names.
_SELF_EVOLUTION = (
    Path(__file__).resolve().parent.parent / "docs" / "self-evolution.md"
)
_REGISTRY_RULE_COUNT = re.compile(
    r"registry\b[^.\n]*?\blists\b[^.\n]*?"
    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"twenty-one|twenty-two|twenty-three|twenty-four|twenty-five|twenty-six|"
    r"twenty-seven|twenty-eight|twenty-nine|thirty|\d+)\b"
    # Tolerate up to two words between the count and ``rules`` (e.g. "nineteen
    # deterministic rules", "19 audit rules") so a reworded restatement can't
    # slip the guard by inserting an adjective — the drift shape it bans is
    # "registry lists <N> ... rules", not a count adjacent to the noun.
    r"\*{0,2}(?:\s+[\w-]+){0,2}\s+rules?\b",
    re.IGNORECASE,
)


def _find_registry_rule_count(text: str) -> list[str]:
    """Return every "registry ... lists ... <N> ... rules" clause in ``text``."""
    return _REGISTRY_RULE_COUNT.findall(text)


def test_no_hardcoded_registry_count_in_self_evolution() -> None:
    """``docs/self-evolution.md`` must not restate the audit-registry rule count.

    Two prose clauses ("the registry now lists sixteen/twenty rules") were both
    wrong, contradicted each other, and contradicted the live registry
    (nineteen rules under default config). ``format_audit_checks_summary()``
    already renders the live set, so the prose count is redundant as well as
    drift-prone. This bans the drift *shape*, never a specific value.
    """
    # Direct-file guard mirroring ``_016_PROPOSAL.is_file()`` above: if the doc
    # is ever moved or renamed, ``read_text`` would raise, but the explicit
    # check gives a clearer failure than a bare FileNotFoundError and stops the
    # scan from passing vacuously against a missing file.
    assert _SELF_EVOLUTION.is_file(), (
        f"Expected {_SELF_EVOLUTION} to exist; the file was moved or renamed, "
        "so this guard would not validate anything."
    )
    hits = _find_registry_rule_count(_SELF_EVOLUTION.read_text(encoding="utf-8"))
    assert not hits, (
        "docs/self-evolution.md restates the audit-registry rule count in "
        f"prose: {hits}. Drop the count clause — format_audit_checks_summary() "
        "renders the live registry, and a spelled count silently rots as rules "
        "are added or removed."
    )


def test_registry_count_detector_fires_on_seeded_count() -> None:
    """Negative control: the detector must catch a seeded registry count.

    Without this, a detector regression (e.g. a broken regex matching nothing)
    would make the clean-tree assertion above pass vacuously.
    """
    hits = _find_registry_rule_count("the registry now lists sixteen rules")
    assert hits == ["registry now lists sixteen rules"]


def test_registry_count_detector_fires_on_reworded_count() -> None:
    """The detector catches a count separated from ``rules`` by adjectives.

    Regression for the earlier tightness gap: the regex required the count and
    ``rules`` to be adjacent, so a reworded restatement — CLAUDE.md's own
    "nineteen deterministic rules" phrasing, or a numeric "19 audit rules" —
    slipped past the guard whose whole job is to stop that drift class. Up to
    two intervening words are now tolerated, so the reword is caught too.
    """
    assert _find_registry_rule_count(
        "the registry now lists nineteen deterministic rules"
    ) == ["registry now lists nineteen deterministic rules"]
    assert _find_registry_rule_count("the registry lists 19 audit rules") == [
        "registry lists 19 audit rules"
    ]


def test_registry_count_detector_ignores_non_registry_rule_counts() -> None:
    """Legitimate rule-count prose without a "registry ... lists" clause passes.

    The ``### The eight rules`` section header (a rules 1-8 grouping) and the
    "read the registry ... for the live set" pointer both name rules and/or the
    registry but carry no "registry ... lists ... <N> ... rules" clause, so the
    detector must leave them alone.
    """
    text = (
        "### The eight rules\n"
        "read the registry in audit_checks.py for the live set"
    )
    assert _find_registry_rule_count(text) == []


# Issue #3510: ``docs/testing.md`` advertised a quality-gate mock helper called
# ``make_self_review_mock``, which has never existed anywhere in the tree — the
# real helper is ``make_quality_gates_mock``. A reader following the documented
# mocking surface greps the documented name, finds nothing, and either
# re-derives the helper or writes a duplicate. Nothing tied a symbol name quoted
# in the docs to the test-support surface it claims to describe, so the wrong
# name passed CI indefinitely. This guard checks every backticked ``make_*_mock``
# identifier in the doc against a real definition under ``tests/component/``.
#
# The scan is scoped to ``docs/testing.md`` rather than all of ``docs/`` because
# that is the doc which enumerates the test-support surface, so it is the one
# doc whose helper names are checkable against a known directory; backticked
# ``make_*_mock`` names elsewhere need not be component-test fixtures.
_TESTING_DOC = Path(__file__).resolve().parent.parent / "docs" / "testing.md"
_COMPONENT_DIR = Path(__file__).resolve().parent / "component"
_MOCK_HELPER = re.compile(r"`(make_\w*_mock)`")


def _find_mock_helper_names(text: str) -> list[str]:
    """Return every backticked ``make_*_mock`` identifier in ``text``."""
    return _MOCK_HELPER.findall(text)


def _missing_mock_helpers(cited: list[str], defined: str) -> list[str]:
    """Return the ``cited`` helper names that ``defined`` does not define."""
    return [name for name in cited if f"def {name}" not in defined]


def test_docs_testing_mock_helpers_exist() -> None:
    """Every ``make_*_mock`` helper ``docs/testing.md`` cites must be defined."""
    assert _TESTING_DOC.is_file(), (
        f"Expected {_TESTING_DOC} to exist; the file was moved or renamed, "
        "so this guard would not validate anything."
    )
    assert _COMPONENT_DIR.is_dir(), (
        f"Expected {_COMPONENT_DIR} to exist; the component-test package was "
        "moved, so every cited helper would look undefined."
    )
    cited = _find_mock_helper_names(_TESTING_DOC.read_text(encoding="utf-8"))
    assert cited, (
        "docs/testing.md cites no `make_*_mock` helper at all; the mocking "
        "paragraph was rewritten, so this guard is no longer validating "
        "anything and should be rescoped or removed."
    )
    defined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(_COMPONENT_DIR.rglob("*.py"))
    )
    missing = _missing_mock_helpers(cited, defined)
    assert not missing, (
        f"docs/testing.md cites mock helpers that do not exist: {missing}. "
        "Name the real helper defined under tests/component/ — a phantom name "
        "dead-ends every reader who greps for it."
    )


def test_mock_helper_detector_fires_on_seeded_phantom() -> None:
    """Positive control: the detector must catch a seeded phantom helper name.

    Without this, a detector regression (e.g. a broken regex matching nothing)
    would make the assertion above pass vacuously against a clean doc.
    """
    text = "quality gate tools via `make_self_review_mock`."
    assert _find_mock_helper_names(text) == ["make_self_review_mock"]


def test_mock_helper_detector_ignores_unbackticked_prose() -> None:
    """Prose that is not a backticked helper name is left alone.

    ``SDKMock`` and ``MockVCS`` are cited in the same sentence but are classes,
    not ``make_*_mock`` factories, and a bare unbackticked mention is prose —
    neither should be checked against ``tests/component/``.
    """
    text = "mocked via `SDKMock` and `MockVCS`, and make_unbackticked_mock"
    assert _find_mock_helper_names(text) == []


def test_missing_mock_helper_detector_separates_defined_from_phantom() -> None:
    """The def-existence check must flag only the helper with no definition.

    The controls above exercise the regex alone; this one covers the membership
    logic, so a regression there — scanning the wrong directory, or matching a
    bare mention rather than a ``def`` — cannot pass vacuously.
    """
    defined = "def make_quality_gates_mock(\n    ...\n# make_self_review_mock\n"
    cited = ["make_quality_gates_mock", "make_self_review_mock"]
    assert _missing_mock_helpers(cited, defined) == ["make_self_review_mock"]
    assert _missing_mock_helpers([], defined) == []


# Issue #3583: proposal 111's **What is missing** row named
# ``base_advance_re_entry_enabled`` and ``unwinnable_race_escalation_enabled``
# as surviving collapse targets owned by proposal 117 — but 117's step 1 had
# already collapsed both to unconditional behaviour and deleted their
# ``CUTOVER_FLAGS`` entries, ``Config`` fields and ``maestro.toml`` keys. The
# row is the surface a human consults before approving a collapse PR, so a
# phantom target there points a reviewer at a flag that no longer exists.
# Nothing tied the flag names quoted in that row to the live registry, so the
# drift survived the collapse that caused it. This guard binds the row's
# backticked flag tokens to :data:`CUTOVER_FLAGS`: every name the row cites as
# a flag must still be a registry entry. It is scoped to the one row (not the
# whole doc) because the doc's surrounding prose legitimately discusses
# already-collapsed flags as history; the **What is missing** row alone claims
# present-tense survival.
_111_PROPOSAL = _PROPOSALS_DIR / "core" / "111-cutover-flag-collapse.md"
_MISSING_ROW = re.compile(r"^\|\s*\*\*What is missing\*\*\s*\|.*$", re.MULTILINE)
# Every backticked lowercase identifier in the row is a candidate flag name.
# Keying on an ``_enabled`` suffix instead would be blind to a cut-over flag that
# carries no such suffix — ``account_ui_v2`` is one in the live registry today —
# so the exact drift this guard exists to catch could recur on that flag and go
# unseen. Widening to any lowercase identifier and subtracting an explicit
# allowlist of the non-flag symbols the row legitimately names keeps the scan
# shape-independent; uppercase constants (``CUTOVER_FLAGS``), file paths and
# commit SHAs are excluded by the identifier shape itself.
_BACKTICKED_IDENTIFIER = re.compile(r"`([a-z][a-z0-9_]*)`")

#: Non-flag lowercase symbols proposal 111's missing-row legitimately cites: the
#: burn-in report function, the ``CutoverFlag`` field names, and the ``stage``
#: vocabulary. A new non-flag symbol in that row must be added here — the tax
#: that buys the scan its ability to see a suffix-less flag name.
_NON_FLAG_ROW_SYMBOLS = frozenset(
    {
        "collapse_eligible",
        "collapsed",
        "cutover",
        "cutover_burn_in_report",
        "gate",
        "note",
        "off_path_symbol",
        "proposal",
        "retain",
        "review_by",
        "stage",
    }
)


def _what_is_missing_row(text: str) -> str:
    """Return proposal 111's ``**What is missing**`` table row, or ``""``."""
    match = _MISSING_ROW.search(text)
    return match.group(0) if match else ""


def _cited_flag_tokens(text: str) -> list[str]:
    """Return backticked identifiers in ``text`` that name a cut-over flag."""
    return [
        name
        for name in _BACKTICKED_IDENTIFIER.findall(text)
        if name not in _NON_FLAG_ROW_SYMBOLS
    ]


def _find_absent_cutover_flags(text: str, live_flags: frozenset[str]) -> list[str]:
    """Return row-cited flag tokens that ``live_flags`` does not contain."""
    row = _what_is_missing_row(text)
    return [name for name in _cited_flag_tokens(row) if name not in live_flags]


def test_no_absent_cutover_flags_in_proposal_111() -> None:
    """Every flag proposal 111's missing-row cites must be in ``CUTOVER_FLAGS``.

    A name the registry no longer carries has already been collapsed, so
    listing it as a surviving target misdirects the human who reads the row
    before approving the next collapse PR.
    """
    # Direct-file guard mirroring ``_016_PROPOSAL.is_file()``: a moved or
    # renamed doc would otherwise make the scan pass against no text at all.
    assert _111_PROPOSAL.is_file(), (
        f"Expected {_111_PROPOSAL} to exist; the file was moved or renamed, "
        "so this guard would not validate anything."
    )
    text = _111_PROPOSAL.read_text(encoding="utf-8")
    assert _what_is_missing_row(text), (
        "Proposal 111 has no **What is missing** table row; the metadata table "
        "was reshaped, so this guard is no longer validating anything and "
        "should be rescoped or removed."
    )
    live = frozenset(entry.flag for entry in CUTOVER_FLAGS)
    absent = _find_absent_cutover_flags(text, live)
    assert not absent, (
        "Proposal 111's **What is missing** row names cut-over flags that "
        f"CUTOVER_FLAGS no longer carries: {absent}. Those flags have already "
        "been collapsed and their registry entries dropped, so the row points "
        "a collapse reviewer at targets that do not exist. Name only live "
        "registry entries, describe the collapsed ones as history, or — if a "
        "name is not a flag at all — add it to _NON_FLAG_ROW_SYMBOLS."
    )


def test_absent_cutover_flag_detector_fires_on_seeded_phantom() -> None:
    """Negative control: the detector must catch a seeded collapsed flag."""
    text = (
        "| **What is missing** | 117 for `base_advance_re_entry_enabled` / "
        "`unwinnable_race_escalation_enabled`. |\n"
    )
    assert _find_absent_cutover_flags(text, frozenset({"purpose_engine_enabled"})) == [
        "base_advance_re_entry_enabled",
        "unwinnable_race_escalation_enabled",
    ]


def test_absent_cutover_flag_detector_sees_flags_without_an_enabled_suffix() -> None:
    """A collapsed flag whose name carries no ``_enabled`` suffix is caught."""
    text = "| **What is missing** | 122 for `account_ui_v2`. |\n"
    assert _find_absent_cutover_flags(text, frozenset({"purpose_engine_enabled"})) == [
        "account_ui_v2"
    ]
    assert _find_absent_cutover_flags(text, frozenset({"account_ui_v2"})) == []


def test_absent_cutover_flag_detector_ignores_live_and_non_flag_tokens() -> None:
    """Live flag names and backticked non-flag symbols are both left alone."""
    text = (
        "| **What is missing** | `cutover_burn_in_report` shows every "
        "`cutover`-stage entry below its `review_by`; 113 owns "
        "`purpose_engine_enabled`. |\n"
    )
    assert _find_absent_cutover_flags(text, frozenset({"purpose_engine_enabled"})) == []


def test_absent_cutover_flag_detector_ignores_rows_outside_the_missing_row() -> None:
    """A collapsed flag named elsewhere in the doc is history, not a target."""
    text = (
        "| **What is missing** | nothing outstanding. |\n"
        "Proposal 117 collapsed `base_advance_re_entry_enabled` in step 1.\n"
    )
    assert _find_absent_cutover_flags(text, frozenset()) == []


def test_missing_row_extractor_returns_empty_without_the_row() -> None:
    """The extractor reports absence rather than raising on a reshaped table."""
    assert _what_is_missing_row("| **What landed** | everything. |\n") == ""


# The same row may also state the burn-in floor inline — the flags that carry the
# earliest ``review_by`` and the date itself. Naming the flags fixed the half of
# #3583 where the row cited a date with no way to check it, but an inline date
# is exactly the restated constant that rots: the next collapse moves the floor
# and leaves both the names and the date pointing at nothing. Binding the claim
# to ``CUTOVER_FLAGS`` makes that drift red the build, so the row's pointer at
# ``cutover_burn_in_report`` stays a true statement of where the oracle lives.
#
# The binding is deliberately conditional on the claim being *present*. Dropping
# the inline restatement and pointing only at ``cutover_burn_in_report`` is the
# document-discipline-preferred wording (code is the spec), so a guard that
# demanded the restatement would red the build on the very cleanup it wants.
# ``flags`` must be able to cross commas: once three or more flags share the
# earliest ``review_by``, the natural prose is a comma list ("`a`, `b`, and
# `c`, whose ..."). A comma-excluding class cannot reach the trailing
# ", whose `review_by` is", so the whole match fails, ``_floor_claim_drift``
# returns None and the guard silently stops checking the very claim it exists
# for. The class excludes ``.`` and ``|`` instead, so the capture still cannot
# run past the end of the sentence or out of the table cell, and the lazy
# quantifier stops at the first floor clause.
_FLOOR_CLAIM = re.compile(
    r"floor is carried by (?P<flags>[^.|]*?), whose `review_by` is "
    r"(?P<date>\d{4}-\d{2}-\d{2})"
)
#: The ``_find_floor_claim`` result meaning "the row states no inline floor".
#: Only ever compared against, never returned — each caller builds its own empty
#: result so no caller can mutate the shared list out from under the next.
_NO_FLOOR_CLAIM: tuple[list[str], str] = ([], "")


def _find_floor_claim(text: str) -> tuple[list[str], str]:
    """Return the missing-row's claimed floor flags and date (``([], "")``)."""
    match = _FLOOR_CLAIM.search(_what_is_missing_row(text))
    if match is None:
        return [], ""
    return sorted(_cited_flag_tokens(match.group("flags"))), match.group("date")


def _registry_floor(entries: Iterable[CutoverFlag]) -> tuple[list[str], str]:
    """Return the ``cutover``-stage flags at the earliest ``review_by``."""
    cutover = [entry for entry in entries if entry.stage == "cutover"]
    if not cutover:
        return [], ""
    earliest = min(entry.review_by for entry in cutover)
    return (
        sorted(entry.flag for entry in cutover if entry.review_by == earliest),
        earliest,
    )


def _floor_claim_drift(
    text: str, entries: Iterable[CutoverFlag]
) -> tuple[list[str], str] | None:
    """Return the registry floor a stated claim contradicts, else ``None``.

    A row that states no inline floor is honest by construction — there is no
    restatement left to rot — so it yields ``None`` rather than a violation.
    """
    claimed = _find_floor_claim(text)
    if claimed == _NO_FLOOR_CLAIM:
        return None
    floor = _registry_floor(entries)
    if claimed == floor:
        return None
    return floor


def test_proposal_111_floor_claim_matches_registry() -> None:
    """Any inline floor the missing-row states must match ``CUTOVER_FLAGS``."""
    assert _111_PROPOSAL.is_file(), (
        f"Expected {_111_PROPOSAL} to exist; the file was moved or renamed, "
        "so this guard would not validate anything."
    )
    text = _111_PROPOSAL.read_text(encoding="utf-8")
    assert _floor_claim_drift(text, CUTOVER_FLAGS) is None, (
        f"Proposal 111's **What is missing** row claims floor "
        f"{_find_floor_claim(text)}, but CUTOVER_FLAGS carries "
        f"{_registry_floor(CUTOVER_FLAGS)}. A collapse moved the earliest "
        "cutover-stage review_by; update the row or drop the inline "
        "restatement and point at cutover_burn_in_report."
    )


def test_floor_claim_detector_reads_flags_and_date() -> None:
    """Positive control: the extractor must read a seeded floor claim."""
    text = (
        "| **What is missing** | The floor is carried by `a_enabled` (089) and "
        "`b_enabled`, whose `review_by` is 2027-02-28; read the report. |\n"
    )
    assert _find_floor_claim(text) == (["a_enabled", "b_enabled"], "2027-02-28")


def test_floor_claim_detector_reads_a_comma_separated_flag_list() -> None:
    """A floor carried by three or more flags is read, not silently skipped.

    Regression for the fail-open gap: the ``flags`` class excluded commas, so
    the comma list that three-plus flags force ("`a`, `b`, and `c`, whose ...")
    broke the match entirely and the guard passed on any claim, stale or not.
    """
    text = (
        "| **What is missing** | The floor is carried by `a_enabled`, "
        "`b_enabled`, and `c_enabled`, whose `review_by` is 2027-02-28. |\n"
    )
    assert _find_floor_claim(text) == (
        ["a_enabled", "b_enabled", "c_enabled"],
        "2027-02-28",
    )


def test_floor_claim_detector_does_not_cross_a_sentence_boundary() -> None:
    """The widened class still cannot scavenge flags from a prior sentence.

    ``.`` and ``|`` remain excluded, so a row whose earlier sentence names a
    flag cannot have that flag folded into a later floor claim.
    """
    text = (
        "| **What is missing** | Collapse is owned by `stale_enabled`. The "
        "floor is carried by `a_enabled`, whose `review_by` is 2027-02-28. |\n"
    )
    assert _find_floor_claim(text) == (["a_enabled"], "2027-02-28")


def test_floor_claim_detector_returns_empty_without_a_claim() -> None:
    """A reworded row with no inline floor claim yields the empty result."""
    text = "| **What is missing** | read `cutover_burn_in_report` for the floor. |\n"
    assert _find_floor_claim(text) == _NO_FLOOR_CLAIM


def test_floor_claim_drift_permits_a_row_that_states_no_floor() -> None:
    """Dropping the inline restatement is a clean row, not a violation."""
    text = "| **What is missing** | read `cutover_burn_in_report` for the floor. |\n"
    assert _floor_claim_drift(text, CUTOVER_FLAGS) is None


def test_floor_claim_drift_reports_the_registry_floor_on_a_stale_claim() -> None:
    """A claim the registry contradicts yields the floor it should have named."""
    entries = (
        CutoverFlag(
            flag="live_enabled",
            proposal=1,
            cutover_cycle="2026-01-01",
            off_path_symbol="_a",
            review_by="2027-02-28",
            stage="cutover",
            note="a",
        ),
    )
    stale = (
        "| **What is missing** | The floor is carried by `collapsed_enabled`, "
        "whose `review_by` is 2027-02-28. |\n"
    )
    assert _floor_claim_drift(stale, entries) == (["live_enabled"], "2027-02-28")
    fresh = (
        "| **What is missing** | The floor is carried by `live_enabled`, "
        "whose `review_by` is 2027-02-28. |\n"
    )
    assert _floor_claim_drift(fresh, entries) is None


def test_registry_floor_selects_earliest_cutover_stage_entries() -> None:
    """Only ``cutover``-stage entries at the earliest ``review_by`` count."""
    entries = (
        CutoverFlag(
            flag="early_a_enabled",
            proposal=1,
            cutover_cycle="2026-01-01",
            off_path_symbol="_a",
            review_by="2027-02-28",
            stage="cutover",
            note="a",
        ),
        CutoverFlag(
            flag="early_b_enabled",
            proposal=None,
            cutover_cycle="2026-01-01",
            off_path_symbol="_b",
            review_by="2027-02-28",
            stage="cutover",
            note="b",
        ),
        CutoverFlag(
            flag="later_enabled",
            proposal=2,
            cutover_cycle="2026-01-01",
            off_path_symbol="_c",
            review_by="2027-09-30",
            stage="cutover",
            note="c",
        ),
        CutoverFlag(
            flag="retained_enabled",
            proposal=3,
            cutover_cycle="2026-01-01",
            off_path_symbol="_d",
            review_by="2026-01-01",
            stage="retain",
            note="d",
        ),
    )
    assert _registry_floor(entries) == (
        ["early_a_enabled", "early_b_enabled"],
        "2027-02-28",
    )
    assert _registry_floor(()) == ([], "")
