"""Tests for the extracted ``maestro.loop.record_binds.apply_mergeable_flip_actuator``.

This is ``run``'s ``# 5c-iv-b.`` block lifted into a named, independently-
testable function: the default-off (``[loop] mergeable_merge_request_enabled``)
mergeable-flip merge-request actuator. For a PR-remediation verdict citing
``#N`` whose merge truth is clean-but-unlanded and whose sole readiness blocker
is external and mergeable-terminal (AWAITING_APPROVAL), it sends exactly one
named merge-request to Thomas via ``deliver_handoff_cfg``, idempotent on a persisted
``(pr_number, head_sha)`` serviced marker, and logs one activation row only on a
real delivery.

The extraction is behaviour-preserving, so these tests pin every arm of the
function's own gate — including the ``remediation_pr_num is None`` no-op and the
suppressed-delivery arm no through-``run`` test drives — the way
``tests/test_apply_ci_red_read_bind.py`` pins its sibling. Like that file this
one takes ``mem_fs``: the fixture config leaves ``agent.gate_activations`` and
``agent.merge_request_serviced`` on the JSONL/JSON backends, so both the
activation rows and the serviced marker are read back out of the in-memory
filesystem. No ``agent.run``, no SDK patching — the function is called directly on
a bare ``make_agent()`` agent, with the two chokepoints stubbed per-test.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from maestro.agent import MaestroAgent
from maestro.loop.record_binds import apply_mergeable_flip_actuator
from maestro.outcome_grounding import bind_mergeable_flip_to_merge_request
from maestro.pr_facts import MergeTruth, PRReadiness

#: The PR every arm services.
_PR = 2647
#: The remediation goal ``run`` threads in. "Remediate PR #N: …" reliably
#: carries the ``#N`` citation the actuator's first gate requires.
_GOAL = f"Remediate PR #{_PR}: address review comments"
#: The post-§5c framed verdict prose the merge-request text quotes.
_SUMMARY = "Reconciled the branch to mergeable; awaiting Thomas's approval."
_FINDINGS = "Threads cleared, CI green, branch not behind."
#: The ``origin/<branch>`` head this lap stamped — the idempotency key half.
_HEAD = "flip_head_sha"
_BRANCH = "maestro/evolve/ep2647"


def _flip_rows(
    mem_fs: dict[str, str], agent: MaestroAgent,
) -> list[dict[str, object]]:
    """The ``bind_mergeable_flip_to_merge_request`` activation rows for *agent*."""
    raw = mem_fs.get(
        str(agent.config.memory_dir / "gate_activations.jsonl"), "",
    )
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    return [
        r for r in rows if r["gate"] == "bind_mergeable_flip_to_merge_request"
    ]


def _arm_flip(agent: MaestroAgent) -> None:
    """Stub both chokepoints to the mergeable flip the actuator fires on.

    ``is_pr_settled`` reads clean-but-unlanded (neither settled nor landed) and
    ``observe_pr_readiness`` reads a PR whose sole blocker is the external
    human-approval gate — the one mergeable-terminal blocker a message to Thomas
    recruits.
    """
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=_PR,
            settled=False,
            state="OPEN",
            branch_landed=False,
            source="host_api",
            observed_at=0.0,
        ),
    )
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=PRReadiness(
            pr_number=_PR,
            branch_landed=False,
            branch_behind=False,
            branch_conflicting=False,
            ci_state="GREEN",
            unresolved_threads=0,
            unaddressed_overflow=0,
            awaiting_approval=True,
            ready_and_waiting=True,
            source="host_api",
            observed_at=0.0,
        ),
    )


def test_flag_off_reads_no_chokepoint_and_delivers_nothing(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(a) The default-off flag is the function's first arm → a total no-op.

    Thomas's safety keystroke is preserved: neither chokepoint is consulted (so
    the actuator adds no git/host I/O of its own on the flag-off path), no
    message is delivered, no activation row is written, and no serviced marker
    is recorded.
    """
    agent = make_agent()
    assert agent.config.mergeable_merge_request_enabled is False
    _arm_flip(agent)

    with patch(
        "maestro.loop.record_binds.deliver_handoff_cfg", return_value=True,
    ) as mock_deliver:
        apply_mergeable_flip_actuator(
            agent,
            remediation_pr_num=_PR,
            pr_branch=_BRANCH,
            pr_head_sha_stamp=_HEAD,
            goal=_GOAL,
            summary=_SUMMARY,
            key_findings=_FINDINGS,
        )

    mock_deliver.assert_not_called()
    agent.is_pr_settled.assert_not_called()
    agent.observe_pr_readiness.assert_not_called()
    assert _flip_rows(mem_fs, agent) == []
    assert agent.merge_request_serviced.is_serviced(_PR, _HEAD) is False


