"""Tests for the extracted ``maestro.loop.record_binds.apply_ci_red_read_bind``.

This is ``run``'s ``# 5c-iv-c.`` block lifted into a named, independently-
testable function: the CI-failing red-read bind. For a remediation record
narrating completion while the CI-failure diagnosis oracle surfaced a concrete
failing-check line the lap never addressed, it scrubs the resolution narration
via ``bind_ci_failing_verdict_to_red_read`` (the loop-authored merge-truth note
and every ``Marker`` preserved), keeps the outcome ``partial``, appends the
``[red-unread]`` note to the uncapped ``key_findings``, and offers the
open-the-red-gate directive into an empty ``next_task_directive`` slot (routed
through the 090 dropped-directive ledger on an occupied one).

The extraction is behaviour-preserving, so these tests pin every arm of the
function's own gate — including the occupied-slot ledgered drop no through-``run``
test drives — the way ``tests/test_apply_mechanical_outcome_authority.py`` pins
its sibling. Like that file this one takes ``mem_fs``: the fixture config leaves
``agent.gate_activations`` and ``agent.dropped_directives`` on the JSONL
backends, so the ledger rows are read back out of the in-memory filesystem. No
``agent.run``, no SDK patching — the function is called directly on a bare
``make_agent()`` agent, whose host stub defaults the CI rollup to green.
"""

from __future__ import annotations

import json

from maestro.agent import MaestroAgent
from maestro.audit_checks_phantom import (
    _ON_BRANCH_HANDOFF_MARKERS,
)
from maestro.loop.record_binds import apply_ci_red_read_bind
from maestro.outcome_grounding import _OPEN_THE_RED_GATE_DIRECTIVE, _RED_UNREAD_NOTE

#: The loop-authored merge-truth note ``# 5c`` prepends into the narration. It
#: deliberately carries a resolution keyword (``landed``) so the fire arm can
#: assert the ``protected_note`` shield preserves it byte-for-byte while the
#: agent-authored keyword outside it is scrubbed.
_NOTE = "[note] merge-truth: head not yet landed on origin."
#: A completion-narrating remediation summary: the protected note plus agent
#: prose whose ``Fixed`` is the word-boundary resolution keyword the scrub
#: rewrites to the neutral ``seen`` token.
_COMPLETION_SUMMARY = f"{_NOTE} Fixed the red check."


def _red_read_rows(
    mem_fs: dict[str, str], agent: MaestroAgent,
) -> list[dict[str, object]]:
    """The ``bind_ci_failing_verdict_to_red_read`` activation rows for *agent*."""
    raw = mem_fs.get(
        str(agent.config.memory_dir / "gate_activations.jsonl"), "",
    )
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    return [r for r in rows if r["gate"] == "bind_ci_failing_verdict_to_red_read"]


