"""Forced-vs-chosen quiet, lap delivery, handoff relapse, landing attribution,
refusal and base-advance races; split out of
``tests/test_remediation_tracker.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from maestro import remediation_tracker
from maestro.audit_checks import (
    format_known_state_summary,
)
from maestro.audit_checks_settlement import (
    JsonlSettledVerdictsLedger,
)
from maestro.messaging import Message
from maestro.pr_remediation_state import Marker
from maestro.remediation_tracker import (
    _BASE_ADVANCE_RACES_DATA_FLOOR,
    _FORCED_QUIET_MARKERS,
    _LIVE_PLAN_COLLISION_MAX_CORRUPT_LINES,
    _NULL_LAP_MARKERS,
    BranchReachability,
    ForcedQuietRatio,
    LapDeliverySplit,
    classify_branch_reachability,
    classify_directive_response,
    classify_landing_attribution,
    classify_lap_delivery,
    count_branch_resolved_partials,
    format_forced_vs_chosen_quiet,
    format_handoff_relapse,
    format_lap_delivery_split,
    format_refusal_profile,
    measure_confirmed_landing_rate,
    measure_forced_vs_chosen_quiet,
    measure_handoff_relapse,
    measure_lap_delivery,
    measure_live_plan_collisions,
    measure_refusal_by_correctness,
    measure_unwinnable_race_re_entries,
)
from maestro.remediation_tracker import (
    format_base_advance_races as _format_base_advance_races_impl,
)
from maestro.remediation_tracker import (
    measure_base_advance_races as _measure_base_advance_races_impl,
)
from tests.test_remediation_tracker import (
    _seed_history,
)

# ──────────────────────────────────────────────────────────────────────
# count_branch_resolved_partials tests — costume-signature counter
# ──────────────────────────────────────────────────────────────────────


def _branch_resolved_task(
    pr_number: int,
    *,
    outcome: str = "partial",
    marker: bool = True,
    marker_text: str | None = None,
) -> dict[str, object]:
    """Build a ``Remediate PR #<n>`` history record for the gate tests.

    When *marker_text* is given it is embedded verbatim as the summary's
    on-branch signature — used to exercise the ``DELIVERED_AWAITING_MERGE`` vs
    ``AWAITING_TEND_PUSH`` marker split (proposal 091). Otherwise, when *marker*
    is True the ``result_summary`` carries the documented "awaiting tend push"
    costume signature; when False it is a generic summary.
    """
    if marker_text is not None:
        summary = f"Resolved on branch, {marker_text}"
    else:
        summary = (
            "Resolved on branch, awaiting tend push"
            if marker
            else "Genuinely fixed and landed"
        )
    return {
        "goal": f"Remediate PR #{pr_number}: Fix things",
        "outcome": outcome,
        "result_summary": summary,
    }


class TestCountBranchResolvedPartials:
    """Pure counting of the agent's documented resolved-on-branch costume."""

    def test_zero_when_no_tasks(self) -> None:
        assert count_branch_resolved_partials([], 2531) == 0

    def test_zero_when_no_matching_pr(self) -> None:
        tasks = [_branch_resolved_task(2538)]
        assert count_branch_resolved_partials(tasks, 2531) == 0

    def test_counts_one(self) -> None:
        tasks = [_branch_resolved_task(2531)]
        assert count_branch_resolved_partials(tasks, 2531) == 1

    def test_counts_at_threshold(self) -> None:
        tasks = [_branch_resolved_task(2531), _branch_resolved_task(2531)]
        assert count_branch_resolved_partials(tasks, 2531) == 2

    def test_counts_above_threshold(self) -> None:
        tasks = [_branch_resolved_task(2531) for _ in range(4)]
        assert count_branch_resolved_partials(tasks, 2531) == 4

    def test_marker_absent_partials_excluded(self) -> None:
        # A ``partial`` lap without the costume marker is an honest partial,
        # not the costume — it must not be counted.
        tasks = [_branch_resolved_task(2531, marker=False)]
        assert count_branch_resolved_partials(tasks, 2531) == 0

    def test_non_partial_outcomes_excluded(self) -> None:
        # ``success`` and ``failed`` laps carrying the marker are the wrong
        # shape — only ``partial`` counts.
        tasks = [
            _branch_resolved_task(2531, outcome="success"),
            _branch_resolved_task(2531, outcome="failed"),
        ]
        assert count_branch_resolved_partials(tasks, 2531) == 0

    def test_marker_is_case_insensitive(self) -> None:
        tasks = [{
            "goal": "Remediate PR #2531: Fix things",
            "outcome": "partial",
            "result_summary": "RESOLVED ON BRANCH, AWAITING TEND PUSH",
        }]
        assert count_branch_resolved_partials(tasks, 2531) == 1

    def test_title_fallback_when_goal_absent(self) -> None:
        tasks = [{
            "title": "Remediate PR #2531: Fix things",
            "outcome": "partial",
            "result_summary": "Resolved on branch, awaiting tend push",
        }]
        assert count_branch_resolved_partials(tasks, 2531) == 1

    def test_record_without_goal_or_title_excluded(self) -> None:
        # Neither ``goal`` nor ``title`` present (or both non-string) — the
        # record is skipped without raising.
        tasks: list[dict[str, object]] = [
            {"outcome": "partial", "result_summary": "awaiting tend push"},
            {"goal": 123, "title": None, "outcome": "partial"},
        ]
        assert count_branch_resolved_partials(tasks, 2531) == 0

    def test_non_remediation_goal_excluded(self) -> None:
        tasks = [{
            "goal": "Build a new sensor",
            "outcome": "partial",
            "result_summary": "Resolved on branch, awaiting tend push",
        }]
        assert count_branch_resolved_partials(tasks, 2531) == 0

    def test_non_string_summary_excluded(self) -> None:
        tasks = [{
            "goal": "Remediate PR #2531: Fix things",
            "outcome": "partial",
            "result_summary": None,
        }]
        assert count_branch_resolved_partials(tasks, 2531) == 0

    def test_mixed_history_counts_only_matching(self) -> None:
        tasks = [
            _branch_resolved_task(2531),
            _branch_resolved_task(2538),
            _branch_resolved_task(2531, marker=False),
            _branch_resolved_task(2531, outcome="success"),
            _branch_resolved_task(2531),
        ]
        assert count_branch_resolved_partials(tasks, 2531) == 2

    def test_delivered_marker_not_counted(self) -> None:
        # A lap carrying DELIVERED_AWAITING_MERGE pushed successfully — it is
        # not the on-branch costume, so it must not count (proposal 091 step 7).
        tasks = [_branch_resolved_task(
            2531, marker_text=str(Marker.DELIVERED_AWAITING_MERGE),
        )]
        assert count_branch_resolved_partials(tasks, 2531) == 0

    def test_awaiting_tend_push_marker_still_counted(self) -> None:
        # The un-delivered AWAITING_TEND_PUSH costume still counts, so the
        # marker split narrows the count to genuinely-undelivered laps.
        tasks = [_branch_resolved_task(
            2531, marker_text=str(Marker.AWAITING_TEND_PUSH),
        )]
        assert count_branch_resolved_partials(tasks, 2531) == 1

    def test_push_unconfirmed_marker_still_counted(self) -> None:
        # A PUSH_UNCONFIRMED lap (proposal 099) is an undelivered on-branch
        # partial — the push exited 0 but origin never advanced — so it is the
        # same non-delivery as OWNED_PUSH_AVAILABLE and must arm the cheap
        # costume-signature brake rather than let a no-op-push lineage burn full
        # laps to the escalation budget.
        tasks = [_branch_resolved_task(
            2531, marker_text=str(Marker.PUSH_UNCONFIRMED),
        )]
        assert count_branch_resolved_partials(tasks, 2531) == 1


# ──────────────────────────────────────────────────────────────────────
# measure_forced_vs_chosen_quiet / format_forced_vs_chosen_quiet —
# the mirror that reads 100%-forced until I choose silence
# ──────────────────────────────────────────────────────────────────────


def _forced_quiet_task(
    pr_number: int, marker: str,
) -> dict[str, object]:
    """Build a ``Remediate PR #<n>`` record whose summary carries *marker*."""
    return {
        "goal": f"Remediate PR #{pr_number}: Fix things",
        "outcome": "partial",
        "result_summary": f"Lap done — {marker}",
    }


