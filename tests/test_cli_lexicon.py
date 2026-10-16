"""The ``lexicon`` CLI subcommand; split out of ``tests/test_cli.py``.
"""

from __future__ import annotations

import argparse
import json
from unittest.mock import patch

from maestro.cli import (
    cmd_lexicon,
)
from maestro.config import Config
from tests.conftest import capture_stdout
from tests.test_cli import (
    _make_config,
)

# ---------------------------------------------------------------------------
# cmd_lexicon
# ---------------------------------------------------------------------------

_CLI_LEXICON = """\
| term | definition | names | anchor | variants | minted |
|---|---|---|---|---|---|
| merge-truth | Git landing fact. | The settled answer. | `is_pr_settled` |  | proposal 087 |
| wip-primacy | Finish owned work first. | WIP-first focus. | `select_task_focus` | wip primacy | proposal 092 |
"""


def test_cmd_lexicon_prints_one_line_per_entry(mem_fs: dict[str, str]) -> None:
    """lexicon prints a header plus exactly one aligned line per entry."""
    config = _make_config()
    mem_fs[str(config.workspace / "docs" / "lexicon.md")] = _CLI_LEXICON
    args = argparse.Namespace()
    output = capture_stdout(cmd_lexicon, args, config)

    assert "Coined-term lexicon (docs/lexicon.md)" in output
    entry_lines = [ln for ln in output.splitlines() if "[names:" in ln]
    assert len(entry_lines) == 2
    assert "merge-truth" in output
    assert "wip-primacy" in output
    # Every captured column, including 'names', reaches the printed line.
    assert "names: The settled answer." in output
    assert "names: WIP-first focus." in output
    # The two-variant row renders its variants; the empty-variant row renders a dash.
    assert "variants: wip primacy" in output
    assert "variants: -" in output


def test_cmd_lexicon_empty_file_prints_notice(mem_fs: dict[str, str]) -> None:
    """A table-less lexicon file prints the empty-lexicon notice, not a crash."""
    config = _make_config()
    mem_fs[str(config.workspace / "docs" / "lexicon.md")] = "# No table here\n"
    args = argparse.Namespace()
    output = capture_stdout(cmd_lexicon, args, config)

    assert "(no lexicon entries found)" in output


def test_cmd_lexicon_missing_file_prints_notice(mem_fs: dict[str, str]) -> None:
    """A missing lexicon file prints the notice, not a FileNotFoundError traceback."""
    config = _make_config()
    # Deliberately do not populate mem_fs: docs/lexicon.md does not exist, so
    # cmd_lexicon must fall back to the empty-lexicon notice rather than crash.
    args = argparse.Namespace()
    output = capture_stdout(cmd_lexicon, args, config)

    assert "(no lexicon entries found)" in output


def test_main_dispatches_lexicon(mem_fs: dict[str, str]) -> None:
    """main() dispatches to cmd_lexicon for 'lexicon'."""
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "lexicon"]), \
         patch("maestro.cli.cmd_lexicon") as mock_cmd:
        main()

    mock_cmd.assert_called_once()


# ── lexicon views: --list / --undefined / --lint ──────────────────

# A lexicon defining a canonical term with one variant spelling, for the
# ``--lint`` drift/clean cases.
_CLI_LEXICON_LINT = """\
| term | definition | names | anchor | variants | minted |
|---|---|---|---|---|---|
| self-extinguishing | Fires then stops. | Auto-stop. | `x` | self-extinguish | proposal 101 |
"""


def _seed_journal(
    mem_fs: dict[str, str], config: Config, contents: list[str],
) -> None:
    """Seed ``journal.jsonl`` in mem_fs with one JournalEntry per content string."""
    lines = [
        json.dumps({
            "entry_id": f"j{i}",
            "timestamp": "2026-07-23T00:00:00+00:00",
            "stage": "awakening",
            "entry_type": "reflection",
            "content": content,
            "triggered_by": "contemplation",
        })
        for i, content in enumerate(contents)
    ]
    mem_fs[str(config.memory_dir / "journal.jsonl")] = "\n".join(lines) + "\n"


def test_cmd_lexicon_list_flag_matches_default(mem_fs: dict[str, str]) -> None:
    """--list produces byte-identical output to the no-flag default table view."""
    config = _make_config()
    mem_fs[str(config.workspace / "docs" / "lexicon.md")] = _CLI_LEXICON
    default = capture_stdout(cmd_lexicon, argparse.Namespace(), config)
    listed = capture_stdout(
        cmd_lexicon,
        argparse.Namespace(list=True, undefined=False, lint=False),
        config,
    )
    assert listed == default
    assert "merge-truth" in listed


