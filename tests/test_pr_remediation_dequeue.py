"""Same-cycle eviction of a reconciled-settled PR at the remediation dequeue.

The standing settlement reconciler
(:func:`~maestro.audit_checks_settlement.reconcile_settled_verdicts`) upserts every merged
PR into the durable ``settled_verdicts.jsonl`` ledger each cycle and threads that
set into the phantom audit rules as their pre-filter. Before this change the
merged dequeue gate (``PrRemediationController.gate_merged``) ignored that
reconciled set and re-read merge-truth through the per-iteration
``is_pr_settled`` memo — which, warmed to a stale pre-merge read earlier in the
same iteration, deferred eviction of an already-landed PR by exactly one cycle,
deterministically re-serving landed work as a remediation task (the measured
PR-READMISSION rate of 1.00).

These tests pin the fix: a PR present in the current-cycle reconciled *merged*
settled set is evicted this same cycle, with **no** per-PR ``is_pr_settled``
merge-oracle call; a PR absent from that set is untouched by the reconciled path
and falls through to the existing oracle-based eviction.

The controller is constructed against a narrow duck-typed stub agent — the same
harness idiom as ``tests/test_loop_pr_remediation.py`` — so no real host/git I/O
or filesystem writes occur.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from maestro.loop.pr_remediation import PrRemediationController
from maestro.pr_facts import MergeTruth


def _truth(state: str, *, settled: bool, branch_landed: bool) -> MergeTruth:
    """Build a :class:`MergeTruth` observation for the oracle-fallback path."""
    return MergeTruth(
        pr_number=7,
        settled=settled,
        state=state,  # type: ignore[arg-type]
        branch_landed=branch_landed,
        source="host_api",
        observed_at=1.0,
    )


def _stub(
    *,
    merged_settled: frozenset[int],
    reconciler_enabled: bool = True,
    is_pr_settled: Any = None,
) -> SimpleNamespace:
    """Minimal duck-typed agent exposing only what ``gate_merged`` touches."""
    if is_pr_settled is None:
        # Default: a stale OPEN observation. A gate that (wrongly) consults the
        # oracle for a reconciled-settled PR would read *not landed* here and
        # fail to evict — exactly the pre-fix one-cycle-lag behavior — so the
        # reconciled-eviction test proves the oracle is bypassed, not merely
        # that it happens to agree.
        is_pr_settled = MagicMock(
            return_value=_truth("OPEN", settled=False, branch_landed=False),
        )
    return SimpleNamespace(
        config=SimpleNamespace(
            settlement_reconciler_enabled=reconciler_enabled,
        ),
        settled_verdicts=SimpleNamespace(
            merged_settled_prs=MagicMock(return_value=merged_settled),
        ),
        is_pr_settled=is_pr_settled,
    )


def _controller(stub: SimpleNamespace) -> PrRemediationController:
    return PrRemediationController(stub)  # type: ignore[arg-type]


def test_reconciled_settled_pr_evicted_same_cycle_without_oracle() -> None:
    """A PR in the reconciled merged set is evicted now, oracle untouched.

    Criteria 1+2+3: same-cycle eviction (not next-cycle deferral) *and* no
    per-PR ``is_pr_settled`` merge-oracle call for the settled PR. The stale
    OPEN oracle stub would fail to evict if consulted, so this reproduces the
    same-cycle readmission before the change (the gate returned ``False`` and
    the PR was re-served) and passes after it.
    """
    stub = _stub(merged_settled=frozenset({7}))
    gated = _controller(stub).gate_merged("Remediate PR #7: t", "b7")
    assert gated is True
    stub.is_pr_settled.assert_not_called()
    stub.settled_verdicts.merged_settled_prs.assert_called_once_with()


def test_unsettled_pr_falls_through_to_oracle() -> None:
    """Criterion 4: a PR absent from the reconciled set is left to the oracle.

    The reconciled path does not fire, so the existing ``is_pr_settled``
    eviction behavior is preserved unchanged — here an OPEN observation keeps
    the PR queued (``False``).
    """
    stub = _stub(merged_settled=frozenset({9}))  # 9, not the dequeued 7
    gated = _controller(stub).gate_merged("Remediate PR #7: t", "b7")
    assert gated is False
    stub.is_pr_settled.assert_called_once_with(7)


def test_unsettled_pr_landed_via_oracle_still_evicts() -> None:
    """Oracle-path eviction is intact when the reconciled set misses.

    A PR absent from the reconciled merged set but reported MERGED by the live
    oracle is still evicted — the reconciled short-circuit is additive, never a
    replacement of the chokepoint read.
    """
    stub = _stub(
        merged_settled=frozenset(),
        is_pr_settled=MagicMock(
            return_value=_truth("MERGED", settled=True, branch_landed=True),
        ),
    )
    gated = _controller(stub).gate_merged("Remediate PR #7: t", "b7")
    assert gated is True
    stub.is_pr_settled.assert_called_once_with(7)


def test_reconciler_disabled_ignores_settled_ledger() -> None:
    """With the reconciler master switch off, the ledger is not consulted.

    Byte-identical to pre-097 behavior: the gate falls straight through to the
    oracle even for a PR that happens to sit in a stale ledger, so a disabled
    reconciler never mis-evicts on an unmaintained set.
    """
    stub = _stub(merged_settled=frozenset({7}), reconciler_enabled=False)
    gated = _controller(stub).gate_merged("Remediate PR #7: t", "b7")
    # OPEN oracle observation ⇒ not evicted; the ledger membership is ignored.
    assert gated is False
    stub.settled_verdicts.merged_settled_prs.assert_not_called()
    stub.is_pr_settled.assert_called_once_with(7)


def test_non_remediation_goal_skips_reconciled_and_oracle() -> None:
    """A goal without the ``Remediate PR #<n>:`` header touches neither read."""
    stub = _stub(merged_settled=frozenset({7}))
    gated = _controller(stub).gate_merged("do something else", None)
    assert gated is False
    stub.settled_verdicts.merged_settled_prs.assert_not_called()
    stub.is_pr_settled.assert_not_called()