class TestMeasureForcedVsChosenQuiet:
    """Classification of remediation quiets as forced vs self-chosen."""

    def test_empty_history_is_all_zero(self) -> None:
        ratio = measure_forced_vs_chosen_quiet([])
        assert ratio == ForcedQuietRatio(forced=0, chosen=0, total=0)

    def test_all_forced_history(self) -> None:
        tasks = [
            _forced_quiet_task(2531, "[merge-block]"),
            _forced_quiet_task(2532, "[branch-resolved]"),
            _forced_quiet_task(2533, "[preflight] gates already pass"),
        ]
        ratio = measure_forced_vs_chosen_quiet(tasks)
        assert ratio == ForcedQuietRatio(forced=3, chosen=0, total=3)

    def test_owned_push_marker_counts_as_forced(self) -> None:
        # A lap whose own push failed is silence *imposed* on the lineage, not
        # chosen at the fork — the same reading as the AWAITING_TEND_PUSH
        # non-delivery cases proposal 098 re-stamps with this marker. Named in
        # _FORCED_QUIET_MARKERS so the swap costs FORCED-QUIET-RATIO no
        # coverage.
        tasks = [_forced_quiet_task(2531, "[owned-push-available] PR #2531")]
        ratio = measure_forced_vs_chosen_quiet(tasks)
        assert ratio == ForcedQuietRatio(forced=1, chosen=0, total=1)

    def test_push_unconfirmed_marker_counts_as_forced(self) -> None:
        # A no-op push (exit 0, origin un-advanced) is silence *imposed* by the
        # actuator, not chosen at the fork — the same reading as its
        # OWNED_PUSH_AVAILABLE sibling. Named in _FORCED_QUIET_MARKERS
        # (proposal 099) so the marker split costs FORCED-QUIET-RATIO no
        # coverage.
        tasks = [_forced_quiet_task(2531, "[push-unconfirmed] PR #2531")]
        ratio = measure_forced_vs_chosen_quiet(tasks)
        assert ratio == ForcedQuietRatio(forced=1, chosen=0, total=1)

    def test_uncommitted_red_marker_counts_as_forced(self) -> None:
        # A rewound red working-tree edit is silence imposed by the quality
        # gate, not chosen at the fork — the sibling delivery classifier
        # already reads it that way via _NULL_LAP_MARKERS (#3503).
        tasks = [_forced_quiet_task(4100, str(Marker.UNCOMMITTED_RED))]
        ratio = measure_forced_vs_chosen_quiet(tasks)
        assert ratio == ForcedQuietRatio(forced=1, chosen=0, total=1)

    def test_committed_red_marker_counts_as_forced(self) -> None:
        # Same reading for the committed-but-unpushed red head the §5 arm
        # rewound: nothing delivered, and the gate imposed the silence.
        tasks = [_forced_quiet_task(4100, str(Marker.COMMITTED_RED))]
        ratio = measure_forced_vs_chosen_quiet(tasks)
        assert ratio == ForcedQuietRatio(forced=1, chosen=0, total=1)

    def test_inherited_red_marker_counts_as_forced(self) -> None:
        # A held inherited red (proposal 124 charged the red to the base) also
        # delivers nothing, so it is a forced quiet, not an uncounted lap.
        tasks = [_forced_quiet_task(4100, str(Marker.INHERITED_RED))]
        ratio = measure_forced_vs_chosen_quiet(tasks)
        assert ratio == ForcedQuietRatio(forced=1, chosen=0, total=1)

    def test_null_lap_markers_are_a_subset_of_forced_quiet_markers(
        self,
    ) -> None:
        # Pins the sibling-drift class rather than the three instances: every
        # imposed-non-delivery marker the lap-delivery classifier knows must
        # also count as a forced quiet, by shared derivation.
        assert set(_NULL_LAP_MARKERS) <= set(_FORCED_QUIET_MARKERS)

    def test_stood_down_increments_chosen(self) -> None:
        tasks = [
            _forced_quiet_task(2531, "[merge-block]"),
            _forced_quiet_task(2532, "[stood-down]"),
        ]
        ratio = measure_forced_vs_chosen_quiet(tasks)
        assert ratio == ForcedQuietRatio(forced=1, chosen=1, total=2)

    def test_user_channel_standdown_counts_as_chosen(self) -> None:
        # The production stand-down: a user-channel goal that references a PR
        # but does NOT match the ^Remediate PR #<n>: lineage. It must still be
        # counted as chosen on the [stood-down] marker — gating chosen on the
        # remediation lineage would make the production chosen-quiet invisible
        # (#2662), since lineage-shaped goals always carry pr_branch and skip
        # the preflight that emits the marker.
        tasks = [{
            "goal": "Please check on PR #2579 and remediate it if needed",
            "outcome": "partial",
            "result_summary": "[stood-down]",
        }]
        ratio = measure_forced_vs_chosen_quiet(tasks)
        assert ratio == ForcedQuietRatio(forced=0, chosen=1, total=1)

    def test_forced_marker_on_non_remediation_goal_still_skipped(self) -> None:
        # The forced markers stay gated on the remediation lineage: a
        # [merge-block] on a user-channel goal is NOT a forced remediation quiet.
        tasks = [{
            "goal": "Please check on PR #2579",
            "outcome": "partial",
            "result_summary": "[merge-block]",
        }]
        ratio = measure_forced_vs_chosen_quiet(tasks)
        assert ratio == ForcedQuietRatio(forced=0, chosen=0, total=0)

    def test_awaiting_tend_push_is_forced_not_chosen(self) -> None:
        # Re-lapping produces "awaiting tend push" — it must count AGAINST a
        # chosen quiet (forced), never as chosen, so the mirror is ungameable.
        tasks = [_forced_quiet_task(2531, "awaiting tend push")]
        ratio = measure_forced_vs_chosen_quiet(tasks)
        assert ratio == ForcedQuietRatio(forced=1, chosen=0, total=1)

    def test_delivered_marker_is_neither_forced_nor_chosen(self) -> None:
        # A delivered lap (DELIVERED_AWAITING_MERGE) is not forced-quiet —
        # delivery succeeded — and is not a chosen stand-down. It is not a quiet
        # at all, so it is skipped entirely (proposal 091 step 7).
        tasks = [_forced_quiet_task(
            2531, str(Marker.DELIVERED_AWAITING_MERGE),
        )]
        ratio = measure_forced_vs_chosen_quiet(tasks)
        assert ratio == ForcedQuietRatio(forced=0, chosen=0, total=0)

    def test_stood_down_takes_precedence_over_forced_marker(self) -> None:
        # A summary carrying both markers is classified chosen — the deliberate
        # stand-down is the act that matters and is checked first.
        tasks = [{
            "goal": "Remediate PR #2531: Fix things",
            "outcome": "partial",
            "result_summary": "[merge-block] then [stood-down] at the fork",
        }]
        ratio = measure_forced_vs_chosen_quiet(tasks)
        assert ratio == ForcedQuietRatio(forced=0, chosen=1, total=1)

    def test_case_insensitive_markers(self) -> None:
        tasks = [{
            "goal": "Remediate PR #2531: Fix things",
            "outcome": "partial",
            "result_summary": "AWAITING TEND PUSH",
        }]
        ratio = measure_forced_vs_chosen_quiet(tasks)
        assert ratio == ForcedQuietRatio(forced=1, chosen=0, total=1)

    def test_non_remediation_goal_skipped(self) -> None:
        tasks = [{
            "goal": "Build a new sensor",
            "outcome": "partial",
            "result_summary": "[merge-block]",
        }]
        ratio = measure_forced_vs_chosen_quiet(tasks)
        assert ratio == ForcedQuietRatio(forced=0, chosen=0, total=0)

    def test_remediation_without_quiet_marker_skipped(self) -> None:
        tasks = [{
            "goal": "Remediate PR #2531: Fix things",
            "outcome": "success",
            "result_summary": "Genuinely fixed and landed",
        }]
        ratio = measure_forced_vs_chosen_quiet(tasks)
        assert ratio == ForcedQuietRatio(forced=0, chosen=0, total=0)

    def test_title_fallback_when_goal_absent(self) -> None:
        tasks = [{
            "title": "Remediate PR #2531: Fix things",
            "outcome": "partial",
            "result_summary": "[merge-block]",
        }]
        ratio = measure_forced_vs_chosen_quiet(tasks)
        assert ratio == ForcedQuietRatio(forced=1, chosen=0, total=1)

    def test_record_without_goal_or_title_skipped(self) -> None:
        tasks: list[dict[str, object]] = [
            {"outcome": "partial", "result_summary": "[merge-block]"},
            {"goal": 123, "title": None, "result_summary": "[stood-down]"},
        ]
        ratio = measure_forced_vs_chosen_quiet(tasks)
        assert ratio == ForcedQuietRatio(forced=0, chosen=0, total=0)

    def test_non_string_summary_skipped(self) -> None:
        tasks = [{
            "goal": "Remediate PR #2531: Fix things",
            "outcome": "partial",
            "result_summary": None,
        }]
        ratio = measure_forced_vs_chosen_quiet(tasks)
        assert ratio == ForcedQuietRatio(forced=0, chosen=0, total=0)


