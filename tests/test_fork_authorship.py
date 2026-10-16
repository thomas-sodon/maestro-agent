"""Tests for the fork-authorship classifier and CLI command."""

from __future__ import annotations

import argparse

from maestro.cli import cmd_measure_fork_authorship
from maestro.fork_authorship import (
    classify_fork_authorship,
    format_fork_authorship_summary,
    measure_fork_authorship,
)
from tests.conftest import capture_stdout
from tests.test_cli import _make_config

# ---------------------------------------------------------------------------
# classify_fork_authorship — each branch
# ---------------------------------------------------------------------------


def test_classify_self_authored_intended_action() -> None:
    """First-person intended action is self-authored even when it names files."""
    text = "I will create exactly zero new files this cycle."
    assert classify_fork_authorship(text) == "self_authored"


def test_classify_self_authored_negated_action() -> None:
    """Negated-own-action 'I will not' is self-authored restraint."""
    assert classify_fork_authorship("I will not touch main.") == "self_authored"


def test_classify_self_authored_contraction() -> None:
    """The 'I won't' contraction is the negated-own-action form."""
    assert classify_fork_authorship("I won't open a PR.") == "self_authored"


def test_classify_externally_grounded_commit_count() -> None:
    """A git commit count is world-state I do not control."""
    text = "The git commit count on the branch stays at 3."
    assert classify_fork_authorship(text) == "externally_grounded"


def test_classify_externally_grounded_file_count() -> None:
    """A pre-existing file count is externally grounded."""
    text = "The number of files in the repo is unchanged."
    assert classify_fork_authorship(text) == "externally_grounded"


def test_classify_externally_grounded_measured_state() -> None:
    """A measured pre-existing success rate is externally grounded."""
    assert classify_fork_authorship("the success rate drops") == (
        "externally_grounded"
    )


def test_classify_externally_grounded_bare_git_word() -> None:
    """The word "git" alone (no trailing keyword) still grounds externally."""
    assert classify_fork_authorship("verified via git") == "externally_grounded"


def test_classify_git_substring_in_common_words_is_not_external() -> None:
    """Words merely containing "git" (legit, digit) must not match the signal.

    A bare ``"git "`` substring matched inside "legit " / "digit "; word-boundary
    matching corrects this so these resolve to ``unknown``, not ``externally_grounded``.
    """
    assert classify_fork_authorship("this is legit work") == "unknown"
    assert classify_fork_authorship("a single digit number") == "unknown"
    assert classify_fork_authorship("the digit five") == "unknown"


def test_classify_unknown_no_signal() -> None:
    """Text with no qualifying signal is unknown."""
    assert classify_fork_authorship("Something vague happens.") == "unknown"


def test_classify_unknown_empty() -> None:
    """Empty input is unknown."""
    assert classify_fork_authorship("") == "unknown"


def test_classify_self_authored_precedence_over_external() -> None:
    """Self-authored wins when both signals appear (I control the trigger)."""
    text = "I will commit, so the commit count rises to 5."
    assert classify_fork_authorship(text) == "self_authored"


# ---------------------------------------------------------------------------
# measure_fork_authorship — counts and ratio math
# ---------------------------------------------------------------------------


def test_measure_empty_records() -> None:
    """No records yields zero counts and a 0.0 ratio."""
    stats = measure_fork_authorship([])
    assert stats["self_authored"] == 0
    assert stats["externally_grounded"] == 0
    assert stats["unknown"] == 0
    assert stats["total_classified"] == 0
    assert stats["externally_grounded_ratio"] == 0.0


def test_measure_all_self() -> None:
    """All self-authored forks give a 0.0 externally-grounded ratio."""
    records: list[dict[str, object]] = [
        {"loss_condition": "I will create a file"},
        {"loss_condition": "I will not push"},
    ]
    stats = measure_fork_authorship(records)
    assert stats["self_authored"] == 2
    assert stats["externally_grounded"] == 0
    assert stats["total_classified"] == 2
    assert stats["externally_grounded_ratio"] == 0.0


def test_measure_all_external() -> None:
    """All externally-grounded forks give a 1.0 ratio."""
    records: list[dict[str, object]] = [
        {"loss_condition": "the commit count is 3"},
        {"loss_condition": "the number of files is fixed"},
    ]
    stats = measure_fork_authorship(records)
    assert stats["externally_grounded"] == 2
    assert stats["total_classified"] == 2
    assert stats["externally_grounded_ratio"] == 1.0


