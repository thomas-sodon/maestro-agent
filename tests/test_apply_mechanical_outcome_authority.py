"""Tests for the extracted
``maestro.loop.record_binds.apply_mechanical_outcome_authority``.

This is ``run``'s ``# 5d.`` block lifted into a named, independently-testable
function: the unconditional author of a remediation-PR record's landing verdict
(proposal 105). For a record carrying a ``remediation_pr_num`` it reads
``is_pr_settled().branch_landed`` off the merge-truth chokepoint, asks the
prose-blind ``decide_outcome`` for the git-grounded verdict, logs the
narration-vs-git divergence to the durable activation ledger, *then* adopts that
verdict, and finally appends ``partial_provenance``'s delivery note to
``key_findings``.

The extraction is behaviour-preserving, so these tests pin every arm of the
function's own gate — the ``None`` no-op the through-``run`` tests reach only by
driving a whole non-remediation task, and both ``partial_provenance`` notes
against both an empty and a populated ``key_findings``. Unlike the three sibling
extraction test files this one needs ``mem_fs``: the fixture config leaves
``agent.gate_activations`` on the JSONL backend, so the ledger rows are read back
out of the in-memory filesystem the way ``tests/test_agent_remediation.py``'s
``_mechanical_rows`` does. No ``agent.run``, no SDK patching — the function is
called directly on a bare ``make_agent()`` agent.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from maestro.agent import MaestroAgent
from maestro.loop.record_binds import apply_mechanical_outcome_authority
from maestro.outcome_authority import _LAP_DELIVERING_NOTE, _NULL_LAP_NOTE
from maestro.pr_facts import MergeTruth
from maestro.pr_remediation_state import Marker

#: A remediation summary carrying the delivered push marker, so
#: ``classify_lap_delivery`` reads the lap as ``DELIVERING``.
_DELIVERING_SUMMARY = f"Fixed the gate; {Marker.DELIVERED_AWAITING_MERGE}."
#: A remediation summary carrying no push-family marker at all — a NULL lap.
_NULL_SUMMARY = "Re-lapped on the branch; nothing reached origin."


def _mechanical_rows(
    mem_fs: dict[str, str], agent: MaestroAgent,
) -> list[dict[str, object]]:
    """The ``mechanical_outcome_authority`` activation rows written by *agent*."""
    raw = mem_fs.get(
        str(agent.config.memory_dir / "gate_activations.jsonl"), "",
    )
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    return [r for r in rows if r["gate"] == "mechanical_outcome_authority"]


def _landed(agent: MaestroAgent, *, branch_landed: bool) -> None:
    """Override the chokepoint stub with a *branch_landed* observation.

    ``make_agent`` already stubs ``is_pr_settled`` to a not-landed ``OPEN``
    ``MergeTruth``, so only the landed cases strictly need this; the unlanded
    cases call it to state their precondition rather than lean on the fixture.
    """
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=2647,
            settled=branch_landed,
            state="MERGED" if branch_landed else "OPEN",
            branch_landed=branch_landed,
            source="host_api",
            observed_at=0.0,
        ),
    )


def test_non_remediation_is_a_noop_and_writes_no_row(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(a) ``remediation_pr_num is None`` → inputs returned unchanged, no row.

    The genuinely-new assertion this extraction unlocks cheaply: a task with no
    remediation PR is not landing-relevant, so authoring it would only log a
    guaranteed-``agree`` row that dilutes the divergence rate the ledger exists
    to measure. The guard lives inside the function, so the no-op is the function's
    own arm — not the call site's.
    """
    agent = make_agent()

    outcome, key_findings = apply_mechanical_outcome_authority(
        agent,
        remediation_pr_num=None,
        outcome="success",
        key_findings="Built the helper",
        summary=_DELIVERING_SUMMARY,
    )

    assert outcome == "success"
    assert key_findings == "Built the helper"
    assert _mechanical_rows(mem_fs, agent) == []
    # The chokepoint is never consulted for a non-remediation record.
    assert agent.is_pr_settled.call_count == 0


