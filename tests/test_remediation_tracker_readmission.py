"""Unlanded remediations, PR-lap summary, provenance and readmission sensors;
split out of ``tests/test_remediation_tracker.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from maestro.audit_checks import (
    format_known_state_summary,
)
from maestro.remediation_tracker import (
    _CHOSEN_QUIET_MARKER,
    PR_ADMISSION_CHANNEL_TEND,
    JsonlPrProvenanceLedger,
    ProvenanceRecord,
    PRReadmissionRate,
    _forced_read_directive_text,
    _read_recent_task_tail,
    _readmission_construction_directive_text,
    check_forced_read_directive,
    check_readmission_construction_directive,
    format_pr_lap_summary,
    format_pr_readmission_combined,
    format_unlanded_remediations,
    format_unlanded_remediations_line,
    lap_window_start,
    load_all_provenance_records,
    log_pending_pr_provenance,
    measure_pr_readmission_rate,
    measure_unlanded_remediations,
    pr_number_from_remediation_goal,
    summarize_readmission_records,
)
from maestro.state_store import (
    SqlitePrProvenanceLedger,
    StateStore,
)
from tests.test_remediation_tracker import (
    _seed_history,
)


def _merged_oracle(*merged_prs: int) -> Callable[[int], bool]:
    """Return a git-merge-truth stand-in: True only for *merged_prs*.

    Mirrors :func:`maestro.audit_checks_phantom._git_pr_merged_default` without the git
    subprocess, so tests assert the sensor's behaviour purely from the oracle's
    verdict (no ``version_archive.jsonl`` involved any more).
    """
    merged = frozenset(merged_prs)
    return lambda pr: pr in merged


# An oracle reporting that nothing is merged — every remediation stamp surfaces.
_NONE_MERGED: Callable[[int], bool] = _merged_oracle()


class TestMeasureUnlandedRemediations:
    """Ground-truth detection of remediations that never reached main."""

    def test_merged_in_git_not_archived_not_flagged(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The exact false-flag class #2504 fixed: git-merge-truth shows PR #100
        # merged into main, yet no version_archive.jsonl exists at all. The
        # old version_archive-anchored sensor flagged this; the git-anchored
        # one must NOT, because git is the authority on what reached main.
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "Remediate PR #100: Improve foo", "outcome": "success"},
        ])
        result = measure_unlanded_remediations(
            data_dir, 50, is_pr_merged=_merged_oracle(100),
        )
        assert result == {}

    def test_genuinely_unmerged_surfaces(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A remediation whose PR git shows is NOT merged still surfaces
        # UNLANDED-REMEDIATION — the sensor keeps its real signal.
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "Remediate PR #200: Fix bar", "outcome": "success"},
        ])
        result = measure_unlanded_remediations(
            data_dir, 50, is_pr_merged=_NONE_MERGED,
        )
        assert result == {200: 1}

    def test_git_oracle_failure_propagates_loudly(
        self, mem_fs: dict[str, str],
    ) -> None:
        # When the oracle raises, the sensor must degrade loudly — propagate
        # the error rather than silently treating the PR as not-merged (which
        # would mass-flag every lineage and hide the broken sensor).
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "Remediate PR #200: Fix bar", "outcome": "success"},
        ])

        def _raising(_pr: int) -> bool:
            raise RuntimeError("git exploded")

        with pytest.raises(RuntimeError, match="git exploded"):
            measure_unlanded_remediations(
                data_dir, 50, is_pr_merged=_raising,
            )

    def test_default_oracle_is_git_pr_merged_default(
        self, mem_fs: dict[str, str],
    ) -> None:
        # With no injected predicate, the sensor lazy-imports and consults
        # ``audit_checks_phantom._git_pr_merged_default`` — the same git-merge-truth
        # oracle the PR-keyed audit rules use.
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "Remediate PR #200: Fix bar", "outcome": "success"},
        ])
        with patch(
            "maestro.audit_checks_phantom._git_pr_merged_default",
            return_value=False,
        ) as oracle:
            result = measure_unlanded_remediations(data_dir, 50)
        assert result == {200: 1}
        oracle.assert_called_once_with(200)

    def test_oracle_cached_per_pr(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Two stamps for the same PR query the oracle once (per-PR cache).
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "Remediate PR #200: Fix bar", "outcome": "partial"},
            {"goal": "Remediate PR #200: Fix bar", "outcome": "success"},
        ])
        calls: list[int] = []

        def _counting(pr: int) -> bool:
            calls.append(pr)
            return False

        result = measure_unlanded_remediations(
            data_dir, 50, is_pr_merged=_counting,
        )
        assert result == {200: 2}
        assert calls == [200]

    def test_partial_outcome_counts_as_stamp(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "Remediate PR #200: Fix bar", "outcome": "partial"},
            {"goal": "Remediate PR #200: Fix bar", "outcome": "success"},
        ])
        result = measure_unlanded_remediations(
            data_dir, 50, is_pr_merged=_NONE_MERGED,
        )
        assert result == {200: 2}

    def test_injected_oracle_filters_per_pr_selectively(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Merged and unmerged remediations coexist in the same window. The
        # injected oracle differentiates by PR number (#100 merged, #200 not),
        # so only the unmerged lineage surfaces — exercising both the
        # merged (continue) and unmerged (count) branches of the per-PR check
        # in a single scan, which the uniform-oracle cases above do not.
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "Remediate PR #100: Improve foo", "outcome": "success"},
            {"goal": "Remediate PR #200: Fix bar", "outcome": "success"},
        ])
        assert measure_unlanded_remediations(
            data_dir, 50, is_pr_merged=lambda n: n == 100,
        ) == {200: 1}

    def test_results_sorted_ascending_by_pr_number(
        self, mem_fs: dict[str, str],
    ) -> None:
        # PRs encountered out of order in history are returned sorted ascending
        # by PR number (the ``sorted(stamps.items())`` contract), so the
        # rendered UNLANDED-REMEDIATION line is deterministic regardless of the
        # order remediation tasks happened to run.
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "Remediate PR #300: Fix baz", "outcome": "success"},
            {"goal": "Remediate PR #150: Fix qux", "outcome": "partial"},
            {"goal": "Remediate PR #220: Fix quux", "outcome": "success"},
        ])
        result = measure_unlanded_remediations(
            data_dir, 50, is_pr_merged=lambda n: False,
        )
        assert list(result.keys()) == [150, 220, 300]
        assert result == {150: 1, 220: 1, 300: 1}

    def test_unlanded_beyond_window_not_detected(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The remediation exists but sits older than the n_cycles tail,
        # so the bounded scan never reaches it.
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "Remediate PR #300: Fix baz", "outcome": "success"},
            {"goal": "Refactor module A", "outcome": "success"},
            {"goal": "Refactor module B", "outcome": "success"},
        ])
        # Only the last 2 records are scanned; the remediation is the first.
        result = measure_unlanded_remediations(
            data_dir, 2, is_pr_merged=_NONE_MERGED,
        )
        assert result == {}

    def test_failed_remediation_not_counted(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "Remediate PR #200: Fix bar", "outcome": "failed"},
        ])
        result = measure_unlanded_remediations(
            data_dir, 50, is_pr_merged=_NONE_MERGED,
        )
        assert result == {}

    def test_non_remediation_tasks_ignored(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "Investigate #200 throughput", "outcome": "success"},
        ])
        result = measure_unlanded_remediations(
            data_dir, 50, is_pr_merged=_NONE_MERGED,
        )
        assert result == {}

    def test_non_positive_cycles_returns_empty(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A non-positive window short-circuits before the oracle is consulted.
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "Remediate PR #200: Fix bar", "outcome": "success"},
        ])

        def _never(_pr: int) -> bool:
            raise AssertionError("oracle must not be called")

        assert measure_unlanded_remediations(
            data_dir, 0, is_pr_merged=_never,
        ) == {}

    def test_missing_files_return_empty(
        self, mem_fs: dict[str, str],
    ) -> None:
        # No task_history.jsonl seeded at all.
        data_dir = Path("/fake/data")
        result = measure_unlanded_remediations(
            data_dir, 50, is_pr_merged=_NONE_MERGED,
        )
        assert result == {}

    def test_corrupt_lines_skipped(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        mem_fs[str(data_dir / "task_history.jsonl")] = (
            "not json {\n"
            + json.dumps(
                {"goal": "Remediate PR #200: Fix bar", "outcome": "success"},
            )
            + "\n"
        )
        result = measure_unlanded_remediations(
            data_dir, 50, is_pr_merged=_NONE_MERGED,
        )
        assert result == {200: 1}

    def test_blank_lines_skipped(self, mem_fs: dict[str, str]) -> None:
        data_dir = Path("/fake/data")
        mem_fs[str(data_dir / "task_history.jsonl")] = (
            "\n"
            + json.dumps(
                {"goal": "Remediate PR #200: Fix bar", "outcome": "success"},
            )
            + "\n\n"
        )
        result = measure_unlanded_remediations(
            data_dir, 50, is_pr_merged=_NONE_MERGED,
        )
        assert result == {200: 1}

    def test_non_dict_records_skipped(self, mem_fs: dict[str, str]) -> None:
        # A valid-JSON but non-dict line is ignored, not crashed.
        data_dir = Path("/fake/data")
        mem_fs[str(data_dir / "task_history.jsonl")] = (
            "[1, 2, 3]\n"
            + json.dumps(
                {"goal": "Remediate PR #200: Fix bar", "outcome": "success"},
            )
            + "\n"
        )
        result = measure_unlanded_remediations(
            data_dir, 50, is_pr_merged=_NONE_MERGED,
        )
        assert result == {200: 1}

    def test_title_fallback_when_goal_absent(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A record carrying only ``title`` (no ``goal``) still classifies.
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"title": "Remediate PR #205: Some fix", "outcome": "success"},
        ])
        result = measure_unlanded_remediations(
            data_dir, 50, is_pr_merged=_NONE_MERGED,
        )
        assert result == {205: 1}

    def test_record_without_goal_or_title_skipped(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"outcome": "success"},
            {"goal": "Remediate PR #200: Fix bar", "outcome": "success"},
        ])
        result = measure_unlanded_remediations(
            data_dir, 50, is_pr_merged=_NONE_MERGED,
        )
        assert result == {200: 1}

    def test_empty_title_still_flagged_when_unmerged(
        self, mem_fs: dict[str, str],
    ) -> None:
        # ``Remediate PR #210:`` with no title after the colon still matches
        # the goal shape; landing is now decided purely by git-merge-truth, so
        # an unmerged PR surfaces regardless of the (empty) title.
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "Remediate PR #210:", "outcome": "success"},
        ])
        result = measure_unlanded_remediations(
            data_dir, 50, is_pr_merged=_NONE_MERGED,
        )
        assert result == {210: 1}

    def test_oserror_on_read_returns_empty(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The file exists (path.exists() is True) but open() raises: the
        # reader fails open to empty rather than crashing.
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "Remediate PR #200: Fix bar", "outcome": "success"},
        ])
        with patch("builtins.open", side_effect=OSError("disk error")):
            result = measure_unlanded_remediations(
                data_dir, 50, is_pr_merged=_NONE_MERGED,
            )
        assert result == {}

    def test_read_recent_task_tail_non_positive_n(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The standalone reader guards a non-positive window defensively even
        # though ``measure_unlanded_remediations`` already short-circuits.
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "Remediate PR #200: Fix bar", "outcome": "success"},
        ])
        assert _read_recent_task_tail(data_dir, 0) == []

    def test_lap_window_start_returns_oldest_tail_timestamp(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The window starts at the oldest record of the same trailing tail the
        # lap measure scans; a record outside the tail is not the start.
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "a", "timestamp": "2026-01-01T00:00:00+00:00"},
            {"goal": "b"},
            {"goal": "c", "timestamp": "2026-02-01T00:00:00+00:00"},
            {"goal": "d", "timestamp": "2026-03-01T00:00:00+00:00"},
        ])
        assert lap_window_start(data_dir, 3) == "2026-02-01T00:00:00+00:00"

    def test_lap_window_start_none_on_missing_file(
        self, mem_fs: dict[str, str],
    ) -> None:
        assert lap_window_start(Path("/fake/data"), 50) is None

    def test_lap_window_start_none_on_non_positive_n(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "a", "timestamp": "2026-01-01T00:00:00+00:00"},
        ])
        assert lap_window_start(data_dir, 0) is None

    def test_lap_window_start_none_without_string_timestamp(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "a"}, {"goal": "b", "timestamp": 123}, {"goal": "c", "timestamp": ""},
        ])
        assert lap_window_start(data_dir, 50) is None


class TestFormatUnlandedRemediations:
    """The rendered ``UNLANDED-REMEDIATION`` line and its suppression."""

    def test_no_unlanded_yields_empty_string(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "Remediate PR #100: Improve foo", "outcome": "success"},
        ])
        line = format_unlanded_remediations(
            data_dir, 50, is_pr_merged=_merged_oracle(100),
        )
        assert line == ""

    def test_unlanded_line_exact_text(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "Remediate PR #200: Fix bar", "outcome": "success"},
            {"goal": "Remediate PR #359: Fix qux", "outcome": "partial"},
        ])
        line = format_unlanded_remediations(
            data_dir, 50, is_pr_merged=_NONE_MERGED,
        )
        assert line == (
            "UNLANDED-REMEDIATION: 2 PR(s) stamped success/partial but "
            "not merged into main per git-merge-truth within last "
            "50 tasks: #200 (x1), #359 (x1)"
        )

    def test_format_default_oracle_is_git_pr_merged_default(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The formatter forwards its default (None) predicate through to the
        # git-merge-truth oracle.
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "Remediate PR #200: Fix bar", "outcome": "success"},
        ])
        with patch(
            "maestro.audit_checks_phantom._git_pr_merged_default",
            return_value=True,
        ) as oracle:
            line = format_unlanded_remediations(data_dir, 50)
        assert line == ""
        oracle.assert_called_once_with(200)

    def test_no_filesystem_writes(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The detector is a pure read: calling it must not add or change
        # any in-memory filesystem entries (zero filesystem writes).
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "Remediate PR #200: Fix bar", "outcome": "success"},
        ])
        before = dict(mem_fs)
        format_unlanded_remediations(data_dir, 50, is_pr_merged=_NONE_MERGED)
        measure_unlanded_remediations(data_dir, 50, is_pr_merged=_NONE_MERGED)
        assert mem_fs == before


class TestFormatUnlandedRemediationsLine:
    """The pure dict→line formatter underlying ``format_unlanded_remediations``.

    Splitting the rendering out lets a caller that already measured the
    ``{pr: count}`` dict (to also feed ``format_pr_lap_summary``) render the
    UNLANDED-REMEDIATION line without a second host-oracle measurement pass.
    """

    def test_empty_dict_yields_empty_string(self) -> None:
        assert format_unlanded_remediations_line({}, 50) == ""

    def test_renders_each_pr_and_count(self) -> None:
        line = format_unlanded_remediations_line({200: 1, 359: 2}, 50)
        assert line == (
            "UNLANDED-REMEDIATION: 2 PR(s) stamped success/partial but "
            "not merged into main per git-merge-truth within last "
            "50 tasks: #200 (x1), #359 (x2)"
        )

    def test_wrapper_measures_once_and_delegates_to_line_formatter(
        self, mem_fs: dict[str, str],
    ) -> None:
        # ``format_unlanded_remediations`` measures once then renders via the
        # pure formatter, so its output equals the line formatter applied to the
        # measured dict — and it consults the host oracle a single time per PR.
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "Remediate PR #200: Fix bar", "outcome": "partial"},
        ])
        calls: list[int] = []

        def _oracle(pr: int) -> bool:
            calls.append(pr)
            return False

        line = format_unlanded_remediations(data_dir, 50, is_pr_merged=_oracle)
        measured = measure_unlanded_remediations(
            data_dir, 50, is_pr_merged=_NONE_MERGED,
        )
        assert line == format_unlanded_remediations_line(measured, 50)
        assert calls == [200]


class TestUnlandedRemediationWiring:
    """The line surfaces through ``format_known_state_summary``."""

    def test_known_state_summary_includes_unlanded_line(
        self, mem_fs: dict[str, str],
    ) -> None:
        # An unlanded remediation, surfaced end-to-end through the known-state
        # context the contemplation/generate-task phases inject.
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "Remediate PR #200: Fix bar", "outcome": "success"},
        ])
        line = format_unlanded_remediations(
            data_dir, 50, is_pr_merged=_NONE_MERGED,
        )
        summary = format_known_state_summary(
            {}, [], unlanded_remediation_context=line,
        )
        assert "UNLANDED-REMEDIATION" in summary
        assert "#200" in summary

    def test_known_state_summary_omits_when_landed(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A landed remediation (git shows merged) produces no line, so the
        # known-state summary stays empty when nothing else is supplied.
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "Remediate PR #100: Improve foo", "outcome": "success"},
        ])
        line = format_unlanded_remediations(
            data_dir, 50, is_pr_merged=_merged_oracle(100),
        )
        summary = format_known_state_summary(
            {}, [], unlanded_remediation_context=line,
        )
        assert summary == ""


class TestFormatPrLapSummary:
    """The pure ``PR-LAPS`` lap-budget summary (proposal 080 step 4)."""

    def test_pr_at_half_budget_surfaces(self) -> None:
        # 3 laps == budget/2 (budget 6) → the PR is surfaced.
        line = format_pr_lap_summary({770: 3}, set(), budget=6)
        assert line.startswith("PR-LAPS:")
        assert "#770 (3/6 laps)" in line

    def test_below_half_budget_suppressed(self) -> None:
        # 2 laps < budget/2 (budget 6) and no standdown → nothing to report.
        assert format_pr_lap_summary({770: 2}, set(), budget=6) == ""

    def test_odd_budget_uses_ceiling(self) -> None:
        # budget 5 → half is 2.5; 2 laps is below, 3 laps is at/above.
        assert format_pr_lap_summary({1: 2}, set(), budget=5) == ""
        assert "#1 (3/5 laps)" in format_pr_lap_summary({1: 3}, set(), budget=5)

    def test_standdown_surfaces_even_at_zero_laps(self) -> None:
        # A stood-down PR with no laps in the window is still shown, annotated.
        line = format_pr_lap_summary({}, {880}, budget=6)
        assert "#880 (0/6 laps) [stood-down]" in line

    def test_standdown_annotation_on_lapping_pr(self) -> None:
        line = format_pr_lap_summary({880: 4}, {880}, budget=6)
        assert "#880 (4/6 laps) [stood-down]" in line

    def test_refuted_standdown_annotated_distinctly(self) -> None:
        """A contradicted latch renders ``[standdown-refuted]``, not ``[stood-down]``."""
        line = format_pr_lap_summary({880: 4}, {880}, budget=6, refuted_prs={880})
        assert "#880 (4/6 laps) [standdown-refuted]" in line
        assert "[stood-down]" not in line

    def test_refutation_annotation_shares_no_marker_substring(self) -> None:
        """The refutation tag must not contain the chosen-quiet marker.

        ``_CHOSEN_QUIET_MARKER`` is matched by *substring*, so a
        ``[stood-down-refuted]`` spelling would let a contradicted latch be
        scored as a deliberate quiet act.
        """
        line = format_pr_lap_summary({880: 4}, {880}, budget=6, refuted_prs={880})
        assert _CHOSEN_QUIET_MARKER not in line

    def test_refuted_pr_without_a_standdown_is_unannotated(self) -> None:
        """Refutation only re-labels an *active* latch; it never invents one."""
        line = format_pr_lap_summary({770: 3}, set(), budget=6, refuted_prs={770})
        assert "#770 (3/6 laps)" in line
        assert "refuted" not in line

    def test_mixed_refuted_and_holding_standdowns(self) -> None:
        line = format_pr_lap_summary(
            {}, {880, 881}, budget=6, refuted_prs={881},
        )
        assert "#880 (0/6 laps) [stood-down]" in line
        assert "#881 (0/6 laps) [standdown-refuted]" in line

    def test_default_refuted_prs_is_behaviour_identical(self) -> None:
        """Omitting the keyword renders exactly the pre-135 line."""
        assert format_pr_lap_summary(
            {880: 4}, {880}, budget=6,
        ) == format_pr_lap_summary({880: 4}, {880}, budget=6, refuted_prs=())

    def test_non_positive_budget_yields_empty(self) -> None:
        # Budget 0 disables the feature; never divides or surfaces.
        assert format_pr_lap_summary({770: 9}, {880}, budget=0) == ""

    def test_empty_inputs_yield_empty(self) -> None:
        assert format_pr_lap_summary({}, set(), budget=6) == ""

    def test_prs_sorted_ascending(self) -> None:
        # Oldest-bug-first: lower PR numbers come first in the line.
        line = format_pr_lap_summary({900: 3, 100: 3}, set(), budget=6)
        assert line.index("#100") < line.index("#900")

    def test_surfaces_through_known_state_summary(self) -> None:
        # End-to-end: the PR-LAPS line flows through format_known_state_summary
        # and the section renders even when it is the only non-empty input.
        line = format_pr_lap_summary({770: 3}, {880}, budget=6)
        summary = format_known_state_summary({}, [], pr_lap_context=line)
        assert "## Already Known State" in summary
        assert "PR-LAPS:" in summary
        assert "#770" in summary
        assert "#880" in summary


# ──────────────────────────────────────────────────────────────────────
# Pending-PR admission provenance — readmission-channel sensor
# ──────────────────────────────────────────────────────────────────────


def _seed_provenance(
    mem_fs: dict[str, str],
    data_dir: Path,
    records: list[dict[str, object]],
) -> None:
    """Write *records* as ``pending_pr_provenance.jsonl`` lines into mem_fs."""
    mem_fs[str(data_dir / "pending_pr_provenance.jsonl")] = "".join(
        json.dumps(r) + "\n" for r in records
    )


class TestPrNumberFromRemediationGoal:
    """The directive→PR-number recovery helper."""

    def test_extracts_pr_number(self) -> None:
        goal = "Remediate PR #2457: Fix the conflict resolver"
        assert pr_number_from_remediation_goal(goal) == 2457

    def test_empty_goal_returns_none(self) -> None:
        assert pr_number_from_remediation_goal("") is None

    def test_non_remediation_goal_returns_none(self) -> None:
        assert pr_number_from_remediation_goal("Investigate the loop") is None


class TestLogPendingPrProvenance:
    """The append-only writer for the admission provenance log."""

    def test_appends_one_record_per_call(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        log_pending_pr_provenance(data_dir, 100, PR_ADMISSION_CHANNEL_TEND)
        log_pending_pr_provenance(
            data_dir, 100, PR_ADMISSION_CHANNEL_TEND,
        )
        raw = mem_fs[str(data_dir / "pending_pr_provenance.jsonl")]
        lines = [json.loads(line) for line in raw.splitlines()]
        assert lines == [
            {"pr_number": 100, "channel": PR_ADMISSION_CHANNEL_TEND},
            {"pr_number": 100, "channel": PR_ADMISSION_CHANNEL_TEND},
        ]

    def test_non_positive_pr_number_is_ignored(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        log_pending_pr_provenance(data_dir, 0, PR_ADMISSION_CHANNEL_TEND)
        log_pending_pr_provenance(data_dir, -5, PR_ADMISSION_CHANNEL_TEND)
        assert str(data_dir / "pending_pr_provenance.jsonl") not in mem_fs


def _channel_summary(
    data_dir: Path, n_records: int = 500,
) -> dict[str, int]:
    """Summarize the trailing-*n_records* admission window (post-refactor path).

    Reads what production now reads — the JSONL backend's ``recent`` window
    summarized by :func:`summarize_readmission_records` — so the edge-case
    coverage of the retired one-shot ``measure_pr_readmission_channel`` reader
    (corrupted-tail tolerance, channel coercion, trailing window, fail-open)
    survives its removal.
    """
    return summarize_readmission_records(
        JsonlPrProvenanceLedger(data_dir).recent(n_records),
    )


class TestReadmissionChannelSummary:
    """Grouping, counting, and the >=2 re-admission threshold.

    Exercises the summary the ``PR-READMISSION`` line reads — the JSONL
    ``recent`` window fed through :func:`summarize_readmission_records` (see
    :func:`_channel_summary`) — after the standalone
    ``measure_pr_readmission_channel`` reader was retired.
    """

    def test_absent_file_yields_zero_counts(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        assert _channel_summary(data_dir) == {
            "total_admissions": 0,
            "distinct_prs": 0,
            "readmitted_prs": 0,
        }

    def test_empty_file_yields_zero_counts(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        _seed_provenance(mem_fs, data_dir, [])
        assert _channel_summary(data_dir) == {
            "total_admissions": 0,
            "distinct_prs": 0,
            "readmitted_prs": 0,
        }

    def test_groups_and_counts_readmissions(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        _seed_provenance(mem_fs, data_dir, [
            {"pr_number": 100, "channel": PR_ADMISSION_CHANNEL_TEND},
            {"pr_number": 100, "channel": PR_ADMISSION_CHANNEL_TEND},
            {"pr_number": 100, "channel": PR_ADMISSION_CHANNEL_TEND},
            {"pr_number": 200, "channel": PR_ADMISSION_CHANNEL_TEND},
            {"pr_number": 200, "channel": PR_ADMISSION_CHANNEL_TEND},
            {"pr_number": 300, "channel": PR_ADMISSION_CHANNEL_TEND},
        ])
        # PR 100 (x3) and PR 200 (x2) are re-admitted; PR 300 (x1) is not.
        assert _channel_summary(data_dir) == {
            "total_admissions": 6,
            "distinct_prs": 3,
            "readmitted_prs": 2,
        }

    def test_single_admission_is_not_readmission(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        _seed_provenance(mem_fs, data_dir, [
            {"pr_number": 100, "channel": PR_ADMISSION_CHANNEL_TEND},
        ])
        assert _channel_summary(data_dir) == {
            "total_admissions": 1,
            "distinct_prs": 1,
            "readmitted_prs": 0,
        }

    def test_oserror_on_read_returns_zero_counts(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The file exists but open() raises: the reader fails open to
        # all-zero counts rather than crashing the zero-cost sensor path.
        data_dir = Path("/fake/data")
        _seed_provenance(mem_fs, data_dir, [
            {"pr_number": 100, "channel": PR_ADMISSION_CHANNEL_TEND},
        ])
        with patch("builtins.open", side_effect=OSError("disk error")):
            assert _channel_summary(data_dir) == {
                "total_admissions": 0,
                "distinct_prs": 0,
                "readmitted_prs": 0,
            }

    def test_skips_malformed_and_invalid_records(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        path = str(data_dir / "pending_pr_provenance.jsonl")
        mem_fs[path] = "\n".join([
            "not-json",
            json.dumps([1, 2, 3]),  # not a dict
            json.dumps({"channel": "x"}),  # missing pr_number
            json.dumps({"pr_number": "100"}),  # non-int pr_number
            json.dumps({"pr_number": True}),  # bool rejected as int
            json.dumps({"pr_number": 0}),  # non-positive
            json.dumps({"pr_number": 100, "channel": PR_ADMISSION_CHANNEL_TEND}),
            json.dumps({"pr_number": 100, "channel": PR_ADMISSION_CHANNEL_TEND}),
            "",  # blank line
        ]) + "\n"
        assert _channel_summary(data_dir) == {
            "total_admissions": 2,
            "distinct_prs": 1,
            "readmitted_prs": 1,
        }

    def test_row_with_missing_or_non_string_channel_is_still_counted(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A valid positive pr_number is counted even when the channel is absent
        # or non-string (coerced to ""): the summary groups on the number alone,
        # so this preserves parity with the pre-record counting path.
        data_dir = Path("/fake/data")
        _seed_provenance(mem_fs, data_dir, [
            {"pr_number": 100},  # missing channel
            {"pr_number": 100, "channel": 42},  # non-string channel
        ])
        assert _channel_summary(data_dir) == {
            "total_admissions": 2,
            "distinct_prs": 1,
            "readmitted_prs": 1,
        }

    def test_trailing_window_ages_out_older_records(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The two PR-100 re-admissions are the oldest records; a window of 2
        # keeps only the trailing two single-admission PRs, so PR 100 ages out
        # and no readmission is reported — the signal does not latch forever.
        data_dir = Path("/fake/data")
        _seed_provenance(mem_fs, data_dir, [
            {"pr_number": 100, "channel": PR_ADMISSION_CHANNEL_TEND},
            {"pr_number": 100, "channel": PR_ADMISSION_CHANNEL_TEND},
            {"pr_number": 200, "channel": PR_ADMISSION_CHANNEL_TEND},
            {"pr_number": 300, "channel": PR_ADMISSION_CHANNEL_TEND},
        ])
        assert _channel_summary(data_dir, n_records=2) == {
            "total_admissions": 2,
            "distinct_prs": 2,
            "readmitted_prs": 0,
        }
        # Unbounded over the same data, PR 100 is still a readmission.
        assert _channel_summary(data_dir, n_records=500) == {
            "total_admissions": 4,
            "distinct_prs": 3,
            "readmitted_prs": 1,
        }

    def test_non_positive_window_yields_zero_counts(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        _seed_provenance(mem_fs, data_dir, [
            {"pr_number": 100, "channel": PR_ADMISSION_CHANNEL_TEND},
            {"pr_number": 100, "channel": PR_ADMISSION_CHANNEL_TEND},
        ])
        assert _channel_summary(data_dir, n_records=0) == {
            "total_admissions": 0,
            "distinct_prs": 0,
            "readmitted_prs": 0,
        }


class TestFormatPrReadmissionCombined:
    """The single combined ``PR-READMISSION`` line and its suppression."""

    def test_empty_both_yields_empty_string(
        self, mem_fs: dict[str, str],
    ) -> None:
        # No channel re-admission (single admission) and no readmitted-merged
        # rate (empty task history) → neither term positive → no line.
        data_dir = Path("/fake/data")
        _seed_provenance(mem_fs, data_dir, [
            {"pr_number": 100, "channel": PR_ADMISSION_CHANNEL_TEND},
        ])
        line = format_pr_readmission_combined(
            JsonlPrProvenanceLedger(data_dir), [], pr_merged_at=lambda _pr: None,
        )
        assert line == ""

    def test_channel_only_renders_channel_count_with_zero_rate(
        self, mem_fs: dict[str, str],
    ) -> None:
        # PR 100 admitted twice → channel term 1; empty task history → rate 0.
        data_dir = Path("/fake/data")
        _seed_provenance(mem_fs, data_dir, [
            {"pr_number": 100, "channel": PR_ADMISSION_CHANNEL_TEND},
            {"pr_number": 100, "channel": PR_ADMISSION_CHANNEL_TEND},
        ])
        line = format_pr_readmission_combined(
            JsonlPrProvenanceLedger(data_dir), [], pr_merged_at=lambda _pr: _MERGE_AT,
        )
        assert "PR-READMISSION:" in line
        assert "1 PR(s) re-admitted" in line
        assert "0 already-merged PR(s) re-serviced" in line
        assert "rate 0.00" in line

    def test_rate_only_renders_rate_count_with_zero_channel(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Single channel admission → channel term 0; PR 200 re-serviced twice
        # and merged → rate term 1 (the inverse of the channel-only case).
        data_dir = Path("/fake/data")
        _seed_provenance(mem_fs, data_dir, [
            {"pr_number": 999, "channel": PR_ADMISSION_CHANNEL_TEND},
        ])
        tasks: list[dict[str, object]] = [
            {"goal": "Remediate PR #200: fix the thing", "timestamp": _AFTER_MERGE},
            {"goal": "Remediate PR #200: fix the thing again", "timestamp": _AFTER_MERGE},
        ]
        line = format_pr_readmission_combined(
            JsonlPrProvenanceLedger(data_dir), tasks, pr_merged_at=lambda _pr: _MERGE_AT,
        )
        assert "0 PR(s) re-admitted" in line
        assert "1 already-merged PR(s) re-serviced" in line
        assert "rate 1.00" in line

    def test_both_present_renders_both_numbers_in_one_line(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        _seed_provenance(mem_fs, data_dir, [
            {"pr_number": 100, "channel": PR_ADMISSION_CHANNEL_TEND},
            {"pr_number": 100, "channel": PR_ADMISSION_CHANNEL_TEND},
        ])
        tasks: list[dict[str, object]] = [
            {"goal": "Remediate PR #200: fix the thing", "timestamp": _AFTER_MERGE},
            {"goal": "Remediate PR #200: fix the thing again", "timestamp": _AFTER_MERGE},
        ]
        line = format_pr_readmission_combined(
            JsonlPrProvenanceLedger(data_dir), tasks, pr_merged_at=lambda _pr: _MERGE_AT,
        )
        assert "1 PR(s) re-admitted" in line
        assert "1 already-merged PR(s) re-serviced" in line
        # Both intents share one physical line — no embedded newline.
        assert "\n" not in line

    def test_pre_merge_laps_only_yields_empty_string(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Regression (#4111): with no channel re-admission, a merged PR whose
        # laps all preceded its merge must not render a readmission line.
        data_dir = Path("/fake/data")
        _seed_provenance(mem_fs, data_dir, [
            {"pr_number": 100, "channel": PR_ADMISSION_CHANNEL_TEND},
        ])
        tasks = [
            _rem(200, timestamp=_BEFORE_MERGE),
            _rem(200, timestamp=_BEFORE_MERGE),
        ]
        line = format_pr_readmission_combined(
            JsonlPrProvenanceLedger(data_dir), tasks,
            pr_merged_at=_merged_at_oracle(200),
        )
        assert line == ""

    def test_no_sdk_query_symbol_referenced(self) -> None:
        # Zero-SDK-cost invariant: the formatter/measurer module must never
        # reach for an SDK session entry point.
        import maestro.remediation_tracker as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "query_on_session" not in source
        assert "query(" not in source


class TestPrReadmissionCombinedWiring:
    """The combined line surfaces through ``format_known_state_summary`` once."""

    def test_known_state_summary_emits_exactly_one_readmission_line(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        _seed_provenance(mem_fs, data_dir, [
            {"pr_number": 100, "channel": PR_ADMISSION_CHANNEL_TEND},
            {"pr_number": 100, "channel": PR_ADMISSION_CHANNEL_TEND},
        ])
        tasks: list[dict[str, object]] = [
            {"goal": "Remediate PR #200: fix the thing", "timestamp": _AFTER_MERGE},
            {"goal": "Remediate PR #200: fix the thing again", "timestamp": _AFTER_MERGE},
        ]
        line = format_pr_readmission_combined(
            JsonlPrProvenanceLedger(data_dir), tasks, pr_merged_at=lambda _pr: _MERGE_AT,
        )
        summary = format_known_state_summary(
            {}, [], pr_readmission_context=line,
        )
        # Exactly one PR-READMISSION line, not the prior two (channel + rate).
        assert summary.count("PR-READMISSION") == 1
        assert "1 PR(s) re-admitted" in summary
        assert "1 already-merged PR(s) re-serviced" in summary

    def test_known_state_summary_omits_when_empty(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        _seed_provenance(mem_fs, data_dir, [
            {"pr_number": 100, "channel": PR_ADMISSION_CHANNEL_TEND},
        ])
        line = format_pr_readmission_combined(
            JsonlPrProvenanceLedger(data_dir), [], pr_merged_at=lambda _pr: None,
        )
        summary = format_known_state_summary(
            {}, [], pr_readmission_context=line,
        )
        assert summary == ""


# ──────────────────────────────────────────────────────────────────────
# ProvenanceRecord backend seam — JSONL adapter, SQLite parity, seed source
# ──────────────────────────────────────────────────────────────────────


class TestJsonlPrProvenanceLedger:
    """The default flag-off backend wrapping the JSONL write/read core."""

    def test_record_appends_via_log_writer(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/jsonl")
        ledger = JsonlPrProvenanceLedger(data_dir)
        ledger.record(100, PR_ADMISSION_CHANNEL_TEND)
        ledger.record(200, PR_ADMISSION_CHANNEL_TEND)
        raw = mem_fs[str(data_dir / "pending_pr_provenance.jsonl")]
        lines = [json.loads(line) for line in raw.splitlines()]
        assert lines == [
            {"pr_number": 100, "channel": PR_ADMISSION_CHANNEL_TEND},
            {"pr_number": 200, "channel": PR_ADMISSION_CHANNEL_TEND},
        ]

    def test_record_drops_non_positive_pr_number(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/jsonl-guard")
        ledger = JsonlPrProvenanceLedger(data_dir)
        ledger.record(0, PR_ADMISSION_CHANNEL_TEND)
        ledger.record(-5, PR_ADMISSION_CHANNEL_TEND)
        # The shared JSONL writer's guard drops phantom admissions entirely.
        assert str(data_dir / "pending_pr_provenance.jsonl") not in mem_fs
        assert ledger.recent(10) == []

    def test_recent_is_newest_first_within_window(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/jsonl-window")
        ledger = JsonlPrProvenanceLedger(data_dir)
        for pr_number in (1, 2, 3):
            ledger.record(pr_number, PR_ADMISSION_CHANNEL_TEND)
        got = ledger.recent(2)
        assert got == [
            ProvenanceRecord(pr_number=3, channel=PR_ADMISSION_CHANNEL_TEND),
            ProvenanceRecord(pr_number=2, channel=PR_ADMISSION_CHANNEL_TEND),
        ]

    def test_recent_non_positive_returns_empty(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/jsonl-nonpos")
        ledger = JsonlPrProvenanceLedger(data_dir)
        ledger.record(1, PR_ADMISSION_CHANNEL_TEND)
        assert ledger.recent(0) == []
        assert ledger.recent(-1) == []

    def test_recent_absent_file_returns_empty(
        self, mem_fs: dict[str, str],
    ) -> None:
        ledger = JsonlPrProvenanceLedger(Path("/fake/jsonl-absent"))
        assert ledger.recent(10) == []


def test_load_all_provenance_records_is_oldest_first(
    mem_fs: dict[str, str],
) -> None:
    """The SQLite seed source returns records in append (oldest-first) order."""
    data_dir = Path("/fake/loadall")
    log_pending_pr_provenance(data_dir, 1, "a")
    log_pending_pr_provenance(data_dir, 2, "b")
    log_pending_pr_provenance(data_dir, 3, "c")
    assert load_all_provenance_records(data_dir) == [
        ProvenanceRecord(pr_number=1, channel="a"),
        ProvenanceRecord(pr_number=2, channel="b"),
        ProvenanceRecord(pr_number=3, channel="c"),
    ]


def test_load_all_provenance_records_absent_file_is_empty() -> None:
    assert load_all_provenance_records(Path("/fake/loadall-absent")) == []


def test_jsonl_and_sqlite_agree_on_recent_and_summary(
    mem_fs: dict[str, str],
) -> None:
    """Both backends round-trip the same admissions to the same window+summary.

    Seeds an identical admission sequence into the JSONL and SQLite adapters and
    asserts ``recent(n)`` and :func:`summarize_readmission_records` match — the
    substrate-parity contract that keeps the ``PR-READMISSION`` line invariant
    across ``[paths] sqlite_state_enabled``.
    """
    data_dir = Path("/fake/parity")
    jsonl = JsonlPrProvenanceLedger(data_dir)
    sqlite = SqlitePrProvenanceLedger(StateStore(":memory:"))
    admissions = [
        (100, PR_ADMISSION_CHANNEL_TEND),
        (100, PR_ADMISSION_CHANNEL_TEND),
        (200, PR_ADMISSION_CHANNEL_TEND),
        (300, PR_ADMISSION_CHANNEL_TEND),
    ]
    for pr_number, channel in admissions:
        jsonl.record(pr_number, channel)
        sqlite.record(pr_number, channel)
    assert jsonl.recent(500) == sqlite.recent(500)
    assert summarize_readmission_records(
        jsonl.recent(500),
    ) == summarize_readmission_records(sqlite.recent(500))
    # And that shared summary is the expected channel count (PR 100 re-admitted).
    assert summarize_readmission_records(jsonl.recent(500)) == {
        "total_admissions": 4,
        "distinct_prs": 3,
        "readmitted_prs": 1,
    }


def test_jsonl_and_sqlite_agree_on_recent_with_malformed_lines(
    mem_fs: dict[str, str],
) -> None:
    """A malformed JSONL line does not diverge the two backends' windows.

    Regression for the trailing-window parity gap (proposal 106 review): the
    JSONL ``recent(n)`` takes the last *n* **valid** records, not the last *n*
    raw lines, so a corrupted line inside the window never shrinks its result
    below the SQLite backend's "last n valid rows". Seeds a malformed line among
    the valid admissions, mirrors the valid rows into SQLite via the one-time
    seed source, and asserts both windows match — with the requested window
    reaching past the corrupted line rather than stopping short at it.
    """
    data_dir = Path("/fake/parity-malformed")
    path = str(data_dir / "pending_pr_provenance.jsonl")
    mem_fs[path] = "\n".join([
        json.dumps({"pr_number": 100, "channel": PR_ADMISSION_CHANNEL_TEND}),
        "not-json",  # corrupted line inside the requested window
        json.dumps({"pr_number": 200, "channel": PR_ADMISSION_CHANNEL_TEND}),
        json.dumps({"pr_number": 300, "channel": PR_ADMISSION_CHANNEL_TEND}),
    ]) + "\n"
    jsonl = JsonlPrProvenanceLedger(data_dir)
    # The SQLite adapter seeds from the same legacy JSONL, dropping the malformed
    # line via load_all_provenance_records.
    sqlite = SqlitePrProvenanceLedger(
        StateStore(":memory:"), legacy_jsonl_dir=data_dir,
    )
    # A window of 3 returns all three VALID records on both backends — the
    # malformed line neither counts toward nor caps the window at 2.
    expected = [
        ProvenanceRecord(pr_number=300, channel=PR_ADMISSION_CHANNEL_TEND),
        ProvenanceRecord(pr_number=200, channel=PR_ADMISSION_CHANNEL_TEND),
        ProvenanceRecord(pr_number=100, channel=PR_ADMISSION_CHANNEL_TEND),
    ]
    assert jsonl.recent(3) == expected
    assert jsonl.recent(3) == sqlite.recent(3)


# ──────────────────────────────────────────────────────────────────────
# measure_pr_readmission_rate tests
# ──────────────────────────────────────────────────────────────────────


# Fixed merge time every timed oracle below reports, with lap timestamps
# either side of it — the readmission predicate is "a lap after the merge".
_MERGE_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
_BEFORE_MERGE = "2026-09-01T11:00:00+00:00"
_AFTER_MERGE = "2026-09-01T13:00:00+00:00"


def _merged_at_oracle(*merged_prs: int) -> Callable[[int], datetime | None]:
    """Timed oracle reporting :data:`_MERGE_AT` for *merged_prs*, else ``None``."""
    merged = set(merged_prs)

    def _oracle(pr: int) -> datetime | None:
        return _MERGE_AT if pr in merged else None

    return _oracle


_NEVER_MERGED_AT: Callable[[int], datetime | None] = _merged_at_oracle()


def _rem(
    pr: int, outcome: str = "success", timestamp: str | None = _AFTER_MERGE,
) -> dict[str, object]:
    """Build a ``Remediate PR #<pr>`` task record (post-merge by default)."""
    record: dict[str, object] = {
        "goal": f"Remediate PR #{pr}: Fix thing", "outcome": outcome,
    }
    if timestamp is not None:
        record["timestamp"] = timestamp
    return record


