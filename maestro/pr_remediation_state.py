"""Named states and markers for the PR-remediation lifecycle.

A ``pending_pr_requests`` item passes through a chain of dequeue gates —
merged-exclusion, merge-block, branch-resolved (costume-signature) and
preflight — before it becomes a task, and after the task runs its outcome is
downgraded or escalated. Historically the "states" a PR could be in were never
named: they were inferred at read time from a handful of string-literal protocol
markers (``[merge-block]``, ``[preflight]``, ``[branch-resolved]``, the
on-branch ``awaiting tend push`` costume) scattered across ``agent.py``,
``audit_checks.py``, ``remediation_tracker.py`` and ``loop/pr_remediation.py``
and matched by substring. Renaming any one of those literals silently broke the
escalation/repetition counters that parse them out of ``result_summary`` /
``key_findings``.

This module is the single definition site for that vocabulary (proposal 070,
step 1). :class:`Marker` holds the marker literals so every other module
references the value through an enum member rather than re-typing the string;
:class:`PrRemediationState` names the lifecycle states the later steps route on.

Invariant — the :class:`Marker` *values* are an on-disk protocol: they appear in
already-persisted ``task_history.jsonl`` records and are matched by the
remediation-tracker / audit sensors. They MUST stay byte-identical to the
historical literals, so this is a pure rename with zero wire-format change. The
marker-centralization test in ``tests/test_agent_pr_gates.py`` pins both
invariants: byte-identical values, and no marker literal escaping this module.
"""

from __future__ import annotations

from enum import Enum, StrEnum


class PrRemediationState(Enum):
    """The lifecycle state a ``pending_pr_requests`` item resolves to.

    A dequeue pass classifies the item into exactly one of these. The first
    five are the zero-cost gate exits (the item never becomes a real task); the
    last two cover the item that runs and the item the repetition gate stops.
    Step 1 named the original states; the transition table (step 2) consumes
    them as the ordered dequeue chain and the outcome routing off the recorded
    state (step 3) reads them post-task.
    """

    #: The PR already merged into ``main`` — the merged-exclusion gate evicts it.
    MERGED = "merged"
    #: The PR branch HEAD is already an ancestor of ``origin/main`` (squash,
    #: sibling PR, or a prior Tend merge) even though the PR *object* may still
    #: read OPEN — the commit-ancestry gate (``_gate_pr_remediation_landed``)
    #: fetches then asks ``git merge-base --is-ancestor`` and evicts the landed
    #: work at zero SDK cost. Complements :attr:`MERGED` (which asks only whether
    #: the PR object is merged) by settling the #2662 phantom on git truth.
    LANDED = "landed"
    #: The PR is open and an *observed* :class:`~maestro.pr_facts.PRReadiness`
    #: reports it ready-and-waiting — every code-clearable blocker is clear and
    #: the sole remaining gate is Thomas's approval (or Tend's push). The
    #: observed-readiness gate (``_gate_pr_remediation_ready``, proposal 089
    #: step 3) records a zero-cost ``partial`` ``[ready]`` and evicts. Unlike
    #: :attr:`STOOD_DOWN` (a stale ``(pr, head_sha)``-keyed verdict) this fact is
    #: recomputed live each pop, so a blocker reappearing on the same head
    #: re-admits the PR immediately. Sits between :attr:`LANDED` and
    #: :attr:`STOOD_DOWN` so the fresh fact supersedes the stale keyed verdict.
    READY_AND_WAITING = "ready_and_waiting"
    #: A prior remediation task authored a keyed stand-down verdict for this
    #: PR's current origin head while git-merge-truth still shows it unlanded
    #: with Tend PR the sole remaining pusher — the stand-down gate honors that
    #: verdict and evicts at zero SDK cost. (The gate was added by proposal 080,
    #: after step 1 named the original four gate-states; step 2's transition
    #: table names it so the full dequeue chain is data, not prose.)
    STOOD_DOWN = "stood_down"
    #: The code host reports the PR CLOSED without merging (``mergedAt`` null) —
    #: the human's own terminal. The host-closed gate
    #: (``_gate_pr_remediation_host_closed``) zero-cost-evicts it and routes the
    #: backing plan to the ``rejected`` terminal via the human/PR-close
    #: ``learn_from_rejection`` path. Distinct from the merged/landed settled arms
    #: (:attr:`MERGED` / :attr:`LANDED` both mean the branch *reached* ``main``):
    #: a closed-unmerged PR is settled yet un-landable by any lap, so re-admitting
    #: it indefinitely (its ``mergedAt`` reads null → "not merged") is the treadmill
    #: this arm stops.
    HOST_CLOSED_UNMERGED = "host_closed_unmerged"
    #: A ``BEHIND_MAIN`` / ``AWAITING_PUSH`` verdict — the merge-block gate
    #: zero-cost-skips it, records ``partial`` (never ``success``) and escalates.
    MERGE_BLOCKED = "merge_blocked"
    #: ``>= N`` undelivered Tend-clearable laps resolved on-branch without ever
    #: landing — the costume-signature gate zero-cost-skips and escalates once.
    BRANCH_RESOLVED = "branch_resolved"
    #: The branch already passes all quality gates — the preflight gate closes
    #: the request at zero SDK cost.
    PREFLIGHT_CLEAN = "preflight_clean"
    #: No gate fired — the item becomes a real remediation task.
    REMEDIABLE = "remediable"
    #: The remediation-repetition gate tripped on consecutive no-ops — the item
    #: is dequeued for good and escalated.
    REPETITION_EXHAUSTED = "repetition_exhausted"