def _dropped_rows(
    mem_fs: dict[str, str], agent: MaestroAgent,
) -> list[dict[str, object]]:
    """The dropped-directive ledger rows persisted for *agent*."""
    raw = mem_fs.get(
        str(agent.config.memory_dir / "dropped_directives.jsonl"), "",
    )
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def test_non_remediation_is_a_noop_and_pays_no_host_read(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(a) ``remediation_pr_num is None`` → inputs returned unchanged, no row.

    The outer guard is the function's own arm now, so the no-op is pinned without
    driving a whole non-remediation task through ``run``. The host CI rollup is
    never consulted.
    """
    agent = make_agent()

    summary, key_findings, outcome = apply_ci_red_read_bind(
        agent,
        remediation_pr_num=None,
        outcome="success",
        summary=_COMPLETION_SUMMARY,
        key_findings="Diagnosed the log.",
        executed_commands=["pytest -x"],
        kept_branch=None,
        push_delivered=None,
        remediation_note=_NOTE,
        pr_branch=None,
        pr_head_watermark=None,
    )

    assert summary == _COMPLETION_SUMMARY
    assert key_findings == "Diagnosed the log."
    assert outcome == "success"
    host_read = agent.evolution.code_host.get_pr_check_conclusion
    assert host_read.call_count == 0  # type: ignore[attr-defined]
    assert _red_read_rows(mem_fs, agent) == []


def test_failure_outcome_passes_through_without_host_read(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(b) A ``failure`` verdict never narrates completion → unchanged, no read.

    The outer guard's outcome arm: even with the oracle cache primed, a
    non-completion outcome returns the inputs untouched and pays no host read.
    """
    agent = make_agent()
    agent._ci_oracle_surfaced[2647] = True

    summary, key_findings, outcome = apply_ci_red_read_bind(
        agent,
        remediation_pr_num=2647,
        outcome="failure",
        summary=_COMPLETION_SUMMARY,
        key_findings=None,
        executed_commands=["pytest -x"],
        kept_branch=None,
        push_delivered=None,
        remediation_note=_NOTE,
        pr_branch=None,
        pr_head_watermark=None,
    )

    assert summary == _COMPLETION_SUMMARY
    assert key_findings is None
    assert outcome == "failure"
    host_read = agent.evolution.code_host.get_pr_check_conclusion
    assert host_read.call_count == 0  # type: ignore[attr-defined]
    assert _red_read_rows(mem_fs, agent) == []


def test_unsurfaced_oracle_pays_no_host_read(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(c) No concrete red surfaced for this PR this lap → no host read, no scrub.

    ``_ci_oracle_surfaced`` has no entry for the PR, so ``guard_may_fire`` is
    False and the bind is invoked with ``blocker_ci_failing=False`` — a
    conservative no-op.
    """
    agent = make_agent()

    summary, key_findings, outcome = apply_ci_red_read_bind(
        agent,
        remediation_pr_num=2647,
        outcome="success",
        summary=_COMPLETION_SUMMARY,
        key_findings="Diagnosed the log.",
        executed_commands=["pytest -x"],
        kept_branch=None,
        push_delivered=None,
        remediation_note=_NOTE,
        pr_branch=None,
        pr_head_watermark=None,
    )

    assert summary == _COMPLETION_SUMMARY
    assert key_findings == "Diagnosed the log."
    assert outcome == "success"
    host_read = agent.evolution.code_host.get_pr_check_conclusion
    assert host_read.call_count == 0  # type: ignore[attr-defined]
    assert _red_read_rows(mem_fs, agent) == []


def test_addressed_red_pays_no_host_read(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(c) A surfaced red the lap committed-and-pushed a fix for → no host read.

    ``kept_branch`` plus ``push_delivered is True`` is the in-scope
    "edited-and-pushed a fix this lap" signal, so the guard cannot fire and the
    host CI read is never paid.
    """
    agent = make_agent()
    agent._ci_oracle_surfaced[2647] = True

    summary, key_findings, outcome = apply_ci_red_read_bind(
        agent,
        remediation_pr_num=2647,
        outcome="success",
        summary=_COMPLETION_SUMMARY,
        key_findings="Pushed the fix.",
        executed_commands=["pytest -x"],
        kept_branch="maestro/evolve/ep2647",
        push_delivered=True,
        remediation_note=_NOTE,
        pr_branch=None,
        pr_head_watermark=None,
    )

    assert summary == _COMPLETION_SUMMARY
    assert key_findings == "Pushed the fix."
    assert outcome == "success"
    host_read = agent.evolution.code_host.get_pr_check_conclusion
    assert host_read.call_count == 0  # type: ignore[attr-defined]
    assert _red_read_rows(mem_fs, agent) == []


def test_empty_command_record_pays_no_host_read(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(c) A session that recorded no commands at all → no host read, no scrub.

    A branchless off-channel remediation is not evidence the red was ignored,
    so the guard stands aside even with the oracle cache primed.
    """
    agent = make_agent()
    agent._ci_oracle_surfaced[2647] = True

    summary, key_findings, outcome = apply_ci_red_read_bind(
        agent,
        remediation_pr_num=2647,
        outcome="success",
        summary=_COMPLETION_SUMMARY,
        key_findings=None,
        executed_commands=[],
        kept_branch=None,
        push_delivered=None,
        remediation_note=_NOTE,
        pr_branch=None,
        pr_head_watermark=None,
    )

    assert summary == _COMPLETION_SUMMARY
    assert key_findings is None
    assert outcome == "success"
    host_read = agent.evolution.code_host.get_pr_check_conclusion
    assert host_read.call_count == 0  # type: ignore[attr-defined]
    assert _red_read_rows(mem_fs, agent) == []


def test_committed_but_unpushed_edge_reads_as_unaddressed(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(c) ``push_delivered=None`` (committed-but-never-pushed) is unaddressed.

    A kept branch whose push never reached origin has not addressed the red —
    ``addressed_red`` demands the delivered truth (``push_delivered is True``),
    so the guard may fire and the host CI read IS paid; the fixture-default
    green conclusion then stands the bind down without a scrub.
    """
    agent = make_agent()
    agent._ci_oracle_surfaced[2647] = True

    summary, key_findings, outcome = apply_ci_red_read_bind(
        agent,
        remediation_pr_num=2647,
        outcome="success",
        summary=_COMPLETION_SUMMARY,
        key_findings="Committed but the push failed.",
        executed_commands=["pytest -x"],
        kept_branch="maestro/evolve/ep2647",
        push_delivered=None,
        remediation_note=_NOTE,
        pr_branch=None,
        pr_head_watermark=None,
    )

    assert summary == _COMPLETION_SUMMARY
    assert key_findings == "Committed but the push failed."
    assert outcome == "success"
    host_read = agent.evolution.code_host.get_pr_check_conclusion
    assert host_read.call_count == 1  # type: ignore[attr-defined]
    assert _red_read_rows(mem_fs, agent) == []


def test_fire_scrubs_narration_downgrades_and_fills_empty_slot(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(d) Surfaced-unaddressed red on a CI-red PR narrating completion → fire.

    The outcome is capped to ``partial``, the agent-authored resolution keyword
    is rewritten to the neutral token while the loop-authored *remediation_note*
    survives byte-for-byte, the ``[red-unread]`` note lands in the uncapped
    ``key_findings``, the open-the-red-gate directive fills the empty
    ``next_task_directive`` slot and the model is persisted, and one
    ``directive_set`` activation row is logged. Nothing is ledgered — the slot
    was empty.
    """
    agent = make_agent()
    agent._ci_oracle_surfaced[2647] = True
    agent.evolution.code_host.get_pr_check_conclusion.return_value = "failure"  # type: ignore[attr-defined]

    summary, key_findings, outcome = apply_ci_red_read_bind(
        agent,
        remediation_pr_num=2647,
        outcome="success",
        summary=_COMPLETION_SUMMARY,
        key_findings="Diagnosed the failing check log.",
        executed_commands=["pytest tests/test_agent.py"],
        kept_branch=None,
        push_delivered=None,
        remediation_note=_NOTE,
        pr_branch=None,
        pr_head_watermark=None,
    )

    assert outcome == "partial"
    assert summary == f"{_NOTE} seen the red check."
    assert key_findings == f"Diagnosed the failing check log. {_RED_UNREAD_NOTE}"
    model = agent.consciousness.load_self_model()
    assert model.next_task_directive == _OPEN_THE_RED_GATE_DIRECTIVE
    rows = _red_read_rows(mem_fs, agent)
    assert len(rows) == 1
    assert rows[0]["action"] == "directive_set"
    assert rows[0]["detail"] == "pr=#2647"
    assert _dropped_rows(mem_fs, agent) == []


def test_fire_preserves_on_branch_handoff_phrase_for_rule_14(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(d/#2970) The marker-less, note-less slice rule 14 exists to catch.

    With ``remediation_note=None`` and no ``Marker`` in the narration, the bind
    still fires — outcome capped to ``partial`` and the ``[red-unread]`` note
    appended — yet the on-branch handoff phrase ("resolved on branch") survives
    the scrub, so the recorded summary still admits under
    ``_ON_BRANCH_HANDOFF_MARKERS``. Before the fix the scrub rewrote that phrase
    to "seen on branch" and rule 14 went blind on exactly this slice.
    """
    agent = make_agent()
    agent._ci_oracle_surfaced[2638] = True
    agent.evolution.code_host.get_pr_check_conclusion.return_value = "failure"  # type: ignore[attr-defined]

    summary, key_findings, outcome = apply_ci_red_read_bind(
        agent,
        remediation_pr_num=2638,
        outcome="success",
        summary="Resolved on branch for PR #2638; committed ac48a769.",
        key_findings="Diagnosed the failing check log.",
        executed_commands=["pytest tests/test_agent.py"],
        kept_branch=None,
        push_delivered=None,
        remediation_note=None,
        pr_branch=None,
        pr_head_watermark=None,
    )

    assert outcome == "partial"
    assert "resolved on branch" in summary.lower()
    assert any(marker in summary.lower() for marker in _ON_BRANCH_HANDOFF_MARKERS)
    assert key_findings == f"Diagnosed the failing check log. {_RED_UNREAD_NOTE}"
    rows = _red_read_rows(mem_fs, agent)
    assert len(rows) == 1
    assert rows[0]["action"] == "directive_set"


def test_occupied_slot_banks_directive_and_keeps_model(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(e) An occupied directive slot ledgers the drop instead of overwriting.

    With the replay flag on, the directive is banked to the dropped-directive
    ledger grounded on the PR, the pre-existing directive survives (the model is
    not re-persisted by this bind), and the activation row reads ``dropped`` —
    mirroring ``tests/test_self_audit_actuators.py``'s ledgered-drop assertions for
    the sibling ``rule_14_pr_less`` call site. The scrub itself still fires:
    the verdict cap never depends on the directive slot.
    """
    agent = make_agent()
    agent.config.dropped_directive_replay_enabled = True
    model = agent.consciousness.load_self_model()
    model.next_task_directive = "already set"
    agent.consciousness.save_self_model(model)
    agent._ci_oracle_surfaced[2647] = True
    agent.evolution.code_host.get_pr_check_conclusion.return_value = "failure"  # type: ignore[attr-defined]

    summary, key_findings, outcome = apply_ci_red_read_bind(
        agent,
        remediation_pr_num=2647,
        outcome="success",
        summary=_COMPLETION_SUMMARY,
        key_findings=None,
        executed_commands=["ruff check maestro/"],
        kept_branch=None,
        push_delivered=None,
        remediation_note=_NOTE,
        pr_branch=None,
        pr_head_watermark=None,
    )

    assert outcome == "partial"
    assert summary == f"{_NOTE} seen the red check."
    assert key_findings == _RED_UNREAD_NOTE
    updated = agent.consciousness.load_self_model()
    assert updated.next_task_directive == "already set"
    dropped = _dropped_rows(mem_fs, agent)
    assert len(dropped) == 1
    assert dropped[0]["text"] == _OPEN_THE_RED_GATE_DIRECTIVE
    assert dropped[0]["source"] == "bind_ci_failing_verdict_to_red_read"
    assert dropped[0]["pr_numbers"] == [2647]
    rows = _red_read_rows(mem_fs, agent)
    assert len(rows) == 1
    assert rows[0]["action"] == "dropped"
    assert rows[0]["detail"] == "pr=#2647"


def test_green_ci_stands_aside_after_paying_the_read(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(f) Preconditions hold but the host rollup is green → conservative no-op.

    The guard may fire, so the host CI read is paid; ``ci_state_from_conclusion``
    reads the fixture-default ``"success"`` conclusion as not-FAILING, so the
    bind is invoked with ``blocker_ci_failing=False`` — no scrub, no directive,
    no activation row, and the directive slot stays empty.
    """
    agent = make_agent()
    agent._ci_oracle_surfaced[2647] = True

    summary, key_findings, outcome = apply_ci_red_read_bind(
        agent,
        remediation_pr_num=2647,
        outcome="success",
        summary=_COMPLETION_SUMMARY,
        key_findings="Diagnosed the log.",
        executed_commands=["pytest -x"],
        kept_branch=None,
        push_delivered=None,
        remediation_note=_NOTE,
        pr_branch=None,
        pr_head_watermark=None,
    )

    assert summary == _COMPLETION_SUMMARY
    assert key_findings == "Diagnosed the log."
    assert outcome == "success"
    host_read = agent.evolution.code_host.get_pr_check_conclusion
    assert host_read.call_count == 1  # type: ignore[attr-defined]
    model = agent.consciousness.load_self_model()
    assert model.next_task_directive is None
    assert _red_read_rows(mem_fs, agent) == []


def _self_commit_call(  # type: ignore[no-untyped-def]
    agent, *, push_delivered, watermark=("w" * 40,),
    kept_branch=None, executed_commands=("make check",),
):
    return apply_ci_red_read_bind(
        agent,
        remediation_pr_num=2647,
        outcome="success",
        summary=_COMPLETION_SUMMARY,
        key_findings="Pushed the fix.",
        executed_commands=list(executed_commands),
        kept_branch=kept_branch,
        push_delivered=push_delivered,
        remediation_note=_NOTE,
        pr_branch="maestro/evolve/ep2647",
        pr_head_watermark=watermark,
    )


def test_agent_self_commit_reads_as_addressed(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """A lap-authored commit since the PR-head watermark, delivered, is
    addressed even with no loop-side ``kept_branch`` — no host read."""
    agent = make_agent()
    agent._ci_oracle_surfaced[2647] = True
    agent._vcs.lap_commits_since.return_value = 1

    summary, key_findings, outcome = _self_commit_call(
        agent, push_delivered=True,
    )

    assert summary == _COMPLETION_SUMMARY
    assert key_findings == "Pushed the fix."
    assert outcome == "success"
    agent._vcs.lap_commits_since.assert_called_once_with(
        "maestro/evolve/ep2647", ("w" * 40,),
    )
    host_read = agent.evolution.code_host.get_pr_check_conclusion
    assert host_read.call_count == 0  # type: ignore[attr-defined]


def test_agent_self_commit_undelivered_reads_as_unaddressed(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """Without a delivered push the self-commit signal is never probed and the
    red reads unaddressed (the host read is paid)."""
    for delivered in (None, False):
        agent = make_agent()
        agent._ci_oracle_surfaced[2647] = True
        agent._vcs.lap_commits_since.return_value = 1

        _self_commit_call(agent, push_delivered=delivered)

        agent._vcs.lap_commits_since.assert_not_called()
        host_read = agent.evolution.code_host.get_pr_check_conclusion
        assert host_read.call_count == 1  # type: ignore[attr-defined]


def test_unknown_lap_commit_probe_degrades_to_kept_branch_only(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """A probe that cannot measure (``None``) falls back to the
    ``kept_branch``-only predicate — unaddressed, so the host read is paid."""
    agent = make_agent()
    agent._ci_oracle_surfaced[2647] = True
    agent._vcs.lap_commits_since.return_value = None

    _self_commit_call(agent, push_delivered=True)

    host_read = agent.evolution.code_host.get_pr_check_conclusion
    assert host_read.call_count == 1  # type: ignore[attr-defined]


def test_missing_watermark_skips_lap_commit_probe(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """No checkout watermark → no probe, ``kept_branch``-only predicate."""
    agent = make_agent()
    agent._ci_oracle_surfaced[2647] = True

    _self_commit_call(agent, push_delivered=True, watermark=None)

    agent._vcs.lap_commits_since.assert_not_called()
    host_read = agent.evolution.code_host.get_pr_check_conclusion
    assert host_read.call_count == 1  # type: ignore[attr-defined]


def test_unsurfaced_oracle_skips_lap_commit_probe(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """No surfaced red → the lap-commit probe is never paid."""
    agent = make_agent()

    _self_commit_call(agent, push_delivered=True)

    agent._vcs.lap_commits_since.assert_not_called()
    host_read = agent.evolution.code_host.get_pr_check_conclusion
    assert host_read.call_count == 0  # type: ignore[attr-defined]


def test_kept_branch_skips_lap_commit_probe(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """A kept branch already addresses the red → the probe is never paid,
    even with a PR branch and watermark in hand."""
    agent = make_agent()
    agent._ci_oracle_surfaced[2647] = True

    _self_commit_call(
        agent, push_delivered=True, kept_branch="maestro/evolve/ep2647",
    )

    agent._vcs.lap_commits_since.assert_not_called()
    host_read = agent.evolution.code_host.get_pr_check_conclusion
    assert host_read.call_count == 0  # type: ignore[attr-defined]


def test_empty_command_record_skips_lap_commit_probe(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """No recorded commands → the probe is never paid, even with a PR branch
    and watermark in hand."""
    agent = make_agent()
    agent._ci_oracle_surfaced[2647] = True

    _self_commit_call(agent, push_delivered=True, executed_commands=())

    agent._vcs.lap_commits_since.assert_not_called()
