"""Tests for :mod:`maestro.pr_owed_act` — the per-open-PR owed-act classifier."""

from __future__ import annotations

import pytest

from maestro import ci_diagnosis, remediation_tracker
from maestro.ci_diagnosis import CheckRun
from maestro.pr_owed_act import (
    OwedAct,
    check_runs_from_ci_state,
    check_runs_from_conclusion,
    classify_pr_owed_act,
    derive_owed_act_blocker,
    describe_owed_act,
    owed_act_from_readiness_snapshot,
)


def _failing() -> tuple[CheckRun, ...]:
    return (CheckRun(name="quality", conclusion="failure"),)


def _running() -> tuple[CheckRun, ...]:
    return (CheckRun(name="quality", conclusion="in_progress"),)


def _green() -> tuple[CheckRun, ...]:
    return (CheckRun(name="quality", conclusion="success"),)


# ── the four owned-PR mappings (criterion 2) ────────────────────────────────


def test_concluded_failing_ci_is_code_owed() -> None:
    """An external merge blocker + concluded-failing CI → I owe the code fix."""
    act = classify_pr_owed_act(
        101,
        blocker=remediation_tracker.MERGE_BLOCK_CI_FAILING,
        check_runs=_failing(),
        settled_pr_numbers=frozenset(),
    )
    assert act is OwedAct.CODE_OWED


def test_all_green_unapproved_is_handoff_ready() -> None:
    """Green CI whose sole remaining gate is approval → hand off to Thomas."""
    act = classify_pr_owed_act(
        102,
        blocker=remediation_tracker.MERGE_BLOCK_AWAITING_APPROVAL,
        check_runs=_green(),
        settled_pr_numbers=frozenset(),
    )
    assert act is OwedAct.HANDOFF_READY


def test_clean_green_is_handoff_ready() -> None:
    """A clean merge state with green CI is also handoff-ready (NONE blocker)."""
    act = classify_pr_owed_act(
        103,
        blocker=remediation_tracker.MERGE_BLOCK_NONE,
        check_runs=_green(),
        settled_pr_numbers=frozenset(),
    )
    assert act is OwedAct.HANDOFF_READY


def test_ci_running_is_wait_ci() -> None:
    """CI still running (the treadmill state) → wait the clock, do not remediate."""
    act = classify_pr_owed_act(
        104,
        blocker=remediation_tracker.MERGE_BLOCK_CI_PENDING,
        check_runs=_running(),
        settled_pr_numbers=frozenset(),
    )
    assert act is OwedAct.WAIT_CI


def test_empty_check_runs_is_wait_ci() -> None:
    """No concluded checks at all reads as RUNNING → WAIT_CI."""
    act = classify_pr_owed_act(
        105,
        blocker=remediation_tracker.MERGE_BLOCK_CI_PENDING,
        check_runs=(),
        settled_pr_numbers=frozenset(),
    )
    assert act is OwedAct.WAIT_CI


def test_external_behind_main_is_external_blocked() -> None:
    """A green branch behind main (Tend's mechanical catch-up) → external gate."""
    act = classify_pr_owed_act(
        106,
        blocker=remediation_tracker.MERGE_BLOCK_BEHIND_MAIN,
        check_runs=_green(),
        settled_pr_numbers=frozenset(),
    )
    assert act is OwedAct.EXTERNAL_BLOCKED


# ── owned merge blockers short-cut to CODE_OWED regardless of CI ─────────────


def test_owned_conflict_is_code_owed_even_when_green() -> None:
    """An owned blocker (conflict) is my code to write, independent of CI state."""
    act = classify_pr_owed_act(
        107,
        blocker=remediation_tracker.MERGE_BLOCK_CONFLICTING,
        check_runs=_green(),
        settled_pr_numbers=frozenset(),
    )
    assert act is OwedAct.CODE_OWED


def test_owned_threads_unresolved_is_code_owed() -> None:
    act = classify_pr_owed_act(
        108,
        blocker=remediation_tracker.MERGE_BLOCK_THREADS_UNRESOLVED,
        check_runs=_running(),
        settled_pr_numbers=frozenset(),
    )
    assert act is OwedAct.CODE_OWED