class TestMeasurePrReadmissionRate:
    """Router-level pulse: how often an already-merged PR is re-admitted."""

    def test_empty_tasks_zero_ratio(self) -> None:
        result = measure_pr_readmission_rate([], pr_merged_at=_NEVER_MERGED_AT)
        assert result == PRReadmissionRate(
            ratio=0.0, readmitted_prs=(), total_distinct_prs=0,
        )

    def test_no_remediation_entries_zero_ratio(self) -> None:
        # Non-remediation tasks are ignored; denominator stays zero.
        tasks: list[dict[str, object]] = [
            {"goal": "Refactor the planner", "outcome": "success"},
            {"title": "Add a dashboard widget", "outcome": "partial"},
        ]
        result = measure_pr_readmission_rate(tasks, pr_merged_at=_merged_at_oracle(1))
        assert result == PRReadmissionRate(
            ratio=0.0, readmitted_prs=(), total_distinct_prs=0,
        )

    def test_single_occurrence_not_readmitted(self) -> None:
        # A PR seen once cannot have been re-admitted, even if merged.
        result = measure_pr_readmission_rate(
            [_rem(100)], pr_merged_at=_merged_at_oracle(100),
        )
        assert result.ratio == 0.0
        assert result.readmitted_prs == ()
        assert result.total_distinct_prs == 1

    def test_merged_and_readmitted_surfaces(self) -> None:
        # PR #100 is merged per git yet appears as a later remediation task.
        result = measure_pr_readmission_rate(
            [_rem(100), _rem(100)], pr_merged_at=_merged_at_oracle(100),
        )
        assert result.ratio == 1.0
        assert result.readmitted_prs == (100,)
        assert result.total_distinct_prs == 1

    def test_readmitted_but_unmerged_not_flagged(self) -> None:
        # Re-appearing but NOT merged is genuine remediation churn, not the
        # readmitted-merged class this sensor measures.
        result = measure_pr_readmission_rate(
            [_rem(200), _rem(200)], pr_merged_at=_NEVER_MERGED_AT,
        )
        assert result.ratio == 0.0
        assert result.readmitted_prs == ()
        assert result.total_distinct_prs == 1

    def test_ratio_is_readmitted_over_distinct(self) -> None:
        # Two distinct PRs; only #100 is merged-and-readmitted → ratio 1/2.
        tasks = [_rem(100), _rem(100), _rem(200), _rem(200)]
        result = measure_pr_readmission_rate(
            tasks, pr_merged_at=_merged_at_oracle(100),
        )
        assert result.ratio == 0.5
        assert result.readmitted_prs == (100,)
        assert result.total_distinct_prs == 2

    def test_outcome_is_not_filtered(self) -> None:
        # Unlike the unlanded sensor, occurrences count regardless of outcome:
        # the router re-fed the PR whatever each lap was stamped.
        tasks = [
            _rem(300, outcome="failed"),
            _rem(300, outcome="investigation"),
        ]
        result = measure_pr_readmission_rate(
            tasks, pr_merged_at=_merged_at_oracle(300),
        )
        assert result.readmitted_prs == (300,)
        assert result.total_distinct_prs == 1

    def test_offending_prs_sorted_ascending(self) -> None:
        tasks = [_rem(300), _rem(100), _rem(300), _rem(100)]
        result = measure_pr_readmission_rate(
            tasks, pr_merged_at=_merged_at_oracle(100, 300),
        )
        assert result.readmitted_prs == (100, 300)
        assert result.total_distinct_prs == 2
        assert result.ratio == 1.0

    def test_title_fallback_when_goal_absent(self) -> None:
        # PR number parsed from ``title`` when ``goal`` is missing/empty.
        tasks: list[dict[str, object]] = [
            {"title": "Remediate PR #400: Fix thing", "outcome": "success"},
            {
                "goal": "", "title": "Remediate PR #400: Fix thing",
                "timestamp": _AFTER_MERGE,
            },
        ]
        result = measure_pr_readmission_rate(
            tasks, pr_merged_at=_merged_at_oracle(400),
        )
        assert result.readmitted_prs == (400,)
        assert result.total_distinct_prs == 1

    def test_records_without_goal_or_title_skipped(self) -> None:
        # A record carrying neither a usable ``goal`` nor ``title`` is skipped
        # rather than crashing the scan.
        tasks: list[dict[str, object]] = [
            {"outcome": "success"},
            {"goal": "", "title": None},
            {"goal": 42},
        ]
        result = measure_pr_readmission_rate(tasks, pr_merged_at=_NEVER_MERGED_AT)
        assert result == PRReadmissionRate(
            ratio=0.0, readmitted_prs=(), total_distinct_prs=0,
        )

    def test_oracle_cached_per_pr_and_only_for_reappearing(self) -> None:
        # The oracle is consulted once per re-appearing PR; single-occurrence
        # PRs never reach it.
        tasks = [_rem(100), _rem(100), _rem(200)]
        calls: list[int] = []

        def _counting(pr: int) -> datetime | None:
            calls.append(pr)
            return _MERGE_AT

        result = measure_pr_readmission_rate(tasks, pr_merged_at=_counting)
        assert calls == [100]
        assert result.readmitted_prs == (100,)
        assert result.total_distinct_prs == 2

    def test_oracle_failure_propagates_loudly(self) -> None:
        def _raising(_pr: int) -> datetime | None:
            raise RuntimeError("git exploded")

        with pytest.raises(RuntimeError, match="git exploded"):
            measure_pr_readmission_rate(
                [_rem(100), _rem(100)], pr_merged_at=_raising,
            )

    def test_default_oracle_is_git_pr_merged_at_default(self) -> None:
        with patch(
            "maestro.audit_checks_phantom._git_pr_merged_at_default",
            return_value=_MERGE_AT,
        ) as oracle:
            result = measure_pr_readmission_rate([_rem(100), _rem(100)])
        assert result.readmitted_prs == (100,)
        oracle.assert_called_once_with(100)

    def test_pre_merge_laps_not_readmitted(self) -> None:
        # Regression (#4111): a PR that took two laps while open and then
        # merged is the healthy case, not a readmission of a merged PR.
        tasks = [
            _rem(100, timestamp="2026-09-01T09:00:00+00:00"),
            _rem(100, timestamp=_BEFORE_MERGE),
        ]
        result = measure_pr_readmission_rate(
            tasks, pr_merged_at=_merged_at_oracle(100),
        )
        assert result.readmitted_prs == ()
        assert result.ratio == 0.0
        assert result.total_distinct_prs == 1

    def test_post_merge_lap_readmitted(self) -> None:
        # A third lap after the merge is a genuine readmission.
        tasks = [
            _rem(100, timestamp="2026-09-01T09:00:00+00:00"),
            _rem(100, timestamp=_BEFORE_MERGE),
            _rem(100, timestamp=_AFTER_MERGE),
        ]
        result = measure_pr_readmission_rate(
            tasks, pr_merged_at=_merged_at_oracle(100),
        )
        assert result.readmitted_prs == (100,)
        assert result.ratio == 1.0

    def test_lap_exactly_at_merge_not_readmitted(self) -> None:
        # "After the merge" is strict: a lap stamped at the merge instant is not.
        tasks = [
            _rem(100, timestamp=_BEFORE_MERGE),
            _rem(100, timestamp=_MERGE_AT.isoformat()),
        ]
        result = measure_pr_readmission_rate(
            tasks, pr_merged_at=_merged_at_oracle(100),
        )
        assert result.readmitted_prs == ()

    def test_untimestamped_lap_not_readmitted(self) -> None:
        # A lap with a missing or unparseable timestamp cannot prove it
        # followed the merge → fail-closed, not counted.
        tasks = [
            _rem(100, timestamp=None),
            _rem(100, timestamp="not-a-timestamp"),
        ]
        result = measure_pr_readmission_rate(
            tasks, pr_merged_at=_merged_at_oracle(100),
        )
        assert result.readmitted_prs == ()
        assert result.total_distinct_prs == 1

    def test_latest_lap_decides_regardless_of_order(self) -> None:
        # The post-merge lap need not be last in list order.
        tasks = [
            _rem(100, timestamp=_AFTER_MERGE),
            _rem(100, timestamp=_BEFORE_MERGE),
        ]
        result = measure_pr_readmission_rate(
            tasks, pr_merged_at=_merged_at_oracle(100),
        )
        assert result.readmitted_prs == (100,)

    def test_lap_straddling_merge_not_readmitted(self) -> None:
        # Recorded an hour after the merge but running for two hours → it was
        # admitted before the merge, so it is not a readmission.
        straddler = _rem(100, timestamp=_AFTER_MERGE)
        straddler["duration_secs"] = 7200.0
        tasks = [_rem(100, timestamp=_BEFORE_MERGE), straddler]
        result = measure_pr_readmission_rate(
            tasks, pr_merged_at=_merged_at_oracle(100),
        )
        assert result.readmitted_prs == ()

    def test_short_lap_admitted_after_merge_readmitted(self) -> None:
        # A duration that still places admission after the merge counts.
        lap = _rem(100, timestamp=_AFTER_MERGE)
        lap["duration_secs"] = 60
        tasks = [_rem(100, timestamp=_BEFORE_MERGE), lap]
        result = measure_pr_readmission_rate(
            tasks, pr_merged_at=_merged_at_oracle(100),
        )
        assert result.readmitted_prs == (100,)

    @pytest.mark.parametrize("duration", [-7200.0, True, "7200", None])
    def test_malformed_duration_falls_back_to_timestamp(
        self, duration: object,
    ) -> None:
        # Negative, bool, non-numeric, or null durations are ignored.
        lap = _rem(100, timestamp=_AFTER_MERGE)
        lap["duration_secs"] = duration
        tasks = [_rem(100, timestamp=_BEFORE_MERGE), lap]
        result = measure_pr_readmission_rate(
            tasks, pr_merged_at=_merged_at_oracle(100),
        )
        assert result.readmitted_prs == (100,)


