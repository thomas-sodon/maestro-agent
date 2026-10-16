"""Tests for the consecutive-no-progress lap sensor (proposal 089 step 4).

``pr_readiness_improved`` and ``measure_no_progress_laps`` define a *productive*
remediation lap — one that cleared a freshly-arrived blocker — and count the
current run of laps that cleared nothing, so a PR churning *with* progress (the
healthy churn of a fast main) never accumulates while a genuinely stuck one does.
``mem_fs`` only — no real filesystem, per Testing Principles.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from maestro.escalation_bridge import (
    JsonEscalationLedger,
    UnlandedCureHandoff,
    detect_unlanded_cure_handoff,
)
from maestro.remediation_tracker import (
    format_pr_churn_summary,
    measure_greenness_stall,
    measure_no_progress_laps,
    pr_readiness_improved,
)

_DIR = Path("/fake/np")

#: The default JSON escalation backend the detector reads standdowns through
#: (proposal 106); reads/writes ``_DIR/escalation_handoffs.json``.
_BE = JsonEscalationLedger(_DIR)


def _unsettled(pr: int) -> bool:
    """Settlement oracle reporting every PR as live (unmerged, open)."""
    return False


def _snap(
    *,
    ready: bool = False,
    threads: int = 0,
    overflow: int = 0,
    ci: str = "GREEN",
    behind: bool = False,
    conflicting: bool = False,
) -> dict[str, object]:
    return {
        "ready_and_waiting": ready,
        "unresolved_threads": threads,
        "unaddressed_overflow": overflow,
        "ci_state": ci,
        "branch_behind": behind,
        "branch_conflicting": conflicting,
    }


def _seed(mem_fs: dict[str, str], records: list[dict[str, object]]) -> None:
    path = str(_DIR / "task_history.jsonl")
    mem_fs[path] = "".join(json.dumps(r) + "\n" for r in records)


def _lap(
    pr: int, snapshot: dict[str, object] | None, *, head: str = "h0",
) -> dict[str, object]:
    rec: dict[str, object] = {
        "task_id": f"t-{pr}-{head}",
        "goal": f"Remediate PR #{pr}: fix the thing",
        "outcome": "partial",
        "pr_head_sha": head,
    }
    if snapshot is not None:
        rec["pr_readiness_snapshot"] = snapshot
    return rec


# --- pr_readiness_improved ---------------------------------------------------


def test_improved_on_ready_and_waiting() -> None:
    assert pr_readiness_improved(_snap(threads=2), _snap(ready=True)) is True


def test_improved_on_fewer_threads() -> None:
    assert pr_readiness_improved(_snap(threads=3), _snap(threads=1)) is True


def test_improved_on_fewer_overflow() -> None:
    assert pr_readiness_improved(_snap(overflow=2), _snap(overflow=0)) is True


def test_improved_on_ci_advancing_to_green() -> None:
    assert pr_readiness_improved(
        _snap(ci="FAILING"), _snap(ci="GREEN"),
    ) is True


def test_improved_on_conflict_resolved() -> None:
    assert pr_readiness_improved(
        _snap(conflicting=True), _snap(conflicting=False),
    ) is True


def test_improved_on_behind_resolved() -> None:
    assert pr_readiness_improved(
        _snap(behind=True), _snap(behind=False),
    ) is True


def test_not_improved_on_identical_snapshots() -> None:
    assert pr_readiness_improved(_snap(threads=2), _snap(threads=2)) is False


def test_not_improved_when_blocker_grows() -> None:
    assert pr_readiness_improved(_snap(threads=1), _snap(threads=2)) is False


def test_improved_defensive_on_missing_keys() -> None:
    # A malformed snapshot defaults to the no-blocker reading and never falsely
    # claims progress.
    assert pr_readiness_improved({}, {}) is False


# --- measure_no_progress_laps ------------------------------------------------


def test_no_cycles_returns_empty(mem_fs: dict[str, str]) -> None:
    _seed(mem_fs, [_lap(7, _snap(threads=1))])
    assert measure_no_progress_laps(_DIR, 0) == {}


def test_identical_laps_accumulate(mem_fs: dict[str, str]) -> None:
    _seed(mem_fs, [_lap(7, _snap(threads=1)) for _ in range(4)])
    assert measure_no_progress_laps(_DIR, 50) == {7: 4}


def test_clearing_a_blocker_each_lap_never_accumulates(
    mem_fs: dict[str, str],
) -> None:
    # threads 3 -> 2 -> 1 -> 0: every lap clears one, so the run never grows.
    _seed(
        mem_fs,
        [
            _lap(7, _snap(threads=3)),
            _lap(7, _snap(threads=2)),
            _lap(7, _snap(threads=1)),
            _lap(7, _snap(threads=0, ci="GREEN")),
        ],
    )
    assert measure_no_progress_laps(_DIR, 50) == {}


def test_ready_and_waiting_lap_resets_run(mem_fs: dict[str, str]) -> None:
    _seed(
        mem_fs,
        [
            _lap(7, _snap(threads=1)),
            _lap(7, _snap(threads=1)),
            _lap(7, _snap(ready=True)),
        ],
    )
    assert measure_no_progress_laps(_DIR, 50) == {}


def test_head_advance_alone_does_not_reset(mem_fs: dict[str, str]) -> None:
    # Three identical no-progress laps, the last on a fresh head (h1). ``run()``
    # self-pushes a commit per lap, so a new ``pr_head_sha`` proves nothing about
    # readiness — the run keeps accumulating across the head change (#2895).
    _seed(
        mem_fs,
        [
            _lap(7, _snap(threads=1), head="h0"),
            _lap(7, _snap(threads=1), head="h0"),
            _lap(7, _snap(threads=1), head="h1"),
        ],
    )
    assert measure_no_progress_laps(_DIR, 50) == {7: 3}


def test_head_advance_without_readiness_progress_accumulates(
    mem_fs: dict[str, str],
) -> None:
    # A self-pushed treadmill: a fresh head SHA every lap but an identical,
    # non-improving snapshot. The count must climb to the lap total, not
    # oscillate at 1 — otherwise the progress-aware budget could never fire on
    # exactly the SHA-advancing-but-unlanded treadmill it targets (#2895).
    _seed(
        mem_fs,
        [
            _lap(7, _snap(threads=1), head="h0"),
            _lap(7, _snap(threads=1), head="h1"),
            _lap(7, _snap(threads=1), head="h2"),
            _lap(7, _snap(threads=1), head="h3"),
        ],
    )
    assert measure_no_progress_laps(_DIR, 50) == {7: 4}


def test_readiness_progress_resets_across_head_advance(
    mem_fs: dict[str, str],
) -> None:
    # Head advances every lap *and* readiness improves (threads 3 -> 2 -> 1 -> 0):
    # it is the readiness progress, not the head movement, that resets — so the
    # healthy churn of a fast main never accumulates (acceptance criterion 2).
    _seed(
        mem_fs,
        [
            _lap(7, _snap(threads=3), head="h0"),
            _lap(7, _snap(threads=2), head="h1"),
            _lap(7, _snap(threads=1), head="h2"),
            _lap(7, _snap(threads=0, ci="GREEN"), head="h3"),
        ],
    )
    assert measure_no_progress_laps(_DIR, 50) == {}


def test_laps_without_snapshot_are_skipped(mem_fs: dict[str, str]) -> None:
    _seed(
        mem_fs,
        [
            _lap(7, _snap(threads=1)),
            _lap(7, None),  # legacy record — no snapshot
            _lap(7, _snap(threads=1)),
        ],
    )
    # The two snapshot-bearing laps are identical (no progress) → run of 2.
    assert measure_no_progress_laps(_DIR, 50) == {7: 2}


def test_multiple_prs_independent(mem_fs: dict[str, str]) -> None:
    _seed(
        mem_fs,
        [
            _lap(7, _snap(threads=1)),
            _lap(8, _snap(threads=2)),
            _lap(7, _snap(threads=1)),
            _lap(8, _snap(threads=1)),  # PR 8 improved -> resets
        ],
    )
    assert measure_no_progress_laps(_DIR, 50) == {7: 2}


# --- real sensor -> progress-aware handoff (end-to-end) ----------------------


def test_real_sensor_drives_progress_aware_handoff(
    mem_fs: dict[str, str],
) -> None:
    # End-to-end over the *real* ``measure_no_progress_laps`` — not a literal
    # ``{100: 4}`` dict as the mocked cases in test_escalation_bridge use, which
    # cannot catch the inversion (#2895): four SHA-advancing laps with an
    # identical non-improving snapshot accumulate to 4, so the merge-or-close
    # handoff fires once the run reaches the lap budget.
    _seed(
        mem_fs,
        [_lap(100, _snap(threads=1), head=f"h{i}") for i in range(4)],
    )
    laps = measure_no_progress_laps(_DIR, 50)
    assert laps == {100: 4}
    handoff = detect_unlanded_cure_handoff(
        _DIR,
        backend=_BE, threshold=4,
        is_pr_merged=lambda _pr: False,
        no_progress_laps=laps,
    )
    assert handoff == UnlandedCureHandoff(
        pr_number=100, head_sha="h3", stamp_count=4,
    )


def test_real_sensor_healthy_churn_yields_no_handoff(
    mem_fs: dict[str, str],
) -> None:
    # The complement: a PR clearing a blocker every SHA-advancing lap never
    # accumulates, so the real sensor reports an empty run and no handoff fires
    # even well past the gross budget.
    _seed(
        mem_fs,
        [
            _lap(100, _snap(threads=3), head="h0"),
            _lap(100, _snap(threads=2), head="h1"),
            _lap(100, _snap(threads=1), head="h2"),
            _lap(100, _snap(threads=0, ci="GREEN"), head="h3"),
        ],
    )
    laps = measure_no_progress_laps(_DIR, 50)
    assert laps == {}
    assert (
        detect_unlanded_cure_handoff(
            _DIR,
            backend=_BE, threshold=1,
            is_pr_merged=lambda _pr: False,
            no_progress_laps=laps,
        )
        is None
    )


# --- measure_greenness_stall -------------------------------------------------


def test_greenness_stall_no_cycles_empty(mem_fs: dict[str, str]) -> None:
    _seed(mem_fs, [_lap(7, _snap(conflicting=True))])
    assert measure_greenness_stall(_DIR, 0) == {}


def test_greenness_stall_all_conflicting_zero_gain(
    mem_fs: dict[str, str],
) -> None:
    # Acceptance criterion 4: an all-CONFLICTING window nets zero greenness gain
    # — the mechanical signature of a same-severity stall.
    _seed(mem_fs, [_lap(7, _snap(conflicting=True)) for _ in range(3)])
    assert measure_greenness_stall(_DIR, 50) == {7: 0}


def test_greenness_stall_rotation_nonzero_gain(
    mem_fs: dict[str, str],
) -> None:
    # Acceptance criterion 4: CONFLICTING -> CI_FAILING nets +1 — the blocker
    # rotated one class greener (toward code-clearable work).
    _seed(
        mem_fs,
        [_lap(7, _snap(conflicting=True)), _lap(7, _snap(ci="FAILING"))],
    )
    assert measure_greenness_stall(_DIR, 50) == {7: 1}


def test_greenness_stall_threads_to_green_gain(
    mem_fs: dict[str, str],
) -> None:
    # THREADS_UNRESOLVED -> GREEN (threads cleared, CI green) nets +1.
    _seed(
        mem_fs,
        [_lap(7, _snap(threads=2)), _lap(7, _snap(threads=0, ci="GREEN"))],
    )
    assert measure_greenness_stall(_DIR, 50) == {7: 1}


def test_greenness_stall_overflow_counts_as_threads(
    mem_fs: dict[str, str],
) -> None:
    # An unaddressed overflow finding alone ranks THREADS_UNRESOLVED; clearing it
    # to green nets +1, so overflow is a code-clearable blocker like a thread.
    _seed(
        mem_fs,
        [_lap(7, _snap(overflow=3)), _lap(7, _snap(overflow=0))],
    )
    assert measure_greenness_stall(_DIR, 50) == {7: 1}


def test_greenness_stall_regression_negative(mem_fs: dict[str, str]) -> None:
    # CI_FAILING -> CONFLICTING is a severity regression: nets -1.
    _seed(
        mem_fs,
        [_lap(7, _snap(ci="FAILING")), _lap(7, _snap(conflicting=True))],
    )
    assert measure_greenness_stall(_DIR, 50) == {7: -1}


def test_greenness_stall_single_lap_zero(mem_fs: dict[str, str]) -> None:
    # One lap: no movement observed yet -> zero gain.
    _seed(mem_fs, [_lap(7, _snap(conflicting=True))])
    assert measure_greenness_stall(_DIR, 50) == {7: 0}


def test_greenness_stall_skips_laps_without_snapshot(
    mem_fs: dict[str, str],
) -> None:
    # A snapshot-less lap between two snapshot-bearing ones is skipped, so the
    # CONFLICTING -> CI_FAILING movement still nets +1.
    _seed(
        mem_fs,
        [
            _lap(7, _snap(conflicting=True)),
            _lap(7, None),
            _lap(7, _snap(ci="FAILING")),
        ],
    )
    assert measure_greenness_stall(_DIR, 50) == {7: 1}


def test_greenness_stall_goal_shape_branches(mem_fs: dict[str, str]) -> None:
    # Exercises the goal/title fallback and both skip guards: a record with no
    # ``goal`` falls back to ``title`` (and still matches), a record with neither
    # is skipped, and a non-remediation goal is skipped — so only the two
    # title-matched PR-7 laps count (CONFLICTING -> CI_FAILING = +1).
    _seed(
        mem_fs,
        [
            {
                "title": "Remediate PR #7: fix",
                "pr_readiness_snapshot": _snap(conflicting=True),
            },
            {"pr_readiness_snapshot": _snap(conflicting=True)},
            {
                "goal": "Investigate something else",
                "pr_readiness_snapshot": _snap(conflicting=True),
            },
            {
                "title": "Remediate PR #7: fix",
                "pr_readiness_snapshot": _snap(ci="FAILING"),
            },
        ],
    )
    assert measure_greenness_stall(_DIR, 50) == {7: 1}


def test_greenness_stall_multiple_prs_independent(
    mem_fs: dict[str, str],
) -> None:
    _seed(
        mem_fs,
        [
            _lap(7, _snap(conflicting=True)),
            _lap(8, _snap(conflicting=True)),
            _lap(7, _snap(conflicting=True)),
            _lap(8, _snap(ci="FAILING")),
        ],
    )
    assert measure_greenness_stall(_DIR, 50) == {7: 0, 8: 1}


# --- format_pr_churn_summary -------------------------------------------------


def test_churn_summary_pr_ready_line(mem_fs: dict[str, str]) -> None:
    history = [_lap(7, _snap(ready=True))]
    _seed(mem_fs, history)
    out = format_pr_churn_summary(
        history, {}, 6, _DIR, settled_prs=frozenset,
        is_pr_settled=_unsettled,
    )
    assert "PR-READY #7" in out
    assert "PR-STUCK" not in out
    assert "PR-ROTATING" not in out


def test_churn_summary_pr_stuck_line_at_budget(mem_fs: dict[str, str]) -> None:
    history = [_lap(7, _snap(threads=1))]
    _seed(mem_fs, history)
    out = format_pr_churn_summary(
        history, {7: 6}, 6, _DIR, settled_prs=frozenset,
        is_pr_settled=_unsettled,
    )
    assert "PR-STUCK #7" in out
    assert "6 consecutive" in out
    assert "PR-READY" not in out


def test_churn_summary_pr_stuck_all_conflicting(
    mem_fs: dict[str, str],
) -> None:
    # Acceptance criterion 3: an all-CONFLICTING lineage past the budget with no
    # greenness gain reads PR-STUCK (stalled -> retire), never PR-ROTATING.
    history = [_lap(7, _snap(conflicting=True)) for _ in range(3)]
    _seed(mem_fs, history)
    out = format_pr_churn_summary(
        history, {7: 6}, 6, _DIR, settled_prs=frozenset,
        is_pr_settled=_unsettled,
    )
    assert "PR-STUCK #7" in out
    assert "no greenness gain" in out
    assert "PR-ROTATING" not in out


def test_churn_summary_pr_rotating_line(mem_fs: dict[str, str]) -> None:
    # Acceptance criterion 3: a CONFLICTING -> CI_FAILING lineage reads
    # PR-ROTATING (blocker went greener -> fix the red), never PR-STUCK.
    history = [_lap(7, _snap(conflicting=True)), _lap(7, _snap(ci="FAILING"))]
    _seed(mem_fs, history)
    out = format_pr_churn_summary(
        history, {}, 6, _DIR, settled_prs=frozenset,
        is_pr_settled=_unsettled,
    )
    assert "PR-ROTATING #7" in out
    assert "strictly greener" in out
    assert "PR-STUCK" not in out


def test_churn_summary_ready_lineage_no_rotating(
    mem_fs: dict[str, str],
) -> None:
    # A multi-lap CONFLICTING -> ready lineage telescopes to a positive net
    # greenness gain, yet the latest snapshot is ready_and_waiting: PR-ROTATING
    # must NOT fire (there is no red left to clear), only PR-READY. Guards the
    # PR-READY / PR-ROTATING contradiction for a healthy trajectory that lands
    # on GREEN.
    history = [_lap(7, _snap(conflicting=True)), _lap(7, _snap(ready=True))]
    _seed(mem_fs, history)
    out = format_pr_churn_summary(
        history, {}, 6, _DIR, settled_prs=frozenset,
        is_pr_settled=_unsettled,
    )
    assert "PR-READY #7" in out
    assert "PR-ROTATING" not in out


def test_churn_summary_rotating_outranks_stuck_at_budget(
    mem_fs: dict[str, str],
) -> None:
    # Even past the no-progress budget, a PR whose blocker rotated greener reads
    # PR-ROTATING, not PR-STUCK — the two states are mutually exclusive on gain.
    history = [_lap(7, _snap(conflicting=True)), _lap(7, _snap(ci="FAILING"))]
    _seed(mem_fs, history)
    out = format_pr_churn_summary(
        history, {7: 9}, 6, _DIR, settled_prs=frozenset,
        is_pr_settled=_unsettled,
    )
    assert "PR-ROTATING #7" in out
    assert "PR-STUCK" not in out


def test_churn_summary_stuck_below_budget_silent(
    mem_fs: dict[str, str],
) -> None:
    history = [_lap(7, _snap(threads=1))]
    _seed(mem_fs, history)
    out = format_pr_churn_summary(
        history, {7: 5}, 6, _DIR, settled_prs=frozenset,
        is_pr_settled=_unsettled,
    )
    assert out == ""


def test_churn_summary_healthy_churn_yields_neither(
    mem_fs: dict[str, str],
) -> None:
    # Latest snapshot is not ready, the PR is absent from no_progress_laps (it
    # cleared a blocker each lap), and its blocker stayed the same severity class
    # (threads throughout) → no greenness gain → none of the loop lines.
    history = [_lap(7, _snap(threads=2)), _lap(7, _snap(threads=1))]
    _seed(mem_fs, history)
    out = format_pr_churn_summary(
        history, {}, 6, _DIR, settled_prs=frozenset,
        is_pr_settled=_unsettled,
    )
    assert out == ""


def test_churn_summary_disabled_budget_suppresses_stuck(
    mem_fs: dict[str, str],
) -> None:
    history = [_lap(7, _snap(threads=1))]
    _seed(mem_fs, history)
    out = format_pr_churn_summary(
        history, {7: 9}, 0, _DIR, settled_prs=frozenset,
        is_pr_settled=_unsettled,
    )
    assert out == ""


def test_churn_summary_uses_latest_snapshot_per_pr(
    mem_fs: dict[str, str],
) -> None:
    # An earlier ready snapshot then a later not-ready one → not PR-READY.
    history = [_lap(7, _snap(ready=True)), _lap(7, _snap(threads=1))]
    _seed(mem_fs, history)
    out = format_pr_churn_summary(
        history, {}, 6, _DIR, settled_prs=frozenset,
        is_pr_settled=_unsettled,
    )
    assert "PR-READY" not in out


def test_churn_summary_skips_laps_without_snapshot(
    mem_fs: dict[str, str],
) -> None:
    # A remediation lap carrying no readiness snapshot (legacy / non-089 record)
    # is skipped, so it contributes no PR-READY line on its own.
    history = [_lap(7, None)]
    _seed(mem_fs, history)
    out = format_pr_churn_summary(
        history, {}, 6, _DIR, settled_prs=frozenset,
        is_pr_settled=_unsettled,
    )
    assert out == ""


def test_churn_summary_goal_shape_branches(mem_fs: dict[str, str]) -> None:
    # The latest-snapshot loop's goal/title fallback and skip guards: a record
    # missing ``goal`` falls back to ``title`` (matches → PR-READY), while a
    # record with neither and a non-remediation goal are both skipped.
    history: list[dict[str, object]] = [
        {"title": "Remediate PR #7: fix", "pr_readiness_snapshot": _snap(ready=True)},
        {"pr_readiness_snapshot": _snap(ready=True)},
        {"goal": "Investigate something", "pr_readiness_snapshot": _snap(ready=True)},
    ]
    _seed(mem_fs, history)
    out = format_pr_churn_summary(history, {}, 6, _DIR, settled_prs=frozenset, is_pr_settled=_unsettled)
    assert "PR-READY #7" in out


# --- format_pr_churn_summary: merge-truth settlement filter (#4596) ----------


def _rotating(pr: int) -> list[dict[str, object]]:
    return [_lap(pr, _snap(conflicting=True)), _lap(pr, _snap(ci="FAILING"))]


def test_churn_summary_settled_pr_suppresses_rotating(mem_fs: dict[str, str]) -> None:
    history = _rotating(7)
    _seed(mem_fs, history)
    out = format_pr_churn_summary(history, {}, 6, _DIR, settled_prs=frozenset, is_pr_settled=lambda n: True)
    assert out == ""


def test_churn_summary_unsettled_pr_still_rotates(mem_fs: dict[str, str]) -> None:
    history = _rotating(7)
    _seed(mem_fs, history)
    out = format_pr_churn_summary(history, {}, 6, _DIR, settled_prs=frozenset, is_pr_settled=lambda n: False)
    assert "PR-ROTATING #7" in out


def test_churn_summary_settled_pr_suppresses_ready(mem_fs: dict[str, str]) -> None:
    history = [_lap(7, _snap(ready=True))]
    _seed(mem_fs, history)
    out = format_pr_churn_summary(history, {}, 6, _DIR, settled_prs=frozenset, is_pr_settled=lambda n: True)
    assert out == ""


def test_churn_summary_settled_pr_suppresses_stuck(mem_fs: dict[str, str]) -> None:
    history = [_lap(7, _snap(threads=1))]
    _seed(mem_fs, history)
    out = format_pr_churn_summary(
        history, {7: 6}, 6, _DIR, settled_prs=frozenset, is_pr_settled=lambda n: True,
    )
    assert out == ""


def test_churn_summary_settled_filter_is_per_pr(mem_fs: dict[str, str]) -> None:
    history = _rotating(7) + _rotating(8)
    _seed(mem_fs, history)
    out = format_pr_churn_summary(
        history, {}, 6, _DIR, settled_prs=frozenset, is_pr_settled=lambda n: n == 7,
    )
    assert "PR-ROTATING #8" in out
    assert "PR-ROTATING #7" not in out


def test_churn_summary_oracle_cached_and_scoped(mem_fs: dict[str, str]) -> None:
    # PR 7 is a READY candidate and a STUCK candidate (budgeted, no gain);
    # PR 9 is quiet. The oracle is queried once for 7 and never for 9.
    history = [_lap(7, _snap(ready=True)), _lap(9, _snap(threads=1))]
    _seed(mem_fs, history)
    calls: list[int] = []

    def oracle(n: int) -> bool:
        calls.append(n)
        return False

    out = format_pr_churn_summary(history, {7: 6}, 6, _DIR, settled_prs=frozenset, is_pr_settled=oracle)
    assert "PR-READY #7" in out
    assert "PR-STUCK #7" in out
    assert calls == [7]


def test_churn_summary_oracle_error_propagates(mem_fs: dict[str, str]) -> None:
    history = _rotating(7)
    _seed(mem_fs, history)

    def oracle(n: int) -> bool:
        raise RuntimeError("host down")

    with pytest.raises(RuntimeError):
        format_pr_churn_summary(history, {}, 6, _DIR, settled_prs=frozenset, is_pr_settled=oracle)


def test_churn_summary_settled_prs_suppress_all_lines(
    mem_fs: dict[str, str],
) -> None:
    history = [_lap(77, _snap(ready=True)), _lap(88, _snap(threads=1))]
    _seed(mem_fs, history)
    out = format_pr_churn_summary(
        history, {88: 6}, 6, _DIR, settled_prs=lambda: frozenset({77, 88}), is_pr_settled=_unsettled,
    )
    assert out == ""


def test_churn_summary_settled_ready_pr_skipped_open_stuck_kept(
    mem_fs: dict[str, str],
) -> None:
    history = [_lap(77, _snap(ready=True)), _lap(88, _snap(threads=1))]
    _seed(mem_fs, history)
    out = format_pr_churn_summary(
        history, {88: 6}, 6, _DIR, settled_prs=lambda: frozenset({77}), is_pr_settled=_unsettled,
    )
    assert "PR-STUCK #88" in out
    assert "PR-READY #77" not in out


def test_churn_summary_settled_rotating_pr_skipped(
    mem_fs: dict[str, str],
) -> None:
    history = [_lap(7, _snap(conflicting=True)), _lap(7, _snap(ci="FAILING"))]
    _seed(mem_fs, history)
    control = format_pr_churn_summary(history, {}, 6, _DIR, settled_prs=frozenset, is_pr_settled=_unsettled)
    assert "PR-ROTATING #7" in control
    out = format_pr_churn_summary(history, {}, 6, _DIR, settled_prs=lambda: frozenset({7}), is_pr_settled=_unsettled)
    assert "PR-ROTATING" not in out


def test_churn_summary_quiet_block_skips_settled_read(
    mem_fs: dict[str, str],
) -> None:
    history = [_lap(7, _snap(threads=1))]
    _seed(mem_fs, history)
    thunk = MagicMock(return_value=frozenset())
    out = format_pr_churn_summary(history, {7: 5}, 6, _DIR, settled_prs=thunk, is_pr_settled=_unsettled)
    assert out == ""
    assert thunk.call_count == 0


def test_churn_summary_rendering_block_reads_settled_once(
    mem_fs: dict[str, str],
) -> None:
    history = [_lap(77, _snap(ready=True)), _lap(88, _snap(threads=1))]
    _seed(mem_fs, history)
    thunk = MagicMock(return_value=frozenset())
    out = format_pr_churn_summary(history, {88: 6}, 6, _DIR, settled_prs=thunk, is_pr_settled=_unsettled)
    assert "PR-READY #77" in out
    assert "PR-STUCK #88" in out
    assert thunk.call_count == 1


def test_churn_summary_ledger_settled_pr_skips_oracle(
    mem_fs: dict[str, str],
) -> None:
    # A PR the settlement ledger already names never reaches the live oracle;
    # a PR the ledger misses is still settled by it (#4596 over #4586).
    history = _rotating(7) + _rotating(8)
    _seed(mem_fs, history)
    calls: list[int] = []

    def oracle(n: int) -> bool:
        calls.append(n)
        return True

    out = format_pr_churn_summary(
        history, {}, 6, _DIR,
        settled_prs=lambda: frozenset({7}), is_pr_settled=oracle,
    )
    assert out == ""
    assert calls == [8]