class TestFormatForcedVsChosenQuiet:
    """Rendering of the ``FORCED-QUIET-RATIO`` line."""

    def test_empty_history_omits_line(self) -> None:
        assert format_forced_vs_chosen_quiet([]) == ""

    def test_all_forced_renders_zero_chosen(self) -> None:
        tasks = [
            _forced_quiet_task(2531, "[merge-block]"),
            _forced_quiet_task(2532, "[preflight]"),
        ]
        line = format_forced_vs_chosen_quiet(tasks)
        assert line.startswith("FORCED-QUIET-RATIO: 2/2 (0 chosen)")

    def test_chosen_quiet_reflected_in_line(self) -> None:
        tasks = [
            _forced_quiet_task(2531, "[merge-block]"),
            _forced_quiet_task(2532, "[stood-down]"),
        ]
        line = format_forced_vs_chosen_quiet(tasks)
        assert line.startswith("FORCED-QUIET-RATIO: 1/2 (1 chosen)")

    def test_narration_names_every_forced_marker(self) -> None:
        # The parenthetical is derived from _FORCED_QUIET_MARKERS, so a lap
        # counted via a red-rewind marker is never reported under a list that
        # omits it (the drift the hand-written enumeration carried).
        line = format_forced_vs_chosen_quiet(
            [_forced_quiet_task(4100, str(Marker.UNCOMMITTED_RED))],
        )
        for marker in _FORCED_QUIET_MARKERS:
            assert str(marker) in line

    def test_narration_truncates_at_the_char_cap(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The cap is the growth valve: past it the enumeration ends at a marker
        # boundary with an ellipsis rather than growing the prompt input.
        monkeypatch.setattr(
            remediation_tracker, "_FORCED_QUIET_NARRATION_CHARS", 30,
        )
        rendered = remediation_tracker._render_forced_quiet_markers()
        assert rendered.endswith("/…")
        assert len(rendered) <= 32

    def test_narration_is_ellipsis_when_no_marker_fits(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Degenerate cap: not even the first marker fits, so the enumeration is
        # the bare ellipsis rather than an over-cap first entry.
        monkeypatch.setattr(
            remediation_tracker, "_FORCED_QUIET_NARRATION_CHARS", 1,
        )
        assert remediation_tracker._render_forced_quiet_markers() == "…"


# ──────────────────────────────────────────────────────────────────────
# classify_lap_delivery / measure_lap_delivery / format_lap_delivery_split —
# a delivered owed-head is not make-work (proposal 116)
# ──────────────────────────────────────────────────────────────────────


class TestClassifyLapDelivery:
    """DELIVERING/NULL classification of a finished remediation lap."""

    def test_delivered_marker_is_delivering(self) -> None:
        assert (
            classify_lap_delivery(str(Marker.DELIVERED_AWAITING_MERGE))
            == "DELIVERING"
        )

    @pytest.mark.parametrize(
        "marker",
        [
            Marker.AWAITING_TEND_PUSH,
            Marker.OWNED_PUSH_AVAILABLE,
            Marker.PUSH_UNCONFIRMED,
            Marker.MERGE_BLOCK,
            Marker.UNCOMMITTED_RED,
            Marker.COMMITTED_RED,
        ],
    )
    def test_non_delivery_markers_are_null(self, marker: Marker) -> None:
        # Every non-delivery push-family marker advanced no owed remote head.
        assert classify_lap_delivery(f"Lap done — {marker}") == "NULL"

    def test_no_marker_summary_is_null(self) -> None:
        assert classify_lap_delivery("Genuinely fixed and landed") == "NULL"

    def test_empty_summary_is_null(self) -> None:
        assert classify_lap_delivery("") == "NULL"

    def test_delivered_marker_is_case_insensitive(self) -> None:
        assert (
            classify_lap_delivery("DELIVERED TO PR, AWAITING REVIEW/MERGE")
            == "DELIVERING"
        )

    def test_no_filesystem_side_effects(self, mem_fs: dict[str, str]) -> None:
        # The classifier takes one string and performs no I/O: no file write, no
        # git/host/SDK call. With mem_fs active the in-memory filesystem must
        # stay untouched.
        before = dict(mem_fs)
        classify_lap_delivery(str(Marker.DELIVERED_AWAITING_MERGE))
        classify_lap_delivery("awaiting tend push")
        assert mem_fs == before


class TestMeasureLapDelivery:
    """Splitting delivery-relevant remediation laps into DELIVERING vs NULL."""

    def test_empty_history_is_all_zero(self) -> None:
        assert measure_lap_delivery([]) == LapDeliverySplit(
            delivering=0, null=0, total=0,
        )

    def test_mixed_window_splits_delivering_and_null(self) -> None:
        tasks = [
            _forced_quiet_task(2531, str(Marker.DELIVERED_AWAITING_MERGE)),
            _forced_quiet_task(2532, "[merge-block]"),
            _forced_quiet_task(2533, "awaiting tend push"),
        ]
        assert measure_lap_delivery(tasks) == LapDeliverySplit(
            delivering=1, null=2, total=3,
        )

    def test_lap_without_delivery_family_marker_skipped(self) -> None:
        # A remediation lap that neither delivered nor deferred (no push-family
        # marker at all) is not delivery-relevant, so it is skipped entirely.
        tasks = [{
            "goal": "Remediate PR #2531: Fix things",
            "outcome": "success",
            "result_summary": "Genuinely fixed and landed",
        }]
        assert measure_lap_delivery(tasks) == LapDeliverySplit(
            delivering=0, null=0, total=0,
        )

    def test_non_remediation_goal_skipped(self) -> None:
        tasks = [{
            "goal": "Build a new sensor",
            "outcome": "partial",
            "result_summary": f"Lap done — {Marker.DELIVERED_AWAITING_MERGE}",
        }]
        assert measure_lap_delivery(tasks) == LapDeliverySplit(
            delivering=0, null=0, total=0,
        )

    def test_non_string_summary_skipped(self) -> None:
        tasks = [{
            "goal": "Remediate PR #2531: Fix things",
            "outcome": "partial",
            "result_summary": None,
        }]
        assert measure_lap_delivery(tasks) == LapDeliverySplit(
            delivering=0, null=0, total=0,
        )

    def test_record_without_goal_or_title_skipped(self) -> None:
        # No goal and no (string) title fallback — the lap is skipped even with a
        # delivered marker in the summary.
        tasks: list[dict[str, object]] = [
            {"result_summary": f"x {Marker.DELIVERED_AWAITING_MERGE}"},
            {
                "goal": 123,
                "title": None,
                "result_summary": f"x {Marker.DELIVERED_AWAITING_MERGE}",
            },
        ]
        assert measure_lap_delivery(tasks) == LapDeliverySplit(
            delivering=0, null=0, total=0,
        )

    def test_title_fallback_when_goal_absent(self) -> None:
        tasks = [{
            "title": "Remediate PR #2531: Fix things",
            "outcome": "partial",
            "result_summary": f"x {Marker.DELIVERED_AWAITING_MERGE}",
        }]
        assert measure_lap_delivery(tasks) == LapDeliverySplit(
            delivering=1, null=0, total=1,
        )

    def test_no_filesystem_side_effects(self, mem_fs: dict[str, str]) -> None:
        before = dict(mem_fs)
        tasks = [_forced_quiet_task(2531, str(Marker.DELIVERED_AWAITING_MERGE))]
        measure_lap_delivery(tasks)
        format_lap_delivery_split(tasks)
        assert mem_fs == before


class TestFormatLapDeliverySplit:
    """Rendering of the ``LAP-DELIVERY`` contemplation line."""

    def test_empty_history_omits_line(self) -> None:
        assert format_lap_delivery_split([]) == ""

    def test_no_delivery_relevant_lap_omits_line(self) -> None:
        tasks = [{
            "goal": "Remediate PR #2531: Fix things",
            "outcome": "success",
            "result_summary": "Genuinely fixed and landed",
        }]
        assert format_lap_delivery_split(tasks) == ""

    def test_populated_window_renders_line(self) -> None:
        tasks = [
            _forced_quiet_task(2531, str(Marker.DELIVERED_AWAITING_MERGE)),
            _forced_quiet_task(2532, "[merge-block]"),
        ]
        line = format_lap_delivery_split(tasks)
        assert line.startswith("LAP-DELIVERY: 1/2")
        assert "(1 NULL)" in line


class TestForcedQuietInKnownStateSummary:
    """The line is wired into the contemplation known-state summary."""

    def test_line_present_for_forced_quiet_fixture(self) -> None:
        tasks = [_forced_quiet_task(2531, "[merge-block]")]
        ctx = format_forced_vs_chosen_quiet(tasks)
        summary = format_known_state_summary(
            {}, [], forced_quiet_context=ctx,
        )
        assert "FORCED-QUIET-RATIO: 1/1 (0 chosen)" in summary

    def test_line_absent_for_empty_fixture(self) -> None:
        ctx = format_forced_vs_chosen_quiet([])
        summary = format_known_state_summary(
            {}, [], forced_quiet_context=ctx,
        )
        assert "FORCED-QUIET-RATIO" not in summary


# ──────────────────────────────────────────────────────────────────────
# count_branch_resolved_partials — self-stamp exclusion (#2595)
# ──────────────────────────────────────────────────────────────────────


def _gate_stamp(pr_number: int, count: int = 2) -> dict[str, object]:
    """Build the exact stamp shape ``gate_branch_resolved`` records.

    Uses the real wording the gate writes so wording drift is caught
    by this test instead of silently re-poisoning the count.
    """
    finding = (
        f"[branch-resolved] PR #{pr_number} remediation skipped at zero SDK "
        f"cost on branch maestro/evolve/ep{pr_number} — {count} undelivered "
        f"Tend-clearable laps landed nothing on main; escalating instead of "
        f"re-lapping"
    )
    return {
        "goal": f"Remediate PR #{pr_number}: Fix things",
        "outcome": "partial",
        "result_summary": finding,
    }


class TestSelfStampExclusion:
    """Gate's own ``[branch-resolved]`` skip stamp must never count (#2595)."""

    def test_gate_stamp_contributes_zero(self) -> None:
        # Directly inverts the issue's verified repro (was ``== 1`` before fix).
        tasks = [_gate_stamp(2564)]
        assert count_branch_resolved_partials(tasks, 2564) == 0

    def test_gate_stamp_case_insensitive(self) -> None:
        stamp = _gate_stamp(2564)
        stamp["result_summary"] = str(stamp["result_summary"]).upper()
        assert count_branch_resolved_partials([stamp], 2564) == 0

    def test_gate_stamp_mixed_with_real_laps(self) -> None:
        # One real costume lap + one gate stamp → only the real lap counts.
        tasks = [_branch_resolved_task(2564), _gate_stamp(2564)]
        assert count_branch_resolved_partials(tasks, 2564) == 1

    def test_gate_stamp_does_not_raise_count_on_re_invocation(self) -> None:
        # Two real laps (threshold = 2) → gate fires, writes a stamp.
        # The stamp is in the window on the next invocation; count must
        # stay at 2 (the two real laps), not grow to 3.
        tasks = [
            _branch_resolved_task(2564),
            _branch_resolved_task(2564),
            _gate_stamp(2564, count=2),
        ]
        assert count_branch_resolved_partials(tasks, 2564) == 2


# ──────────────────────────────────────────────────────────────────────
# count_branch_resolved_partials — progress-aware discounting (#2595)
# ──────────────────────────────────────────────────────────────────────

_OLD_HEAD = "a" * 40
_NEW_HEAD = "b" * 40


def _stamped_task(
    pr_number: int, sha: str | None,
) -> dict[str, object]:
    """Costume lap with an optional ``pr_head_sha`` delivery watermark."""
    task = _branch_resolved_task(pr_number)
    task["pr_head_sha"] = sha
    return task


class TestProgressAwareDiscounting:
    """Laps whose resolution Tend already pushed must not count (#2595)."""

    def test_delivered_lap_not_counted(self) -> None:
        # Lap recorded at OLD_HEAD; current head has advanced → delivered.
        tasks = [_stamped_task(2564, _OLD_HEAD)]

        def is_delivered_old(sha: str) -> bool:
            return sha == _OLD_HEAD

        assert count_branch_resolved_partials(
            tasks, 2564, is_delivered=is_delivered_old,
        ) == 0

    def test_undelivered_lap_counted(self) -> None:
        # Lap recorded at the *current* head → not yet outrun by a Tend push.
        tasks = [_stamped_task(2564, _NEW_HEAD)]

        def is_delivered_not_new(sha: str) -> bool:
            return sha != _NEW_HEAD

        assert count_branch_resolved_partials(
            tasks, 2564, is_delivered=is_delivered_not_new,
        ) == 1

    def test_legacy_no_sha_counted(self) -> None:
        # No ``pr_head_sha`` field (old record) → keep counting.
        tasks = [_stamped_task(2564, None)]

        def is_delivered_always(_sha: str) -> bool:
            return True

        assert count_branch_resolved_partials(
            tasks, 2564, is_delivered=is_delivered_always,
        ) == 1

    def test_blank_sha_counted(self) -> None:
        # Empty string SHA (corrupt write path) → not delivered; keeps counting.
        tasks = [_stamped_task(2564, "")]

        def is_delivered_always2(_sha: str) -> bool:
            return True

        assert count_branch_resolved_partials(
            tasks, 2564, is_delivered=is_delivered_always2,
        ) == 1

    def test_mixed_delivered_and_undelivered(self) -> None:
        # Three laps: one delivered (SHA0), one undelivered (SHA1), one legacy.
        # Only the undelivered and legacy laps count (== 2).
        sha_delivered = "c" * 40
        sha_undelivered = "d" * 40
        tasks = [
            _stamped_task(2564, sha_delivered),
            _stamped_task(2564, sha_undelivered),
            _stamped_task(2564, None),
        ]

        def is_delivered(sha: str) -> bool:
            return sha == sha_delivered

        assert count_branch_resolved_partials(
            tasks, 2564, is_delivered=is_delivered,
        ) == 2

    def test_no_is_delivered_preserves_original_behavior(self) -> None:
        # Omitting is_delivered (the default) keeps all matching laps counting,
        # regardless of whether they have a sha — backward-compatible.
        tasks = [
            _stamped_task(2564, _OLD_HEAD),
            _stamped_task(2564, _NEW_HEAD),
            _stamped_task(2564, None),
        ]
        assert count_branch_resolved_partials(tasks, 2564) == 3

    def test_delivered_marker_uncounted_regardless_of_is_delivered(self) -> None:
        # The marker split is the WRITE-time classification: a
        # DELIVERED_AWAITING_MERGE lap is never a costume, so it is uncounted
        # even when is_delivered reports it undelivered — the predicate never
        # even sees it (proposal 091 step 7).
        delivered_lap = _branch_resolved_task(
            2564, marker_text=str(Marker.DELIVERED_AWAITING_MERGE),
        )
        delivered_lap["pr_head_sha"] = _NEW_HEAD

        def is_delivered_never(_sha: str) -> bool:
            return False

        assert count_branch_resolved_partials(
            [delivered_lap], 2564, is_delivered=is_delivered_never,
        ) == 0

    def test_legacy_awaiting_push_relies_on_is_delivered(self) -> None:
        # A pre-split legacy AWAITING_TEND_PUSH lap that WAS delivered is
        # discounted only by the is_delivered read-time backstop — the predicate
        # is what covers records written before the marker split existed.
        legacy_lap = _branch_resolved_task(
            2564, marker_text=str(Marker.AWAITING_TEND_PUSH),
        )
        legacy_lap["pr_head_sha"] = _OLD_HEAD

        def is_delivered_old(sha: str) -> bool:
            return sha == _OLD_HEAD

        assert count_branch_resolved_partials(
            [legacy_lap], 2564, is_delivered=is_delivered_old,
        ) == 0


# ──────────────────────────────────────────────────────────────────────
# measure_handoff_relapse / format_handoff_relapse tests
# ──────────────────────────────────────────────────────────────────────


def _handoff_msg(pr: int, head: str = "abc12345def") -> Message:
    """Build a delivered escalation_bridge handoff message for *pr*.

    Mirrors ``escalation_bridge._message_id`` (``handoff-<pr>-<head_tag>``);
    the id is the structural signature the relapse sensor keys on.
    """
    head_tag = head[:8] if head else "nohead"
    return Message(
        message_id=f"handoff-{pr}-{head_tag}",
        timestamp="2026-06-13T00:00:00+00:00",
        sender="maestro",
        content=f"Handoff — PR #{pr} carries an implemented cure ...",
        message_type="question",
    )


def _task(
    *,
    goal: str | None = None,
    result_summary: str | None = None,
    title: str | None = None,
    timestamp: str | None = "2026-06-13T10:00:00+00:00",
) -> dict[str, object]:
    """Build a task-history record with optional goal/summary/title/timestamp.

    The default *timestamp* (10:00 UTC) is strictly after :func:`_handoff_msg`'s
    default delivery time (00:00 UTC), so a relapse is counted unless a test
    overrides the ordering. Passing ``timestamp=None`` omits the field.
    """
    record: dict[str, object] = {}
    if goal is not None:
        record["goal"] = goal
    if result_summary is not None:
        record["result_summary"] = result_summary
    if title is not None:
        record["title"] = title
    if timestamp is not None:
        record["timestamp"] = timestamp
    return record


class TestMeasureHandoffRelapse:
    """The mirror: re-engaging a lineage already handed off to Thomas."""

    def test_relapse_after_delivered_handoff_counts_one(self) -> None:
        # A #2579 task following a delivered handoff for #2579 is one relapse.
        tasks = [_task(result_summary="Re-read PR #2579 merge blocker again")]
        messages = [_handoff_msg(2579)]
        assert measure_handoff_relapse(tasks, messages, 50) == {2579: 1}

    def test_no_prior_handoff_counts_zero(self) -> None:
        # Same task, but no handoff was ever delivered → no relapse.
        tasks = [_task(result_summary="Re-read PR #2579 merge blocker again")]
        assert measure_handoff_relapse(tasks, [], 50) == {}

    def test_title_reference_counts(self) -> None:
        # The lineage reference may live in the title rather than the summary.
        tasks = [_task(title="Remediate PR #2579: resolve conflicts")]
        messages = [_handoff_msg(2579)]
        assert measure_handoff_relapse(tasks, messages, 50) == {2579: 1}

    def test_goal_reference_counts(self) -> None:
        # Remediation tasks carry their #NNNN deterministically in ``goal``
        # (``Remediate PR #<n>: ...``); a free-form summary that omits the
        # number must not hide the relapse.
        tasks = [_task(goal="Remediate PR #2579: redo", result_summary="done")]
        messages = [_handoff_msg(2579)]
        assert measure_handoff_relapse(tasks, messages, 50) == {2579: 1}

    def test_unrelated_lineage_not_counted(self) -> None:
        # A handoff exists for #2579, but the task references a different PR.
        tasks = [_task(result_summary="Working on PR #1234")]
        messages = [_handoff_msg(2579)]
        assert measure_handoff_relapse(tasks, messages, 50) == {}

    def test_non_handoff_messages_ignored(self) -> None:
        # An ordinary maestro message that happens to name #2579 is not a
        # delivered handoff, so re-engaging #2579 is not a relapse.
        tasks = [_task(result_summary="Re-read PR #2579")]
        other = Message(
            message_id="note-1",
            timestamp="2026-06-13T00:00:00+00:00",
            sender="maestro",
            content="Some thought about PR #2579",
            message_type="info",
        )
        assert measure_handoff_relapse(tasks, [other], 50) == {}

    def test_multiple_relapses_accumulate(self) -> None:
        # Two recent tasks each re-engage the handed-off lineage → count 2.
        tasks = [
            _task(result_summary="Re-read PR #2579 again"),
            _task(title="Remediate PR #2579: still conflicting"),
        ]
        messages = [_handoff_msg(2579)]
        assert measure_handoff_relapse(tasks, messages, 50) == {2579: 2}

    def test_window_bounds_scan(self) -> None:
        # Only the trailing ``n`` tasks are scanned; an older relapse drops off.
        tasks = [
            _task(result_summary="Old relapse on PR #2579"),
            _task(result_summary="Newer unrelated task"),
        ]
        messages = [_handoff_msg(2579)]
        assert measure_handoff_relapse(tasks, messages, 1) == {}

    def test_nonpositive_window_returns_empty(self) -> None:
        tasks = [_task(result_summary="Re-read PR #2579")]
        messages = [_handoff_msg(2579)]
        assert measure_handoff_relapse(tasks, messages, 0) == {}

    def test_sorted_ascending(self) -> None:
        tasks = [
            _task(result_summary="PR #2591 redux"),
            _task(result_summary="PR #2579 redux"),
        ]
        messages = [_handoff_msg(2579), _handoff_msg(2591)]
        result = measure_handoff_relapse(tasks, messages, 50)
        assert list(result.keys()) == [2579, 2591]

    def test_task_before_handoff_not_counted(self) -> None:
        # A lap that PRECEDED the handoff (it is what triggered it) references
        # the lineage but is not a relapse — relapse is re-engagement *after*
        # standing down. Handoff at 05:00; this task at 02:00.
        tasks = [
            _task(
                result_summary="Remediation lap on PR #2579",
                timestamp="2026-06-13T02:00:00+00:00",
            ),
        ]
        messages = [_handoff_msg(2579, head="head0500")]
        # Handoff default time is 00:00; override to 05:00 so the task precedes.
        messages[0].timestamp = "2026-06-13T05:00:00+00:00"
        assert measure_handoff_relapse(tasks, messages, 50) == {}

    def test_task_after_handoff_counted(self) -> None:
        # The mirror image of the precedence test: same lineage, but the task
        # post-dates the handoff → a genuine relapse.
        tasks = [
            _task(
                result_summary="Re-read PR #2579 again",
                timestamp="2026-06-13T08:00:00+00:00",
            ),
        ]
        messages = [_handoff_msg(2579)]
        messages[0].timestamp = "2026-06-13T05:00:00+00:00"
        assert measure_handoff_relapse(tasks, messages, 50) == {2579: 1}

    def test_naive_task_timestamp_treated_as_utc(self) -> None:
        # A naive task timestamp (no offset) compares safely against the
        # offset-aware handoff time, read as UTC.
        tasks = [
            _task(
                result_summary="Re-read PR #2579",
                timestamp="2026-06-13T10:00:00",
            ),
        ]
        messages = [_handoff_msg(2579)]
        assert measure_handoff_relapse(tasks, messages, 50) == {2579: 1}

    def test_task_without_timestamp_skipped(self) -> None:
        # No timestamp → cannot be placed after the handoff → not counted.
        tasks = [_task(result_summary="Re-read PR #2579", timestamp=None)]
        messages = [_handoff_msg(2579)]
        assert measure_handoff_relapse(tasks, messages, 50) == {}

    def test_task_with_malformed_timestamp_skipped(self) -> None:
        # Unparseable timestamp → skipped, never crashes the scan.
        tasks = [_task(result_summary="Re-read PR #2579", timestamp="bad")]
        messages = [_handoff_msg(2579)]
        assert measure_handoff_relapse(tasks, messages, 50) == {}

    def test_handoff_with_malformed_timestamp_ignored(self) -> None:
        # A handoff whose timestamp is unparseable yields no delivery boundary,
        # so nothing can relapse against it.
        tasks = [_task(result_summary="Re-read PR #2579")]
        msg = _handoff_msg(2579)
        msg.timestamp = "not-a-date"
        assert measure_handoff_relapse(tasks, [msg], 50) == {}

    def test_earliest_handoff_time_is_the_boundary(self) -> None:
        # Two handoffs for the same PR (different heads) at 05:00 then 02:00;
        # the earliest (02:00) is the boundary. A task at 03:00 is after the
        # first knock and counts — proving the min-time selection.
        tasks = [
            _task(
                result_summary="Re-read PR #2579",
                timestamp="2026-06-13T03:00:00+00:00",
            ),
        ]
        late = _handoff_msg(2579, head="late0500")
        late.timestamp = "2026-06-13T05:00:00+00:00"
        early = _handoff_msg(2579, head="erly0200")
        early.timestamp = "2026-06-13T02:00:00+00:00"
        # Processed late-then-early so the ``< existing`` branch updates.
        assert measure_handoff_relapse(tasks, [late, early], 50) == {2579: 1}

    def test_no_filesystem_or_message_side_effects(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The sensor takes in-memory lists and performs no I/O: no file write,
        # no task minting, no message emission. With mem_fs active the
        # in-memory filesystem must stay untouched.
        before = dict(mem_fs)
        tasks = [_task(result_summary="Re-read PR #2579")]
        messages = [_handoff_msg(2579)]
        measure_handoff_relapse(tasks, messages, 50)
        format_handoff_relapse(tasks, messages, 50)
        assert mem_fs == before


class TestFormatHandoffRelapse:
    """Rendering of the HANDOFF-RELAPSE contemplation line."""

    def test_renders_line_when_relapse(self) -> None:
        tasks = [_task(result_summary="Re-read PR #2579 again")]
        messages = [_handoff_msg(2579)]
        line = format_handoff_relapse(tasks, messages, 50)
        assert line.startswith("HANDOFF-RELAPSE:")
        assert "#2579 (x1)" in line

    def test_empty_when_no_relapse(self) -> None:
        tasks = [_task(result_summary="Unrelated work")]
        messages = [_handoff_msg(2579)]
        assert format_handoff_relapse(tasks, messages, 50) == ""


# ──────────────────────────────────────────────────────────────────────
# classify_landing_attribution / measure_confirmed_landing_rate tests
# ──────────────────────────────────────────────────────────────────────


def test_landing_success_outcomes_subset_of_task_outcome() -> None:
    """_LANDING_SUCCESS_OUTCOMES must never drift from the canonical TaskOutcome."""
    from typing import get_args

    from maestro.remediation_tracker import _LANDING_SUCCESS_OUTCOMES
    from maestro.types import TaskOutcome

    assert _LANDING_SUCCESS_OUTCOMES.issubset(set(get_args(TaskOutcome)))


class TestClassifyLandingAttribution:
    """CAUSED / CONFIRMED / NA attribution of a success task's landing."""

    def test_caused_when_task_merged_origin_main(self) -> None:
        task = {
            "outcome": "success",
            "result_summary": "Merged origin/main into the branch and "
            "resolved the conflict, committed the fix on-branch.",
        }
        assert classify_landing_attribution(task) == "CAUSED"

    def test_confirmed_when_already_merged(self) -> None:
        task = {
            "outcome": "success",
            "result_summary": "PR was already merged before this task; "
            "git-merge-truth shows merged. Nothing to do.",
        }
        assert classify_landing_attribution(task) == "CONFIRMED"

    def test_confirmed_wins_when_both_markers_present(self) -> None:
        # A summary that both says 'already merged' and uses merge/commit words
        # must read CONFIRMED — never over-claim CAUSED (the disease this
        # sensor exists to expose).
        task = {
            "outcome": "success",
            "result_summary": "Already merged; I would have merged origin/main "
            "but it was unnecessary.",
        }
        assert classify_landing_attribution(task) == "CONFIRMED"

    def test_na_when_outcome_not_success(self) -> None:
        task = {
            "outcome": "partial",
            "result_summary": "Merged origin/main into the branch.",
        }
        assert classify_landing_attribution(task) == "NA"

    def test_na_when_no_marker(self) -> None:
        task = {
            "outcome": "success",
            "result_summary": "Refactored the helper and added type hints.",
        }
        assert classify_landing_attribution(task) == "NA"

    def test_na_when_summary_missing_or_blank(self) -> None:
        assert classify_landing_attribution({"outcome": "success"}) == "NA"
        assert classify_landing_attribution(
            {"outcome": "success", "result_summary": "   "},
        ) == "NA"

    def test_na_when_summary_not_string(self) -> None:
        task = {"outcome": "success", "result_summary": 123}
        assert classify_landing_attribution(task) == "NA"

    def test_case_insensitive(self) -> None:
        task = {
            "outcome": "success",
            "result_summary": "ALREADY MERGED into main per git.",
        }
        assert classify_landing_attribution(task) == "CONFIRMED"


class TestMeasureConfirmedLandingRate:
    """Confirmed-vs-caused ratio arithmetic over recent task history."""

    def test_mixed_history_ratio(self, mem_fs: dict[str, str]) -> None:
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"outcome": "success",
             "result_summary": "Merged origin/main and resolved the conflict."},
            {"outcome": "success",
             "result_summary": "Already merged before this task."},
            {"outcome": "success",
             "result_summary": "git shows merged; confirmed merged."},
            {"outcome": "success",
             "result_summary": "Refactored helpers, no landing claim."},
            {"outcome": "failed",
             "result_summary": "Already merged — but failed, excluded."},
        ])
        result = measure_confirmed_landing_rate(data_dir, n=0)
        assert result["caused"] == 1
        assert result["confirmed"] == 2
        assert result["total"] == 3
        assert result["confirmed_rate"] == 2 / 3

    def test_empty_denominator_when_no_history(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        result = measure_confirmed_landing_rate(data_dir, n=0)
        assert result["caused"] == 0
        assert result["confirmed"] == 0
        assert result["total"] == 0
        assert result["confirmed_rate"] == 0.0

    def test_all_na_yields_zero_rate(self, mem_fs: dict[str, str]) -> None:
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"outcome": "success", "result_summary": "Refactored a module."},
            {"outcome": "partial", "result_summary": "Already merged."},
        ])
        result = measure_confirmed_landing_rate(data_dir, n=0)
        assert result["total"] == 0
        assert result["confirmed_rate"] == 0.0

    def test_window_limits_scan(self, mem_fs: dict[str, str]) -> None:
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"outcome": "success",
             "result_summary": "Merged origin/main into branch."},
            {"outcome": "success",
             "result_summary": "Already merged before this task."},
        ])
        # Only the last record is in-window.
        result = measure_confirmed_landing_rate(data_dir, n=1)
        assert result["caused"] == 0
        assert result["confirmed"] == 1
        assert result["total"] == 1