# ──────────────────────────────────────────────────────────────────────
# measure_pr_readmission_rate — mem_fs-backed end-to-end tests
# ──────────────────────────────────────────────────────────────────────


class TestMeasurePrReadmissionRateMemFs:
    """Read task history from mem_fs, then measure with a mocked oracle.

    These exercise the realistic caller path — seed ``task_history.jsonl`` via
    the ``mem_fs`` fixture, read it with :func:`_read_recent_task_tail`, and
    feed the parsed records to :func:`measure_pr_readmission_rate` — so the
    file I/O, parsing, and measurement are covered together. ``pr_merged_at``
    is always a mocked oracle; no git subprocess runs.
    """

    @staticmethod
    def _tasks_from_history(data_dir: Path) -> list[dict[str, object]]:
        """Read seeded history back out of mem_fs in chronological order.

        The ``mem_fs`` fixture must be active at the call site so
        :func:`_read_recent_task_tail`'s ``open()`` resolves to the in-memory
        filesystem.
        """
        return _read_recent_task_tail(data_dir, 100)

    def test_zero_readmissions_ratio_zero(
        self, mem_fs: dict[str, str],
    ) -> None:
        # (a) Distinct PRs exist but none is both merged and re-admitted: a
        # single merged remediation (#100) and a single unmerged one (#200).
        # No PR appears twice → ratio 0.0, empty offending list.
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            _rem(100),
            _rem(200),
        ])
        tasks = self._tasks_from_history(data_dir)
        result = measure_pr_readmission_rate(
            tasks, pr_merged_at=_merged_at_oracle(100),
        )
        assert result.ratio == 0.0
        assert result.readmitted_prs == ()
        assert result.total_distinct_prs == 2

    def test_merged_pr_readmitted_surfaces_with_ratio(
        self, mem_fs: dict[str, str],
    ) -> None:
        # (b) PR #100 (merged per the mocked oracle) re-appears as a later
        # Remediate task → non-zero ratio with #100 in the offending list.
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            _rem(100, outcome="partial"),
            _rem(100, outcome="success"),
        ])
        tasks = self._tasks_from_history(data_dir)
        result = measure_pr_readmission_rate(
            tasks, pr_merged_at=_merged_at_oracle(100),
        )
        assert result.ratio == 1.0
        assert result.readmitted_prs == (100,)
        assert result.total_distinct_prs == 1

    def test_unmerged_reappearing_pr_excluded(
        self, mem_fs: dict[str, str],
    ) -> None:
        # (c) Two PRs each re-appear, but only #100 is merged. The unmerged
        # repeat (#200) is genuine churn, not a readmitted-merged lap → it is
        # excluded from the offending list; ratio is 1 of 2 distinct PRs.
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            _rem(100),
            _rem(200),
            _rem(100),
            _rem(200),
        ])
        tasks = self._tasks_from_history(data_dir)
        result = measure_pr_readmission_rate(
            tasks, pr_merged_at=_merged_at_oracle(100),
        )
        assert result.ratio == 0.5
        assert result.readmitted_prs == (100,)
        assert result.total_distinct_prs == 2

    def test_empty_history_ratio_zero(
        self, mem_fs: dict[str, str],
    ) -> None:
        # (d) Empty task history → no distinct PRs → ratio 0.0.
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [])
        tasks = self._tasks_from_history(data_dir)
        result = measure_pr_readmission_rate(tasks, pr_merged_at=_NEVER_MERGED_AT)
        assert result == PRReadmissionRate(
            ratio=0.0, readmitted_prs=(), total_distinct_prs=0,
        )

    def test_no_distinct_prs_when_history_has_no_remediations(
        self, mem_fs: dict[str, str],
    ) -> None:
        # (d) History with only non-remediation tasks → no distinct PRs parsed
        # → ratio 0.0 even though the oracle would report a merge.
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"goal": "Refactor the planner", "outcome": "success"},
            {"goal": "Tidy docs", "outcome": "partial"},
        ])
        tasks = self._tasks_from_history(data_dir)
        result = measure_pr_readmission_rate(
            tasks, pr_merged_at=_merged_at_oracle(100, 200),
        )
        assert result == PRReadmissionRate(
            ratio=0.0, readmitted_prs=(), total_distinct_prs=0,
        )


