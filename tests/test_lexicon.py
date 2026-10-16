"""Unit tests for the coined-term lexicon parser (``maestro/lexicon.py``).

All file content is supplied as in-memory strings via the ``mem_fs`` fixture;
no real file is read or written.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from maestro.lexicon import (
    COLUMNS,
    LexiconEntry,
    all_spellings,
    canonical_terms,
    concept_key,
    extract_coinages,
    extract_prose_coinages,
    extract_sensor_tag_map_table,
    extract_sensor_tags,
    load_lexicon,
    render_sensor_tag_map,
)

_LEXICON_PATH = Path("/docs/lexicon.md")

# A well-formed lexicon: intro prose before the table, one row with empty
# variants and one row with two variants, and trailing prose after the table.
_LEXICON = """\
# Coined-Term Lexicon

Intro prose that is not a table row.

| term | definition | names | anchor | variants | minted |
|---|---|---|---|---|---|
| merge-truth | Git-grounded landing fact. | The settled answer. | `is_pr_settled` |  | proposal 087 |
| stand-down | Honest disengagement verdict. | The stop-relapping call. | `stand_down_verdicts.jsonl` | stood-down, stand down | remediation ledger |

Trailing prose after the table.
"""


def test_columns_are_the_six_documented_fields() -> None:
    assert COLUMNS == ("term", "definition", "names", "anchor", "variants", "minted")


def test_load_lexicon_round_trips_every_row(mem_fs: dict[str, str]) -> None:
    """Each data row parses into a LexiconEntry with cells and variants intact."""
    mem_fs[str(_LEXICON_PATH)] = _LEXICON

    entries = load_lexicon(_LEXICON_PATH)

    assert len(entries) == 2
    assert entries[0] == LexiconEntry(
        term="merge-truth",
        definition="Git-grounded landing fact.",
        names="The settled answer.",
        anchor="`is_pr_settled`",
        variants=(),
        minted="proposal 087",
    )
    assert entries[1].term == "stand-down"
    assert entries[1].variants == ("stood-down", "stand down")
    assert entries[1].minted == "remediation ledger"


def test_load_lexicon_no_table_returns_empty(mem_fs: dict[str, str]) -> None:
    """A file with no six-column header row is 'no lexicon', not an error."""
    mem_fs[str(_LEXICON_PATH)] = "# Just a title\n\nNo table here.\n"

    assert load_lexicon(_LEXICON_PATH) == ()


def test_load_lexicon_skips_unrelated_preceding_table(mem_fs: dict[str, str]) -> None:
    """A different pipe table before the header is ignored, not mis-parsed."""
    mem_fs[str(_LEXICON_PATH)] = (
        "| other | table |\n"
        "|---|---|\n"
        "| a | b |\n"
        "\n"
        "| term | definition | names | anchor | variants | minted |\n"
        "|---|---|---|---|---|---|\n"
        "| red-root | Root-cause preflight. | First diagnosis. | `diagnose_red_root` |  | red-root diagnosis |\n"
    )

    entries = load_lexicon(_LEXICON_PATH)

    assert canonical_terms(entries) == frozenset({"red-root"})


def test_load_lexicon_wrong_column_count_raises(mem_fs: dict[str, str]) -> None:
    """A data row with the wrong column count fails loud — no silent skip."""
    mem_fs[str(_LEXICON_PATH)] = (
        "| term | definition | names | anchor | variants | minted |\n"
        "|---|---|---|---|---|---|\n"
        "| only-three | too | few |\n"
    )

    with pytest.raises(ValueError, match="expected 6"):
        load_lexicon(_LEXICON_PATH)


def test_canonical_terms_returns_each_term(mem_fs: dict[str, str]) -> None:
    mem_fs[str(_LEXICON_PATH)] = _LEXICON

    entries = load_lexicon(_LEXICON_PATH)

    assert canonical_terms(entries) == frozenset({"merge-truth", "stand-down"})


def test_all_spellings_unions_terms_and_variants(mem_fs: dict[str, str]) -> None:
    mem_fs[str(_LEXICON_PATH)] = _LEXICON

    entries = load_lexicon(_LEXICON_PATH)

    assert all_spellings(entries) == frozenset(
        {"merge-truth", "stand-down", "stood-down", "stand down"}
    )


def test_extract_coinages_matches_hyphenated_compounds_in_order() -> None:
    """Every hyphenated compound is pulled, first-appearance order preserved."""
    text = "The merge-truth chokepoint feeds the owned-act-drought redirect."

    assert extract_coinages(text) == ["merge-truth", "owned-act-drought"]


def test_extract_coinages_preserves_repetition_for_frequency_counting() -> None:
    """A repeated coinage appears once per occurrence so callers can count it."""
    text = "merge-truth again, and merge-truth once more, plus merge-truth."

    assert extract_coinages(text) == ["merge-truth", "merge-truth", "merge-truth"]


def test_extract_coinages_lowercases_before_matching() -> None:
    """Mixed-case input is folded to lowercase, so SETTLED-RELAP → settled-relap."""
    assert extract_coinages("A SETTLED-RELAP lap") == ["settled-relap"]


def test_extract_coinages_drops_common_english_hyphenations() -> None:
    """Ordinary hyphenations in the stoplist are not surfaced as coinages."""
    text = "A read-only, opt-in, zero-cost merge-truth check."

    assert extract_coinages(text) == ["merge-truth"]


def test_extract_coinages_ignores_plain_words_and_digit_prefixes() -> None:
    """A word with no hyphen, and a digit-prefixed compound, are not coinages."""
    assert extract_coinages("plain words and 5-push only") == []


def test_extract_coinages_non_string_returns_empty() -> None:
    """A non-string input is guarded to an empty list, never an error."""
    assert extract_coinages(None) == []  # type: ignore[arg-type]


def test_extract_coinages_empty_string_returns_empty() -> None:
    assert extract_coinages("") == []


def test_extract_prose_coinages_excludes_fenced_code_blocks() -> None:
    """Coinages inside a ``` ``` ```-delimited fence are not prose coinage."""
    text = (
        "A merge-truth read in prose.\n"
        "```\n"
        "run gate-check and pr-lap-ledger inside a fence\n"
        "```\n"
        "Another owned-act line in prose.\n"
    )

    assert extract_prose_coinages(text) == ["merge-truth", "owned-act"]


