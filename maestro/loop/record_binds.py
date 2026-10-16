"""Record-time outcome binds carved out of ``maestro.agent`` (proposal 106).

Holds the five stages ``MaestroAgent.run`` executes after step execution to
shape what the completed lap's ``TaskRecord`` says: the push-truth claim scrub
plus honest remediation framing (``# 5b-ii.`` / ``# 5c.``), the mergeable-flip
merge-request actuator (``# 5c-iv-b.``), the CI-failing red-read bind
(``# 5c-iv-c.``), the mechanical outcome authority (``# 5d.``), and the
frame-discharge costume cap (``# 5f.``). They share one concern already named
as an architecture invariant (record-time outcome binds), and they run as one
contiguous straight-line stretch of ``run``.

The extraction is behaviour-preserving: the same bodies, the same call order,
the same effects — only the receiver changed from ``self`` to a first
positional ``agent`` argument, matching
:func:`maestro.loop.lifecycle.teardown_task_run`. ``deliver_handoff_cfg`` is
imported from :mod:`maestro.escalation_bridge`; tests patch it on this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from maestro import audit_checks
from maestro.directive_ledger import DirectiveGrounding, offer_directive
from maestro.escalation_bridge import UnlandedCureHandoff, deliver_handoff_cfg
from maestro.frame_discharge import assess_frame_discharge
from maestro.outcome_authority import (
    OutcomeFacts,
    bind_delivery_claim_to_push_truth,
    decide_outcome,
    partial_provenance,
)
from maestro.outcome_grounding import (
    MergeableFlipContext,
    bind_ci_failing_verdict_to_red_read,
    bind_costume_discharge_to_partial,
    bind_mergeable_flip_to_merge_request,
)
from maestro.pr_facts import ci_state_from_conclusion
from maestro.remediation_tracker import (
    MERGE_BLOCK_AWAITING_PUSH,
    classify_lap_delivery,
)
from maestro.standdown_validity import StanddownClaim

if TYPE_CHECKING:
    from maestro.agent import MaestroAgent
    from maestro.memory import TaskRecord
    from maestro.types import TaskOutcome



def apply_frame_discharge_cap(
    agent: MaestroAgent,
    *,
    record: TaskRecord,
    outcome: TaskOutcome,
    key_findings: str | None,
    remediation_pr_num: int | None,
) -> tuple[TaskOutcome, str | None]:
    """Cap a frame-discharge costume ``success`` — ``run``'s ``# 5f.`` block,
    extracted verbatim.

    Gated behind the default-off ``[loop] frame_discharge_bind_enabled``, this
    is the self-directed complement to §5c/§5d: those govern only remediation
    PRs, so a self-directed task framed as a read-only investigation but
    discharged as tracked construction (a kept branch) escapes them and
    collects a guaranteed ``success`` a real investigation never had. The
    frame↔discharge owner stamps that ``COSTUME`` from the completed record,
    and this bind caps it to ``partial`` with a ``[frame-discharge-mismatch]``
    note. Domain-disjoint from §5d's mechanical-authority path, which owns
    only remediation PRs (``remediation_pr_num is not None``).

    A behaviour-preserving delegate, not a new stage: it mutates the just-built
    (mutable) ``record`` in place before ``record_task`` and returns the
    possibly-capped ``(outcome, key_findings)`` so the post-record ``# 5e.`` /
    ``# 6.`` tail reads the same values.

    Precondition: *outcome* and *key_findings* must be the values *record* was
    built from (``record.outcome`` / ``record.key_findings``) — the caller's
    live locals, which the block read before the record existed. Desynced
    arguments would cap against one source and write the other, so a caller
    that re-derives either value must rebuild *record* from it first.
    """
    if (
        remediation_pr_num is None
        and agent.config.frame_discharge_bind_enabled
    ):
        costume_verdict = assess_frame_discharge(record)
        bound_outcome, bound_findings = bind_costume_discharge_to_partial(
            key_findings,
            outcome,
            is_costume=costume_verdict.verdict == "COSTUME",
        )
        outcome = cast("TaskOutcome", bound_outcome)
        key_findings = bound_findings
        record.outcome = outcome
        record.key_findings = key_findings
    return outcome, key_findings


def apply_mechanical_outcome_authority(
    agent: MaestroAgent,
    *,
    remediation_pr_num: int | None,
    outcome: TaskOutcome,
    key_findings: str | None,
    summary: str,
) -> tuple[TaskOutcome, str | None]:
    """Author a remediation-PR record's landing verdict from git-truth —
    ``run``'s ``# 5d.`` block, extracted verbatim.

    The git-grounded landing verdict is the unconditional owner of a
    remediation-PR record's recorded outcome (proposal 105). For every
    ``remediation_pr_num is not None`` record: compute the git-grounded
    landing verdict from the merge-truth chokepoint, record every divergence
    from the narration-path *outcome* settled above (§5c) to the durable
    activation ledger for measurement, and then ADOPT the git-grounded
    verdict as the recorded outcome. The authority reads the same
    ``is_pr_settled`` chokepoint that already governs settlement everywhere
    else, so the authored landing verdict can never be *more* wrong about
    landing than the system already is — it removes a second, prose-derived
    opinion that can disagree with git.

    The durable JSONL ledger — not a ``logger.warning`` — is deliberate: an
    expected per-remediation divergence stream must not feed the loop's
    warning-saturation hold gate, and the row shape is what ``maestro
    measure-gate-triggers`` (and the phantom-landing sensor) tallies. Only a
    landing-relevant record (an actual remediation PR) is authored: a
    non-remediation task can never diverge — ``decide_outcome`` returns the
    candidate unchanged when not landing-relevant — so logging its
    guaranteed-``agree`` row would only dilute the divergence rate the ledger
    exists to measure. Reads the per-iteration-memoized ``is_pr_settled``
    chokepoint (no new git I/O), so it costs nothing beyond the ledger append.

    A behaviour-preserving delegate, not a new stage: it reads run-scoped
    values passed in as keyword params and mutates nothing the caller owns —
    the adopted verdict and the annotated findings come back as the returned
    ``(outcome, key_findings)``, which the ``TaskRecord`` is then built from.

    Precondition: runs after §5c has settled the narration-path *outcome* and
    before the record is constructed — the ledger detail is composed from the
    pre-adoption narration verdict, so a ``diverged`` row names both it and
    the git-truth verdict, and the adoption below must not be visible to that
    row. *summary* must be the same string the record is built from, since
    the delivered marker ``classify_lap_delivery`` reads was prepended into
    it earlier this iteration.
    """
    if remediation_pr_num is not None:
        # Delivery provenance (proposal 116): classify whether this lap
        # advanced its owed remote head from the delivered marker
        # ``_frame_remediation_note`` prepended into ``summary`` earlier
        # this iteration. ``decide_outcome`` ignores ``lap_delivering``
        # (the field is defaulted), so the landing verdict is byte-for-
        # byte unchanged; the bit feeds only ``partial_provenance`` below.
        facts = OutcomeFacts(
            landing_relevant=True,
            landing_confirmed=agent.is_pr_settled(
                remediation_pr_num,
            ).branch_landed,
            lap_delivering=(
                classify_lap_delivery(summary) == "DELIVERING"
            ),
        )
        mechanical_outcome = decide_outcome(outcome, facts)
        diverged = mechanical_outcome != outcome
        audit_checks.log_gate_activation(
            agent.gate_activations,
            gate=audit_checks.MECHANICAL_OUTCOME_AUTHORITY_GATE,
            action=(
                audit_checks.MECHANICAL_OUTCOME_DIVERGED_ACTION
                if diverged
                else audit_checks.MECHANICAL_OUTCOME_AGREE_ACTION
            ),
            detail=(
                f"pr=#{remediation_pr_num} "
                f"narr={outcome} mech={mechanical_outcome}"
            ),
        )
        # Adopt the git-grounded verdict AFTER the ledger row is logged,
        # so the ``narr=`` field above records the pre-adoption narration
        # verdict. The recorded ``outcome`` is the authority's, not the
        # model's prose.
        outcome = mechanical_outcome
        # Annotate a ``partial`` record with whether the lap delivered
        # its owed head, so a delivered-but-gated partial is later
        # distinguishable from a make-work one. ``None`` for a
        # ``success`` verdict (or a non-landing-relevant one), leaving
        # ``key_findings`` untouched — measurement/provenance, no verdict.
        delivery_note = partial_provenance(mechanical_outcome, facts)
        if delivery_note is not None:
            key_findings = (
                f"{key_findings} {delivery_note}".strip()
                if key_findings
                else delivery_note
            )
    return outcome, key_findings


def apply_ci_red_read_bind(
    agent: MaestroAgent,
    *,
    remediation_pr_num: int | None,
    outcome: TaskOutcome,
    summary: str,
    key_findings: str | None,
    executed_commands: list[str],
    kept_branch: str | None,
    push_delivered: bool | None,
    remediation_note: str | None,
    pr_branch: str | None,
    pr_head_watermark: tuple[str, ...] | None,
) -> tuple[str, str | None, TaskOutcome]:
    """Scrub a completion verdict that left a surfaced CI red unread —
    ``run``'s ``# 5c-iv-c.`` block, extracted verbatim.

    The write-time complement to the edit-first ordering guard
    (``bind_ci_failing_verdict_to_red_read``): that guard denies a
    head-moving git action before a source edit on an owned branch, but does
    NOT force ADDRESSING the failing gate — a ``CI_FAILING`` blocker (a
    code-clearable red check) can be "diagnosed" lap after lap: the
    CI-failure diagnosis oracle surfaces a concrete failing-check error line
    for this PR, yet the lap edits and pushes nothing on the owned branch.
    This bind keys on the one signal none of the label binds use — did the
    oracle surface a red this lap that the lap left unaddressed — from the
    ``_ci_oracle_surfaced`` cache (a concrete error line was extracted for
    this PR this lap) and whether the lap committed-and-pushed a fix on the
    owned branch. When a remediation on a ``CI_FAILING`` own PR narrates
    completion yet the oracle surfaced a red the lap never addressed, it
    scrubs the resolution narration (the loop-authored merge-truth note
    *remediation_note*, every ``Marker``, and every
    ``PROTECTED_ADMISSION_PHRASES`` entry the downstream sensors read are
    preserved — the ``CAUSED`` landing phrases are deliberately excluded from
    that set so a costume merge claim is neutralised rather than over-claiming
    CAUSED), keeps the outcome
    ``partial``, appends a ``[red-unread]`` note to the uncapped
    *key_findings*, and offers an open-the-red-gate directive into an EMPTY
    ``next_task_directive`` slot (never overwriting; routed through the 090
    dropped-directive ledger like 5c-iii/iv). The ``CI_FAILING`` fact is
    read straight off the host CI rollup (``get_pr_check_conclusion`` →
    ``ci_state_from_conclusion``), NOT the readiness chokepoint, so it never
    triggers the gated readiness snapshot / thread probe; and it is read
    only once the cheap preconditions hold (a recorded command list with a
    surfaced-but-unaddressed red), so a not-surfaced / addressed /
    empty-record lap pays no host read.

    A behaviour-preserving delegate, not a new stage: it reads run-scoped
    values passed in as keyword params and mutates none of the caller's
    locals — the scrubbed narration and downgraded verdict come back as the
    returned ``(summary, key_findings, outcome)``.

    Precondition: runs after ``apply_remediation_narration_framing`` has
    framed *summary* / *key_findings* (so the loop-authored
    *remediation_note* it protects is already prepended) and before
    ``apply_mechanical_outcome_authority``, whose scrub must see the
    already-neutralized narration (it is then a no-op on the
    already-scrubbed text).
    """
    if remediation_pr_num is not None and outcome in (
        "success", "partial",
    ):
        command_record_nonempty = bool(executed_commands)
        # The honest oracle signal: a concrete failing-check line was
        # surfaced for this PR this lap (the ``_ci_oracle_surfaced`` cache)
        # AND the lap did not commit a fix on the owned branch (a kept
        # owned branch OR a lap-authored commit since the PR-head watermark
        # — see the lap-commit comment below — whose push *delivered* to
        # origin is the in-scope "edited-and-pushed a fix this lap" signal;
        # a push that failed OR was never attempted has not reached
        # ``origin/<pr_branch>``, so the red is not addressed).
        oracle_surfaced_failure = agent._ci_oracle_surfaced.get(
            remediation_pr_num, False,
        )
        # ``push_delivered is True`` (not ``is not False``): tighten to the
        # delivered truth so the None edge — committed-but-never-pushed —
        # reads as unaddressed rather than suppressing the downgrade.
        # The lap-commit signal is the loop's own §5 commit (``kept_branch``)
        # OR a first-parent, non-merge commit on the PR branch since its
        # checkout watermark — the agent committing and pushing its fix
        # itself, as the remediation delivery note instructs, leaves a clean
        # tree at §5 so ``kept_branch`` alone misses it. A lap whose head moved
        # only by a merge of ``origin/main`` counts zero; an unknown watermark
        # or probe result degrades to the ``kept_branch``-only predicate.
        lap_authored_commit: bool | None = None
        # Probe only when the result can matter: a surfaced red with a
        # non-empty command record (``guard_may_fire`` below), so a no-op lap
        # pays no ``git rev-list`` subprocess.
        if (
            command_record_nonempty
            and oracle_surfaced_failure
            and push_delivered is True
            and kept_branch is None
            and pr_branch is not None
            and pr_head_watermark
        ):
            count = agent._vcs.lap_commits_since(pr_branch, pr_head_watermark)
            lap_authored_commit = count > 0 if count is not None else None
        addressed_red = push_delivered is True and (
            kept_branch is not None or lap_authored_commit is True
        )
        # Pay the host CI read only when the guard could actually fire — a
        # surfaced-but-unaddressed red. A not-surfaced / addressed /
        # empty-record lap pays no host read.
        guard_may_fire = (
            command_record_nonempty
            and oracle_surfaced_failure
            and not addressed_red
        )
        blocker_ci_failing = False
        if guard_may_fire:
            conclusion = agent.evolution.code_host.get_pr_check_conclusion(
                remediation_pr_num,
            )
            blocker_ci_failing = (
                ci_state_from_conclusion(conclusion) == "FAILING"
            )
        (
            summary,
            key_findings,
            red_read_outcome,
            red_read_directive,
        ) = bind_ci_failing_verdict_to_red_read(
            summary,
            key_findings,
            outcome,
            blocker_ci_failing=blocker_ci_failing,
            command_record_nonempty=command_record_nonempty,
            oracle_surfaced_failure=oracle_surfaced_failure,
            addressed_red=addressed_red,
            protected_note=remediation_note,
        )
        outcome = cast("TaskOutcome", red_read_outcome)
        if red_read_directive is not None:
            model = agent.consciousness.load_self_model()
            offered = offer_directive(
                model,
                red_read_directive,
                source="bind_ci_failing_verdict_to_red_read",
                grounding=DirectiveGrounding(
                    pr_numbers=(remediation_pr_num,),
                ),
                backend=agent.dropped_directives,
                enabled=agent.config.dropped_directive_replay_enabled,
            )
            audit_checks.log_gate_activation(
                agent.gate_activations,
                gate="bind_ci_failing_verdict_to_red_read",
                action="directive_set" if offered else "dropped",
                detail=f"pr=#{remediation_pr_num}",
            )
            if offered:
                agent.consciousness.save_self_model(model)
    return summary, key_findings, outcome


def apply_mergeable_flip_actuator(
    agent: MaestroAgent,
    *,
    remediation_pr_num: int | None,
    pr_branch: str | None,
    pr_head_sha_stamp: str | None,
    goal: str,
    summary: str,
    key_findings: str | None,
) -> None:
    """Send one named merge-request on a mergeable flip — ``run``'s
    ``# 5c-iv-b.`` block, extracted verbatim.

    The mergeable-flip merge-request ACTUATOR
    (``bind_mergeable_flip_to_merge_request``, default-off behind ``[loop]
    mergeable_merge_request_enabled``). Where every bind around it only
    LABELS the outcome, this one ACTS: it closes my chronic
    partial-at-MERGEABLE dropped-keystroke failure. I reliably reconcile a
    PR to MERGEABLE (the owned half of a landing) then fail to send the
    ungated named merge-request that recruits Thomas — the only landing
    available to me at that flip. When this PR-remediation verdict cites
    ``#N``, the merge-truth chokepoint (``is_pr_settled``) shows the branch
    clean-but-unlanded, and the readiness chokepoint's sole blocker
    (``observe_pr_readiness(...).merge_block_reason()``) is external and
    mergeable-terminal (AWAITING_APPROVAL — the sole such code that source
    can emit; AWAITING_PUSH is a local-only-commits state it never
    produces), the actuator sends exactly one named merge-request via
    ``deliver_handoff``.

    Idempotent on a persisted ``(pr_number, head_sha)`` serviced marker;
    ``deliver_handoff`` adds its own ``pr_number``-keyed standdown backstop,
    so at most one message is ever sent. No new push capability — the only
    owned act is one message to Thomas. Reads the same per-iteration-
    memoized chokepoints §5c-iii and §5d/§5d-ii use, so it adds no git I/O
    of its own once they were observed this iteration.

    A behaviour-preserving delegate, not a new stage: it reads run-scoped
    values passed in as keyword params and mutates nothing the caller owns,
    returning ``None`` — the sole effect is the one message (plus its
    serviced marker and activation row).

    Precondition: runs after ``apply_remediation_narration_framing``'s
    honest-framing rewrite (so *summary* / *key_findings* are the framed
    verdict prose the merge-request text quotes) and before
    ``apply_ci_red_read_bind``, whose red-read scrub rewrites that same
    narration afterwards.
    """
    if not agent.config.mergeable_merge_request_enabled:
        return
    if remediation_pr_num is None:
        return
    flip_truth = agent.is_pr_settled(remediation_pr_num)
    flip_readiness = agent.observe_pr_readiness(
        remediation_pr_num, pr_branch,
    )
    flip_ctx = MergeableFlipContext(
        pr_number=remediation_pr_num,
        head_sha=pr_head_sha_stamp or "",
        settled=flip_truth.settled,
        branch_landed=flip_truth.branch_landed,
        blocker=flip_readiness.merge_block_reason(),
    )
    # ``goal`` ("Remediate PR #N: …") reliably carries the ``#N`` citation
    # the actuator guards on; the recorded summary + findings are the
    # verdict prose it accompanies.
    merge_request_text = f"{goal}\n{summary}\n{key_findings or ''}"

    def _deliver_merge_request(ctx: MergeableFlipContext) -> bool:
        delivered = deliver_handoff_cfg(
            agent.config,
            UnlandedCureHandoff(
                pr_number=ctx.pr_number,
                head_sha=ctx.head_sha,
                stamp_count=1,
            ),
            backend=agent.escalation_standdowns,
            message_store=agent.message_store,
            plan_lookup=agent._plan_for_pr_number,
            claim=StanddownClaim.HANDOFF_READY,
        )
        return cast("bool", delivered)

    if bind_mergeable_flip_to_merge_request(
        merge_request_text, flip_ctx, _deliver_merge_request,
        backend=agent.merge_request_serviced,
        # AWAITING_PUSH is owned (proposal 098); behavior is invariant
        # here anyway since the wired caller never emits it.
        own_push=True,
    ):
        # Actuation telemetry (090): one activation row only on a real
        # delivery — ``deliver_handoff`` returns False on the idempotent
        # no-op — so the narration-conversion sensor can confirm the
        # recruited landing.
        audit_checks.log_gate_activation(
            agent.gate_activations,
            gate="bind_mergeable_flip_to_merge_request",
            action="merge_request",
            detail=f"pr=#{remediation_pr_num}",
        )


def apply_remediation_narration_framing(
    agent: MaestroAgent,
    *,
    summary: str,
    key_findings: str | None,
    remediation_note: str | None,
    push_delivered: bool | None,
) -> tuple[str, str | None]:
    """Shape a completed remediation's narration — ``run``'s ``# 5b-ii.``
    and ``# 5c.`` blocks, extracted verbatim as one stage.

    ``# 5b-ii.`` binds a false delivery claim to push-truth. An agent
    step-output "delivered to pr, awaiting review/merge" survives into the
    summary/findings even when this lap's mechanical truth is push-owed
    (recorded blocker AWAITING_PUSH, no confirmed push) — a half-transaction
    narrated as whole. The scrub rewrites the claim to a push-owed marker so
    ``classify_lap_delivery`` and prior-task context read "push owed" where a
    push is owed. The honest mechanical DELIVERED marker (stamped only on a
    confirmed push, ``push_delivered is True``) is left untouched by the
    ``push_delivered`` guard, so a genuine delivery whose blocker field is
    merely a stale pre-push read keeps its honest phrasing.

    ``# 5c.`` then frames a remediation the stamp gate downgraded: it
    prepends the host verdict to both the summary and the distilled findings
    so prior-task context injection and ``format_known_state_summary``
    report "awaiting tend push" rather than "delivered" (#2457). Summary
    keeps its 500-char cap.

    The two live in one method because their **order is load-bearing**: the
    scrub must run *before* the framing so the loop-authored
    *remediation_note* — which is already honest — is never rewritten by the
    scrub. As inline code, that constraint was documented only in a comment;
    here it is the method's own body.

    A behaviour-preserving delegate, not a new stage: it reads run-scoped
    values passed in as keyword params (plus
    ``agent._last_remediation_merge_block``, exactly as the inline block did)
    and reassigns none of the caller's locals — the shaped narration comes
    back as the returned ``(summary, key_findings)``.
    """
    if agent._last_remediation_merge_block == MERGE_BLOCK_AWAITING_PUSH:
        summary = bind_delivery_claim_to_push_truth(
            summary, agent._last_remediation_merge_block,
            push_delivered=push_delivered,
        ) or summary
        key_findings = bind_delivery_claim_to_push_truth(
            key_findings, agent._last_remediation_merge_block,
            push_delivered=push_delivered,
        )

    if remediation_note is not None:
        summary, key_findings = agent._frame_remediation_note(
            summary, key_findings, remediation_note,
        )
    return summary, key_findings