# ──────────────────────────────────────────────────────────────────────
# check_forced_read_directive tests
# ──────────────────────────────────────────────────────────────────────


class _DirectiveModel:
    """Minimal stand-in carrying a mutable ``next_task_directive`` field."""

    def __init__(self, directive: str | None = None) -> None:
        self.next_task_directive = directive


class TestCheckForcedReadDirective:
    """The unlanded sensor turned into a boundary the next task must keep."""

    def test_fires_on_threshold_and_unmerged(self) -> None:
        # A PR serviced exactly threshold times and git-unmerged → directive.
        tasks = [_rem(2554), _rem(2554)]
        directive = check_forced_read_directive(
            tasks, _DirectiveModel(), is_pr_merged=_NONE_MERGED,
        )
        assert directive is not None
        assert "#2554" in directive
        assert "no new lap" in directive
        assert len(directive) <= 200

    def test_no_fire_when_merged(self) -> None:
        # Serviced ≥ threshold but git-merge-truth shows merged → self-extinct.
        tasks = [_rem(2554), _rem(2554)]
        directive = check_forced_read_directive(
            tasks, _DirectiveModel(), is_pr_merged=_merged_oracle(2554),
        )
        assert directive is None

    def test_no_fire_when_directive_already_set(self) -> None:
        # A higher-precedence gate already set a directive → never overwrite.
        tasks = [_rem(2554), _rem(2554)]
        directive = check_forced_read_directive(
            tasks,
            _DirectiveModel("pre-existing directive"),
            is_pr_merged=_NONE_MERGED,
        )
        assert directive is None

    def test_no_fire_below_threshold(self) -> None:
        # Serviced only once (below threshold) → no directive even if unmerged.
        tasks = [_rem(2554)]
        directive = check_forced_read_directive(
            tasks, _DirectiveModel(), is_pr_merged=_NONE_MERGED,
        )
        assert directive is None

    def test_lowest_pr_chosen_when_several_qualify(self) -> None:
        # Two qualifying lineages; the oldest (lowest number) is named.
        tasks = [_rem(2554), _rem(2554), _rem(2531), _rem(2531)]
        directive = check_forced_read_directive(
            tasks, _DirectiveModel(), is_pr_merged=_NONE_MERGED,
        )
        assert directive is not None
        assert "#2531" in directive
        assert "#2554" not in directive

    def test_merged_lineage_skipped_unmerged_one_surfaces(self) -> None:
        # The lower PR is merged; the bridge skips it and names the unmerged one.
        tasks = [_rem(2531), _rem(2531), _rem(2554), _rem(2554)]
        directive = check_forced_read_directive(
            tasks, _DirectiveModel(), is_pr_merged=_merged_oracle(2531),
        )
        assert directive is not None
        assert "#2554" in directive

    def test_outcome_does_not_filter_occurrences(self) -> None:
        # Unlike the unlanded sensor, every occurrence counts regardless of
        # outcome — the question is router re-feed, not claimed progress.
        tasks = [_rem(2554, outcome="failed"), _rem(2554, outcome="failed")]
        directive = check_forced_read_directive(
            tasks, _DirectiveModel(), is_pr_merged=_NONE_MERGED,
        )
        assert directive is not None
        assert "#2554" in directive

    def test_serviced_count_rendered(self) -> None:
        # The directive reports how many times the PR was serviced.
        tasks = [_rem(2554), _rem(2554), _rem(2554)]
        directive = check_forced_read_directive(
            tasks, _DirectiveModel(), is_pr_merged=_NONE_MERGED,
        )
        assert directive is not None
        assert "serviced 3x" in directive

    def test_non_positive_threshold_returns_none(self) -> None:
        tasks = [_rem(2554), _rem(2554)]
        directive = check_forced_read_directive(
            tasks, _DirectiveModel(), is_pr_merged=_NONE_MERGED, threshold=0,
        )
        assert directive is None

    def test_non_remediation_tasks_ignored(self) -> None:
        tasks: list[dict[str, object]] = [
            {"goal": "Refactor planner", "outcome": "success"},
            {"title": "Tidy docs", "outcome": "partial"},
        ]
        directive = check_forced_read_directive(
            tasks, _DirectiveModel(), is_pr_merged=_NONE_MERGED,
        )
        assert directive is None

    def test_title_fallback_when_goal_absent(self) -> None:
        # Records carrying only a ``title`` (no ``goal``) still match.
        tasks: list[dict[str, object]] = [
            {"title": "Remediate PR #2554: Fix thing", "outcome": "success"},
            {"title": "Remediate PR #2554: Fix thing", "outcome": "partial"},
        ]
        directive = check_forced_read_directive(
            tasks, _DirectiveModel(), is_pr_merged=_NONE_MERGED,
        )
        assert directive is not None
        assert "#2554" in directive

    def test_oracle_failure_propagates_loudly(self) -> None:
        tasks = [_rem(2554), _rem(2554)]

        def _raising(_pr: int) -> bool:
            raise RuntimeError("git exploded")

        with pytest.raises(RuntimeError, match="git exploded"):
            check_forced_read_directive(
                tasks, _DirectiveModel(), is_pr_merged=_raising,
            )

    def test_oracle_is_required(self) -> None:
        # No silent fallback to the merged-only git-subject oracle (#4311).
        tasks = [_rem(2554), _rem(2554)]
        with pytest.raises(TypeError, match="is_pr_merged"):
            check_forced_read_directive(  # type: ignore[call-arg]
                tasks, _DirectiveModel(),
            )

    def test_directive_capped_at_200_chars(self) -> None:
        # A very large PR number still produces a directive within the cap.
        tasks = [_rem(999999999), _rem(999999999)]
        directive = check_forced_read_directive(
            tasks, _DirectiveModel(), is_pr_merged=_NONE_MERGED,
        )
        assert directive is not None
        assert len(directive) <= 200

    def test_records_without_goal_or_title_skipped(self) -> None:
        # A record carrying neither a string ``goal`` nor a string ``title``
        # is skipped without raising — the lineage scan tolerates junk rows.
        tasks: list[dict[str, object]] = [
            {"outcome": "success"},
            {"goal": 123, "title": None},
            _rem(2554),
            _rem(2554),
        ]
        directive = check_forced_read_directive(
            tasks, _DirectiveModel(), is_pr_merged=_NONE_MERGED,
        )
        assert directive is not None
        assert "#2554" in directive