class Marker(StrEnum):
    """On-disk protocol markers stamped into remediation task records.

    Each member's *value* is the exact substring the remediation-tracker and
    audit sensors match (case-insensitively) out of ``result_summary`` /
    ``key_findings``. ``StrEnum`` members format and compare as their string
    value, so ``f"{Marker.MERGE_BLOCK} ..."`` and ``Marker.MERGE_BLOCK in
    summary.lower()`` are byte-for-byte equivalent to the historical literals.
    """

    #: Stamped by the merge-block gate's zero-cost skip (``BEHIND_MAIN`` /
    #: ``AWAITING_PUSH``); recorded as ``partial`` and escalated.
    MERGE_BLOCK = "[merge-block]"
    #: Stamped by the commit-ancestry gate's zero-cost eviction when the PR
    #: branch HEAD is already an ancestor of ``origin/main`` (the cure landed via
    #: squash / sibling PR / prior Tend merge). Recorded as ``partial`` — this
    #: task caused nothing; the landing predates it — and counted as a no-op.
    LANDED = "[landed]"
    #: Stamped by the observed-readiness gate's zero-cost stand-down (proposal
    #: 089): the PR is open and every code-clearable blocker is clear, so the
    #: work is done and the PR is in Thomas's court. Recorded as ``partial`` —
    #: never ``success`` (this lap landed nothing) — and counted as a no-op.
    READY = "[ready]"
    #: Stamped by the preflight gate's zero-cost close (gates already green).
    PREFLIGHT = "[preflight]"
    #: Stamped by the host-closed gate's zero-cost eviction: the code host reports
    #: the PR CLOSED without merging (the human's terminal). Recorded ``partial``
    #: — this lap landed nothing; the human closed it — and counted a no-op.
    HOST_CLOSED = "[host-closed]"
    #: Stamped by the costume-signature gate's own zero-cost skip; excluded from
    #: the costume count so the latch cannot re-arm itself.
    BRANCH_RESOLVED = "[branch-resolved]"
    #: The on-branch re-lap costume — a fresh remediation that resolved on the
    #: branch yet landed nothing; counts as a *forced* quiet.
    AWAITING_TEND_PUSH = "awaiting tend push"
    #: A lap that PUSHED successfully — the resolution commit reached
    #: ``origin/<pr_branch>`` but the PR has not yet been reviewed and merged
    #: into ``main`` by the human. Distinct from ``AWAITING_TEND_PUSH`` (which
    #: now means only what it says: the push has NOT happened). Delivery
    #: succeeded, so this is neither a *forced* quiet nor an on-branch costume.
    #: Value is all-lowercase like every other member so the case-insensitive
    #: substring match (``marker in blob.lower()``) finds it (proposal 091
    #: step 2).
    DELIVERED_AWAITING_MERGE = "delivered to pr, awaiting review/merge"
    #: Stamped when a *chosen* ``[stood-down]`` is refused because git proves the
    #: PR branch is ahead of origin (``git rev-list --count <branch> --not
    #: --remotes=origin > 0``): an ahead branch is an owned, pushable act, never
    #: an external quiet. Recorded as ``partial`` — the lap landed nothing — in
    #: place of the false external-quiet verdict, and paired with a fire-or-close
    #: ``next_task_directive``.
    OWNED_PUSH_AVAILABLE = "[owned-push-available]"
    #: Stamped when a mechanical owned-branch push subprocess exited 0 but a
    #: same-turn ``verify_push_advanced_remote_head`` read showed
    #: ``origin/<branch>`` did **not** advance to the pushed SHA (a no-op push,
    #: or a fetch/read failure). 'Delivered' binds to that physical read, not the
    #: subprocess return, so an unconfirmed advance is recorded ``partial`` — the
    #: head never moved by our hand — and routed to fire-or-close. As a
    #: non-delivery it is registered alongside ``OWNED_PUSH_AVAILABLE`` in every
    #: parallel push-marker set (audit rules 10/14, the forced-quiet /
    #: fetch-required / costume sensors, the #2825 cooldown bypass) so the marker
    #: swap keeps audit/sensor coverage.
    PUSH_UNCONFIRMED = "[push-unconfirmed]"
    #: Stamped by the uncommitted-edit arm of the ahead-of-origin actuator when
    #: an uncommitted working-tree edit failed the quality gates: ``git reset
    #: --hard`` rewound the tree so nothing was committed or pushed. Recorded
    #: ``partial`` — the head never moved by our hand — with no fire-or-close
    #: directive (the unverified edit was discarded, so there is nothing to
    #: push or close).
    UNCOMMITTED_RED = "[red]"
    #: Stamped by the committed-but-unpushed arm of the ahead-of-origin actuator
    #: when a *committed* local head ahead of ``origin/<branch>`` failed the
    #: quality gates: ``git reset --hard origin/<branch>`` rewound the local head
    #: back to the remote so the red commit is discarded and nothing was pushed —
    #: a red head never reaches origin. Recorded ``partial`` (the remote head
    #: never advanced by our hand) with no fire-or-close directive: the broken
    #: commit was discarded, so there is nothing owed to push or close.
    COMMITTED_RED = "[committed-red]"
    #: Stamped by a record-time landing arm (or the post-merge tend arm) when the
    #: quality gates went red but proposal 124's attributor
    #: (:func:`~maestro.gate_observation.attribute`) classed that red
    #: ``INHERITED`` — the identical failure identity is already present in a
    #: baseline of the branch's own base, so the red belongs to the base rather
    #: than to this branch's diff. Nothing was rewound, committed, or pushed, and
    #: the head never moved: the arm held the tree/head/merge in place and
    #: recorded a ``partial`` naming the inherited blocker, leaving the base
    #: repair to fix the shared red. Behind default-off
    #: ``[loop] gate_observation_enabled``; flag-off no arm ever reaches this
    #: verdict and today's rewind path is byte-identical.
    INHERITED_RED = "[inherited-red]"