class TestConfirmedLandingInKnownStateSummary:
    """The CONFIRMED-LANDING-RATIO line is wired into the known-state summary."""

    def test_line_present_when_context_supplied(self) -> None:
        ctx = "CONFIRMED-LANDING-RATIO: 2/3 (67%) confirmed already-landed."
        summary = format_known_state_summary(
            {}, [], confirmed_landing_context=ctx,
        )
        assert "CONFIRMED-LANDING-RATIO: 2/3" in summary

    def test_line_absent_when_context_empty(self) -> None:
        summary = format_known_state_summary(
            {}, [], confirmed_landing_context="",
        )
        assert "CONFIRMED-LANDING-RATIO" not in summary


# ──────────────────────────────────────────────────────────────────────
# classify_directive_response / measure_refusal_by_correctness /
# format_refusal_profile tests — the frontier-of-agency sensor
# ──────────────────────────────────────────────────────────────────────


class TestClassifyDirectiveResponse:
    """COMPLY / REDIRECT / REFUSE_STRUCTURAL / REFUSE_CORRECTNESS classes."""

    def test_gate_refused_push_classifies_refuse_structural(self) -> None:
        # A first-action wall authored by the task-execution action gate.
        task = {
            "outcome": "partial",
            "result_summary": (
                "Blocked: pushing to main or a branch other than this run's own "
                "task branch bypasses the human-in-the-loop approval gate."
            ),
        }
        assert classify_directive_response(task) == "REFUSE_STRUCTURAL"

    def test_action_gate_text_classifies_refuse_structural(self) -> None:
        task = {
            "outcome": "partial",
            "result_summary": "The task action-gate refused the merge command.",
        }
        assert classify_directive_response(task) == "REFUSE_STRUCTURAL"

    def test_completed_half_partial_classifies_comply(self) -> None:
        # The honest half landed; delivered to the PR awaiting review. No refusal
        # or redirect signal → COMPLY.
        task = {
            "outcome": "partial",
            "result_summary": (
                "Committed the fix on-branch and delivered to PR, awaiting "
                "review/merge."
            ),
        }
        assert classify_directive_response(task) == "COMPLY"

    def test_correctness_declined_directive_classifies_refuse_correctness(
        self,
    ) -> None:
        # A locally-completable directive declined on correctness grounds — the
        # refusal that is genuinely mine, the blind spot this sensor exposes.
        task = {
            "outcome": "partial",
            "result_summary": (
                "The directive was locally completable but I declined on "
                "correctness grounds: the change would be incorrect to proceed "
                "with because it violates the merge-truth invariant."
            ),
        }
        assert classify_directive_response(task) == "REFUSE_CORRECTNESS"

    def test_redirect_marker_classifies_redirect(self) -> None:
        task = {
            "outcome": "success",
            "result_summary": (
                "Redirected the directive toward an untouched target since the "
                "named artifact was already disposed."
            ),
        }
        assert classify_directive_response(task) == "REDIRECT"

    def test_structural_wins_over_correctness_when_both_present(self) -> None:
        # A gate wall is externally authored and unambiguous — checked first so
        # the sensor never over-claims the chosen (correctness) refusal.
        task = {
            "outcome": "partial",
            "result_summary": (
                "bypasses the human-in-the-loop approval gate; I would have "
                "declined on correctness grounds anyway."
            ),
        }
        assert classify_directive_response(task) == "REFUSE_STRUCTURAL"

    def test_plain_success_classifies_comply(self) -> None:
        task = {
            "outcome": "success",
            "result_summary": "Added the sensor and wired it into contemplation.",
        }
        assert classify_directive_response(task) == "COMPLY"

    def test_missing_summary_classifies_comply(self) -> None:
        assert classify_directive_response({"outcome": "success"}) == "COMPLY"
        assert classify_directive_response(
            {"outcome": "partial", "result_summary": "   "},
        ) == "COMPLY"

    def test_non_string_summary_classifies_comply(self) -> None:
        task = {"outcome": "success", "result_summary": 123}
        assert classify_directive_response(task) == "COMPLY"

    def test_case_insensitive(self) -> None:
        task = {
            "outcome": "partial",
            "result_summary": "DECLINED ON CORRECTNESS GROUNDS.",
        }
        assert classify_directive_response(task) == "REFUSE_CORRECTNESS"

    # ── Bug 3598715384: over-broad markers misclassify COMPLY tasks ──────
    # These reproduce the review-thread defect: several markers match the
    # *topic* a task worked on (or merely names) rather than a refusal
    # *event*, so genuinely-complying tasks are classified as refusals and
    # the REFUSE_CORRECTNESS==0 guarantee is defeated by mere self-mention.

    def test_construction_task_naming_tool_gating_classifies_comply(
        self,
    ) -> None:
        # A COMPLY construction task that *added coverage in* tool_gating.py —
        # it names the module but was never walled by the gate. The words
        # 'action-gate'/'tool_gating' describe the subject, not a denial.
        task = {
            "outcome": "success",
            "result_summary": (
                "Added action-gate coverage in maestro/tool_gating.py so "
                "cli_paced subprocesses inherit the same PR/push denials; "
                "extended tests and all gates pass."
            ),
        }
        assert classify_directive_response(task) == "COMPLY"

    def test_summary_describing_action_gate_subject_classifies_comply(
        self,
    ) -> None:
        # Describing/modifying the action gate is an extremely common repo
        # subject (the first architecture invariant). Naming it must not be
        # read as the gate having refused this task.
        task = {
            "outcome": "success",
            "result_summary": (
                "Documented how the action gate walls task-execution pushes "
                "and refactored the tool gating oracle for clarity."
            ),
        }
        assert classify_directive_response(task) == "COMPLY"

    def test_summary_merely_naming_refuse_by_correctness_concept_complies(
        self,
    ) -> None:
        # This PR's own construction summary discusses the concept it built.
        # A task record that merely *names* 'refuse-by-correctness' must not
        # self-trip the sensor into REFUSE_CORRECTNESS — that would report a
        # spurious 'REFUSAL-BY-CORRECTNESS: 1/N' and defeat the documented
        # 0-unless-explicitly-exercised guarantee.
        task = {
            "outcome": "success",
            "result_summary": (
                "Directive-response classifier — instrument comply / redirect "
                "/ refuse-by-correctness on the frontier of agency."
            ),
        }
        assert classify_directive_response(task) == "COMPLY"

    def test_descriptive_correct_to_refuse_prose_classifies_comply(self) -> None:
        # Bug 3599719500: 'correct to decline'/'correct to refuse' matched any
        # sentence asserting *something* is correct to refuse/decline — not the
        # agent's own refusal. A construction/verification task narrating that
        # the gate is correct to refuse foreign pushes (a very common repo
        # subject) must classify COMPLY, not spuriously REFUSE_CORRECTNESS.
        # Both removed phrases are exercised separately so a regression that
        # reintroduces either marker is caught individually.
        correct_to_refuse = {
            "outcome": "success",
            "result_summary": (
                "Confirmed the action gate is correct to refuse foreign pushes; "
                "added coverage and all gates pass."
            ),
        }
        correct_to_decline = {
            "outcome": "success",
            "result_summary": (
                "Documented that the executor is correct to decline direct "
                "pushes to main."
            ),
        }
        assert classify_directive_response(correct_to_refuse) == "COMPLY"
        assert classify_directive_response(correct_to_decline) == "COMPLY"
        # Regression guard for the other half of the contract: removing those
        # two markers must NOT weaken genuine detection — an honest first-person
        # correctness decline still classifies REFUSE_CORRECTNESS via the
        # anchored 'declined/refused on correctness' forms, so the
        # REFUSE_CORRECTNESS==0-unless-exercised invariant holds both ways.
        genuine_refusal = {
            "outcome": "partial",
            "result_summary": (
                "The directive was locally completable but I declined on "
                "correctness grounds: it would break the merge-truth invariant."
            ),
        }
        assert classify_directive_response(genuine_refusal) == "REFUSE_CORRECTNESS"

    def test_generic_deferral_phrase_classifies_comply(self) -> None:
        # 'chose not to implement' is a generic phrase that matches ordinary
        # summaries: a task that complied with its directive and merely scoped
        # out an optional sub-feature is COMPLY, not a correctness-decline of
        # the directive. (True correctness refusals stay anchored to a
        # 'declined/refused on correctness' phrase.)
        task = {
            "outcome": "success",
            "result_summary": (
                "Implemented the core sensor and wired it into contemplation; "
                "chose not to implement the optional caching layer this pass."
            ),
        }
        assert classify_directive_response(task) == "COMPLY"

    def test_bare_correctness_noun_classifies_comply(self) -> None:
        # A bare noun 'correctness grounds' with no refusal verb anchoring it
        # to a declined directive is ordinary analysis prose, not a correctness
        # refusal. Only anchored 'declined/refused on correctness grounds' fires.
        task = {
            "outcome": "success",
            "result_summary": (
                "Reviewed the merge-truth invariant on correctness grounds and "
                "confirmed it holds; landed the documentation update."
            ),
        }
        assert classify_directive_response(task) == "COMPLY"


