"""Work-in-progress primacy — one pure owner of "finish owned work before new".

Proposal 092, step 1. The loop chooses what to work on next through an emergent
pile: ``_run_task_phase``'s flat precedence, ``generate_task``'s gate chain over
the single ``next_task_directive`` slot, and the audit→directive bridge. No
component reads open-owned-PR state and self-model state *together* to answer one
question: "is there work I already started that I should finish before starting
anything new?" — so a self-authored PR sitting on a ``maestro/evolve/*`` branch
with a blocker only the loop can clear (a merge conflict, an un-answered AI
review thread) goes unfinished while the loop spends its budget minting new
machinery.

This module is the first piece of a single Work-In-Progress owner:
:func:`select_task_focus` reads the already-observed per-PR
:class:`~maestro.pr_facts.PRReadiness` fact for every open owned PR and
answers either :class:`TendOwnedPR` ("finish this PR, here is its owned blocker")
or :class:`Discretionary` ("no owned WIP — discretionary work is allowed"). It is
pure, deterministic, and zero-SDK: it observes nothing itself, reuses the
authoritative :func:`~maestro.remediation_tracker.classify_owned_blocker` /
:func:`~maestro.remediation_tracker.classify_blocker_ownership` classifiers
rather than inventing a rival owned-vs-external set, and never governs a landed
PR or a non-owned (human-authored) one. The chokepoint gate and the audit-bridge
guard that consume it live in :mod:`maestro.task_gates` / :mod:`maestro.agent`;
this module only decides.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from maestro.pr_facts import PRReadiness
from maestro.pr_owed_act import OwedAct
from maestro.remediation_tracker import (
    BLOCKER_OWNERSHIP_OWNED,
    MERGE_BLOCK_CI_FAILING,
    MERGE_BLOCK_THREADS_UNRESOLVED,
    classify_blocker_ownership,
    classify_owned_blocker,
)


@dataclass(frozen=True)
class OpenOwnedPR:
    """Identity and age of one open own ``maestro/evolve/*`` pull request.

    The minimal projection :func:`select_task_focus` needs off each
    :class:`~maestro.vcs.PrMergeState`: :attr:`pr_number` to key readiness on and
    name in the directive, and the ISO-8601 :attr:`created_at` to order the open
    PRs oldest-first. Frozen so a focus decision cannot mutate its inputs.
    """

    pr_number: int
    created_at: str


@dataclass(frozen=True)
class TendOwnedPR:
    """Focus verdict: finish this open owned PR before starting new work.

    :attr:`pr_number` is the PR to converge; :attr:`owned_blocker` is the
    self-clearable :data:`~maestro.remediation_tracker.MERGE_BLOCK_CONFLICTING` /
    :data:`~maestro.remediation_tracker.MERGE_BLOCK_THREADS_UNRESOLVED` code the
    loop must clear on it — or, when the focus fired on the owed act rather
    than a branch flag (proposal 137, step 11), the code
    :func:`_code_owed_blocker` reads off the readiness that owed act was
    derived from.
    """

    pr_number: int
    owned_blocker: str


@dataclass(frozen=True)
class Discretionary:
    """Focus verdict: no owned WIP with a self-clearable blocker — new work is allowed."""


#: The two possible task-focus verdicts :func:`select_task_focus` returns.
TaskFocus = TendOwnedPR | Discretionary


def select_task_focus(
    open_prs: Sequence[OpenOwnedPR],
    readiness_by_pr: Mapping[int, PRReadiness],
    owed_act_by_pr: Mapping[int, OwedAct | None] = MappingProxyType({}),
) -> TaskFocus:
    """Answer "finish owned work, or is discretionary work allowed?" — purely.

    Walks *open_prs* oldest-first (by :attr:`OpenOwnedPR.created_at`, then
    :attr:`OpenOwnedPR.pr_number` as a stable tie-break) and, for each PR whose
    :class:`~maestro.pr_facts.PRReadiness` is present in *readiness_by_pr*,
    derives the blocker the same way every PR-servicing face does:

    * :meth:`PRReadiness.merge_block_reason` recovers the merge-block reason code
      from the branch flags,
    * :func:`~maestro.remediation_tracker.classify_owned_blocker` promotes it to
      ``THREADS_UNRESOLVED`` when an un-answered review thread is open,
    * :func:`~maestro.remediation_tracker.classify_blocker_ownership` decides
      whether resolving it is *mine* to drive.

    Returns :class:`TendOwnedPR` for the first PR whose blocker classifies
    :data:`~maestro.remediation_tracker.BLOCKER_OWNERSHIP_OWNED`, else
    :class:`Discretionary`. A landed PR (:attr:`PRReadiness.branch_landed`) is
    never owned WIP — there is nothing left to finish — and a PR with no
    readiness observation is skipped rather than guessed at. Pure, deterministic,
    zero SDK cost.

    *owed_act_by_pr* (proposal 137, step 11) is the second, *weaker* arm. The
    branch-flag codes above see only the merge-block axis, so an own PR that is
    0-behind, 0-conflict and thread-free while simply **red** — a failing quality
    gate, the single most common shape an own PR gets stuck in — carries
    :data:`~maestro.remediation_tracker.MERGE_BLOCK_NONE`, classifies external,
    and used to read :class:`Discretionary` while the loop's only open PR sat on
    a red it owns. When the owned-merge-blocker arm does not fire and the
    single owner of "what does this PR owe me right now?"
    (:func:`~maestro.pr_owed_act.classify_pr_owed_act`, read through the caller's
    map) reports :attr:`~maestro.pr_owed_act.OwedAct.CODE_OWED`, that PR is owned
    WIP too, reported under the blocker :func:`_code_owed_blocker` reads back
    off that same observation rather than a fixed code. The member is
    only ever *compared* against, never minted here, so the owed-act chokepoint
    guard stays clean. An omitted or empty map reproduces today's verdict for
    every input.
    """
    for pr in sorted(open_prs, key=lambda p: (p.created_at, p.pr_number)):
        readiness = readiness_by_pr.get(pr.pr_number)
        if readiness is None or readiness.branch_landed:
            continue
        owned_blocker = classify_owned_blocker(
            readiness.merge_block_reason(), readiness.unresolved_threads,
        )
        if classify_blocker_ownership(owned_blocker) == BLOCKER_OWNERSHIP_OWNED:
            return TendOwnedPR(
                pr_number=pr.pr_number, owned_blocker=owned_blocker,
            )
        if owed_act_by_pr.get(pr.pr_number) is OwedAct.CODE_OWED:
            return TendOwnedPR(
                pr_number=pr.pr_number,
                owned_blocker=_code_owed_blocker(readiness),
            )
    return Discretionary()


def _code_owed_blocker(readiness: PRReadiness) -> str:
    """Name the blocker a ``CODE_OWED`` verdict actually rests on (137, step 11).

    :func:`~maestro.pr_owed_act.owed_act_from_readiness_snapshot` folds three
    independent grounds into one ``CODE_OWED``: a concluded CI red, an
    unresolved review thread / unaddressed overflow finding, and a plain
    branch-flag merge block the ownership arm above did not claim. Reporting
    all three as :data:`~maestro.remediation_tracker.MERGE_BLOCK_CI_FAILING`
    would name a red the readiness never measured, so the code is read back off
    the *same* observation the owed act was derived from, in the owed act's own
    precedence: a measured ``FAILING`` CI first, then review work under
    :data:`~maestro.remediation_tracker.MERGE_BLOCK_THREADS_UNRESOLVED`, else
    whatever branch flag :meth:`~maestro.pr_facts.PRReadiness.merge_block_reason`
    already reports. Pure; invents nothing the readiness does not carry.
    """
    if readiness.ci_state == "FAILING":
        return MERGE_BLOCK_CI_FAILING
    if readiness.unresolved_threads or readiness.unaddressed_overflow:
        return MERGE_BLOCK_THREADS_UNRESOLVED
    return readiness.merge_block_reason()