class TestForcedReadDirectiveText:
    """The pure renderer honours the ≤200-char directive cap."""

    def test_normal_directive_under_cap(self) -> None:
        directive = _forced_read_directive_text(2554, 2)
        assert directive.startswith("PR #2554 serviced 2x")
        assert "no new lap" in directive
        assert len(directive) <= 200

    def test_oversized_directive_truncated_with_ellipsis(self) -> None:
        # Implausibly large PR number and serviced count overflow the base
        # text past the cap; the renderer truncates and appends an ellipsis.
        directive = _forced_read_directive_text(999999999999, 99)
        assert len(directive) == 200
        assert directive.endswith("…")


# ──────────────────────────────────────────────────────────────────────
# check_readmission_construction_directive tests — the dial turned to a lever
# ──────────────────────────────────────────────────────────────────────


class TestCheckReadmissionConstructionDirective:
    """The landed readmission-rate sensor actuated into a construction steer."""

    def test_fires_at_or_above_threshold(self) -> None:
        # PR #100 merged yet re-fed → ratio 1.0 ≥ 0.5 → non-empty directive.
        tasks = [_rem(100), _rem(100)]
        directive = check_readmission_construction_directive(
            tasks, ratio_threshold=0.5, pr_merged_at=_merged_at_oracle(100),
        )
        assert directive is not None
        assert "BUILD" in directive
        assert len(directive) <= 200

    def test_fires_exactly_at_threshold(self) -> None:
        # ratio 0.5 with threshold 0.5 fires (at/above, not strictly above).
        tasks = [_rem(100), _rem(100), _rem(200), _rem(200)]
        directive = check_readmission_construction_directive(
            tasks, ratio_threshold=0.5, pr_merged_at=_merged_at_oracle(100),
        )
        assert directive is not None

    def test_none_below_threshold(self) -> None:
        # ratio 0.5 with threshold 0.9 stays below → None.
        tasks = [_rem(100), _rem(100), _rem(200), _rem(200)]
        directive = check_readmission_construction_directive(
            tasks, ratio_threshold=0.9, pr_merged_at=_merged_at_oracle(100),
        )
        assert directive is None

    def test_none_on_empty_history(self) -> None:
        # No remediation tasks → undefined rate → None even at threshold 0.0.
        directive = check_readmission_construction_directive(
            [], ratio_threshold=0.0, pr_merged_at=_NEVER_MERGED_AT,
        )
        assert directive is None

    def test_none_when_no_remediation_entries(self) -> None:
        # Non-remediation tasks leave the denominator zero → None.
        tasks: list[dict[str, object]] = [
            {"goal": "Refactor the planner", "outcome": "success"},
        ]
        directive = check_readmission_construction_directive(
            tasks, ratio_threshold=0.0, pr_merged_at=_NEVER_MERGED_AT,
        )
        assert directive is None

    def test_names_example_pr(self) -> None:
        # The directive cites the lowest offending PR number.
        tasks = [_rem(100), _rem(100)]
        directive = check_readmission_construction_directive(
            tasks, ratio_threshold=0.5, pr_merged_at=_merged_at_oracle(100),
        )
        assert directive is not None
        assert "#100" in directive

    def test_none_when_all_laps_pre_merge(self) -> None:
        # Regression (#4111): a merged multi-lap PR whose laps all preceded the
        # merge is no readmission → no false "merged PRs keep re-lapping" steer.
        tasks = [
            _rem(100, timestamp=_BEFORE_MERGE),
            _rem(100, timestamp=_BEFORE_MERGE),
        ]
        directive = check_readmission_construction_directive(
            tasks, ratio_threshold=0.5, pr_merged_at=_merged_at_oracle(100),
        )
        assert directive is None

    def test_none_when_no_readmissions_at_zero_threshold(self) -> None:
        # Distinct remediated PRs exist but NONE are readmitted-merged, so
        # ``ratio`` is 0.0. At ``ratio_threshold=0.0`` the ``ratio < threshold``
        # guard alone (0.0 < 0.0 is False) would fire a directive asserting
        # "merged PRs keep re-lapping" when zero PRs were readmitted — factually
        # false. The bridge must stay silent when there is no readmission signal.
        tasks = [_rem(200), _rem(200)]
        directive = check_readmission_construction_directive(
            tasks, ratio_threshold=0.0, pr_merged_at=_NEVER_MERGED_AT,
        )
        assert directive is None


class TestReadmissionConstructionDirectiveText:
    """The pure renderer honours the ≤200-char directive cap."""

    def test_normal_directive_under_cap(self) -> None:
        rate = PRReadmissionRate(
            ratio=0.5, readmitted_prs=(100,), total_distinct_prs=2,
        )
        directive = _readmission_construction_directive_text(rate)
        assert "BUILD" in directive
        assert "#100" in directive
        assert len(directive) <= 200

    def test_no_example_when_readmitted_empty(self) -> None:
        # Defensive branch: a rate with no readmitted PRs omits the example.
        rate = PRReadmissionRate(
            ratio=0.5, readmitted_prs=(), total_distinct_prs=2,
        )
        directive = _readmission_construction_directive_text(rate)
        assert "e.g. #" not in directive

    def test_oversized_directive_truncated_with_ellipsis(self) -> None:
        # An implausibly large PR number overflows the base text past the cap;
        # the renderer truncates and appends an ellipsis.
        rate = PRReadmissionRate(
            ratio=0.5,
            readmitted_prs=(10**60,),
            total_distinct_prs=2,
        )
        directive = _readmission_construction_directive_text(rate)
        assert len(directive) == 200
        assert directive.endswith("…")
