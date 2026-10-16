"""Mechanical outcome authority — the landing verdict as a pure function of git.

Proposal 105. The loop once let the model narrate a remediation outcome in prose
and then relied on a record-time apparatus (:mod:`maestro.outcome_grounding`)
plus post-hoc audit rules to catch the cases where that prose disagreed with
whether the PR actually landed. The durable fix is to make the landing verdict a
pure function of observed git-truth, computed from the merge-truth chokepoint
(:meth:`maestro.agent.MaestroAgent.is_pr_settled`) — never reading the summary
text as a status signal.

:func:`decide_outcome` is that authority. It reads **no** summary or findings
text; the only completion signal it takes from *candidate_outcome* is whether the
task failed, which is derived upstream from step/criteria assessment, not from
prose. Pure, zero SDK cost, no I/O: the caller supplies the git facts on
:class:`OutcomeFacts`.

``run()`` records every divergence from the narration path to the activation
ledger and then adopts this verdict as a remediation-PR record's persisted
``TaskRecord.outcome`` — the unconditional, sole author of the landing verdict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from maestro.pr_remediation_state import Marker
from maestro.remediation_tracker import MERGE_BLOCK_AWAITING_PUSH

if TYPE_CHECKING:
    from maestro.types import TaskOutcome


@dataclass(frozen=True)
class OutcomeFacts:
    """Injected git-truth for the landing dimension of a task verdict.

    Both fields are booleans the caller reads from the merge-truth chokepoint;
    this module performs no I/O of its own.
    """

    #: A PR-landing dimension applies to this record (there is a PR to ground
    #: the verdict against). ``False`` for a task with no remediation PR.
    landing_relevant: bool
    #: Git shows the PR's branch reached ``origin/main`` (``is_pr_settled(...)
    #: .branch_landed``). Only meaningful when :attr:`landing_relevant`.
    landing_confirmed: bool
    #: This remediation lap advanced its owed remote head — the resolution
    #: reached ``origin/<pr_branch>`` (``classify_lap_delivery`` returned
    #: ``"DELIVERING"``). Defaulted, so :func:`decide_outcome` ignores it and
    #: every landing verdict is byte-for-byte unchanged; read only by
    #: :func:`partial_provenance` to annotate a ``partial`` record's delivery
    #: provenance. Only meaningful when :attr:`landing_relevant`.
    lap_delivering: bool = False


def decide_outcome(
    candidate_outcome: TaskOutcome,
    facts: OutcomeFacts,
) -> TaskOutcome:
    """Return the git-grounded landing verdict — never reads prose.

    Logic, in order:

    * a ``"failure"`` *candidate_outcome* is returned unchanged — a landing
      check never rescues or manufactures a failure;
    * when :attr:`OutcomeFacts.landing_relevant` is ``False`` the candidate is
      returned unchanged — there is no PR to ground against;
    * otherwise the verdict is ``"success"`` iff
      :attr:`OutcomeFacts.landing_confirmed`, else ``"partial"``.

    The function takes **no** summary/findings parameter: the only completion
    signal it reads from *candidate_outcome* is whether the task failed, which is
    derived upstream from step/criteria assessment, not from narration. Pure,
    zero SDK cost, no I/O.
    """
    if candidate_outcome == "failure":
        return candidate_outcome
    if not facts.landing_relevant:
        return candidate_outcome
    return "success" if facts.landing_confirmed else "partial"


#: Provenance note appended to a ``partial`` record whose remediation lap
#: advanced its owed remote head — the resolution is on the PR branch and only a
#: human merge remains, so the ``partial`` is delivery pending merge, not
#: make-work. Bracketed marker + explanation, carrying no resolution keyword,
#: shaped like :data:`maestro.outcome_grounding._FRAME_DISCHARGE_MISMATCH_NOTE`;
#: it deliberately contains none of the push-family marker strings a downstream
#: ``key_findings`` scanner reads (proposal 116).
_LAP_DELIVERING_NOTE = (
    "[delivering] this partial lap advanced its owed remote head — the "
    "resolution is on the PR branch and a human merge is the only remaining "
    "act, so the partial is delivery pending merge, not make-work."
)

#: Provenance note appended to a ``partial`` record whose remediation lap
#: advanced no owed remote head — an on-branch re-lap, an owned-but-unpushed
#: head, a no-op push, a merge-block skip, or a rewound red head. Bracketed
#: marker + explanation, carrying no resolution keyword.
_NULL_LAP_NOTE = (
    "[null-lap] this partial lap advanced no owed remote head — an on-branch "
    "re-lap, an owned-but-unpushed head, a no-op push, a merge-block skip, or a "
    "rewound red head, not delivery."
)


def partial_provenance(
    outcome: TaskOutcome,
    facts: OutcomeFacts,
) -> str | None:
    """Return the delivery-provenance note for a ``partial`` landing verdict.

    Measurement / provenance only — never a verdict, never a reward. Given the
    *decided* landing *outcome* (from :func:`decide_outcome`) and its
    :class:`OutcomeFacts`, returns:

    * :data:`_LAP_DELIVERING_NOTE` (``[delivering]``) when *outcome* is
      ``"partial"``, the record is landing-relevant, and the lap advanced its
      owed remote head (:attr:`OutcomeFacts.lap_delivering`) — a delivered-but-
      gated partial;
    * :data:`_NULL_LAP_NOTE` (``[null-lap]``) when *outcome* is ``"partial"``
      and landing-relevant but the lap advanced no owed head — a make-work
      partial;
    * ``None`` otherwise — a ``success`` / ``failure`` verdict has no delivery
      provenance to record, and a non-landing-relevant ``partial`` (no PR to
      ground against) has no owed head the note could describe.

    Appended to a record's ``key_findings`` at the §5d record-time site so a
    delivered-but-gated ``partial`` is distinguishable from a make-work one for
    later sensor and prior-context reads. Pure, zero SDK cost, no I/O — reads
    only the decided verdict and the injected git-truth facts, never narration.
    """
    if outcome != "partial" or not facts.landing_relevant:
        return None
    return _LAP_DELIVERING_NOTE if facts.lap_delivering else _NULL_LAP_NOTE


# ──────────────────────────────────────────────────────────────────────
# Delivery clause bound to push-truth — forbid "delivered to pr" when the
# recorded blocker is AWAITING_PUSH (a push I still owe).
# ──────────────────────────────────────────────────────────────────────

#: The honest delivery clause — the resolution reached ``origin/<pr_branch>`` and
#: only a human review/merge remains. Byte-identical to
#: :attr:`Marker.DELIVERED_AWAITING_MERGE` so a *retained* clause is still read as
#: genuine delivery by the delivery sensors
#: (:func:`maestro.remediation_tracker.classify_lap_delivery`). Kept for an
#: external-only blocker (review/CI).
_DELIVERED_CLAUSE = str(Marker.DELIVERED_AWAITING_MERGE)

#: The clause substituted when the recorded blocker is ``AWAITING_PUSH`` and the
#: push was *attempted and confirmed-failed* (``push_delivered is False``): the
#: local commits did not reach ``origin/<branch>``, so HEAD is genuinely ahead —
#: the record asserts that git fact. Carries **no** ``delivered to pr`` substring
#: (so a downstream :func:`classify_lap_delivery` scan cannot misread a push-owed
#: lap as DELIVERING) and none of the push-family protocol markers a sensor keys
#: on; its own ``[push-owed]`` bracket is distinct from every :class:`Marker`
#: value.
_PUSH_OWED_CLAUSE = (
    "[push-owed] HEAD ahead of origin/<branch> — push still owed, "
    "my half of the transaction not closed"
)

#: The clause substituted for an ``AWAITING_PUSH`` blocker when the push was
#: *never attempted* (``push_delivered is None`` — a non-deliverable outcome or a
#: checkout fallback left ``branch_name != pr_branch``). Here git has *not*
#: confirmed HEAD is ahead: the branch may already be up-to-date on ``origin``
#: from a prior lap, so asserting "HEAD ahead of origin" would be a git-ungrounded
#: false record. Because "a push is owed" is true *iff* local commits have not
#: reached ``origin/<branch>`` — i.e. iff HEAD is ahead of origin — a settled
#: "push still owed" / "transaction not closed" claim would re-assert that *same*
#: unverified git fact, merely reworded (PR #3586, comment 3868416285). So this
#: wording asserts only *non-confirmation*: git never verified the resolution
#: reached origin, so closure is not *verified* — not that a push definitely is
#: owed. Like :data:`_PUSH_OWED_CLAUSE` it carries no ``delivered to pr`` substring
#: and no push-family protocol marker, and its ``[push-owed]`` bracket is its own.
_PUSH_OWED_UNVERIFIED_CLAUSE = (
    "[push-owed] resolution not confirmed on origin/<branch> — "
    "my half of the transaction not verified closed"
)

#: Case-insensitive matcher for an agent-authored delivery claim: the exact
#: :attr:`Marker.DELIVERED_AWAITING_MERGE` phrasing in any case, so a model that
#: varies capitalisation is still bound (``classify_lap_delivery`` lower-cases
#: before matching, so a mixed-case claim would otherwise still fool it).
_DELIVERED_CLAIM_RE = re.compile(re.escape(_DELIVERED_CLAUSE), re.IGNORECASE)


def _blocker_is_awaiting_push(merge_block: str | None) -> bool:
    """True when *merge_block* is the owned ``AWAITING_PUSH`` blocker.

    Case- and whitespace-insensitive, mirroring the comparison the committed-
    unpushed actuator uses (``blocker.strip().upper()``). ``None`` / empty is
    never AWAITING_PUSH.
    """
    return (
        merge_block is not None
        and merge_block.strip().upper() == MERGE_BLOCK_AWAITING_PUSH
    )


def compose_partial_delivery_clause(
    merge_block: str | None, *, push_delivered: bool | None = False,
) -> str:
    """Return the delivery clause for a partial remediation summary, push-bound.

    The recorded merge *merge_block* and this lap's *push_delivered* truth decide
    the clause:

    * ``AWAITING_PUSH`` with *push_delivered* ``False`` (a push was attempted and
      both attempts failed) — the local commits did not reach ``origin`` so HEAD
      is genuinely ahead — yields :data:`_PUSH_OWED_CLAUSE`, naming that git fact
      and carrying no ``delivered to pr`` phrasing;
    * ``AWAITING_PUSH`` with *push_delivered* ``None`` (the push was never
      attempted) — git has *not* confirmed HEAD is ahead, so the branch may
      already be up-to-date on ``origin`` — yields the neutral
      :data:`_PUSH_OWED_UNVERIFIED_CLAUSE`, which asserts only *non-confirmation*
      (closure not verified on origin), never that a push definitely is owed —
      that would re-assert the unverified "HEAD ahead of origin" git fact;
    * any other blocker (an external-only review/CI gate) yields the retained
      :data:`_DELIVERED_CLAUSE` — the resolution *is* on the PR branch and only a
      human act remains, so the honest delivery phrasing stands.

    *push_delivered* ``True`` never reaches here (the caller returns early on a
    confirmed push); the parameter defaults to ``False`` so a bare call keeps the
    assertive push-owed clause. Pure, zero SDK cost, no I/O — a table over the
    blocker string and the push-truth boolean, never a read of the record's own
    narration. It composes fresh outcome text from git-truth-derived facts; it
    does not label a landing (that is :func:`decide_outcome`, which this never
    touches).
    """
    if _blocker_is_awaiting_push(merge_block):
        if push_delivered is None:
            return _PUSH_OWED_UNVERIFIED_CLAUSE
        return _PUSH_OWED_CLAUSE
    return _DELIVERED_CLAUSE


def bind_delivery_claim_to_push_truth(
    text: str | None,
    merge_block: str | None,
    *,
    push_delivered: bool | None,
) -> str | None:
    """Scrub a false ``delivered to pr`` claim from a push-owed record's *text*.

    The record-time actuator that closes the half-transaction-theater valve: an
    agent-authored step-output ``delivered to pr, awaiting review/merge`` can
    survive into a remediation record's ``result_summary`` / ``key_findings``
    even when this lap's mechanical truth is *push-owed* — the recorded blocker is
    ``AWAITING_PUSH`` and no confirmed push advanced the remote head. Left intact
    it makes :func:`classify_lap_delivery` misread the push-owed lap as
    ``DELIVERING`` and stamps a flattering false-delivery record.

    Rewrites every :attr:`Marker.DELIVERED_AWAITING_MERGE` occurrence (any case)
    to :func:`compose_partial_delivery_clause`'s push-owed clause when **both**:

    * *merge_block* is the owned ``AWAITING_PUSH`` blocker, and
    * *push_delivered* is not ``True`` — this hand did not confirm a remote-head
      advance this lap. When *push_delivered* is ``True`` the mechanical marker
      swap already stamped an honest delivery, so the claim stands untouched (the
      blocker field is merely a stale pre-push read).

    The substituted clause is push-truth-aware: a confirmed-failed push
    (*push_delivered* ``False``) proves HEAD is ahead and gets the assertive
    :data:`_PUSH_OWED_CLAUSE`, while a never-attempted push (*push_delivered*
    ``None``) gets the neutral :data:`_PUSH_OWED_UNVERIFIED_CLAUSE` — it asserts
    only non-confirmation (closure not verified on origin), never that a push
    definitely is owed, which would re-assert the unverified "HEAD ahead of origin"
    fact the branch may not satisfy (it could already be up-to-date on ``origin``
    from a prior lap).

    Returns *text* unchanged for a falsy *text*, a confirmed push, or any
    non-``AWAITING_PUSH`` blocker (an external review/CI gate keeps the honest
    delivery phrasing). Pure, zero SDK cost, no I/O — the caller supplies the
    push-truth boolean and the recorded blocker.
    """
    if not text or push_delivered is True:
        return text
    if not _blocker_is_awaiting_push(merge_block):
        return text
    return _DELIVERED_CLAIM_RE.sub(
        compose_partial_delivery_clause(
            merge_block, push_delivered=push_delivered,
        ),
        text,
    )