# ── unresolved review threads promote an approval/clean blocker to CODE_OWED ──


def test_green_approval_with_open_thread_is_code_owed() -> None:
    """Green+approval-wait with an open review thread is owned code, not handoff."""
    act = classify_pr_owed_act(
        109,
        blocker=remediation_tracker.MERGE_BLOCK_AWAITING_APPROVAL,
        check_runs=_green(),
        settled_pr_numbers=frozenset(),
        unresolved_threads=1,
    )
    assert act is OwedAct.CODE_OWED


def test_green_clean_with_open_thread_is_code_owed() -> None:
    """A clean+green PR with an open review thread is owned code, not handoff."""
    act = classify_pr_owed_act(
        110,
        blocker=remediation_tracker.MERGE_BLOCK_NONE,
        check_runs=_green(),
        settled_pr_numbers=frozenset(),
        unresolved_threads=2,
    )
    assert act is OwedAct.CODE_OWED


def test_zero_threads_leaves_handoff_ready_unchanged() -> None:
    """The default (no thread count) path is byte-identical: still handoff-ready."""
    act = classify_pr_owed_act(
        111,
        blocker=remediation_tracker.MERGE_BLOCK_AWAITING_APPROVAL,
        check_runs=_green(),
        settled_pr_numbers=frozenset(),
        unresolved_threads=0,
    )
    assert act is OwedAct.HANDOFF_READY


def test_open_thread_does_not_override_behind_main() -> None:
    """An open thread promotes only approval/clean — a behind-main gate stands."""
    act = classify_pr_owed_act(
        112,
        blocker=remediation_tracker.MERGE_BLOCK_BEHIND_MAIN,
        check_runs=_green(),
        settled_pr_numbers=frozenset(),
        unresolved_threads=1,
    )
    assert act is OwedAct.EXTERNAL_BLOCKED


def test_settled_pr_ignores_thread_count() -> None:
    """A settled PR is owed nothing even with open threads (short-circuit wins)."""
    act = classify_pr_owed_act(
        113,
        blocker=remediation_tracker.MERGE_BLOCK_NONE,
        check_runs=_green(),
        settled_pr_numbers=frozenset({113}),
        unresolved_threads=3,
    )
    assert act is None


def test_awaiting_push_owned_only_under_own_push() -> None:
    """AWAITING_PUSH is external by default but owned code once own_push is set."""
    default = classify_pr_owed_act(
        109,
        blocker=remediation_tracker.MERGE_BLOCK_AWAITING_PUSH,
        check_runs=_green(),
        settled_pr_numbers=frozenset(),
    )
    assert default is OwedAct.EXTERNAL_BLOCKED
    owned = classify_pr_owed_act(
        109,
        blocker=remediation_tracker.MERGE_BLOCK_AWAITING_PUSH,
        check_runs=_green(),
        settled_pr_numbers=frozenset(),
        own_push=True,
    )
    assert owned is OwedAct.CODE_OWED


# ── settled short-circuit (criterion 1) ─────────────────────────────────────


def test_settled_pr_returns_none() -> None:
    act = classify_pr_owed_act(
        200,
        blocker=remediation_tracker.MERGE_BLOCK_CI_FAILING,
        check_runs=_failing(),
        settled_pr_numbers=frozenset({200}),
    )
    assert act is None


