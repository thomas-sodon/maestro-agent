"""Regression: CLAUDE.md uses only the lexicon's canonical spellings.

CLAUDE.md is loaded into every Claude conversation, and Maestro coins hyphenated
compounds (``merge-truth``, ``self-extinguishing``, ``stand-down``) to fuse a
recurring behaviour into a single handle. When the same concept drifts across
spellings in that one document, the handle stops being a stable pointer. The
``maestro lexicon --lint`` CLI already reports such drift, but it is an on-demand
read — nothing runs it, so a stale form can silently reappear.

This golden test turns that lint into a committed, zero-LLM-cost guard — the same
deterministic-guard family as ``tests/test_no_line_citations_in_docs.py``. It scans the *real* repo-root
CLAUDE.md against the *real* ``docs/lexicon.md`` through the one shared
:func:`~maestro.lexicon.find_canonical_drift` helper (so the test and the CLI can
never disagree about what counts as drift), and its reach grows automatically as
the lexicon defines more terms.

The real-file scan reads two small committed files, mirroring the sibling golden
tests; the negative controls hand in-memory strings straight to the pure helper,
so they touch no filesystem.
"""

from __future__ import annotations

import ast
import difflib
import re
from pathlib import Path

import pytest

from maestro.lexicon import (
    LexiconEntry,
    all_spellings,
    extract_prose_coinages,
    extract_sensor_tag_map_table,
    extract_sensor_tags,
    find_canonical_drift,
    load_lexicon,
    render_sensor_tag_map,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CLAUDE_MD = _REPO_ROOT / "CLAUDE.md"
_LEXICON = _REPO_ROOT / "docs" / "lexicon.md"
_SELF_EVOLUTION = _REPO_ROOT / "docs" / "self-evolution.md"
_MAESTRO_PKG = _REPO_ROOT / "maestro"

#: Prompt-assembly roots the measured-data sensors must be reachable from.
_PROMPT_ROOTS = ("contemplate", "format_known_state_summary", "generate_task")


def test_claude_md_uses_only_canonical_spellings() -> None:
    """CLAUDE.md must use each lexicon term's canonical spelling, never a variant.

    The vacuous-pass guard (``entries`` non-empty) mirrors the missing-directory
    guards in the sibling golden tests: an empty lexicon would make the drift scan
    pass without validating anything.
    """
    entries = load_lexicon(_LEXICON)
    assert entries, (
        f"Expected at least one lexicon entry from {_LEXICON}; an empty lexicon "
        "would make the drift scan below pass vacuously."
    )
    drift = find_canonical_drift(_CLAUDE_MD.read_text(encoding="utf-8"), entries)
    assert drift == [], (
        "CLAUDE.md uses non-canonical spellings the lexicon canonicalises: "
        + "; ".join(f"{variant} → {canonical} ({count})" for variant, canonical, count in drift)
        + ". Replace each with the canonical spelling from docs/lexicon.md, or — "
        "if the form is a distinct marker/verdict rather than prose drift — move it "
        "out of that term's variants cell."
    )


def test_drift_detector_fires_on_seeded_variant() -> None:
    """Negative control: the detector must catch a seeded variant spelling.

    Without this, a detector regression (e.g. a broken regex matching nothing)
    would make the clean-tree assertion above pass vacuously.
    """
    entries = (
        LexiconEntry(
            term="self-extinguishing",
            definition="",
            names="",
            anchor="",
            variants=("self-extinguish",),
            minted="",
        ),
    )
    drift = find_canonical_drift("The rule is self-extinguish on merge.", entries)
    assert drift == [("self-extinguish", "self-extinguishing", 1)]


def test_canonical_and_hyphen_adjacent_do_not_register() -> None:
    """The canonical spelling and a hyphen-adjacent compound are not drift.

    ``self-extinguish`` is a prefix of the canonical ``self-extinguishing`` (the
    word boundary must keep the canonical from registering), and a variant sitting
    hyphen-adjacent inside a longer coinage (``self-extinguish-foo`` /
    ``foo-self-extinguish``) must be suppressed by the hyphen lookarounds.
    """
    entries = (
        LexiconEntry(
            term="self-extinguishing",
            definition="",
            names="",
            anchor="",
            variants=("self-extinguish",),
            minted="",
        ),
    )
    assert find_canonical_drift("It is self-extinguishing on merge.", entries) == []
    assert find_canonical_drift(
        "the self-extinguish-foo rule and the foo-self-extinguish rule", entries
    ) == []


def test_self_matching_variant_is_skipped() -> None:
    """A variant equal case-insensitively to its own term never self-matches.

    A term that lists a tag form of itself (``settled-relap`` with variant
    ``SETTLED-RELAP``) must not report its own canonical use as drift.
    """
    entries = (
        LexiconEntry(
            term="settled-relap",
            definition="",
            names="",
            anchor="",
            variants=("SETTLED-RELAP",),
            minted="",
        ),
    )
    assert find_canonical_drift("the settled-relap sensor SETTLED-RELAP", entries) == []


def test_claude_md_defines_all_coinages() -> None:
    """Every coined compound in CLAUDE.md's prose must be defined or stoplisted.

    Extracts the prose-only hyphenated coinages from the *real* repo-root CLAUDE.md
    (fenced code blocks, inline code spans, and Markdown links stripped by
    :func:`~maestro.lexicon.extract_prose_coinages`) and subtracts every spelling
    ``docs/lexicon.md`` defines, compared lowercased. The remainder must be empty:
    a coinage that is neither a lexicon term nor an ordinary hyphenation on
    ``maestro.lexicon._COMMON_ENGLISH_STOPLIST`` is an undefined term in the one
    document loaded into every conversation, with nowhere for a reader (or the
    agent re-reading its own reasoning) to look it up. Same deterministic,
    zero-LLM-cost guard family as the canonical-spelling test above; its reach
    grows automatically as the lexicon defines more terms, and it self-extinguishes
    per term the instant that term is defined (or stoplisted).

    The vacuous-pass guards (non-empty extraction and non-empty lexicon) mirror the
    sibling golden tests: an empty CLAUDE.md or an empty lexicon would make the
    subtraction pass without validating anything.
    """
    coinages = extract_prose_coinages(_CLAUDE_MD.read_text(encoding="utf-8"))
    assert coinages, (
        f"Expected at least one prose coinage in {_CLAUDE_MD}; an empty extraction "
        "would make the coverage subtraction below pass vacuously."
    )
    entries = load_lexicon(_LEXICON)
    assert entries, (
        f"Expected at least one lexicon entry from {_LEXICON}; an empty lexicon "
        "would make the coverage subtraction below pass vacuously."
    )
    defined = {spelling.lower() for spelling in all_spellings(entries)}
    undefined = sorted({coinage for coinage in coinages if coinage not in defined})
    assert undefined == [], (
        "CLAUDE.md uses coined hyphenated compounds that are neither defined in "
        "docs/lexicon.md nor on maestro.lexicon._COMMON_ENGLISH_STOPLIST: "
        + ", ".join(undefined)
        + ". Define each load-bearing one with a docs/lexicon.md row, or — if it is "
        "an ordinary hyphenation, not a minted concept — add it to the stoplist."
    )


def test_coverage_detector_flags_seeded_undefined_coinage() -> None:
    """Negative control: an undefined coinage seeded into prose is flagged.

    Without this, a detector regression (``extract_prose_coinages`` returning
    nothing, or the stoplist swallowing everything) would make the clean-tree
    assertion above pass vacuously. Hands an in-memory string to the pure helper,
    so it touches no filesystem.
    """
    seeded = "The zzz-made-up-term guard runs before the merge-truth read.\n"
    coinages = extract_prose_coinages(seeded)
    defined = {"merge-truth"}
    undefined = {coinage for coinage in coinages if coinage not in defined}
    assert "zzz-made-up-term" in undefined
    assert "merge-truth" not in undefined


def test_self_evolution_tag_map_defines_all_tags() -> None:
    """Every behavioral-sensor tag in the tag map must have a lexicon entry.

    Extracts the uppercase tag tokens from the *real* ``docs/self-evolution.md``
    behavioral-sensor tag map (via
    :func:`~maestro.lexicon.extract_sensor_tags`) and subtracts every spelling
    ``docs/lexicon.md`` defines, compared lowercased. The remainder must be
    empty: a tag the sidecar surfaces to the LLM but that the lexicon does not
    define is a handle with nowhere for a reader (or the agent re-reading its own
    reasoning) to look it up, and lets the sidecar silently run ahead of the
    definitional source. Same deterministic, zero-LLM-cost guard family as the
    canonical-spelling and prose-coverage tests above; its reach grows
    automatically as the tag map or the lexicon changes.

    The vacuous-pass guards (non-empty tag extraction and non-empty lexicon)
    mirror the sibling golden tests: an empty tag map or an empty lexicon would
    make the subset check pass without validating anything.
    """
    tags = extract_sensor_tags(_SELF_EVOLUTION.read_text(encoding="utf-8"))
    assert tags, (
        f"Expected at least one sensor tag from {_SELF_EVOLUTION}; an empty "
        "extraction would make the coverage subset below pass vacuously."
    )
    entries = load_lexicon(_LEXICON)
    assert entries, (
        f"Expected at least one lexicon entry from {_LEXICON}; an empty lexicon "
        "would make the coverage subset below pass vacuously."
    )
    defined = {spelling.lower() for spelling in all_spellings(entries)}
    uncovered = sorted({tag for tag in tags if tag.lower() not in defined})
    assert uncovered == [], (
        "docs/self-evolution.md's behavioral sensor tag map lists tags with no "
        "matching docs/lexicon.md entry: "
        + ", ".join(uncovered)
        + ". Define each with a docs/lexicon.md row (term = the lowercased tag, "
        "its uppercase form as the sole variant), so the tag map cannot run ahead "
        "of the definitional source."
    )


def test_base_advance_races_row_is_defined() -> None:
    """The proposal 099 base-advance-races sensor carries a lexicon row.

    A focused guard beside the coverage and render golden tests: it pins the
    ``base-advance-races`` entry's anchor, tag variant, and behavioral-sensor
    provenance so the sensor tag can never surface to the LLM without its
    definitional row, and so :func:`~maestro.lexicon.render_sensor_tag_map`
    places it as a sensor-tag row rather than dropping it.
    """
    entries = load_lexicon(_LEXICON)
    by_term = {entry.term: entry for entry in entries}
    assert "base-advance-races" in by_term
    row = by_term["base-advance-races"]
    assert row.anchor == "`measure_base_advance_races`"
    assert row.variants == ("BASE-ADVANCE-RACES",)
    assert row.minted == "behavioral-sensor tag map"


def test_tag_coverage_detector_flags_uncovered_tag() -> None:
    """Negative control: a tag map tag with no lexicon entry is flagged.

    Without this, a detector regression (``extract_sensor_tags`` returning
    nothing, or the subset comparison never subtracting) would make the
    clean-tree assertion above pass vacuously. Hands an in-memory tag-map string
    and a hand-built lexicon tuple straight to the pure comparison, so it touches
    no filesystem.
    """
    tag_map = (
        "## Behavioral sensor tag map\n"
        "\n"
        "| Tag | Sensor |\n"
        "|---|---|\n"
        "| `SETTLED-RELAP` | `measure_relap_rate` |\n"
        "| `NEW-UNCOVERED-TAG` | `measure_something` |\n"
    )
    entries = (
        LexiconEntry(
            term="settled-relap",
            definition="",
            names="",
            anchor="",
            variants=("SETTLED-RELAP",),
            minted="",
        ),
    )
    tags = extract_sensor_tags(tag_map)
    defined = {spelling.lower() for spelling in all_spellings(entries)}
    uncovered = {tag for tag in tags if tag.lower() not in defined}
    assert "NEW-UNCOVERED-TAG" in uncovered
    assert "SETTLED-RELAP" not in uncovered


def test_self_evolution_tag_map_matches_lexicon_render() -> None:
    """The committed tag map must equal the render of the lexicon's sensor rows.

    Stronger than the subset test above: it asserts ``docs/self-evolution.md``'s
    behavioral-sensor tag map is byte-identical to what
    :func:`~maestro.lexicon.render_sensor_tag_map` emits from
    ``docs/lexicon.md``'s sensor-tag entries, so the sidecar becomes a *generated
    view* of the lexicon — editing a sensor definition means editing the lexicon,
    and the table regenerates from it (via ``maestro lexicon --tag-map``).
    Equality subsumes the subset check above, but that subset test is kept for its
    sharper "undefined tag" failure message.

    The vacuous-pass guards (non-empty render and non-empty extracted block)
    mirror the sibling golden tests: an empty render or an empty committed table
    would make the equality below pass without validating anything. On drift a
    unified line-by-line diff names the offending row.
    """
    rendered = render_sensor_tag_map(load_lexicon(_LEXICON))
    assert rendered, (
        f"Expected a non-empty render from {_LEXICON}; an empty render would make "
        "the equality below pass vacuously."
    )
    committed = extract_sensor_tag_map_table(
        _SELF_EVOLUTION.read_text(encoding="utf-8")
    )
    assert committed, (
        f"Expected a non-empty tag-map table in {_SELF_EVOLUTION}; an empty "
        "committed table would make the equality below pass vacuously."
    )
    assert rendered == committed, (
        "docs/self-evolution.md's behavioral sensor tag map has drifted from the "
        "render of docs/lexicon.md's sensor-tag entries. Regenerate it with "
        "`maestro lexicon --tag-map` (or edit the sensor's docs/lexicon.md row):\n"
        + "\n".join(
            difflib.unified_diff(
                rendered.splitlines(),
                committed.splitlines(),
                fromfile="render (docs/lexicon.md)",
                tofile="committed (docs/self-evolution.md)",
                lineterm="",
            )
        )
    )


def _name_call_graph(sources: list[str]) -> dict[str, set[str]]:
    """Map each function name to every bare name / attribute its body references.

    Name-based and deliberately loose (a reference counts, not only a call), so
    a sensor handed around as a callback still reads as reached. Same-named
    functions in different modules merge into one node — an over-approximation
    that can only make the guard more permissive, never flag a live sensor.
    """
    graph: dict[str, set[str]] = {}
    for source in sources:
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                refs = graph.setdefault(node.name, set())
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Name):
                        refs.add(sub.id)
                    elif isinstance(sub, ast.Attribute):
                        refs.add(sub.attr)
    return graph