def test_cmd_lexicon_undefined_lists_high_frequency_coinage(
    mem_fs: dict[str, str],
) -> None:
    """--undefined surfaces repeated undefined coinages, highest-frequency first."""
    config = _make_config()
    mem_fs[str(config.workspace / "docs" / "lexicon.md")] = _CLI_LEXICON
    _seed_journal(
        mem_fs,
        config,
        ["the flux-capacitor hums"] * 6 + ["warp-core online"] * 5,
    )
    output = capture_stdout(
        cmd_lexicon,
        argparse.Namespace(undefined=True, lint=False),
        config,
    )

    lines = output.splitlines()
    flux_line = next(ln for ln in lines if "flux-capacitor" in ln)
    warp_line = next(ln for ln in lines if "warp-core" in ln)
    assert flux_line.split()[-1] == "6"
    assert warp_line.split()[-1] == "5"
    # Highest-frequency coinage is listed first.
    assert lines.index(flux_line) < lines.index(warp_line)
    # A term the lexicon already defines never surfaces as undefined.
    assert "merge-truth" not in output


def test_cmd_lexicon_undefined_self_extinguishes_once_defined(
    mem_fs: dict[str, str],
) -> None:
    """A coinage drops out of --undefined the moment the lexicon defines it."""
    config = _make_config()
    lexicon = _CLI_LEXICON + (
        "| flux-capacitor | A hum. | The hum. | `x` |  | proposal 101 |\n"
    )
    mem_fs[str(config.workspace / "docs" / "lexicon.md")] = lexicon
    _seed_journal(mem_fs, config, ["the flux-capacitor hums"] * 6)
    output = capture_stdout(
        cmd_lexicon,
        argparse.Namespace(undefined=True, lint=False),
        config,
    )

    assert "flux-capacitor" not in output
    assert "(no undefined coinages above the frequency floor)" in output


def test_cmd_lexicon_undefined_below_floor_prints_notice(
    mem_fs: dict[str, str],
) -> None:
    """A coinage below the frequency floor does not surface in --undefined."""
    config = _make_config()
    mem_fs[str(config.workspace / "docs" / "lexicon.md")] = _CLI_LEXICON
    _seed_journal(mem_fs, config, ["the flux-capacitor hums"] * 3)
    output = capture_stdout(
        cmd_lexicon,
        argparse.Namespace(undefined=True, lint=False),
        config,
    )

    assert "flux-capacitor" not in output
    assert "(no undefined coinages above the frequency floor)" in output


def test_cmd_lexicon_undefined_missing_files_guard(mem_fs: dict[str, str]) -> None:
    """--undefined guards a missing lexicon and a missing journal to the notice."""
    config = _make_config()
    # Neither docs/lexicon.md nor journal.jsonl exist in mem_fs: the defined set
    # guards to empty and the journal tail reads as empty.
    output = capture_stdout(
        cmd_lexicon,
        argparse.Namespace(undefined=True, lint=False),
        config,
    )

    assert "(no undefined coinages above the frequency floor)" in output


def test_cmd_lexicon_lint_reports_variant_drift(mem_fs: dict[str, str]) -> None:
    """--lint reports a CLAUDE.md variant spelling with its canonical form."""
    config = _make_config()
    mem_fs[str(config.workspace / "docs" / "lexicon.md")] = _CLI_LEXICON_LINT
    mem_fs[str(config.workspace / "CLAUDE.md")] = (
        "The rule is self-extinguish on merge.\n"
    )
    output = capture_stdout(
        cmd_lexicon,
        argparse.Namespace(undefined=False, lint=True),
        config,
    )

    assert "self-extinguish → self-extinguishing (1)" in output


def test_cmd_lexicon_lint_clean_when_only_canonical(mem_fs: dict[str, str]) -> None:
    """--lint reports no drift when CLAUDE.md uses only canonical spellings.

    ``self-extinguish`` is a prefix of the canonical ``self-extinguishing``;
    word-boundary matching must keep the canonical from registering as drift.
    """
    config = _make_config()
    mem_fs[str(config.workspace / "docs" / "lexicon.md")] = _CLI_LEXICON_LINT
    mem_fs[str(config.workspace / "CLAUDE.md")] = (
        "The rule is self-extinguishing on merge.\n"
    )
    output = capture_stdout(
        cmd_lexicon,
        argparse.Namespace(undefined=False, lint=True),
        config,
    )

    assert "→" not in output
    assert "(no drift — CLAUDE.md uses only canonical spellings)" in output