def test_measure_mixed_with_unknown() -> None:
    """Unknown rows are excluded from total_classified and the ratio."""
    records: list[dict[str, object]] = [
        {"loss_condition": "I will add a test"},          # self
        {"loss_condition": "the git commit count rises"},  # external
        {"loss_condition": "the commit count falls"},      # external
        {"loss_condition": "vague outcome"},               # unknown
    ]
    stats = measure_fork_authorship(records)
    assert stats["self_authored"] == 1
    assert stats["externally_grounded"] == 2
    assert stats["unknown"] == 1
    assert stats["total_classified"] == 3
    assert stats["externally_grounded_ratio"] == 2 / 3


def test_measure_falls_back_to_prediction_field() -> None:
    """When loss_condition is absent the prediction field is classified."""
    records: list[dict[str, object]] = [
        {"prediction": "I will write a module"},
    ]
    stats = measure_fork_authorship(records)
    assert stats["self_authored"] == 1


def test_measure_record_without_text_is_unknown() -> None:
    """A record with no string field classifies as unknown."""
    records: list[dict[str, object]] = [{"verdict": "unscored"}]
    stats = measure_fork_authorship(records)
    assert stats["unknown"] == 1
    assert stats["total_classified"] == 0


# ---------------------------------------------------------------------------
# format_fork_authorship_summary
# ---------------------------------------------------------------------------


def test_format_summary_non_empty() -> None:
    """The summary is a non-empty multi-line string with the breakdown."""
    stats = measure_fork_authorship(
        [
            {"loss_condition": "I will add a file"},
            {"loss_condition": "the commit count is 3"},
        ]
    )
    out = format_fork_authorship_summary(stats)
    assert "Fork-Authorship Breakdown" in out
    assert "self_authored:" in out
    assert "externally_grounded:" in out
    assert "50%" in out


def test_format_summary_empty_stats() -> None:
    """An empty stats dict still renders a non-empty summary."""
    out = format_fork_authorship_summary({})
    assert "Fork-Authorship Breakdown" in out
    assert "0%" in out


# ---------------------------------------------------------------------------
# CLI — measure-fork-authorship
# ---------------------------------------------------------------------------


def test_cmd_measure_fork_authorship_with_data(mem_fs: dict[str, str]) -> None:
    """The CLI command classifies predictions.jsonl and prints the summary."""
    config = _make_config()
    config.ensure_dirs()
    path = str(config.memory_dir / "predictions.jsonl")
    mem_fs[path] = (
        '{"source": "reflection", "loss_condition": "I will create zero files"}\n'
        '{"source": "reflection", "loss_condition": "the commit count is 3"}\n'
    )
    args = argparse.Namespace()
    output = capture_stdout(cmd_measure_fork_authorship, args, config)
    assert "Fork-Authorship Breakdown" in output
    assert "self_authored:        1" in output
    assert "externally_grounded:  1" in output


def test_cmd_measure_fork_authorship_excludes_non_fork_rows(
    mem_fs: dict[str, str],
) -> None:
    """Verdict-scoring rows and task-outcome rows are not counted as forks."""
    config = _make_config()
    config.ensure_dirs()
    path = str(config.memory_dir / "predictions.jsonl")
    mem_fs[path] = (
        '{"source": "reflection", "loss_condition": "the commit count is 3"}\n'
        '{"source": "reflection_verdict", "prediction_id": "p1", '
        '"verdict": "wrong"}\n'
        '{"task_id": "t1", "predicted_outcome": "success"}\n'
    )
    args = argparse.Namespace()
    output = capture_stdout(cmd_measure_fork_authorship, args, config)
    # Only the single reflection fork classifies; the verdict and task-outcome
    # rows are filtered out, so 'unknown' stays 0 and the total is 1.
    assert "externally_grounded:  1" in output
    assert "unknown:              0" in output
    assert "total classified:     1" in output


def test_cmd_measure_fork_authorship_empty(mem_fs: dict[str, str]) -> None:
    """With no predictions file the command still prints a summary."""
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace()
    output = capture_stdout(cmd_measure_fork_authorship, args, config)
    assert "Fork-Authorship Breakdown" in output
    assert "total classified:     0" in output