def test_non_remediation_is_a_noop_even_with_the_flag_on(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(b) ``remediation_pr_num is None`` → the second arm, also a total no-op.

    A non-remediation record has no PR to recruit a landing for, so the flag
    being on changes nothing: the chokepoints stay unread. Pinned directly here
    rather than by driving a whole non-remediation task through ``run``.
    """
    agent = make_agent()
    agent.config.mergeable_merge_request_enabled = True
    _arm_flip(agent)

    with patch(
        "maestro.loop.record_binds.deliver_handoff_cfg", return_value=True,
    ) as mock_deliver:
        apply_mergeable_flip_actuator(
            agent,
            remediation_pr_num=None,
            pr_branch=None,
            pr_head_sha_stamp=None,
            goal="Improve the audit registry",
            summary=_SUMMARY,
            key_findings=_FINDINGS,
        )

    mock_deliver.assert_not_called()
    agent.is_pr_settled.assert_not_called()
    agent.observe_pr_readiness.assert_not_called()
    assert _flip_rows(mem_fs, agent) == []


def test_mergeable_flip_delivers_one_named_merge_request(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(c) The fire arm: one ``deliver_handoff_cfg``, one row, one serviced marker.

    With the flag on, a verdict citing ``#N``, a clean-but-unlanded merge truth
    and an AWAITING_APPROVAL sole blocker, the actuator delivers exactly one
    ``UnlandedCureHandoff`` naming the PR at this lap's head, logs the single
    ``merge_request`` activation row, and records the ``(pr, head_sha)`` marker
    that makes a repeat on the same commit a no-op. The bind is spied through
    (``wraps``) so the composed verdict text it gates on is asserted exactly.
    """
    agent = make_agent()
    agent.config.mergeable_merge_request_enabled = True
    _arm_flip(agent)

    with patch(
        "maestro.loop.record_binds.deliver_handoff_cfg", return_value=True,
    ) as mock_deliver, patch(
        "maestro.loop.record_binds.bind_mergeable_flip_to_merge_request",
        wraps=bind_mergeable_flip_to_merge_request,
    ) as spy_bind:
        apply_mergeable_flip_actuator(
            agent,
            remediation_pr_num=_PR,
            pr_branch=_BRANCH,
            pr_head_sha_stamp=_HEAD,
            goal=_GOAL,
            summary=_SUMMARY,
            key_findings=_FINDINGS,
        )

    # Both chokepoints are read for the cited PR — the merge truth by number,
    # the readiness for this PR's own branch.
    agent.is_pr_settled.assert_called_once_with(_PR)
    agent.observe_pr_readiness.assert_called_once_with(_PR, _BRANCH)
    # The verdict the bind gates on is goal + framed summary + findings.
    assert spy_bind.call_args.args[0] == f"{_GOAL}\n{_SUMMARY}\n{_FINDINGS}"
    # AWAITING_PUSH is owned (proposal 098) — the caller declares it.
    assert spy_bind.call_args.kwargs["own_push"] is True
    # Exactly one message, naming the cited PR at this lap's head.
    mock_deliver.assert_called_once()
    handoff = mock_deliver.call_args.args[1]
    assert handoff.pr_number == _PR
    assert handoff.head_sha == _HEAD
    assert handoff.stamp_count == 1
    # One activation row, only because the delivery was real.
    rows = _flip_rows(mem_fs, agent)
    assert len(rows) == 1
    assert rows[0]["action"] == "merge_request"
    assert rows[0]["detail"] == f"pr=#{_PR}"
    # The head is serviced, so a repeat lap on the same commit stands down.
    assert agent.merge_request_serviced.is_serviced(_PR, _HEAD) is True


def test_suppressed_delivery_records_no_marker_and_no_row(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(d) ``deliver_handoff_cfg`` returning False → no row, no serviced marker.

    Its own ``pr_number``-keyed standdown already suppressed the knock, so no
    message went out. The activation row is telemetry for a *real* delivery, and
    leaving the head un-serviced is what stops a suppressed delivery from
    masking a later genuine knock on the same commit.
    """
    agent = make_agent()
    agent.config.mergeable_merge_request_enabled = True
    _arm_flip(agent)

    with patch(
        "maestro.loop.record_binds.deliver_handoff_cfg", return_value=False,
    ) as mock_deliver:
        apply_mergeable_flip_actuator(
            agent,
            remediation_pr_num=_PR,
            pr_branch=_BRANCH,
            pr_head_sha_stamp=_HEAD,
            goal=_GOAL,
            summary=_SUMMARY,
            key_findings=_FINDINGS,
        )

    mock_deliver.assert_called_once()
    assert _flip_rows(mem_fs, agent) == []
    assert agent.merge_request_serviced.is_serviced(_PR, _HEAD) is False


def test_missing_head_sha_and_findings_fall_back_to_empty_strings(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(e) An unstamped head and absent findings degrade to ``""``, not a crash.

    ``pr_head_sha_stamp`` is ``None`` whenever the lap's ``origin/<branch>``
    rev-parse found no ref, and ``key_findings`` is ``None`` when distillation
    produced nothing. Both fall back to the empty string: the handoff carries
    ``head_sha=""`` (informational only — the standdown keys on ``pr_number``)
    and the composed verdict simply ends after the summary line, still citing
    ``#N`` so the delivery fires.
    """
    agent = make_agent()
    agent.config.mergeable_merge_request_enabled = True
    _arm_flip(agent)

    with patch(
        "maestro.loop.record_binds.deliver_handoff_cfg", return_value=True,
    ) as mock_deliver, patch(
        "maestro.loop.record_binds.bind_mergeable_flip_to_merge_request",
        wraps=bind_mergeable_flip_to_merge_request,
    ) as spy_bind:
        apply_mergeable_flip_actuator(
            agent,
            remediation_pr_num=_PR,
            pr_branch=_BRANCH,
            pr_head_sha_stamp=None,
            goal=_GOAL,
            summary=_SUMMARY,
            key_findings=None,
        )

    assert spy_bind.call_args.args[0] == f"{_GOAL}\n{_SUMMARY}\n"
    mock_deliver.assert_called_once()
    assert mock_deliver.call_args.args[1].head_sha == ""
    assert len(_flip_rows(mem_fs, agent)) == 1
    # The empty-string head is what the marker is keyed on.
    assert agent.merge_request_serviced.is_serviced(_PR, "") is True