def test_cmd_lexicon_lint_ignores_hyphen_adjacent_compound(
    mem_fs: dict[str, str],
) -> None:
    """--lint does not flag a variant that is a hyphen-adjacent segment of a compound.

    A bare ``\\bvariant\\b`` false-positives when the variant sits hyphen-adjacent
    inside a longer coinage (``self-extinguish`` inside ``self-extinguish-foo`` /
    ``foo-self-extinguish``), because ``-`` is a non-word char and a word boundary
    sits between it and the segment. The hyphen lookarounds must suppress both.
    """
    config = _make_config()
    mem_fs[str(config.workspace / "docs" / "lexicon.md")] = _CLI_LEXICON_LINT
    mem_fs[str(config.workspace / "CLAUDE.md")] = (
        "The self-extinguish-foo rule and the foo-self-extinguish rule.\n"
    )
    output = capture_stdout(
        cmd_lexicon,
        argparse.Namespace(undefined=False, lint=True),
        config,
    )

    assert "→" not in output
    assert "(no drift — CLAUDE.md uses only canonical spellings)" in output


def test_cmd_lexicon_lint_missing_files_guard(mem_fs: dict[str, str]) -> None:
    """--lint guards a missing lexicon and a missing CLAUDE.md to a clean report."""
    config = _make_config()
    # Neither docs/lexicon.md nor CLAUDE.md exist in mem_fs.
    output = capture_stdout(
        cmd_lexicon,
        argparse.Namespace(undefined=False, lint=True),
        config,
    )

    assert "(no drift — CLAUDE.md uses only canonical spellings)" in output


# A lexicon with two behavioral-sensor tag-map rows plus a non-sensor row, for
# the ``--tag-map`` view.
_CLI_LEXICON_TAG_MAP = """\
| term | definition | names | anchor | variants | minted |
|---|---|---|---|---|---|
| merge-truth | Landing fact. | The answer. | `is_pr_settled` |  | proposal 087 |
| settled-relap | Relap sensor. | The relapse signal. | `measure_relap_rate` | SETTLED-RELAP | behavioral-sensor tag map |
| promotable backlog | Promotable sensor. | The backlog signal. | `format_promotable_summary` | PROMOTABLE BACKLOG | behavioral-sensor tag map |
"""


def test_cmd_lexicon_tag_map_renders_block(mem_fs: dict[str, str]) -> None:
    """--tag-map prints the | Tag | Sensor | block from the lexicon's sensor rows."""
    config = _make_config()
    mem_fs[str(config.workspace / "docs" / "lexicon.md")] = _CLI_LEXICON_TAG_MAP
    output = capture_stdout(
        cmd_lexicon,
        argparse.Namespace(undefined=False, lint=False, tag_map=True),
        config,
    )

    assert "| Tag | Sensor |" in output
    assert "|---|---|" in output
    # The placeholder row and both sensor rows render.
    assert "| (task shape / origin) |" in output
    assert "| `SETTLED-RELAP` | `measure_relap_rate` |" in output
    assert "| `PROMOTABLE BACKLOG` | `format_promotable_summary` |" in output
    # The non-sensor row (a different ``minted``) is filtered out.
    assert "merge-truth" not in output


def test_cmd_lexicon_tag_map_missing_file_guard(mem_fs: dict[str, str]) -> None:
    """--tag-map guards a missing lexicon to the header/placeholder prefix, not a crash."""
    config = _make_config()
    # docs/lexicon.md does not exist in mem_fs: the entries guard to () and the
    # render still emits the header/separator/placeholder prefix (no sensor rows).
    output = capture_stdout(
        cmd_lexicon,
        argparse.Namespace(undefined=False, lint=False, tag_map=True),
        config,
    )

    assert "| Tag | Sensor |" in output
    assert "| (task shape / origin) |" in output


def test_main_lexicon_tag_map_flag_parses(mem_fs: dict[str, str]) -> None:
    """``maestro lexicon --tag-map`` parses to a namespace with tag_map=True."""
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "lexicon", "--tag-map"]), \
         patch("maestro.cli.cmd_lexicon") as mock_cmd:
        main()

    mock_cmd.assert_called_once()
    parsed_args = mock_cmd.call_args.args[0]
    assert parsed_args.tag_map is True