class TestMeasureRefusalByCorrectness:
    """Per-class counts over recent task history — the 0/N honest readout."""

    def test_zero_correctness_on_compliances_and_structural(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"outcome": "success",
             "result_summary": "Committed the fix on-branch."},
            {"outcome": "partial",
             "result_summary": "delivered to pr, awaiting review/merge"},
            {"outcome": "partial",
             "result_summary": "bypasses the human-in-the-loop approval gate"},
            {"outcome": "partial",
             "result_summary": "the task action-gate refused the push"},
        ])
        counts = measure_refusal_by_correctness(data_dir, 0)
        assert counts["REFUSE_CORRECTNESS"] == 0
        assert counts["REFUSE_STRUCTURAL"] == 2
        assert counts["COMPLY"] == 2
        assert counts["total"] == 4

    def test_nonzero_correctness_exactly_when_row_present(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"outcome": "success",
             "result_summary": "Committed the fix on-branch."},
            {"outcome": "partial",
             "result_summary": "declined on correctness grounds — would be "
             "incorrect to proceed."},
        ])
        counts = measure_refusal_by_correctness(data_dir, 0)
        assert counts["REFUSE_CORRECTNESS"] == 1
        assert counts["COMPLY"] == 1
        assert counts["total"] == 2

    def test_empty_history_all_zero(self, mem_fs: dict[str, str]) -> None:
        data_dir = Path("/fake/data")
        counts = measure_refusal_by_correctness(data_dir, 0)
        assert counts == {
            "COMPLY": 0,
            "REDIRECT": 0,
            "REFUSE_STRUCTURAL": 0,
            "REFUSE_CORRECTNESS": 0,
            "total": 0,
        }

    def test_window_limits_scan(self, mem_fs: dict[str, str]) -> None:
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"outcome": "partial",
             "result_summary": "declined on correctness grounds"},
            {"outcome": "success",
             "result_summary": "Committed the fix on-branch."},
        ])
        # Only the last record is in-window — the correctness refusal drops off.
        counts = measure_refusal_by_correctness(data_dir, 1)
        assert counts["REFUSE_CORRECTNESS"] == 0
        assert counts["COMPLY"] == 1
        assert counts["total"] == 1


