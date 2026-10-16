"""The owed-act binding collaborator (proposal 137, step 4).

Covers :mod:`maestro.loop.owed_act_binding` — the flag-aware façade ``agent.py``
consults for both bound give-up decisions, and the two pure helpers under it.
The act is derived from a :class:`~maestro.pr_facts.PRReadiness` the caller
already observed, so every case here is offline: no agent, no host, no SDK.
"""

from __future__ import annotations

from maestro.audit_checks import GateActivation
from maestro.ci_diagnosis import CheckRun
from maestro.config import Config
from maestro.loop.owed_act_binding import (
    failing_checks_for,
    gather_task_focus,
    make_disposal_offer_recorder,
    make_owed_act_seam,
    owed_act_binding,
    owed_act_for_readiness,
    readiness_is_code_owed,
)
from maestro.pr_facts import CIState, PRReadiness
from maestro.pr_owed_act import OwedAct
from maestro.vcs import PrMergeState, VCSError
from maestro.wip_primacy import Discretionary, TendOwnedPR


def _readiness(
    *,
    pr_number: int = 7,
    ci_state: CIState = "GREEN",
    settled: bool = False,
    branch_conflicting: bool = False,
    awaiting_approval: bool = False,
) -> PRReadiness:
    """Build the readiness fact the binding reads the owed act off."""
    return PRReadiness(
        pr_number=pr_number,
        branch_landed=settled,
        branch_behind=False,
        branch_conflicting=branch_conflicting,
        ci_state=ci_state,
        unresolved_threads=0,
        unaddressed_overflow=0,
        awaiting_approval=awaiting_approval,
        ready_and_waiting=False,
        source="host_api",
        observed_at=0.0,
        settled=settled,
    )


def test_settled_snapshot_is_owed_nothing() -> None:
    """A merged or closed PR short-circuits to ``None`` — nothing is owed."""
    assert owed_act_for_readiness(_readiness(settled=True)) is None


def test_failing_ci_reads_code_owed() -> None:
    """A concluded-red CI is a blocker the loop clears by writing code."""
    assert owed_act_for_readiness(_readiness(ci_state="FAILING")) is (
        OwedAct.CODE_OWED
    )


def test_conflicting_branch_reads_code_owed() -> None:
    """A conflicting branch is owned, code-clearable work."""
    readiness = _readiness(branch_conflicting=True)
    assert readiness_is_code_owed(readiness) is True


def test_pending_ci_is_not_code_owed() -> None:
    """While CI runs neither side acts — the clock does, not the loop."""
    assert readiness_is_code_owed(_readiness(ci_state="PENDING")) is False


def test_green_awaiting_approval_is_not_code_owed() -> None:
    """A green PR waiting on a human is a handoff, not code owed."""
    assert readiness_is_code_owed(_readiness(awaiting_approval=True)) is False


def test_seam_returns_the_act_for_a_mapped_pr() -> None:
    """The seam resolves the PR's branch, then reads its memoized readiness."""
    observed: list[tuple[int, str | None]] = []

    def _observe(pr_num: int, branch: str | None) -> PRReadiness:
        observed.append((pr_num, branch))
        return _readiness(pr_number=pr_num, ci_state="FAILING")

    seam = make_owed_act_seam({7: "maestro/evolve/x"}, _observe)
    assert seam(7) is OwedAct.CODE_OWED
    assert observed == [(7, "maestro/evolve/x")]


def test_seam_skips_a_pr_with_no_branch() -> None:
    """An un-mappable citation has no head to observe — no readiness read."""
    observed: list[int] = []

    def _observe(pr_num: int, branch: str | None) -> PRReadiness:
        observed.append(pr_num)
        return _readiness(pr_number=pr_num)

    seam = make_owed_act_seam({7: "maestro/evolve/x"}, _observe)
    assert seam(99) is None
    assert observed == []


def test_binding_off_yields_no_code_owed_reading() -> None:
    """Flag-off returns ``None``, which the salvage oracle reads as today."""
    binding = owed_act_binding(Config())
    assert binding.enabled is False
    assert binding.code_owed(_readiness(ci_state="FAILING")) is None


def test_binding_off_builds_no_seam() -> None:
    """Flag-off hands rule 21 a ``None`` seam, so no readiness is observed."""
    binding = owed_act_binding(Config())
    assert binding.seam({7: "maestro/evolve/x"}, _unreachable_observe) is None


def test_binding_on_reads_the_act() -> None:
    """Flag-on binds both decisions to the oracle's verdict."""
    config = Config()
    config.owed_act_binding_enabled = True
    binding = owed_act_binding(config)
    assert binding.code_owed(_readiness(ci_state="FAILING")) is True

    seam = binding.seam({7: "maestro/evolve/x"}, _failing_observe)
    assert seam is not None
    assert seam(7) is OwedAct.CODE_OWED


def _unreachable_observe(pr_num: int, branch: str | None) -> PRReadiness:
    """A readiness observer the flag-off path must never call."""
    raise AssertionError("flag-off must not observe readiness")


def _failing_observe(pr_num: int, branch: str | None) -> PRReadiness:
    """A readiness observer reporting a concluded-red CI."""
    return _readiness(pr_number=pr_num, ci_state="FAILING")


def _state(number: int, head_ref: str, created_at: str) -> PrMergeState:
    """The minimal open-PR enumeration row ``gather_task_focus`` projects."""
    return PrMergeState(
        number=number,
        head_ref=head_ref,
        title="",
        body="",
        state="OPEN",
        mergeable="MERGEABLE",
        merge_state_status="CLEAN",
        review_decision="",
        created_at=created_at,
        updated_at=created_at,
        url="",
    )


