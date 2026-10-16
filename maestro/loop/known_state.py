"""Known-state context composers carved out of ``maestro.agent``.

``contemplate()``'s measured-data block is assembled from many small, pure
sensors. Most fold into their line in one call, but the ``PR-LAPS`` line reads
*three* stores — the per-PR unlanded-lap counts, the escalation ledger's active
standdowns, and (proposal 135) the validity of each latched claim against the
live owed-act reading derived from persisted readiness snapshots. Inlining that
composition in ``maestro/agent.py`` would have grown a file then held shrink-only by
``tests/test_source_file_size.py``, and the three-store fold is a unit in its own
right, so it lives here — the same per-concern sibling split
``loop/task_setup.py`` and ``loop/phases.py`` made before it.

Nothing here is a phase or a policy: these are pure compositions over stores the
caller already holds open. Zero SDK cost, and no host, merge-oracle or network
call is added.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from maestro.escalation_bridge import (
    EscalationLedgerBackend,
    standdown_claims,
    standdown_delivery_times,
)
from maestro.memory import TaskRecord
from maestro.remediation_tracker import (
    format_pr_lap_summary,
    pr_number_from_remediation_goal,
)
from maestro.standdown_validity import measure_refuted_standdowns

# Marker appended to a recent remediation task's contemplation line when the PR
# it cites has since settled per the settlement ledger. Read fresh at
# contemplation time so a merged lineage is narrated as landed rather than as
# the pre-merge ``not yet landed per merge-truth`` verdict its cached
# ``result_summary`` still records (audit #1263).
SINCE_MERGED_MARKER = "[since merged]"


def annotate_settled_remediation_feed(
    recent: list[TaskRecord],
    load_settled: Callable[[], frozenset[int]],
) -> list[TaskRecord]:
    """Cross-check each recent remediation task's cited PR against settlement.

    For every ``Remediate PR #<n>`` task in *recent* whose PR number *load_settled*
    reports settled, return a copy of the list with that task's rendered findings
    prefixed by :data:`SINCE_MERGED_MARKER`, so the recent-tasks context the
    contemplation reflection reads narrates a merged lineage as landed rather than
    as the pre-merge ``not yet landed per merge-truth`` verdict its cached summary
    still carries (audit #1263). The prefix leads the findings so it survives the
    reflection renderer's per-line truncation.

    *load_settled* — a fetch-free settlement-ledger read — is consulted **at most
    once, and only when at least one task cites a PR number**, so the common
    PR-less window pays nothing. It reaches no SDK, push, or merge seam. Non-
    remediation tasks, PR-less tasks, and unsettled lineages pass through by
    identity (the same object), so nothing but a confirmed-settled lineage's line
    changes. Pure and zero-SDK given its injected reader.
    """
    prs_by_index: dict[int, int] = {}
    for index, task in enumerate(recent):
        pr_number = pr_number_from_remediation_goal(task.goal or "")
        if pr_number is not None:
            prs_by_index[index] = pr_number
    if not prs_by_index:
        return recent
    settled = load_settled()
    annotated = list(recent)
    for index, pr_number in prs_by_index.items():
        if pr_number in settled:
            task = recent[index]
            findings = task.key_findings or task.result_summary or ""
            marked = f"{SINCE_MERGED_MARKER} {findings}".rstrip()
            annotated[index] = replace(task, key_findings=marked)
    return annotated


def compose_pr_lap_context(
    unlanded_laps: dict[int, int],
    ledger: EscalationLedgerBackend,
    task_history: list[dict[str, object]],
    budget: int,
) -> str:
    """Render the ``PR-LAPS`` line, annotating each PR with its standdown state.

    Folds the three reads the line needs into one call:

    * *unlanded_laps* — the ``{pr: count}`` the caller already measured (it feeds
      the ``UNLANDED-REMEDIATION`` line too, so the host-first merge-truth oracle
      runs once per iteration, not twice);
    * *ledger* — the escalation standdowns, read **once** and folded into the
      three views the line needs: presence (which PRs are suppressed, annotated
      ``[stood-down]``), each record's claim, and each record's delivery time
      (the staleness floor below which a readiness snapshot cannot refute the
      claim it predates). One read keeps the three views from skewing against
      each other mid-iteration;
    * *task_history* — the in-memory task records whose persisted
      ``pr_readiness_snapshot`` yields each PR's live owed act, so
      :func:`~maestro.standdown_validity.measure_refuted_standdowns` can name the
      latches the live reading contradicts (annotated ``[standdown-refuted]``
      instead) without a single host call.

    Returns ``""`` when *budget* is non-positive or no PR qualifies, exactly as
    :func:`~maestro.remediation_tracker.format_pr_lap_summary` does — the caller
    injects only when there is something to report. Pure apart from the single
    ledger read.
    """
    records = ledger.read()
    return format_pr_lap_summary(
        unlanded_laps,
        set(records),
        budget,
        refuted_prs=measure_refuted_standdowns(
            standdown_claims(records),
            task_history,
            delivered_at=standdown_delivery_times(records),
        ),
    )
