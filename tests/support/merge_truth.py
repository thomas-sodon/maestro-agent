"""Shared ``settled_oracle`` builders for the merge-truth chokepoint (#2958).

``EvolutionEngine.check_merged_plans`` and ``EvolutionEngine.tend_evolution_prs``
both take a required keyword-only ``settled_oracle`` — the agent's
``MaestroAgent.is_pr_settled`` merge-truth chokepoint (proposal 087) — rather
than calling raw host merge primitives. Every test that drives either method has
to supply one, so the two builders live here instead of being copy-pasted into
each test module.
"""

from __future__ import annotations

from collections.abc import Callable

from maestro.pr_facts import MergeState, MergeTruth


def merge_truth(
    pr_number: int,
    *,
    settled: bool,
    branch_landed: bool,
    state: MergeState,
) -> MergeTruth:
    """Build one :class:`~maestro.pr_facts.MergeTruth` observation.

    The provenance fields are fixed (``source="host_api"``, ``observed_at=1.0``)
    because no caller asserts on them — the decisions under test read only
    :attr:`~maestro.pr_facts.MergeTruth.branch_landed` and
    :attr:`~maestro.pr_facts.MergeTruth.settled`.
    """
    return MergeTruth(
        pr_number=pr_number,
        settled=settled,
        state=state,
        branch_landed=branch_landed,
        source="host_api",
        observed_at=1.0,
    )


def merge_oracle(
    truths: dict[int, MergeTruth] | None = None,
) -> Callable[[int], MergeTruth]:
    """A ``settled_oracle`` over a ``{pr_number: MergeTruth}`` map.

    Any PR absent from *truths* (and every PR when it is omitted) is reported
    still-open — unsettled, not landed — mirroring the chokepoint's verdict for
    a PR that is neither merged nor closed.
    """
    known = truths or {}

    def _observe(pr_number: int) -> MergeTruth:
        return known.get(
            pr_number,
            merge_truth(
                pr_number, settled=False, branch_landed=False, state="OPEN",
            ),
        )

    return _observe
