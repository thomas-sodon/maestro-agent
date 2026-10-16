"""Stand-down *validity* oracle — does the live reading still support the latch?

A handoff stand-down is a claim about a fact: *"this PR is merge-ready; only
Thomas can act on it."* The fact is measured live —
:func:`~maestro.audit_checks_phantom.green_ready_handoff_prs` reads the
:class:`~maestro.pr_facts.PRReadiness` ``ready_and_waiting`` observation and
fires the moment an owned PR reads green and unblocked. The *claim*, once made,
is latched: :mod:`maestro.escalation_bridge`'s ``pr_number``-keyed ledger record
releases only via :func:`~maestro.escalation_bridge.prune_resolved_standdowns`
(the host reports merged / closed) or
:func:`~maestro.escalation_bridge.prune_expired_standdowns` (an ``expires_at``
deadline, which no PR handoff ever carries). Nothing re-asks whether the fact
that earned the latch still reads the same way.

When a sibling PR merges and turns a stood-down PR ``CONFLICTING``, the claim
becomes false: the blocker is owned, code-clearable work, while the latch above
the dequeue gate keeps suppressing the remediation that would clear it. The loop
can observe the contradiction from both sides and has no vocabulary for it.

This module is that vocabulary, and nothing more. It **names** what a ledger
record claims (:class:`StanddownClaim`), decides whether a live owed-act reading
refutes that claim (:func:`assess_standdown_validity`), and folds a whole
``{pr: claim}`` map against a whole ``{pr: OwedAct}`` map into the contradicted
subset (:func:`measure_contradicted_standdowns`). It **releases nothing** — no
pruner, no ledger write, no route change. Landing the sensor before any actuator
gives the later release path a baseline to be measured against, and gives the
contradiction a name the journal can carry.

Pure and zero-cost by construction: its ``maestro`` imports are
:mod:`maestro.pr_owed_act` (a pure composer of existing oracles) and the
latest-readiness reader in :mod:`maestro.remediation_tracker` that the owed-act
composer already depends on, and it reaches no filesystem, no network, no SDK
and no merge-truth seam. The owed acts it reads are derived from the
``pr_readiness_snapshot`` already persisted on each
:class:`~maestro.memory.TaskRecord`, so naming a contradiction costs no host
call.

The sensor lives here rather than in :mod:`maestro.remediation_tracker` because
the dependency runs one way — :mod:`maestro.pr_owed_act` already imports the
tracker, so placing it beside the formatter would cycle.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping

from maestro.pr_owed_act import OwedAct, owed_act_from_readiness_snapshot
from maestro.remediation_tracker import latest_pr_readiness_snapshots


class StanddownClaim(enum.Enum):
    """The fact a durable stand-down record asserts — one member per writer.

    Each :mod:`maestro.escalation_bridge` deliverer stamps the claim it actually
    makes, so a later reader can re-ask *that* question rather than assuming
    every latch means the same thing:

    * :attr:`HANDOFF_READY` (:func:`~maestro.escalation_bridge.deliver_handoff`)
      — "this PR is merge-ready; no owned code work remains, only Thomas can
      act." Stamped by readiness callers only. The one claim a live reading can
      refute, because the owed-act oracle answers exactly that question.
    * :attr:`LAP_BUDGET_EXHAUSTED`
      (:func:`~maestro.escalation_bridge.deliver_handoff`) — "the lap / no-op
      budget for this PR was spent without landing; owned work may well remain,
      so the owed-act oracle does not refute it." Stamped by exhaustion callers
      (#4110) — the lap / no-op budget and unlanded-cure gates, plus the rule-14
      unconfirmed-landing arm, whose premise is "unlanded past its grace window"
      — each of whose PR is typically still ``CODE_OWED`` when the record lands.
    * :attr:`REALITY_REFUTED`
      (:func:`~maestro.escalation_bridge.deliver_reality_refutation`) — "a
      git-grounded finding kept being refuted while the PR stayed unmerged."
      A claim about the *audit rule's* recurrence, not about merge-readiness.
    * :attr:`RETIREMENT_PENDING`
      (:func:`~maestro.escalation_bridge.deliver_retirement_disposition`) — "a
      disposition for this PR's plan is on record and retirement may proceed."
    * :attr:`ESCALATION_CLAIMED`
      (:func:`~maestro.escalation_bridge.deliver_claimed_escalation`) — "an
      escalation was claimed for this number." Issue-keyed records land here and
      keep their ``expires_at`` age release; a plain issue number has no PR from
      which to read an owed act, so this claim is never refutable by
      construction.

    The values are the strings persisted in the ledger's ``claim`` key; a record
    written before this key existed carries no claim at all (see
    :func:`parse_standdown_claim`).
    """

    HANDOFF_READY = "handoff-ready"
    LAP_BUDGET_EXHAUSTED = "lap-budget-exhausted"
    REALITY_REFUTED = "reality-refuted"
    RETIREMENT_PENDING = "retirement-pending"
    ESCALATION_CLAIMED = "escalation-claimed"


class StanddownValidity(enum.Enum):
    """Whether a live reading still supports a latched stand-down claim.

    * :attr:`HOLDS` — the claim is unrefuted (or unreadable), so the latch keeps
      suppressing everything it was built to suppress. The safe default.
    * :attr:`REFUTED` — the live reading contradicts the claim: owned,
      code-clearable work has reappeared under a latch that said none was owed.
    """

    HOLDS = "holds"
    REFUTED = "refuted"


def parse_standdown_claim(raw: str | None) -> StanddownClaim | None:
    """Parse a persisted ``claim`` string into a :class:`StanddownClaim`.

    Tolerant by design, mirroring
    :func:`~maestro.escalation_bridge._parse_standdown_entry`'s graceful
    degradation: ``None`` (a legacy row written before the key existed), an
    empty string, or an unrecognised value all return ``None`` — *unclaimed*,
    which :func:`assess_standdown_validity` reads as :attr:`StanddownValidity.HOLDS`.
    Defaulting an unclaimed row to :attr:`StanddownClaim.HANDOFF_READY` instead
    would mis-attribute a retirement-disposition or reality-refutation record and
    let a live reading "refute" a claim that record never made.

    Case- and whitespace-normalised. Pure.
    """
    if raw is None:
        return None
    normalised = raw.strip().lower()
    for claim in StanddownClaim:
        if claim.value == normalised:
            return claim
    return None


def assess_standdown_validity(
    claim: StanddownClaim | None, live_act: OwedAct | None,
) -> StanddownValidity:
    """Return whether *claim* survives the live *live_act* reading.

    :attr:`StanddownValidity.REFUTED` iff *claim* is
    :attr:`StanddownClaim.HANDOFF_READY` **and** *live_act* is
    :attr:`~maestro.pr_owed_act.OwedAct.CODE_OWED` — the latch asserted no owned
    work remained, and the live reading says owned, code-clearable work is owed.
    That is the single contradiction this oracle names.

    Every other combination reads :attr:`StanddownValidity.HOLDS`:

    * an unclaimed record (*claim* ``None``) — there is no claim to re-read;
    * a non-handoff claim — a lap-budget-exhausted, reality-refutation,
      retirement-disposition or claimed-escalation record asserts something the
      owed-act oracle does not answer;
    * ``HANDOFF_READY`` against ``HANDOFF_READY`` / ``WAIT_CI`` /
      ``EXTERNAL_BLOCKED`` — still green-and-waiting, still on the CI clock, or
      blocked by a gate my code does not clear; none contradicts "only Thomas can
      act";
    * a ``None`` *live_act* — the PR is settled (merged / closed), which the
      existing merge/close pruner already releases.

    Failing toward ``HOLDS`` is the conservative direction: an unnamed
    contradiction is a missed measurement, while a spurious refutation would
    later license releasing a latch that is doing its job. Pure.
    """
    if claim is StanddownClaim.HANDOFF_READY and live_act is OwedAct.CODE_OWED:
        return StanddownValidity.REFUTED
    return StanddownValidity.HOLDS


def measure_contradicted_standdowns(
    claims: Mapping[int, str], owed_acts: Mapping[int, OwedAct],
) -> dict[int, OwedAct]:
    """Return the ``{pr: owed_act}`` subset whose latched claim is refuted.

    *claims* is the raw ``{pr_number: claim-string}`` map from
    :func:`~maestro.escalation_bridge.standdown_claims` (raw strings, so
    that module need not import this one); *owed_acts* is the
    ``{pr_number: OwedAct}`` the caller derived from each PR's latest persisted
    readiness snapshot via
    :func:`~maestro.pr_owed_act.owed_act_from_readiness_snapshot`.

    A PR contributes iff it appears in **both** maps and
    :func:`assess_standdown_validity` reads its pair
    :attr:`StanddownValidity.REFUTED`. A stood-down PR with no snapshot in the
    window is simply absent — unmeasured is not refuted. Empty inputs yield an
    empty map. Pure, zero SDK cost, no I/O.
    """
    contradicted: dict[int, OwedAct] = {}
    for pr_number, raw_claim in claims.items():
        live_act = owed_acts.get(pr_number)
        if live_act is None:
            continue
        claim = parse_standdown_claim(raw_claim)
        if assess_standdown_validity(claim, live_act) is StanddownValidity.REFUTED:
            contradicted[pr_number] = live_act
    return contradicted


def measure_refuted_standdowns(
    claims: Mapping[int, str],
    task_history: list[dict[str, object]],
    *,
    delivered_at: Mapping[int, str] | None = None,
) -> set[int]:
    """Return the stood-down PR numbers whose claim *task_history* refutes.

    The one call a measurement surface needs, composing the whole reading in one
    place so no caller re-inlines it:

    1. :func:`~maestro.remediation_tracker.latest_pr_readiness_snapshots` picks
       each PR's most recent persisted ``pr_readiness_snapshot`` out of
       *task_history* — the shared latest-readiness reader the PR-churn line
       also goes through — bounded below by *delivered_at*, the
       ``{pr: latch-timestamp}`` map from
       :func:`~maestro.escalation_bridge.standdown_delivery_times`. That bound
       is load-bearing rather than an optimisation: a snapshot observed *before*
       the claim was latched cannot refute it, and because the latch suppresses
       the remediation whose completion stamps the next snapshot, an unbounded
       reading is older than the claim by construction — yielding a false
       ``[standdown-refuted]`` off a stale pre-handoff lap. A PR whose only
       snapshots predate its latch drops out as unmeasured;
    2. :func:`~maestro.pr_owed_act.owed_act_from_readiness_snapshot` derives that
       PR's live owed act from the snapshot alone (a settled PR yields ``None``
       and drops out);
    3. :func:`measure_contradicted_standdowns` keeps only the PRs whose latched
       claim that reading contradicts.

    Returns the key set, the shape
    :func:`~maestro.remediation_tracker.format_pr_lap_summary`'s ``refuted_prs``
    annotation reads. A PR with no snapshot in the window is simply absent —
    unmeasured is not refuted.

    Zero-cost and side-effect-free: the caller supplies both the claims and the
    already-loaded history, so naming a contradiction adds no host call, no
    merge-oracle call and no SDK call. Reading
    :mod:`maestro.remediation_tracker` here introduces no new coupling —
    :mod:`maestro.pr_owed_act`, this module's only other import, already depends
    on it, and the dependency runs one way (nothing in the tracker imports this
    module, which is why the sensor lives here rather than beside the formatter).
    """
    owed_acts = {
        pr_number: act
        for pr_number, snapshot in latest_pr_readiness_snapshots(
            task_history, not_before=delivered_at,
        ).items()
        if (act := owed_act_from_readiness_snapshot(snapshot)) is not None
    }
    return set(measure_contradicted_standdowns(claims, owed_acts))