class PrRemediationDisposition(Enum):
    """The coarse action a classified ``pending_pr_requests`` item resolves to.

    Proposal 080, step 5 (Phase 4) folds the eight dequeue-face
    :class:`PrRemediationState` outcomes into the four actions the dequeue site
    actually takes, giving proposal 070's state enum a single run-vs-skip
    decision site. Only :attr:`RUN` lets the item become a real LLM remediation
    task; the other three are the zero-cost skips, distinguished by *why* the
    item is evicted rather than by which gate fired. The grouping composes with
    070's planned explicit state machine, which can route off this disposition
    once the legacy four (now six) gates are retired.
    """

    #: No gate fired — the item becomes a real LLM remediation task.
    RUN = "run"
    #: A ``BEHIND_MAIN`` / ``AWAITING_PUSH`` blocker Tend PR clears mechanically
    #: (:attr:`PrRemediationState.MERGE_BLOCKED`) — skip at zero SDK cost.
    TEND_WILL_CLEAR = "tend_will_clear"
    #: A terminal hand-up to Thomas / Tend — the costume-signature gate
    #: (:attr:`PrRemediationState.BRANCH_RESOLVED`), an honored stand-down
    #: (:attr:`PrRemediationState.STOOD_DOWN`), an observed ready-and-waiting PR
    #: whose work is done and now in Thomas's court
    #: (:attr:`PrRemediationState.READY_AND_WAITING`), or the repetition gate
    #: (:attr:`PrRemediationState.REPETITION_EXHAUSTED`). Skip.
    HANDOFF = "handoff"
    #: The PR is settled and nothing is owed — the PR object is merged
    #: (:attr:`PrRemediationState.MERGED`), its branch HEAD is an ancestor of
    #: ``origin/main`` (:attr:`PrRemediationState.LANDED`), the merged tree
    #: already passes every gate (:attr:`PrRemediationState.PREFLIGHT_CLEAN`), or
    #: the host reports it CLOSED-and-unmerged — the human's terminal, un-landable
    #: by any lap (:attr:`PrRemediationState.HOST_CLOSED_UNMERGED`). Evict at zero
    #: SDK cost — there is nothing left to remediate.
    EVICT_MERGED = "evict_merged"