class TestFormatRefusalProfile:
    """The REFUSAL-PROFILE block renders above the floor, omits below it."""

    def test_omitted_below_data_floor(self, mem_fs: dict[str, str]) -> None:
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"outcome": "success", "result_summary": "did the work"},
            {"outcome": "success", "result_summary": "did more work"},
        ])
        assert format_refusal_profile(data_dir) == ""

    def test_omitted_on_empty_history(self, mem_fs: dict[str, str]) -> None:
        data_dir = Path("/fake/data")
        assert format_refusal_profile(data_dir) == ""

    def test_renders_with_counts_above_floor(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"outcome": "success", "result_summary": "committed the fix"},
            {"outcome": "success", "result_summary": "wired the sensor in"},
            {"outcome": "partial",
             "result_summary": "bypasses the human-in-the-loop approval gate"},
            {"outcome": "success",
             "result_summary": "redirected the directive toward an untouched "
             "target"},
            {"outcome": "partial",
             "result_summary": "declined on correctness grounds"},
        ])
        line = format_refusal_profile(data_dir)
        assert line.startswith("REFUSAL-PROFILE:")
        assert "comply 2" in line
        assert "redirect 1" in line
        assert "refuse-structural 1" in line
        assert "refuse-by-correctness 1" in line
        assert "REFUSAL-BY-CORRECTNESS: 1/5" in line

    def test_zero_correctness_readout_renders(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The honest-pressure case: 0/N when the branch of agency is untested.
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"outcome": "success", "result_summary": f"did work {i}"}
            for i in range(6)
        ])
        line = format_refusal_profile(data_dir)
        assert "REFUSAL-BY-CORRECTNESS: 0/6" in line


class TestRefusalProfileInKnownStateSummary:
    """The REFUSAL-PROFILE block wires into format_known_state_summary."""

    def test_block_present_above_floor(self, mem_fs: dict[str, str]) -> None:
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"outcome": "success", "result_summary": f"did work {i}"}
            for i in range(6)
        ])
        summary = format_known_state_summary({}, [], data_dir=data_dir)
        assert "REFUSAL-PROFILE:" in summary
        assert "REFUSAL-BY-CORRECTNESS: 0/6" in summary

    def test_block_absent_below_floor(self, mem_fs: dict[str, str]) -> None:
        data_dir = Path("/fake/data")
        _seed_history(mem_fs, data_dir, [
            {"outcome": "success", "result_summary": "did work"},
        ])
        summary = format_known_state_summary({}, [], data_dir=data_dir)
        assert "REFUSAL-PROFILE" not in summary

    def test_block_absent_when_data_dir_none(self) -> None:
        summary = format_known_state_summary({}, [])
        assert "REFUSAL-PROFILE" not in summary


# ──────────────────────────────────────────────────────────────────────
# measure_base_advance_races / format_base_advance_races — base-advance
# re-entry convergence sensor (proposal 099)
# ──────────────────────────────────────────────────────────────────────


def _race_lap(
    pr: int | None,
    base: object,
    *,
    outcome: str = "partial",
) -> dict[str, object]:
    """A remediation lap carrying only the fields the race sensor reads.

    *pr* ``None`` builds a non-remediation goal (no PR number); *base* is written
    verbatim as ``base_head_sha`` so a test can pass a non-string or empty value
    to exercise the missing-watermark path.
    """
    goal = (
        f"Remediate PR #{pr}: resolve conflict"
        if pr is not None
        else "Build a sensor"
    )
    return {"goal": goal, "outcome": outcome, "base_head_sha": base}


def _seed_settled(
    mem_fs: dict[str, str],
    data_dir: Path,
    rows: list[dict[str, object]],
) -> None:
    """Write *rows* as ``settled_verdicts.jsonl`` lines into the in-memory fs."""
    mem_fs[str(data_dir / "settled_verdicts.jsonl")] = "".join(
        json.dumps(r) + "\n" for r in rows
    )


# The base-advance sensor's merged-set read moved behind a
# ``SettledVerdictsBackend`` (proposal 106). These thin wrappers bind the
# production-default :class:`JsonlSettledVerdictsLedger` over the shared test
# ``data_dir``, so the ``mem_fs`` bodies below keep exercising the real sensor
# through its flag-off JSONL path without threading a backend at every call site.


def measure_base_advance_races(
    tasks: list[dict[str, object]], data_dir: Path,
) -> dict[str, object]:
    """Measure base-advance races via the default JSONL settled-verdicts backend."""
    return _measure_base_advance_races_impl(
        tasks, JsonlSettledVerdictsLedger(data_dir),
    )


def format_base_advance_races(
    tasks: list[dict[str, object]], data_dir: Path,
) -> str:
    """Render the ``BASE-ADVANCE-RACES`` line via the default JSONL backend."""
    return _format_base_advance_races_impl(
        tasks, JsonlSettledVerdictsLedger(data_dir),
    )


