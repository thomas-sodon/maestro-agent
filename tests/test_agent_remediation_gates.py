"""Tests for the commit-ancestry dequeue gate ``_gate_pr_remediation_landed``.

The gate is the git-truth complement to the PR-object merged-exclusion gate:
where ``_gate_pr_remediation_merged`` asks whether the *PR object* merged, this
gate fetches and asks ``git merge-base --is-ancestor`` whether the branch HEAD is
already an ancestor of ``origin/main`` — the #2662 phantom, where the PR stays
OPEN while its content has landed via squash / a sibling PR / a prior Tend merge.

All git I/O is stubbed (no real subprocesses); the gate body is exercised as an
unbound :class:`MaestroAgent` method against a duck-typed stub, and the
dequeue-chain integration runs through the real
:class:`~maestro.loop.pr_remediation.PrRemediationController` with the genuine
gate bound onto the stub via :class:`types.MethodType`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.consciousness import PendingPRRequest
from maestro.loop.pr_remediation import PrRemediationController
from maestro.pr_facts import MergeTruth
from maestro.pr_remediation_state import PrRemediationState


def _landed_truth(*, branch_landed: bool) -> MergeTruth:
    """A :class:`MergeTruth` for the landed-gate chokepoint tests."""
    return MergeTruth(
        pr_number=7,
        settled=branch_landed,
        state="MERGED" if branch_landed else "OPEN",
        branch_landed=branch_landed,
        source="host_api",
        observed_at=1.0,
    )


def _gate_stub(**overrides: Any) -> SimpleNamespace:
    """Minimal duck-typed agent exposing only what the landed gate touches."""
    stub = SimpleNamespace(
        config=SimpleNamespace(
            git_timeout=10,
            workspace=Path("/fake/agent"),
        ),
        memory=MagicMock(),
        remediation_tracker=MagicMock(),
        _branch_merged_into_main=MagicMock(return_value=False),
        # Proposal 087: the gate reads merge state through this chokepoint
        # first. Default to an unsettled observation so the ancestry-fallback
        # arms run; the host-confirmed test overrides it.
        is_pr_settled=MagicMock(
            return_value=_landed_truth(branch_landed=False),
        ),
    )
    for name, value in overrides.items():
        setattr(stub, name, value)
    return stub


def _call_landed(
    stub: SimpleNamespace,
    pr_request: PendingPRRequest,
    source: str = "external",
) -> bool:
    """Invoke the real gate body against *stub* as an unbound method."""
    return PrRemediationController(stub)._gate_pr_remediation_landed(pr_request, source)


# --- gate body: branch arms ------------------------------------------------


def test_landed_ancestor_head_evicts_and_records_noop() -> None:
    """Host unconfirmed + ancestor head → evict with ``[landed]`` partial + no-op.

    The gate adds no fetch of its own — ``is_pr_settled`` (the chokepoint) owns
    the fail-loud fetch and is stubbed here — so it reads the ancestry of the
    chokepoint-freshened ``origin/<branch>`` ref directly (proposal 087)."""
    stub = _gate_stub(
        _branch_merged_into_main=MagicMock(return_value=True),
    )
    entry = PendingPRRequest(directive="Remediate PR #7: t", pr_branch="b7")
    with patch("maestro.agent.subprocess.run") as run:
        gated = _call_landed(stub, entry)
    assert gated is True
    # No second fetch: the chokepoint owns the fetch (mocked here).
    run.assert_not_called()
    # Ancestry consulted against the origin remote-tracking ref.
    stub._branch_merged_into_main.assert_called_once_with("origin/b7")
    record = stub.memory.record_task.call_args.args[0]
    assert record.outcome == "partial"
    assert "[landed]" in record.result_summary
    assert "[landed]" in record.key_findings
    stub.remediation_tracker.record_outcome.assert_called_once_with(
        7, noop=True,
    )


def test_landed_non_ancestor_head_passes_through_untouched() -> None:
    """A non-ancestor head fetches but evicts nothing — falls through."""
    stub = _gate_stub()  # _branch_merged_into_main defaults to False
    entry = PendingPRRequest(directive="Remediate PR #7: t", pr_branch="b7")
    with patch("maestro.agent.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        gated = _call_landed(stub, entry)
    assert gated is False
    stub._branch_merged_into_main.assert_called_once_with("origin/b7")
    stub.memory.record_task.assert_not_called()
    stub.remediation_tracker.record_outcome.assert_not_called()


def test_landed_chokepoint_fetch_failure_raises_loudly_no_fall_through() -> None:
    """A fail-loud chokepoint fetch failure (surfaced as ``is_pr_settled``
    raising) propagates — no silent fall-through, no ancestry read, no record.

    Post-087 the gate no longer fetches for itself; the fail-loud
    ``git fetch origin`` lives in ``is_pr_settled``, so a fetch failure reaches
    the gate as the chokepoint raising before any verdict is authored."""
    stub = _gate_stub(
        is_pr_settled=MagicMock(
            side_effect=subprocess.CalledProcessError(
                1, ["git", "fetch", "origin"],
            ),
        ),
    )
    entry = PendingPRRequest(directive="Remediate PR #7: t", pr_branch="b7")
    with pytest.raises(subprocess.CalledProcessError):
        _call_landed(stub, entry)
    # No merge-truth verdict authored on unfreshened refs.
    stub._branch_merged_into_main.assert_not_called()
    stub.memory.record_task.assert_not_called()
    stub.remediation_tracker.record_outcome.assert_not_called()


def test_landed_non_remediation_goal_skips_fetch() -> None:
    """A goal without the ``Remediate PR #<n>:`` header never fetches."""
    stub = _gate_stub()
    entry = PendingPRRequest(directive="do something else", pr_branch="b7")
    with patch("maestro.agent.subprocess.run") as run:
        gated = _call_landed(stub, entry)
    assert gated is False
    run.assert_not_called()
    stub._branch_merged_into_main.assert_not_called()