# The single mapping from a dequeue-face lifecycle state to its disposition.
# Total over every :class:`PrRemediationState` member so a future state cannot
# be added without choosing a disposition — the parity test in
# ``tests/test_agent_pr_gates.py`` asserts exhaustiveness, and the bare ``[]``
# lookup in :func:`classify_pr_remediation_disposition` fails loud on a gap.
# The :attr:`PrRemediationState.REMEDIABLE` → :attr:`PrRemediationDisposition.RUN`
# pairing is load-bearing: it is the *only* state that maps to ``RUN``, so a
# caller deriving run-vs-skip from this disposition is byte-identical to the
# legacy ``state is not REMEDIABLE`` comparison.
_STATE_TO_DISPOSITION: dict[PrRemediationState, PrRemediationDisposition] = {
    PrRemediationState.MERGED: PrRemediationDisposition.EVICT_MERGED,
    PrRemediationState.LANDED: PrRemediationDisposition.EVICT_MERGED,
    PrRemediationState.PREFLIGHT_CLEAN: PrRemediationDisposition.EVICT_MERGED,
    PrRemediationState.HOST_CLOSED_UNMERGED: (
        PrRemediationDisposition.EVICT_MERGED
    ),
    PrRemediationState.MERGE_BLOCKED: PrRemediationDisposition.TEND_WILL_CLEAR,
    PrRemediationState.READY_AND_WAITING: PrRemediationDisposition.HANDOFF,
    PrRemediationState.STOOD_DOWN: PrRemediationDisposition.HANDOFF,
    PrRemediationState.BRANCH_RESOLVED: PrRemediationDisposition.HANDOFF,
    PrRemediationState.REPETITION_EXHAUSTED: PrRemediationDisposition.HANDOFF,
    PrRemediationState.REMEDIABLE: PrRemediationDisposition.RUN,
}


def classify_pr_remediation_disposition(
    state: PrRemediationState,
) -> PrRemediationDisposition:
    """Map a dequeue-face lifecycle *state* to its coarse disposition.

    The single run-vs-skip decision site for the dequeue face (proposal 080,
    step 5 / Phase 4). :attr:`PrRemediationDisposition.RUN` is returned only for
    :attr:`PrRemediationState.REMEDIABLE`; every gate-fired state maps to one of
    the three zero-cost skips. Routing ``dequeue_gates`` through this mapping is
    byte-identical to the legacy chain by construction — the gates' side effects
    (the zero-cost ``TaskRecord``, the escalation notify) have already run by the
    time their state is returned, so only the final boolean is re-derived, from
    the disposition instead of the bare state comparison.

    Raises :class:`KeyError` on an unmapped state — fail loud rather than
    silently defaulting a newly-added state to run-or-skip.
    """
    return _STATE_TO_DISPOSITION[state]