def test_settled_pr_short_circuits_before_any_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settled PR returns before either composed oracle is invoked."""

    def _boom_ownership(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("classify_blocker_ownership must not be called")

    def _boom_ci(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("classify_ci_conclusions must not be called")

    monkeypatch.setattr(
        remediation_tracker, "classify_blocker_ownership", _boom_ownership,
    )
    monkeypatch.setattr(ci_diagnosis, "classify_ci_conclusions", _boom_ci)

    act = classify_pr_owed_act(
        201,
        blocker=remediation_tracker.MERGE_BLOCK_CI_FAILING,
        check_runs=_failing(),
        settled_pr_numbers=frozenset({201}),
    )
    assert act is None


def test_unsettled_pr_does_invoke_oracles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror of the short-circuit: an unsettled PR *does* consult them."""
    calls: list[str] = []

    real_ownership = remediation_tracker.classify_blocker_ownership
    real_ci = ci_diagnosis.classify_ci_conclusions

    def _spy_ownership(blocker: str, **kwargs: object) -> str:
        calls.append("ownership")
        return real_ownership(blocker, **kwargs)  # type: ignore[arg-type]

    def _spy_ci(check_runs: object) -> str:
        calls.append("ci")
        return real_ci(check_runs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        remediation_tracker, "classify_blocker_ownership", _spy_ownership,
    )
    monkeypatch.setattr(ci_diagnosis, "classify_ci_conclusions", _spy_ci)

    classify_pr_owed_act(
        202,
        blocker=remediation_tracker.MERGE_BLOCK_CI_PENDING,
        check_runs=_running(),
        settled_pr_numbers=frozenset(),
    )
    assert calls == ["ownership", "ci"]


# ── the classifier always returns a single member (criterion 1) ─────────────


def test_return_is_always_a_single_member_or_none() -> None:
    """Every input yields exactly one OwedAct member (or None for settled)."""
    for blocker in (
        remediation_tracker.MERGE_BLOCK_CONFLICTING,
        remediation_tracker.MERGE_BLOCK_CI_FAILING,
        remediation_tracker.MERGE_BLOCK_CI_PENDING,
        remediation_tracker.MERGE_BLOCK_AWAITING_APPROVAL,
        remediation_tracker.MERGE_BLOCK_BEHIND_MAIN,
        remediation_tracker.MERGE_BLOCK_NONE,
        "GARBAGE_UNRECOGNISED",
    ):
        for check_runs in (_failing(), _running(), _green(), ()):
            act = classify_pr_owed_act(
                1,
                blocker=blocker,
                check_runs=check_runs,
                settled_pr_numbers=frozenset(),
            )
            assert act in set(OwedAct)


# ── check_runs_from_conclusion helper ───────────────────────────────────────


@pytest.mark.parametrize(
    ("conclusion", "expected_status"),
    [
        ("failure", "FAILING"),
        ("FAILURE", "FAILING"),
        ("success", "GREEN"),
        ("none", "GREEN"),
        ("", "RUNNING"),
        ("pending", "RUNNING"),
        ("something-weird", "RUNNING"),
    ],
)
def test_check_runs_from_conclusion(conclusion: str, expected_status: str) -> None:
    runs = check_runs_from_conclusion(conclusion)
    assert ci_diagnosis.classify_ci_conclusions(runs) == expected_status


# ── describe_owed_act covers every member ───────────────────────────────────


def test_describe_owed_act_covers_every_member() -> None:
    for act in OwedAct:
        text = describe_owed_act(act)
        assert isinstance(text, str)
        assert text


# ── check_runs_from_ci_state — the CIState-vocabulary sibling (135) ──────────


@pytest.mark.parametrize(
    ("state", "expected_status"),
    [
        ("FAILING", "FAILING"),
        ("failing", "FAILING"),
        ("GREEN", "GREEN"),
        ("NONE", "GREEN"),
        ("  green  ", "GREEN"),
        ("PENDING", "RUNNING"),
        ("", "RUNNING"),
        ("something-weird", "RUNNING"),
    ],
)
def test_check_runs_from_ci_state(state: str, expected_status: str) -> None:
    """Every CIState member — plus an unrecognised string — round-trips.

    An unreadable state must fail toward ``RUNNING`` (the clock is the actor),
    never manufacture a green or a red.
    """
    runs = check_runs_from_ci_state(state)
    assert ci_diagnosis.classify_ci_conclusions(runs) == expected_status


# ── owed_act_from_readiness_snapshot — the free, snapshot-derived act (135) ──


def _snapshot(**overrides: object) -> dict[str, object]:
    """A green, open, unblocked persisted readiness snapshot, plus overrides."""
    snapshot: dict[str, object] = {
        "pr_number": 7,
        "branch_landed": False,
        "branch_behind": False,
        "branch_conflicting": False,
        "ci_state": "GREEN",
        "unresolved_threads": 0,
        "unaddressed_overflow": 0,
        "awaiting_approval": False,
        "ready_and_waiting": True,
        "settled": False,
    }
    snapshot.update(overrides)
    return snapshot