def test_landed_branchless_request_skips_fetch() -> None:
    """A request carrying no ``pr_branch`` never fetches or consults ancestry."""
    stub = _gate_stub()
    entry = PendingPRRequest(directive="Remediate PR #7: t", pr_branch=None)
    with patch("maestro.agent.subprocess.run") as run:
        gated = _call_landed(stub, entry)
    assert gated is False
    run.assert_not_called()
    stub._branch_merged_into_main.assert_not_called()


def test_landed_2662_phantom_pr_open_but_head_ancestor_evicts() -> None:
    """#2662 regression: PR object reads OPEN yet its head is an ancestor of
    origin/main (squash / sibling PR / prior Tend merge).

    The gate consults *only* commit ancestry — never the PR-object merged
    oracle — so a still-OPEN PR whose content has landed is evicted ``[landed]``
    where the PR-status merged gate (keyed on the object) would route a phantom
    lap. The PR-merged oracle is wired to report ``False`` to prove the gate does
    not consult it.
    """
    pr_merged_oracle = MagicMock(return_value=False)
    stub = _gate_stub(
        _branch_merged_into_main=MagicMock(return_value=True),
        _host_or_git_pr_merged=pr_merged_oracle,
    )
    entry = PendingPRRequest(
        directive="Remediate PR #2662: conflicts with origin/main.",
        pr_branch="maestro/evolve/ep2662",
    )
    with patch("maestro.agent.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        gated = _call_landed(stub, entry)
    assert gated is True
    # The eviction is on git-ancestry truth alone — the PR-status oracle is
    # never consulted.
    pr_merged_oracle.assert_not_called()
    record = stub.memory.record_task.call_args.args[0]
    assert record.outcome == "partial"
    assert "[landed]" in record.result_summary
    stub.remediation_tracker.record_outcome.assert_called_once_with(
        2662, noop=True,
    )


# --- proposal 087 chokepoint routing --------------------------------------


def test_landed_host_confirmed_landing_evicts_without_ancestry() -> None:
    """A host-confirmed landing (``branch_landed``) evicts at zero git cost —
    the chokepoint short-circuits the fetch + ancestry fallback entirely. The
    not-confirmed → fetch + ancestry arms are covered by the branch-arm tests
    above (which run with the default unsettled observation)."""
    stub = _gate_stub(
        is_pr_settled=MagicMock(return_value=_landed_truth(branch_landed=True)),
    )
    entry = PendingPRRequest(directive="Remediate PR #7: t", pr_branch="b7")
    with patch("maestro.agent.subprocess.run") as run:
        gated = _call_landed(stub, entry)
    assert gated is True
    stub.is_pr_settled.assert_called_once_with(7)
    # The host confirmed it — the ancestry fallback is not consulted.
    run.assert_not_called()
    stub._branch_merged_into_main.assert_not_called()
    record = stub.memory.record_task.call_args.args[0]
    assert record.outcome == "partial"
    assert "[landed]" in record.result_summary


# --- dequeue-chain integration (real gate, real controller) ----------------


def _chain_stub(landed_ancestor: bool) -> SimpleNamespace:
    """A stub agent with the *real* landed gate bound and other gates no-op."""
    stub = SimpleNamespace(
        config=SimpleNamespace(
            git_timeout=10,
            workspace=Path("/fake/agent"),
            branch_resolved_gate_enabled=True,
            branch_resolved_repetition_threshold=2,
            memory_dir=Path("/fake/mem"),
            pr_remediation_lap_budget=0,
        ),
        memory=MagicMock(),
        remediation_tracker=MagicMock(),
        # Proposal 087: the chokepoint reports unsettled, so the chain exercises
        # the real ancestry fallback driven by ``_branch_merged_into_main``.
        is_pr_settled=MagicMock(
            return_value=_landed_truth(branch_landed=False),
        ),
        _branch_merged_into_main=MagicMock(return_value=landed_ancestor),
        _gate_pr_remediation_merged=MagicMock(return_value=False),
        _gate_pr_remediation_host_closed=AsyncMock(return_value=False),
        _gate_pr_remediation_ready=MagicMock(return_value=False),
        _gate_pr_remediation_stood_down=AsyncMock(return_value=False),
        _gate_pr_remediation_merge_block=AsyncMock(return_value=False),
        _gate_pr_remediation_branch_resolved=AsyncMock(return_value=False),
        _preflight_pr_remediation=AsyncMock(return_value=False),
        _record_preflight_skip_outcome=AsyncMock(),
    )
    # Bind the genuine gate body so the chain exercises real ancestry logic.
    stub._gate_pr_remediation_landed = (
        PrRemediationController(stub)._gate_pr_remediation_landed
    )
    return stub


_PURE_QG_DIRECTIVE = (
    "Remediate PR #5: t\n\n## Problem\n"
    "After merging origin/main into PR #5, quality gates failed: pytest."
)


@pytest.mark.asyncio
async def test_chain_ancestor_evicts_landed_before_preflight() -> None:
    """An ancestor head evicts ``[landed]`` and never reaches preflight."""
    stub = _chain_stub(landed_ancestor=True)
    entry = PendingPRRequest(directive=_PURE_QG_DIRECTIVE, pr_branch="b5")
    with patch("maestro.agent.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        state = await PrRemediationController(stub).classify_dequeue(  # type: ignore[arg-type]
            _ctx(entry),
        )
    assert state is PrRemediationState.LANDED
    stub._preflight_pr_remediation.assert_not_awaited()
    record = stub.memory.record_task.call_args.args[0]
    assert "[landed]" in record.result_summary


@pytest.mark.asyncio
async def test_chain_non_ancestor_passes_through_to_preflight() -> None:
    """A non-ancestor head falls through the landed gate to the preflight gate."""
    stub = _chain_stub(landed_ancestor=False)
    entry = PendingPRRequest(directive=_PURE_QG_DIRECTIVE, pr_branch="b5")
    with patch("maestro.agent.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        state = await PrRemediationController(stub).classify_dequeue(  # type: ignore[arg-type]
            _ctx(entry),
        )
    # Landed gate fell through (preflight returns False) → remediable, and the
    # real preflight gate WAS reached.
    assert state is PrRemediationState.REMEDIABLE
    stub._preflight_pr_remediation.assert_awaited_once_with(entry)
    stub.memory.record_task.assert_not_called()


def _ctx(entry: PendingPRRequest) -> Any:
    from maestro.loop.pr_remediation import _DequeueContext

    return _DequeueContext(
        goal=entry.directive,
        pr_branch=entry.pr_branch,
        source="external",
        entry=entry,
    )