def _reachable(graph: dict[str, set[str]], roots: tuple[str, ...]) -> set[str]:
    """Every name transitively referenced from *roots* through *graph*."""
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        name = stack.pop()
        if name not in seen:
            seen.add(name)
            stack.extend(graph.get(name, ()))
    return seen


def _orphaned_sensor_terms(
    entries: tuple[LexiconEntry, ...], reached: set[str],
) -> list[str]:
    """Tag-map terms none of whose anchored sensor symbols is *reached*.

    Selects the same rows :func:`render_sensor_tag_map` renders (the
    ``behavioral-sensor tag map`` provenance) and reads the anchor's backtick
    spans — the row's Sensor cell; a dotted ``module.symbol`` span keys on its
    last component. A row is live when *any* anchored symbol is reached — an
    anchor may also list a helper that only an actuator calls.
    """
    orphaned: list[str] = []
    for entry in entries:
        if entry.minted != "behavioral-sensor tag map":
            continue
        symbols = [span.rsplit(".", 1)[-1] for span in re.findall(r"`([^`]+)`", entry.anchor)]
        if symbols and not any(symbol in reached for symbol in symbols):
            orphaned.append(entry.term)
    return orphaned


@pytest.mark.timeout(15)
def test_tag_map_sensors_reachable_from_prompt_assembly() -> None:
    """Every tag-map sensor must be reachable from prompt assembly (#4316).

    The tag map presents each row as a live sensor feeding ``contemplate()``'s
    measured-data block, so a sensor with only a CLI surface reads as "no
    signal" to the loop. ``maestro/cli.py`` is excluded from the call graph on
    purpose: a CLI-only caller must not count as reachable. The vacuous-pass
    guard mirrors the sibling golden tests.
    """
    entries = load_lexicon(_LEXICON)
    assert render_sensor_tag_map(entries).count("\n") > 3
    sources = [
        path.read_text(encoding="utf-8")
        for path in sorted(_MAESTRO_PKG.rglob("*.py"))
        if path != _MAESTRO_PKG / "cli.py"
    ]
    reached = _reachable(_name_call_graph(sources), _PROMPT_ROOTS)
    orphaned = _orphaned_sensor_terms(entries, reached)
    assert orphaned == [], (
        "Tag-map sensors unreachable from prompt assembly "
        f"({', '.join(_PROMPT_ROOTS)}), excluding maestro/cli.py: "
        + ", ".join(orphaned)
        + ". Wire each into contemplate()'s measured-data block, or drop its "
        "tag-map row."
    )


def test_reachability_guard_flags_cli_only_sensor() -> None:
    """Negative control: a formatter with only a CLI caller is flagged.

    Hands in-memory sources to the pure graph helpers, so a regression (an
    empty graph, a walk that reaches everything) cannot make the real-tree
    assertion above pass vacuously. Touches no filesystem.
    """
    prompt_module = (
        "def contemplate():\n"
        "    return format_live()\n"
        "def format_live():\n"
        "    return measure_live()\n"
    )
    entries = tuple(
        LexiconEntry(
            term=term,
            definition="",
            names="",
            anchor=f"`{symbol}`",
            variants=(term.upper(),),
            minted="behavioral-sensor tag map",
        )
        for term, symbol in (("live", "measure_live"), ("cli-only", "measure_cli_only"))
    )
    reached = _reachable(_name_call_graph([prompt_module]), _PROMPT_ROOTS)
    assert _orphaned_sensor_terms(entries, reached) == ["cli-only"]