def test_owed_act_from_snapshot_reads_a_conflict_as_code_owed() -> None:
    act = owed_act_from_readiness_snapshot(_snapshot(branch_conflicting=True))
    assert act is OwedAct.CODE_OWED


def test_owed_act_from_snapshot_reads_a_ci_red_as_code_owed() -> None:
    act = owed_act_from_readiness_snapshot(_snapshot(ci_state="FAILING"))
    assert act is OwedAct.CODE_OWED


def test_owed_act_from_snapshot_reads_an_open_thread_as_code_owed() -> None:
    """An unresolved thread promotes a clean merge state to owned work."""
    act = owed_act_from_readiness_snapshot(_snapshot(unresolved_threads=2))
    assert act is OwedAct.CODE_OWED


def test_owed_act_from_snapshot_reads_an_overflow_finding_as_code_owed() -> None:
    """An unaddressed overflow finding is code-clearable work with no thread."""
    act = owed_act_from_readiness_snapshot(_snapshot(unaddressed_overflow=1))
    assert act is OwedAct.CODE_OWED


def test_owed_act_from_snapshot_reads_pending_ci_as_wait_ci() -> None:
    act = owed_act_from_readiness_snapshot(_snapshot(ci_state="PENDING"))
    assert act is OwedAct.WAIT_CI


def test_owed_act_from_snapshot_reads_green_and_clean_as_handoff_ready() -> None:
    assert owed_act_from_readiness_snapshot(_snapshot()) is OwedAct.HANDOFF_READY


def test_owed_act_from_snapshot_reads_an_approval_wait_as_handoff_ready() -> None:
    act = owed_act_from_readiness_snapshot(_snapshot(awaiting_approval=True))
    assert act is OwedAct.HANDOFF_READY


def test_owed_act_from_snapshot_reads_behind_main_as_external_blocked() -> None:
    """Behind-main is mechanically Tend-cleared, not mine and not a handoff."""
    act = owed_act_from_readiness_snapshot(_snapshot(branch_behind=True))
    assert act is OwedAct.EXTERNAL_BLOCKED


def test_owed_act_from_snapshot_short_circuits_a_settled_pr() -> None:
    """A merged/closed PR is owed nothing — no set and no merge read needed."""
    assert owed_act_from_readiness_snapshot(_snapshot(settled=True)) is None


def test_owed_act_from_snapshot_honours_the_blocker_precedence() -> None:
    """Conflict outranks behind outranks approval-wait, as merge_block_reason does."""
    act = owed_act_from_readiness_snapshot(
        _snapshot(branch_conflicting=True, branch_behind=True, awaiting_approval=True),
    )
    assert act is OwedAct.CODE_OWED


def test_owed_act_from_snapshot_tolerates_an_empty_dict() -> None:
    """A wholly-absent snapshot reads open, unblocked, and CI-unconcluded."""
    assert owed_act_from_readiness_snapshot({}) is OwedAct.WAIT_CI


@pytest.mark.parametrize(
    "threads", [True, "2", None, -3, 1.5],
)
def test_owed_act_from_snapshot_ignores_a_malformed_thread_count(
    threads: object,
) -> None:
    """A bool, string, None, negative or float count degrades to zero threads.

    ``True`` matters specifically: ``bool`` is an ``int`` subclass, so a stray
    flag must not read as one open thread and invent owned work.
    """
    act = owed_act_from_readiness_snapshot(_snapshot(unresolved_threads=threads))
    assert act is OwedAct.HANDOFF_READY


def test_owed_act_from_snapshot_tolerates_a_non_string_ci_state() -> None:
    """A malformed ``ci_state`` reads as unconcluded, not green."""
    act = owed_act_from_readiness_snapshot(_snapshot(ci_state=object()))
    assert act is OwedAct.WAIT_CI


# ── derive_owed_act_blocker — the shared blocker derivation (#4048) ─────────