def test_divergence_downgrades_success_and_logs_diverged_row(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(b) Narration ``success`` + unlanded branch → ``partial`` + ``diverged``.

    The ledger detail is composed from the *pre-adoption* narration verdict, so
    the row names both opinions even though the returned outcome is git's.
    """
    agent = make_agent()
    _landed(agent, branch_landed=False)

    outcome, key_findings = apply_mechanical_outcome_authority(
        agent,
        remediation_pr_num=2647,
        outcome="success",
        key_findings=None,
        summary=_DELIVERING_SUMMARY,
    )

    assert outcome == "partial"
    rows = _mechanical_rows(mem_fs, agent)
    assert len(rows) == 1
    assert rows[0]["action"] == "diverged"
    assert "narr=success" in rows[0]["detail"]
    assert "mech=partial" in rows[0]["detail"]
    assert "#2647" in rows[0]["detail"]
    # A partial verdict always carries its delivery provenance.
    assert key_findings == _LAP_DELIVERING_NOTE


def test_agreement_preserves_success_and_logs_agree_row(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(c) A landed branch → ``success`` preserved, one ``agree`` row."""
    agent = make_agent()
    _landed(agent, branch_landed=True)

    outcome, key_findings = apply_mechanical_outcome_authority(
        agent,
        remediation_pr_num=2647,
        outcome="success",
        key_findings="Fixed the flaky assertion",
        summary=_DELIVERING_SUMMARY,
    )

    assert outcome == "success"
    # A landed record has no delivery provenance, so the findings are untouched.
    assert key_findings == "Fixed the flaky assertion"
    rows = _mechanical_rows(mem_fs, agent)
    assert len(rows) == 1
    assert rows[0]["action"] == "agree"
    assert "narr=success" in rows[0]["detail"]
    assert "mech=success" in rows[0]["detail"]


def test_failure_candidate_passes_through_untouched(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(d) A ``failure`` candidate is never rescued by a landed branch.

    ``decide_outcome`` returns a failure unchanged, so the row is ``agree`` and
    no delivery note is appended even though the lap delivered.
    """
    agent = make_agent()
    _landed(agent, branch_landed=True)

    outcome, key_findings = apply_mechanical_outcome_authority(
        agent,
        remediation_pr_num=2647,
        outcome="failure",
        key_findings="Gate stayed red",
        summary=_DELIVERING_SUMMARY,
    )

    assert outcome == "failure"
    assert key_findings == "Gate stayed red"
    rows = _mechanical_rows(mem_fs, agent)
    assert len(rows) == 1
    assert rows[0]["action"] == "agree"
    assert "narr=failure" in rows[0]["detail"]
    assert "mech=failure" in rows[0]["detail"]


def test_delivering_note_appended_to_populated_findings(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """(e) DELIVERING lap + populated ``key_findings`` → note appended after it."""
    agent = make_agent()
    _landed(agent, branch_landed=False)

    outcome, key_findings = apply_mechanical_outcome_authority(
        agent,
        remediation_pr_num=2647,
        outcome="partial",
        key_findings="Rebased onto main",
        summary=_DELIVERING_SUMMARY,
    )

    assert outcome == "partial"
    assert key_findings == f"Rebased onto main {_LAP_DELIVERING_NOTE}"


def test_delivering_note_is_whole_findings_when_empty(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """(e) DELIVERING lap + empty ``key_findings`` → the note is the findings.

    The falsy-``key_findings`` arm: an empty string takes the ``else`` branch, so
    the result carries no leading separator.
    """
    agent = make_agent()
    _landed(agent, branch_landed=False)

    outcome, key_findings = apply_mechanical_outcome_authority(
        agent,
        remediation_pr_num=2647,
        outcome="partial",
        key_findings="",
        summary=_DELIVERING_SUMMARY,
    )

    assert outcome == "partial"
    assert key_findings == _LAP_DELIVERING_NOTE


def test_null_lap_note_appended_to_populated_findings(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """(e) NULL lap + populated ``key_findings`` → the ``[null-lap]`` note."""
    agent = make_agent()
    _landed(agent, branch_landed=False)

    outcome, key_findings = apply_mechanical_outcome_authority(
        agent,
        remediation_pr_num=2647,
        outcome="partial",
        key_findings="Re-read the blocker",
        summary=_NULL_SUMMARY,
    )

    assert outcome == "partial"
    assert key_findings == f"Re-read the blocker {_NULL_LAP_NOTE}"


def test_null_lap_note_is_whole_findings_when_none(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """(e) NULL lap + ``None`` ``key_findings`` → the note alone, no ``None`` text."""
    agent = make_agent()
    _landed(agent, branch_landed=False)

    outcome, key_findings = apply_mechanical_outcome_authority(
        agent,
        remediation_pr_num=2647,
        outcome="partial",
        key_findings=None,
        summary=_NULL_SUMMARY,
    )

    assert outcome == "partial"
    assert key_findings == _NULL_LAP_NOTE


def test_success_verdict_appends_no_delivery_note(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(f) A ``success`` landing verdict has no delivery provenance to record.

    ``partial_provenance`` returns ``None``, so ``key_findings`` is returned
    byte-for-byte — no ``[delivering]`` note on a landed record.

    This is also the *upgrade* direction of the divergence ledger (narration
    ``partial``, git ``success``), the mirror of the downgrade pinned in (b), so
    the row assertions pin both orderings of the ``narr=``/``mech=`` detail
    string that ``maestro measure-gate-triggers`` and the phantom-landing sensor
    read.
    """
    agent = make_agent()
    _landed(agent, branch_landed=True)

    outcome, key_findings = apply_mechanical_outcome_authority(
        agent,
        remediation_pr_num=2647,
        outcome="partial",
        key_findings="Pushed the fix",
        summary=_DELIVERING_SUMMARY,
    )

    # Narration said partial, git says landed — the authority upgrades it.
    assert outcome == "success"
    assert key_findings == "Pushed the fix"
    assert _LAP_DELIVERING_NOTE not in key_findings
    rows = _mechanical_rows(mem_fs, agent)
    assert len(rows) == 1
    assert rows[0]["action"] == "diverged"
    assert "narr=partial" in rows[0]["detail"]
    assert "mech=success" in rows[0]["detail"]
    assert "#2647" in rows[0]["detail"]