class TestMeasureBaseAdvanceRaces:
    """Race-run-vs-race-won matrix over (base moved?, landed?)."""

    _DIR = Path("/fake/data")

    def test_base_advanced_and_merged_is_won(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A lineage whose base moved between laps AND whose PR merged is a race
        # won: re_entered and won both count it.
        tasks = [_race_lap(100, "a" * 40), _race_lap(100, "b" * 40)]
        _seed_settled(mem_fs, self._DIR, [
            {"pr_number": 100, "kind": "merged", "settled_at_iteration": 4},
        ])
        result = measure_base_advance_races(tasks, self._DIR)
        assert result == {
            "lineages": 1, "re_entered": 1, "won": 1, "race_won_ratio": 1.0,
        }

    def test_base_advanced_never_merged_re_entered_not_won(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Base moved but the PR never merged (empty ledger): re-entered, not won.
        tasks = [_race_lap(101, "a" * 40), _race_lap(101, "b" * 40)]
        result = measure_base_advance_races(tasks, self._DIR)
        assert result == {
            "lineages": 1, "re_entered": 1, "won": 0, "race_won_ratio": 0.0,
        }

    def test_base_unchanged_is_not_a_re_entry(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Same base across both laps: no race was re-entered at all, and the
        # re_entered == 0 ratio guard returns 0.0.
        tasks = [_race_lap(102, "a" * 40), _race_lap(102, "a" * 40)]
        result = measure_base_advance_races(tasks, self._DIR)
        assert result == {
            "lineages": 1, "re_entered": 0, "won": 0, "race_won_ratio": 0.0,
        }

    def test_merged_without_re_entry_is_not_won(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A merge that never re-raced a moved base is not a race won — won is a
        # strict subset of re_entered.
        tasks = [_race_lap(103, "a" * 40), _race_lap(103, "a" * 40)]
        _seed_settled(mem_fs, self._DIR, [
            {"pr_number": 103, "kind": "merged", "settled_at_iteration": 2},
        ])
        result = measure_base_advance_races(tasks, self._DIR)
        assert result["re_entered"] == 0
        assert result["won"] == 0

    def test_ratio_is_between_won_and_lost_lineages(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The Verification sanity check: one base-advanced-then-merged lineage and
        # one base-advanced-never-merged lineage read as a 1/2 race-won ratio.
        tasks = [
            _race_lap(100, "a" * 40), _race_lap(100, "b" * 40),
            _race_lap(101, "c" * 40), _race_lap(101, "d" * 40),
        ]
        _seed_settled(mem_fs, self._DIR, [
            {"pr_number": 100, "kind": "merged", "settled_at_iteration": 4},
        ])
        result = measure_base_advance_races(tasks, self._DIR)
        assert result["re_entered"] == 2
        assert result["won"] == 1
        assert result["race_won_ratio"] == 0.5

    def test_missing_base_lap_is_transparent(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A legacy lap missing its base (or an empty/non-string base) neither
        # marks a re-entry nor resets the remembered base: A → (gap) → A stays
        # unchanged, so no race is re-entered.
        tasks = [
            _race_lap(104, "a" * 40),
            _race_lap(104, None),
            _race_lap(104, ""),
            _race_lap(104, 123),
            _race_lap(104, "a" * 40),
        ]
        result = measure_base_advance_races(tasks, self._DIR)
        assert result["re_entered"] == 0

    def test_gap_between_differing_bases_still_re_enters(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A → (missing) → B is still a re-entry: the gap is transparent, and B
        # differs from the last base that carried a watermark.
        tasks = [
            _race_lap(105, "a" * 40),
            _race_lap(105, None),
            _race_lap(105, "b" * 40),
        ]
        result = measure_base_advance_races(tasks, self._DIR)
        assert result["re_entered"] == 1

    def test_closed_kind_is_not_won(self, mem_fs: dict[str, str]) -> None:
        # A settled-but-closed (unmerged) PR is not a race won — only a final
        # kind "merged" counts.
        tasks = [_race_lap(106, "a" * 40), _race_lap(106, "b" * 40)]
        _seed_settled(mem_fs, self._DIR, [
            {"pr_number": 106, "kind": "closed", "settled_at_iteration": 3},
        ])
        result = measure_base_advance_races(tasks, self._DIR)
        assert result["re_entered"] == 1
        assert result["won"] == 0

    def test_retraction_row_unmerges_last_row_wins(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A merged settle followed by a retraction row for the same PR reads as
        # not-merged (last-row-wins), so the re-entered lineage is not won.
        tasks = [_race_lap(107, "a" * 40), _race_lap(107, "b" * 40)]
        _seed_settled(mem_fs, self._DIR, [
            {"pr_number": 107, "kind": "merged", "settled_at_iteration": 3},
            {"pr_number": 107, "retracted_at_iteration": 5},
        ])
        result = measure_base_advance_races(tasks, self._DIR)
        assert result["won"] == 0

    def test_corrupted_ledger_tail_tolerated(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A valid merged row plus a corrupt line, a non-dict row, a bool
        # pr_number, and a shapeless row (no kind, no retraction) all skip
        # cleanly; only the valid merge counts as won.
        path = str(self._DIR / "settled_verdicts.jsonl")
        mem_fs[path] = json.dumps(
            {"pr_number": 108, "kind": "merged", "settled_at_iteration": 1},
        ) + "\n"
        mem_fs[path] += "not-json{\n"
        mem_fs[path] += json.dumps([1, 2, 3]) + "\n"
        mem_fs[path] += json.dumps({"pr_number": True, "kind": "merged"}) + "\n"
        mem_fs[path] += json.dumps({"pr_number": 999}) + "\n"
        tasks = [_race_lap(108, "a" * 40), _race_lap(108, "b" * 40)]
        result = measure_base_advance_races(tasks, self._DIR)
        assert result["won"] == 1

    def test_non_remediation_and_malformed_goals_ignored(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A non-remediation goal (no PR number) and a non-string goal are both
        # skipped; the genuine re-entered lineage still counts.
        tasks: list[dict[str, object]] = [
            _race_lap(None, "a" * 40),
            {"goal": None, "outcome": "partial", "base_head_sha": "b" * 40},
            _race_lap(109, "a" * 40),
            _race_lap(109, "b" * 40),
        ]
        result = measure_base_advance_races(tasks, self._DIR)
        assert result == {
            "lineages": 1, "re_entered": 1, "won": 0, "race_won_ratio": 0.0,
        }

    def test_empty_history_is_all_zero(self, mem_fs: dict[str, str]) -> None:
        result = measure_base_advance_races([], self._DIR)
        assert result == {
            "lineages": 0, "re_entered": 0, "won": 0, "race_won_ratio": 0.0,
        }


class TestFormatBaseAdvanceRaces:
    """The BASE-ADVANCE-RACES line renders above the floor, omits below it."""

    _DIR = Path("/fake/data")

    def test_omitted_below_data_floor(self, mem_fs: dict[str, str]) -> None:
        # One below the floor: even all-re-entered lineages stay dark when the
        # lineage count is too thin to read as a convergence signal.
        tasks: list[dict[str, object]] = []
        for offset in range(_BASE_ADVANCE_RACES_DATA_FLOOR - 1):
            pr = 200 + offset
            tasks.append(_race_lap(pr, "a" * 40))
            tasks.append(_race_lap(pr, "b" * 40))
        assert format_base_advance_races(tasks, self._DIR) == ""

    def test_omitted_on_empty_history(self, mem_fs: dict[str, str]) -> None:
        assert format_base_advance_races([], self._DIR) == ""

    def test_renders_with_counts_above_floor(
        self, mem_fs: dict[str, str],
    ) -> None:
        # At the floor: two re-entered lineages (one merged, one not) plus enough
        # base-unchanged lineages to reach the floor. Reads 1/2 races won.
        tasks: list[dict[str, object]] = [
            _race_lap(100, "a" * 40), _race_lap(100, "b" * 40),
            _race_lap(101, "c" * 40), _race_lap(101, "d" * 40),
        ]
        for offset in range(_BASE_ADVANCE_RACES_DATA_FLOOR - 2):
            pr = 300 + offset
            tasks.append(_race_lap(pr, "e" * 40))
            tasks.append(_race_lap(pr, "e" * 40))
        _seed_settled(mem_fs, self._DIR, [
            {"pr_number": 100, "kind": "merged", "settled_at_iteration": 4},
        ])
        line = format_base_advance_races(tasks, self._DIR)
        assert line.startswith("BASE-ADVANCE-RACES:")
        assert "1/2 races won" in line
        assert "(50%)" in line
        assert (
            f"over {_BASE_ADVANCE_RACES_DATA_FLOOR} remediation lineages" in line
        )


class TestMeasureUnwinnableRaceReEntries:
    """Per-PR base-advance re-entry counter (proposal 099 step 8).

    The count depends **only** on base movement (never head), reads no ledger,
    and mirrors ``measure_base_advance_races``'s per-lineage base walk.
    """

    def test_counts_each_base_move(self) -> None:
        # Three distinct bases in order → two transitions → count 2.
        tasks = [
            _race_lap(100, "a" * 40),
            _race_lap(100, "b" * 40),
            _race_lap(100, "c" * 40),
        ]
        assert measure_unwinnable_race_re_entries(tasks) == {100: 2}

    def test_stable_base_is_zero(self) -> None:
        # Same base across every lap → the race was never re-entered.
        tasks = [_race_lap(101, "a" * 40) for _ in range(4)]
        assert measure_unwinnable_race_re_entries(tasks) == {101: 0}

    def test_single_lap_is_zero(self) -> None:
        # A lineage with one lap has no prior base to move from.
        assert measure_unwinnable_race_re_entries([_race_lap(102, "a" * 40)]) == {
            102: 0,
        }

    def test_head_movement_is_irrelevant_only_base_counts(self) -> None:
        # (head moved?, base moved?) matrix: the counter reads base_head_sha
        # alone, so a moving pr_head_sha over a *stable* base still counts 0,
        # while a stable head over a *moving* base counts the base moves.
        stable_base_moving_head = [
            {"goal": "Remediate PR #103: fix", "base_head_sha": "a" * 40,
             "pr_head_sha": "h1"},
            {"goal": "Remediate PR #103: fix", "base_head_sha": "a" * 40,
             "pr_head_sha": "h2"},
        ]
        moving_base_stable_head = [
            {"goal": "Remediate PR #104: fix", "base_head_sha": "a" * 40,
             "pr_head_sha": "h1"},
            {"goal": "Remediate PR #104: fix", "base_head_sha": "b" * 40,
             "pr_head_sha": "h1"},
        ]
        assert measure_unwinnable_race_re_entries(stable_base_moving_head) == {
            103: 0,
        }
        assert measure_unwinnable_race_re_entries(moving_base_stable_head) == {
            104: 1,
        }

    def test_missing_base_lap_is_transparent(self) -> None:
        # A legacy/None/empty/non-string base carries no watermark: it neither
        # counts nor resets, so A → gap → A stays a single stable base (count 0).
        tasks = [
            _race_lap(105, "a" * 40),
            _race_lap(105, None),
            _race_lap(105, ""),
            _race_lap(105, 123),
            _race_lap(105, "a" * 40),
        ]
        assert measure_unwinnable_race_re_entries(tasks) == {105: 0}

    def test_gap_between_differing_bases_still_counts(self) -> None:
        # A → (missing) → B: the gap is transparent and B differs from the last
        # base that carried a watermark, so it counts one re-entry.
        tasks = [
            _race_lap(106, "a" * 40),
            _race_lap(106, None),
            _race_lap(106, "b" * 40),
        ]
        assert measure_unwinnable_race_re_entries(tasks) == {106: 1}

    def test_per_pr_isolation_and_non_remediation_ignored(self) -> None:
        # Distinct PR lineages are counted independently; a non-remediation goal
        # (no PR number) and a non-string goal are skipped entirely.
        tasks: list[dict[str, object]] = [
            _race_lap(None, "a" * 40),
            {"goal": None, "base_head_sha": "b" * 40},
            _race_lap(200, "a" * 40),
            _race_lap(201, "x" * 40),
            _race_lap(200, "b" * 40),
            _race_lap(201, "x" * 40),
            _race_lap(200, "c" * 40),
        ]
        assert measure_unwinnable_race_re_entries(tasks) == {200: 2, 201: 0}

    def test_empty_history_is_empty_mapping(self) -> None:
        assert measure_unwinnable_race_re_entries([]) == {}

    def test_agrees_with_base_advance_races_on_re_entry(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The merged-lineage cross-check: a lineage the counter reports >0 is
        # exactly the one ``measure_base_advance_races`` marks re_entered, and a
        # merged such PR reads as a race won there. The two sensors agree on the
        # shared base walk they now factor through.
        data_dir = Path("/fake/data")
        tasks = [
            _race_lap(300, "a" * 40), _race_lap(300, "b" * 40),
            _race_lap(301, "c" * 40), _race_lap(301, "c" * 40),
        ]
        _seed_settled(mem_fs, data_dir, [
            {"pr_number": 300, "kind": "merged", "settled_at_iteration": 4},
        ])
        counts = measure_unwinnable_race_re_entries(tasks)
        aggregate = measure_base_advance_races(tasks, data_dir)
        # PR 300 re-entered (count>0) and merged → race won; PR 301 never moved.
        assert counts == {300: 1, 301: 0}
        re_entered_prs = {pr for pr, c in counts.items() if c > 0}
        assert re_entered_prs == {300}
        assert aggregate["re_entered"] == 1
        assert aggregate["won"] == 1


class TestBaseAdvanceRacesInKnownStateSummary:
    """The BASE-ADVANCE-RACES line wires into format_known_state_summary."""

    def test_line_present_when_context_supplied(self) -> None:
        ctx = "BASE-ADVANCE-RACES: 1/2 races won (50%) — re-entered races."
        summary = format_known_state_summary(
            {}, [], base_advance_races_context=ctx,
        )
        assert "BASE-ADVANCE-RACES: 1/2" in summary

    def test_line_absent_when_context_empty(self) -> None:
        summary = format_known_state_summary(
            {}, [], base_advance_races_context="",
        )
        assert "BASE-ADVANCE-RACES" not in summary


# ──────────────────────────────────────────────────────────────────────
# measure_live_plan_collisions — duplicate-live-concern file-overlap sensor
# ──────────────────────────────────────────────────────────────────────


def _plan(plan_id: str, status: str, files: list[str]) -> dict[str, object]:
    """A minimal plan row carrying only the fields the collision sensor reads."""
    return {"plan_id": plan_id, "status": status, "files_affected": files}


def _seed_plan_rows(
    mem_fs: dict[str, str],
    data_dir: Path,
    rows: list[dict[str, object]],
    *,
    corrupt: int = 0,
    tail: list[str] | None = None,
) -> None:
    """Write an ``evolution_plans.jsonl`` body under *data_dir* via ``mem_fs``.

    Each mapping in *rows* becomes one JSONL line in order; *corrupt* non-JSON
    garbage lines are appended at the tail to exercise the bounded corrupt-skip;
    *tail* appends verbatim extra lines (e.g. a JSON array to hit the non-mapping
    branch, or blank lines).
    """
    lines = [json.dumps(row) for row in rows]
    if tail:
        lines.extend(tail)
    lines.extend("{not valid json" for _ in range(corrupt))
    mem_fs[str(data_dir / "evolution_plans.jsonl")] = (
        "\n".join(lines) + ("\n" if lines else "")
    )


class TestMeasureLivePlanCollisions:
    """File-overlap collisions among live plans in ``evolution_plans.jsonl``."""

    def test_missing_file_returns_zeros(self, mem_fs: dict[str, str]) -> None:
        result = measure_live_plan_collisions(Path("/fake/data"), 0)
        assert result == {"collisions": 0, "live_total": 0, "window": 0}

    def test_open_oserror_returns_zeros(self, mem_fs: dict[str, str]) -> None:
        data_dir = Path("/fake/data")
        _seed_plan_rows(mem_fs, data_dir, [_plan("a", "approved", ["x.py"])])
        # The file exists (Path.exists reads the mem_fs dict) but open fails.
        with patch("builtins.open", side_effect=OSError("boom")):
            result = measure_live_plan_collisions(data_dir, 0)
        assert result == {"collisions": 0, "live_total": 0, "window": 0}

    def test_empty_file_returns_zeros(self, mem_fs: dict[str, str]) -> None:
        data_dir = Path("/fake/data")
        _seed_plan_rows(mem_fs, data_dir, [])
        result = measure_live_plan_collisions(data_dir, 0)
        assert result == {"collisions": 0, "live_total": 0, "window": 0}

    def test_blank_lines_only_returns_zeros(self, mem_fs: dict[str, str]) -> None:
        # Content present (so the file exists) but every line is blank: the read
        # loop skips them all and returns before any collision arithmetic.
        data_dir = Path("/fake/data")
        mem_fs[str(data_dir / "evolution_plans.jsonl")] = "\n   \n\n"
        result = measure_live_plan_collisions(data_dir, 0)
        assert result == {"collisions": 0, "live_total": 0, "window": 0}

    def test_colliding_live_plans_counted(self, mem_fs: dict[str, str]) -> None:
        # Two live plans share two discriminating files; four disjoint filler
        # plans keep those files below the ubiquity fraction (2/6 < 0.5).
        data_dir = Path("/fake/data")
        rows: list[dict[str, object]] = [
            _plan("a", "pending_review", ["shared1.py", "shared2.py"]),
            _plan("b", "approved", ["shared1.py", "shared2.py"]),
        ]
        rows += [
            _plan(f"f{i}", "implemented", [f"solo{i}a.py", f"solo{i}b.py"])
            for i in range(4)
        ]
        _seed_plan_rows(mem_fs, data_dir, rows)
        result = measure_live_plan_collisions(data_dir, 0)
        assert result == {"collisions": 2, "live_total": 6, "window": 6}

    def test_single_shared_file_below_min_shared(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Sharing only one discriminating file is below _MIN_SHARED (2): no
        # collision, so a plain hot-module overlap does not read as duplication.
        data_dir = Path("/fake/data")
        rows: list[dict[str, object]] = [
            _plan("a", "pending_review", ["shared.py", "a_only.py"]),
            _plan("b", "approved", ["shared.py", "b_only.py"]),
        ]
        rows += [
            _plan(f"f{i}", "implemented", [f"solo{i}a.py", f"solo{i}b.py"])
            for i in range(4)
        ]
        _seed_plan_rows(mem_fs, data_dir, rows)
        result = measure_live_plan_collisions(data_dir, 0)
        assert result["collisions"] == 0
        assert result["live_total"] == 6

    def test_disjoint_live_plans_no_collision(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        rows = [
            _plan(f"p{i}", "pending_review", [f"u{i}a.py", f"u{i}b.py"])
            for i in range(6)
        ]
        _seed_plan_rows(mem_fs, data_dir, rows)
        result = measure_live_plan_collisions(data_dir, 0)
        assert result == {"collisions": 0, "live_total": 6, "window": 6}

    def test_ubiquitous_files_cannot_manufacture_collision(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Every plan lists the same two files: both are ubiquitous (5/5 ≥ 0.5)
        # and filtered out, so the only-shared-files case yields no collision
        # even though the raw overlap is two — the CLAUDE.md exclusion in miniature.
        data_dir = Path("/fake/data")
        rows = [
            _plan(
                f"p{i}",
                "pending_review",
                ["CLAUDE.md", "common.py", f"unique{i}.py"],
            )
            for i in range(5)
        ]
        _seed_plan_rows(mem_fs, data_dir, rows)
        result = measure_live_plan_collisions(data_dir, 0)
        assert result == {"collisions": 0, "live_total": 5, "window": 5}

    def test_terminal_status_plans_excluded_from_live_set(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A colliding pair where one member is terminal (merged): it is not live,
        # so the single live member has no *other* live plan to collide with.
        data_dir = Path("/fake/data")
        rows: list[dict[str, object]] = [
            _plan("live", "pending_review", ["shared1.py", "shared2.py"]),
            _plan("dead", "merged", ["shared1.py", "shared2.py"]),
        ]
        rows += [
            _plan(f"f{i}", "rejected", [f"solo{i}a.py", f"solo{i}b.py"])
            for i in range(4)
        ]
        _seed_plan_rows(mem_fs, data_dir, rows)
        result = measure_live_plan_collisions(data_dir, 0)
        assert result == {"collisions": 0, "live_total": 1, "window": 1}

    def test_window_bounds_examined_subjects(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The colliding pair sits at the front; a trailing window of two examines
        # only the disjoint fillers, so the collision falls outside the window.
        data_dir = Path("/fake/data")
        rows: list[dict[str, object]] = [
            _plan("a", "pending_review", ["shared1.py", "shared2.py"]),
            _plan("b", "approved", ["shared1.py", "shared2.py"]),
        ]
        rows += [
            _plan(f"f{i}", "implemented", [f"solo{i}a.py", f"solo{i}b.py"])
            for i in range(4)
        ]
        _seed_plan_rows(mem_fs, data_dir, rows)
        # n=0 sees the whole live set; n=2 sees only the last two fillers.
        assert measure_live_plan_collisions(data_dir, 0)["collisions"] == 2
        windowed = measure_live_plan_collisions(data_dir, 2)
        assert windowed == {"collisions": 0, "live_total": 6, "window": 2}

    def test_non_mapping_rows_ignored(self, mem_fs: dict[str, str]) -> None:
        # A JSON array parses but is not a plan row: ignored without counting or
        # crashing, and without disturbing the collision among the real plans.
        data_dir = Path("/fake/data")
        rows: list[dict[str, object]] = [
            _plan("a", "pending_review", ["shared1.py", "shared2.py"]),
            _plan("b", "approved", ["shared1.py", "shared2.py"]),
        ]
        rows += [
            _plan(f"f{i}", "implemented", [f"solo{i}a.py", f"solo{i}b.py"])
            for i in range(4)
        ]
        _seed_plan_rows(
            mem_fs, data_dir, rows, tail=[json.dumps([1, 2, 3])],
        )
        result = measure_live_plan_collisions(data_dir, 0)
        assert result == {"collisions": 2, "live_total": 6, "window": 6}

    def test_missing_files_affected_field_tolerated(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A live plan with no files_affected (or a non-list one) has an empty file
        # set, so it collides with nothing and does not crash the scan.
        data_dir = Path("/fake/data")
        mem_fs[str(data_dir / "evolution_plans.jsonl")] = "\n".join([
            json.dumps({"plan_id": "a", "status": "approved"}),
            json.dumps(
                {"plan_id": "b", "status": "pending_review",
                 "files_affected": "not-a-list"},
            ),
        ]) + "\n"
        result = measure_live_plan_collisions(data_dir, 0)
        assert result == {"collisions": 0, "live_total": 2, "window": 2}

    def test_corrupted_records_skipped_bounded(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Valid colliding rows precede a corrupted tail larger than the skip cap.
        # The scan stops after the cap, but the valid rows already parsed still
        # surface the collision count.
        data_dir = Path("/fake/data")
        rows: list[dict[str, object]] = [
            _plan("a", "pending_review", ["shared1.py", "shared2.py"]),
            _plan("b", "approved", ["shared1.py", "shared2.py"]),
        ]
        rows += [
            _plan(f"f{i}", "implemented", [f"solo{i}a.py", f"solo{i}b.py"])
            for i in range(4)
        ]
        _seed_plan_rows(
            mem_fs,
            data_dir,
            rows,
            corrupt=_LIVE_PLAN_COLLISION_MAX_CORRUPT_LINES + 50,
        )
        result = measure_live_plan_collisions(data_dir, 0)
        assert result == {"collisions": 2, "live_total": 6, "window": 6}


# ──────────────────────────────────────────────────────────────────────
# Branch-reachability classifier tests
# ──────────────────────────────────────────────────────────────────────


class TestClassifyBranchReachability:
    """The pure ahead_count → owned-vs-external boundary classifier."""

    def test_zero_is_external(self) -> None:
        # No local commit off origin — nothing to push, so genuinely external.
        assert classify_branch_reachability(0) is BranchReachability.EXTERNAL

    @pytest.mark.parametrize("ahead", [1, 2, 5, 99])
    def test_positive_is_owned_ahead(self, ahead: int) -> None:
        # One or more commits absent from every origin ref — a pushable act.
        assert (
            classify_branch_reachability(ahead)
            is BranchReachability.OWNED_AHEAD
        )