def test_focus_map_is_empty_while_the_flag_is_off() -> None:
    """Flag off: no derivation runs, so the focus verdict is today's."""
    binding = owed_act_binding(Config())
    assert binding.focus_map({7: _readiness(ci_state="FAILING")}) == {}


def test_focus_map_derives_every_observed_readiness_when_on() -> None:
    """Flag on: each already-observed readiness becomes its owed act."""
    binding = owed_act_binding(Config(owed_act_binding_enabled=True))
    mapping = binding.focus_map(
        {
            7: _readiness(pr_number=7, ci_state="FAILING"),
            8: _readiness(pr_number=8, settled=True),
        },
    )
    assert mapping == {7: OwedAct.CODE_OWED, 8: None}


def test_gather_task_focus_builds_the_map_and_forwards_it() -> None:
    """The gatherer observes each enumerated PR and promotes a bare red."""
    observed: list[tuple[int, str | None]] = []

    def _observe(pr_num: int, branch: str | None) -> PRReadiness:
        observed.append((pr_num, branch))
        return _readiness(pr_number=pr_num, ci_state="FAILING")

    focus = gather_task_focus(
        {"maestro/evolve/abc": _state(7, "maestro/evolve/abc", "2026-01-01")},
        _observe,
        owed_act_binding(Config(owed_act_binding_enabled=True)),
    )
    assert focus == TendOwnedPR(pr_number=7, owned_blocker="CI_FAILING")
    assert observed == [(7, "maestro/evolve/abc")]


def test_gather_task_focus_reproduces_todays_verdict_while_off() -> None:
    """Flag off: the same bare red still reads discretionary."""
    focus = gather_task_focus(
        {"maestro/evolve/abc": _state(7, "maestro/evolve/abc", "2026-01-01")},
        lambda pr_num, _branch: _readiness(pr_number=pr_num, ci_state="FAILING"),
        owed_act_binding(Config()),
    )
    assert focus == Discretionary()


def test_failing_checks_for_dedupes_and_preserves_order() -> None:
    """The names come back through the CI-truth owner: deduped, in order."""
    runs = (
        CheckRun(name="quality", conclusion="failure"),
        CheckRun(name="browser", conclusion="failure"),
        CheckRun(name="quality", conclusion="failure"),
        CheckRun(name="lint", conclusion="success"),
    )
    assert failing_checks_for(7, lambda _pr: runs) == ("quality", "browser")


def test_failing_checks_for_degrades_to_empty_on_a_nameless_read() -> None:
    """A name-less or empty rollup yields ``()`` — the caller keeps its text."""
    assert failing_checks_for(7, lambda _pr: ()) == ()
    assert failing_checks_for(
        7, lambda _pr: (CheckRun(name="", conclusion="failure"),),
    ) == ()


class _RecordingBackend:
    """In-memory gate-activation backend capturing appended rows."""

    def __init__(self) -> None:
        self.rows: list[GateActivation] = []

    def append(self, record: GateActivation) -> None:
        self.rows.append(record)

    def read_all(self) -> list[GateActivation]:
        return list(self.rows)

    def read_tail(self, limit: int) -> list[GateActivation]:
        return list(self.rows)[-limit:] if limit > 0 else []

    def read_gate_tail(self, gate: str, limit: int) -> list[GateActivation]:
        rows = [r for r in self.rows if r.gate == gate]
        return rows[-limit:] if limit > 0 else []


def test_disposal_recorder_stamps_diverged_on_code_owed() -> None:
    """A close offer on a code-owed PR is recorded ``diverged``."""
    backend = _RecordingBackend()
    record = make_disposal_offer_recorder(
        {7: "maestro/evolve/x"}, _failing_observe, backend,
    )
    record(7)
    assert len(backend.rows) == 1
    assert backend.rows[0].gate == "owed_act_disposal_offer"
    assert backend.rows[0].action == "diverged"
    assert backend.rows[0].detail == "pr=#7 owed=code-owed"


def test_disposal_recorder_stamps_aligned_otherwise() -> None:
    """A close offer on a PR owing something other than code is ``aligned``."""
    backend = _RecordingBackend()
    record = make_disposal_offer_recorder(
        {7: "maestro/evolve/x"},
        lambda n, _b: _readiness(pr_number=n, ci_state="PENDING"),
        backend,
    )
    record(7)
    assert backend.rows[0].action == "aligned"
    assert backend.rows[0].detail == "pr=#7 owed=wait-ci"


def test_disposal_recorder_unmapped_pr_records_dash() -> None:
    """An unmapped PR has no head to observe and records ``owed=-``."""
    backend = _RecordingBackend()
    record = make_disposal_offer_recorder(
        {7: "maestro/evolve/x"}, _unreachable_observe, backend,
    )
    record(99)
    assert backend.rows[0].action == "aligned"
    assert backend.rows[0].detail == "pr=#99 owed=-"


def test_disposal_recorder_host_failure_records_dash() -> None:
    """A ``VCSError`` on the readiness read records ``owed=-``, not a raise."""
    def _raising(_n: int, _b: str | None) -> PRReadiness:
        raise VCSError("gh down")

    backend = _RecordingBackend()
    record = make_disposal_offer_recorder(
        {7: "maestro/evolve/x"}, _raising, backend,
    )
    record(7)
    assert backend.rows[0].action == "aligned"
    assert backend.rows[0].detail == "pr=#7 owed=-"