def test_extract_prose_coinages_excludes_inline_code_spans() -> None:
    """A coinage inside a `...` inline code span is stripped, prose survives."""
    text = "The `gate-check` flag guards the merge-truth chokepoint."

    assert extract_prose_coinages(text) == ["merge-truth"]


def test_extract_prose_coinages_unbalanced_backtick_stays_within_its_line() -> None:
    """A stray backtick swallows at most its own line, never later prose coinages.

    Regression for the joined-document strip: applying the inline-code regex to
    the whole prose at once let an unbalanced backtick pair with a backtick many
    lines away, replacing every coinage between them (a silent false-negative the
    coverage guard's vacuous-pass check can't see). Stripping per line bounds the
    damage to the stray backtick's own line, so ``alpha-coinage`` on the next line
    still surfaces.
    """
    text = (
        "First a stray ` opens an unbalanced span.\n"
        "The alpha-coinage lives on its own prose line.\n"
        "Later a ` closes far away with beta-coinage after it.\n"
    )

    assert extract_prose_coinages(text) == ["alpha-coinage", "beta-coinage"]


def test_extract_prose_coinages_excludes_markdown_links() -> None:
    """Both the target and the text of a ``[text](target)`` link are stripped.

    CLAUDE.md self-links its deep-dives as
    ``[docs/loop-lifecycle.md](docs/loop-lifecycle.md)``; neither the target slug
    nor the identical text slug is prose coinage, so both must be excluded.
    """
    text = "See [docs/loop-lifecycle.md](docs/loop-lifecycle.md) for the merge-truth path."

    assert extract_prose_coinages(text) == ["merge-truth"]


def test_extract_prose_coinages_keeps_prose_outside_constructs() -> None:
    """Coinages in ordinary sentences (and bold headings) survive stripping."""
    text = (
        "**Self-evolution** — the owned-act-drought redirect and the "
        "reality-refutation escalation both fire in prose.\n"
    )

    assert extract_prose_coinages(text) == [
        "self-evolution",
        "owned-act-drought",
        "reality-refutation",
    ]


def test_extract_prose_coinages_still_applies_stoplist() -> None:
    """Delegation to extract_coinages means the common-English stoplist applies."""
    text = "A read-only, opt-in check around the merge-truth read."

    assert extract_prose_coinages(text) == ["merge-truth"]