@pytest.mark.parametrize(
    ("commits_behind", "conclusion", "merge_state_status", "mergeable",
     "cleanly_mergeable", "expected"),
    [
        # CI_PENDING collapses to NONE, measured or unmeasurable.
        (0, "pending", "CLEAN", "MERGEABLE", True,
         remediation_tracker.MERGE_BLOCK_NONE),
        (0, "", "BLOCKED", "MERGEABLE", True,
         remediation_tracker.MERGE_BLOCK_NONE),
        # Measured merge-tree conflict refines NONE / BEHIND_MAIN.
        (0, "pending", "CLEAN", "MERGEABLE", False,
         remediation_tracker.MERGE_BLOCK_CONFLICTING),
        (3, "success", "UNKNOWN", "UNKNOWN", False,
         remediation_tracker.MERGE_BLOCK_CONFLICTING),
        # Host-reported CONFLICTING refines too (case/whitespace-normalised).
        (0, "success", "CLEAN", " conflicting ", True,
         remediation_tracker.MERGE_BLOCK_CONFLICTING),
        # Conflict outranks a concluded red.
        (0, "failure", "CLEAN", "MERGEABLE", False,
         remediation_tracker.MERGE_BLOCK_CONFLICTING),
        # No refinement over AWAITING_APPROVAL.
        (0, "success", "BLOCKED", "CONFLICTING", True,
         remediation_tracker.MERGE_BLOCK_AWAITING_APPROVAL),
        # Clean behind-main passes through.
        (2, "success", "CLEAN", "MERGEABLE", True,
         remediation_tracker.MERGE_BLOCK_BEHIND_MAIN),
        # Red without conflict passes through.
        (0, "failure", "CLEAN", "MERGEABLE", True,
         remediation_tracker.MERGE_BLOCK_CI_FAILING),
    ],
)
def test_derive_owed_act_blocker(
    commits_behind: int,
    conclusion: str,
    merge_state_status: str,
    mergeable: str,
    cleanly_mergeable: bool,
    expected: str,
) -> None:
    assert derive_owed_act_blocker(
        commits_behind=commits_behind,
        check_conclusion=conclusion,
        merge_state_status=merge_state_status,
        mergeable=mergeable,
        cleanly_mergeable=cleanly_mergeable,
    ) == expected


# ── merge-queue landing model: behind-ness is not a blocker (#4307) ─────────


def test_behind_main_under_merge_queue_is_handoff_ready() -> None:
    """A green, thread-free, behind-but-clean PR is handoff-ready once the Tend
    catch-up merge is off — the merge queue tests it against its landing tree."""
    blocker = derive_owed_act_blocker(
        commits_behind=5,
        check_conclusion="success",
        merge_state_status="BEHIND",
        mergeable="MERGEABLE",
        cleanly_mergeable=True,
        tend_catch_up=False,
    )
    act = classify_pr_owed_act(
        4307,
        blocker=blocker,
        check_runs=check_runs_from_conclusion("success"),
        settled_pr_numbers=frozenset(),
    )
    assert act is OwedAct.HANDOFF_READY


def test_behind_main_under_merge_queue_conflicting_is_code_owed() -> None:
    """A behind branch that no longer merges cleanly is still owned work."""
    blocker = derive_owed_act_blocker(
        commits_behind=5,
        check_conclusion="success",
        merge_state_status="BEHIND",
        mergeable="MERGEABLE",
        cleanly_mergeable=False,
        tend_catch_up=False,
    )
    assert blocker == remediation_tracker.MERGE_BLOCK_CONFLICTING


def test_behind_main_under_merge_queue_pending_ci_waits() -> None:
    """Behind-ness removed, a still-running rollup reads wait-ci, not handoff."""
    blocker = derive_owed_act_blocker(
        commits_behind=5,
        check_conclusion="pending",
        merge_state_status="BEHIND",
        mergeable="MERGEABLE",
        cleanly_mergeable=True,
        tend_catch_up=False,
    )
    act = classify_pr_owed_act(
        4308,
        blocker=blocker,
        check_runs=check_runs_from_conclusion("pending"),
        settled_pr_numbers=frozenset(),
    )
    assert act is OwedAct.WAIT_CI