def test_extract_prose_coinages_empty_and_whitespace_return_empty() -> None:
    """Empty, whitespace-only, and non-string inputs all yield []."""
    assert extract_prose_coinages("") == []
    assert extract_prose_coinages("   \n\t  \n") == []
    assert extract_prose_coinages(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# concept_key — hyphenation + light-morphology fold for restatement matching
# ---------------------------------------------------------------------------


def test_concept_key_collapses_self_extinguish_family() -> None:
    """The three self-extinguish spellings fold to one key."""
    base = concept_key("self-extinguish")
    assert concept_key("self-extinguishing") == base
    assert concept_key("self-extinguishes") == base
    # And the fold actually stripped the hyphen and the inflection.
    assert base == "selfextinguish"


def test_concept_key_collapses_stand_down_plural() -> None:
    """stand-down and its plural fold to one key."""
    assert concept_key("stand-downs") == concept_key("stand-down") == "standdown"


def test_concept_key_strips_hyphens() -> None:
    """Every hyphen is removed before matching, regardless of morphology."""
    assert concept_key("merge-truth") == "mergetruth"
    assert concept_key("owned-act-drought") == "ownedactdrought"


def test_concept_key_minimum_stem_guard_leaves_short_words() -> None:
    """A short word is not over-stripped below the minimum-stem length."""
    # Stripping the trailing ``s`` would leave a 2-char stem (< 4), so it is kept.
    assert concept_key("gas") == "gas"
    assert concept_key("bus") == "bus"


def test_concept_key_is_idempotent() -> None:
    """concept_key(concept_key(x)) == concept_key(x) for varied inputs."""
    for term in (
        "self-extinguishing",
        "stand-downs",
        "merge-truth",
        "processes",
        "gas",
        "restatement",
    ):
        once = concept_key(term)
        assert concept_key(once) == once


def test_concept_key_empty_and_non_string_return_empty() -> None:
    """Empty and non-string inputs yield the empty key."""
    assert concept_key("") == ""
    assert concept_key(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# extract_sensor_tags — behavioural-sensor tag-map reader
# ---------------------------------------------------------------------------

# A well-formed tag map: intro prose, a no-backtick placeholder row, a plain
# tag, a space-containing tag, a slash-composite of three, and trailing prose.
_TAG_MAP = """\
## Behavioral sensor tag map

Intro prose that is not a table row.

| Tag | Sensor |
|---|---|
| (task shape / origin) | `classify_task_shape`, `classify_task_origin` |
| `SETTLED-RELAP` | `classify_settled_relap`, `measure_relap_rate` |
| `PENDING-PLAN BACKLOG` | `format_pending_plan_backlog` |
| `PR-READY` / `PR-STUCK` / `PR-ROTATING` | `format_pr_churn_summary` |

Trailing prose after the table.
"""


def test_extract_sensor_tags_reads_every_tag_in_document_order() -> None:
    """Every backtick tag token is pulled, first-appearance order preserved.

    The no-backtick ``(task shape / origin)`` placeholder contributes nothing,
    the slash-composite row splits into one token per span, the space-containing
    tag is preserved verbatim, and the intro/trailing prose is ignored.
    """
    assert extract_sensor_tags(_TAG_MAP) == [
        "SETTLED-RELAP",
        "PENDING-PLAN BACKLOG",
        "PR-READY",
        "PR-STUCK",
        "PR-ROTATING",
    ]


def test_extract_sensor_tags_splits_slash_composite_row() -> None:
    """A single row with several backtick spans yields one token per span."""
    text = (
        "| Tag | Sensor |\n"
        "|---|---|\n"
        "| `PR-READY` / `PR-STUCK` / `PR-ROTATING` | `format_pr_churn_summary` |\n"
    )

    assert extract_sensor_tags(text) == ["PR-READY", "PR-STUCK", "PR-ROTATING"]


def test_extract_sensor_tags_skips_no_backtick_placeholder_row() -> None:
    """A Tag cell with no backtick span (the placeholder row) is skipped."""
    text = (
        "| Tag | Sensor |\n"
        "|---|---|\n"
        "| (task shape / origin) | `classify_task_shape` |\n"
        "| `SETTLED-RELAP` | `measure_relap_rate` |\n"
    )

    assert extract_sensor_tags(text) == ["SETTLED-RELAP"]


def test_extract_sensor_tags_preserves_space_containing_tag() -> None:
    """A space-containing tag is preserved verbatim as one token."""
    text = (
        "| Tag | Sensor |\n"
        "|---|---|\n"
        "| `PROMOTABLE BACKLOG` | `format_promotable_summary` |\n"
    )

    assert extract_sensor_tags(text) == ["PROMOTABLE BACKLOG"]


def test_extract_sensor_tags_ignores_unrelated_preceding_table() -> None:
    """A different pipe table before the Tag/Sensor header is not mis-read."""
    text = (
        "| term | definition |\n"
        "|---|---|\n"
        "| `merge-truth` | landing fact |\n"
        "\n"
        "| Tag | Sensor |\n"
        "|---|---|\n"
        "| `SETTLED-RELAP` | `measure_relap_rate` |\n"
    )

    assert extract_sensor_tags(text) == ["SETTLED-RELAP"]


def test_extract_sensor_tags_no_table_returns_empty() -> None:
    """A string with no ``| Tag | Sensor |`` header returns []."""
    text = "# Some doc\n\nProse with a `code-span` but no tag table.\n"

    assert extract_sensor_tags(text) == []


def test_extract_sensor_tags_non_string_and_empty_return_empty() -> None:
    """Non-string and empty inputs are guarded to [], never an error."""
    assert extract_sensor_tags("") == []
    assert extract_sensor_tags(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# render_sensor_tag_map — lexicon → | Tag | Sensor | table renderer
# ---------------------------------------------------------------------------

# The provenance value marking a lexicon entry as a tag-map row (mirrors
# maestro.lexicon._SENSOR_TAG_MINTED; pinned as a literal so a change to that
# constant surfaces here rather than silently reshaping the render).
_SENSOR_MINTED = "behavioral-sensor tag map"

# The header/separator/placeholder prefix render_sensor_tag_map always emits.
_RENDER_PREFIX = (
    "| Tag | Sensor |\n"
    "|---|---|\n"
    "| (task shape / origin) | `classify_task_shape`, "
    "`classify_task_origin`, `measure_success_by_origin` |"
)


def _sensor(term: str, anchor: str, variant: str) -> LexiconEntry:
    """Build a sensor-tag LexiconEntry (minted = the tag-map provenance)."""
    return LexiconEntry(
        term=term,
        definition="",
        names="",
        anchor=anchor,
        variants=(variant,) if variant else (),
        minted=_SENSOR_MINTED,
    )


def test_render_sensor_tag_map_filters_out_non_sensor_entries() -> None:
    """Only entries minted as tag-map rows render; other lexicon rows are skipped."""
    entries = (
        LexiconEntry("merge-truth", "", "", "`is_pr_settled`", (), "proposal 087"),
        _sensor("settled-relap", "`measure_relap_rate`", "SETTLED-RELAP"),
    )
    rendered = render_sensor_tag_map(entries)

    assert rendered == (
        _RENDER_PREFIX + "\n| `SETTLED-RELAP` | `measure_relap_rate` |"
    )
    # The non-sensor term never reaches the render.
    assert "merge-truth" not in rendered
    assert "is_pr_settled" not in rendered


def test_render_sensor_tag_map_groups_consecutive_same_anchor_into_one_row() -> None:
    """A run of consecutive same-anchor entries coalesces to one slash row."""
    anchor = "`format_pr_churn_summary` / `measure_greenness_stall`"
    entries = (
        _sensor("pr-ready", anchor, "PR-READY"),
        _sensor("pr-stuck", anchor, "PR-STUCK"),
        _sensor("pr-rotating", anchor, "PR-ROTATING"),
    )
    rendered = render_sensor_tag_map(entries)

    assert rendered == (
        _RENDER_PREFIX
        + "\n| `PR-READY` / `PR-STUCK` / `PR-ROTATING` "
        "| `format_pr_churn_summary`, `measure_greenness_stall` |"
    )


def test_render_sensor_tag_map_renders_sensor_cell_in_comma_style() -> None:
    """A slash-separated anchor renders as comma-joined backtick spans."""
    entries = (
        _sensor(
            "owned-net-delta",
            "`record_owned_backlog_snapshot` / `measure_owned_net_delta`",
            "OWNED-NET-DELTA",
        ),
    )
    rendered = render_sensor_tag_map(entries)

    assert rendered.endswith(
        "| `OWNED-NET-DELTA` "
        "| `record_owned_backlog_snapshot`, `measure_owned_net_delta` |"
    )


def test_render_sensor_tag_map_preserves_space_containing_tag() -> None:
    """A space-containing variant is emitted verbatim as the Tag cell."""
    entries = (
        _sensor(
            "promotable backlog", "`format_promotable_summary`", "PROMOTABLE BACKLOG",
        ),
    )
    rendered = render_sensor_tag_map(entries)

    assert rendered.endswith("| `PROMOTABLE BACKLOG` | `format_promotable_summary` |")


def test_render_sensor_tag_map_falls_back_to_upper_term_without_variant() -> None:
    """An entry carrying no variant uses its upper-cased term as the tag."""
    entries = (
        LexiconEntry(
            "settled-relap", "", "", "`measure_relap_rate`", (), _SENSOR_MINTED,
        ),
    )
    rendered = render_sensor_tag_map(entries)

    assert rendered.endswith("| `SETTLED-RELAP` | `measure_relap_rate` |")


def test_render_sensor_tag_map_emits_header_separator_placeholder_prefix() -> None:
    """With no sensor entries the header/separator/placeholder prefix still renders."""
    assert render_sensor_tag_map(()) == _RENDER_PREFIX


def test_render_sensor_tag_map_distinct_anchors_do_not_coalesce() -> None:
    """Adjacent entries with different anchors stay separate rows."""
    entries = (
        _sensor(
            "live-plan-collision",
            "`measure_live_plan_collisions`",
            "LIVE-PLAN-COLLISION",
        ),
        _sensor("pr-readmission", "`measure_pr_readmission_rate`", "PR-READMISSION"),
    )
    rendered = render_sensor_tag_map(entries)

    lines = rendered.splitlines()
    # Prefix (3 lines) + one row per distinct anchor.
    assert lines[-2] == "| `LIVE-PLAN-COLLISION` | `measure_live_plan_collisions` |"
    assert lines[-1] == "| `PR-READMISSION` | `measure_pr_readmission_rate` |"


def test_render_sensor_tag_map_negative_control_diverges_on_mutated_anchor() -> None:
    """A mutated anchor must change the render away from a fixed expected block.

    Without this, a renderer regression (e.g. ignoring the anchor) could let the
    clean-tree golden equality in ``test_lexicon_coverage`` pass vacuously.
    """
    expected = _RENDER_PREFIX + "\n| `SETTLED-RELAP` | `measure_relap_rate` |"
    good = (_sensor("settled-relap", "`measure_relap_rate`", "SETTLED-RELAP"),)
    mutated = (_sensor("settled-relap", "`measure_something_else`", "SETTLED-RELAP"),)

    assert render_sensor_tag_map(good) == expected
    assert render_sensor_tag_map(mutated) != expected


# ---------------------------------------------------------------------------
# extract_sensor_tag_map_table — committed | Tag | Sensor | block reader
# ---------------------------------------------------------------------------

_TAG_MAP_DOC = """\
## Behavioral sensor tag map

Intro prose that is not a table row.

| Tag | Sensor |
|---|---|
| (task shape / origin) | `classify_task_shape` |
| `SETTLED-RELAP` | `measure_relap_rate` |
| `PROMOTABLE BACKLOG` | `format_promotable_summary` |

Trailing prose after the table.
"""


def test_extract_sensor_tag_map_table_slices_the_block() -> None:
    """The header through the last row is returned verbatim, prose excluded."""
    assert extract_sensor_tag_map_table(_TAG_MAP_DOC) == (
        "| Tag | Sensor |\n"
        "|---|---|\n"
        "| (task shape / origin) | `classify_task_shape` |\n"
        "| `SETTLED-RELAP` | `measure_relap_rate` |\n"
        "| `PROMOTABLE BACKLOG` | `format_promotable_summary` |"
    )


def test_extract_sensor_tag_map_table_stops_at_first_non_row_line() -> None:
    """Reading stops at the first non-table line after the rows."""
    text = (
        "| Tag | Sensor |\n"
        "|---|---|\n"
        "| `SETTLED-RELAP` | `measure_relap_rate` |\n"
        "Trailing prose immediately after.\n"
        "| `LATER-TAG` | `measure_later` |\n"
    )

    assert extract_sensor_tag_map_table(text) == (
        "| Tag | Sensor |\n"
        "|---|---|\n"
        "| `SETTLED-RELAP` | `measure_relap_rate` |"
    )


def test_extract_sensor_tag_map_table_ignores_unrelated_preceding_table() -> None:
    """A different pipe table before the Tag/Sensor header is not returned."""
    text = (
        "| term | definition |\n"
        "|---|---|\n"
        "| `merge-truth` | landing fact |\n"
        "\n"
        "| Tag | Sensor |\n"
        "|---|---|\n"
        "| `SETTLED-RELAP` | `measure_relap_rate` |\n"
    )

    assert extract_sensor_tag_map_table(text) == (
        "| Tag | Sensor |\n"
        "|---|---|\n"
        "| `SETTLED-RELAP` | `measure_relap_rate` |"
    )


def test_extract_sensor_tag_map_table_no_table_returns_empty() -> None:
    """A string with no ``| Tag | Sensor |`` header returns ''."""
    assert extract_sensor_tag_map_table("# Doc\n\nNo table here.\n") == ""


def test_extract_sensor_tag_map_table_non_string_and_empty_return_empty() -> None:
    """Non-string and empty inputs are guarded to '', never an error."""
    assert extract_sensor_tag_map_table("") == ""
    assert extract_sensor_tag_map_table(None) == ""  # type: ignore[arg-type]
