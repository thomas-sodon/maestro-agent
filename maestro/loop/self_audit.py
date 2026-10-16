"""Mechanical self-audit sweep carved out of ``maestro.agent`` (proposal 147).

Holds the zero-SDK self-audit phase body formerly at
``MaestroAgent._audit_self_model``: it runs the :mod:`maestro.audit_checks`
rule registry, then the reality-refutation, unwinnable-race, orphaned-cure,
undercredited-landing and owed-act give-up arms, and records the
:class:`~maestro.audit_checks.AuditResult`. It is one concern with one entry
point (``_phase_audit_self_model``), so it moves intact to keep
``maestro/agent.py`` within its source budget.

The extraction is behaviour-preserving: the same body, the same call order,
the same effects — only the receiver changed from ``self`` to a first
positional ``agent`` argument, matching
:func:`maestro.loop.lifecycle.teardown_task_run`. ``MaestroAgent`` keeps a
one-line ``_audit_self_model`` delegator. Every module global the body reads
is imported from its defining module, so a test patches it as
``maestro.loop.self_audit.<name>``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from maestro import audit_checks, audit_checks_phantom, audit_checks_settlement, deed_ledger
from maestro.audit_checks import (
    _UNDEFINED_COINAGE_JOURNAL_WINDOW,
    measure_reality_refutation,
    measure_undefined_coinages,
)
from maestro.consciousness import Consciousness, _normalize_question, upsert_open_question
from maestro.directive_ledger import DirectiveGrounding, offer_directive
from maestro.escalation_bridge import (
    UnlandedCureHandoff,
    deliver_claimed_escalation,
    deliver_handoff_cfg,
    deliver_reality_refutation,
    deliver_retirement_disposition,
    deliver_unwinnable_race,
    has_standdown,
)
from maestro.lever_capture import fire_and_capture
from maestro.lexicon import all_spellings, load_lexicon
from maestro.loop.obsolete_directive import actuate_obsolete_directive
from maestro.loop.owed_act_binding import make_disposal_offer_recorder, owed_act_binding
from maestro.memory import AuditResult, EvolutionPlan
from maestro.outcome_grounding import (
    _SELF_COMMITTED_FIRE_OR_CLOSE_DIRECTIVE,
    extract_self_committed_shas,
)
from maestro.pr_facts import Salvage
from maestro.remediation_tracker import measure_unwinnable_race_re_entries
from maestro.source_budget import split_due_directive_readback
from maestro.standdown_validity import StanddownClaim
from maestro.vcs import VCSError

if TYPE_CHECKING:
    from maestro.agent import MaestroAgent
    from maestro.consciousness import SelfModel

logger = logging.getLogger("maestro")

# Trailing ``audit_history.jsonl`` window scanned by the proposal 088 Phase 2
# reality-refutation sensor/actuator. Generous (mirrors the escalation bridge's
# ``HANDOFF_SCAN_CYCLES``) so a long-running refutation dispute is still seen;
# the consecutive-from-newest walk stops at the first non-firing cycle anyway,
# so a wide window never inflates a lapsed count.
_REALITY_REFUTATION_AUDIT_CYCLES = 50

# Trailing ``task_history.jsonl`` window scanned by the proposal 099 step 8
# unwinnable-race actuator. Wider than the 20-task audit tail so a genuinely
# stuck PR whose remediation laps interleave with other work still accumulates
# enough same-base re-entries to reach the threshold; matches the 50-record
# lookbacks the reality-refutation sensor and ``_STALL_LINEAGE_RESOLUTION_LOOKBACK``
# already use. Widening only ever raises a lineage's re-entry count, and the
# per-PR settled-filter plus message-thread idempotency keep the actuator's
# output identical — one heads-up per still-open, over-threshold PR.
_UNWINNABLE_RACE_TASK_WINDOW = 50


async def audit_self_model(agent: MaestroAgent, iteration: int) -> None:
    """Mechanical self-audit — cross-check self-model against behaviour.

    Runs the deterministic rules in :mod:`maestro.audit_checks` over
    the last 20 tasks + current self-model. For each finding, records
    an ``audit_observation`` journal event (an operational record, kept
    out of the journal's inner-thought narrative) and appends an
    ``[audit]`` entry to
    ``open_questions``. The :class:`AuditResult` is persisted to
    ``audit_history.jsonl`` — zero-finding cycles are still recorded
    so "last audit found nothing" is an observable signal.

    No SDK calls. Exceptions propagate to :meth:`run_loop`'s crash
    boundary — a rule bug or I/O error is a real defect that must
    surface (fail-loud), not be swallowed.
    """
    tasks = agent.memory.get_recent_tasks(20)
    success_rate = agent.memory.get_success_rate(20)
    self_model = agent.consciousness.load_self_model()
    if agent.config.loop_obsolete_directive_actuator_enabled:
        self_model = actuate_obsolete_directive(agent, self_model)
    resolutions = agent.memory.get_recent_question_resolutions(50)
    cited_source_task_ids = agent.memory.get_cited_source_task_ids()
    # Rule 6 (stale_entry) is opt-in via ``audit_stale_entry_enabled``.
    # When enabled, build the textual-evidence corpus from the same
    # rolling sources the LLM authors after seeing the prompt: recent
    # journal thoughts and events (the novelty event protects a new
    # entry), task goals + key_findings, prior audit finding
    # statements, and resolution-finding text. ``None`` keeps rule 6
    # silent.
    stale_entry_corpus: list[str] | None = None
    if agent.config.audit_stale_entry_enabled:
        stale_entry_corpus = []
        for entry in [
            *agent.journal.read_recent(20),
            *agent.journal.read_recent_events(20),
        ]:
            if isinstance(entry.content, str):
                stale_entry_corpus.append(entry.content)
        for task in tasks:
            if task.goal:
                stale_entry_corpus.append(task.goal)
            if task.key_findings:
                stale_entry_corpus.append(task.key_findings)
        for audit in agent.memory.get_recent_audit_results(10):
            for f in audit.findings:
                stale_entry_corpus.append(f.statement)
        for r in resolutions:
            if r.finding:
                stale_entry_corpus.append(r.finding)

    # Standing settlement reconciler (proposal 097). Computed once, ahead
    # of ``run_all_checks`` and the phantom-rule dispatch, so one settled
    # set reaches both: ``run_all_checks`` (pre-filtering
    # ``check_repeated_target_attempt`` — a lap repeatedly naming a settled
    # ``#NNN`` is the remediation that landed it, not an unlearned
    # limitation) and the three phantom rules, each skipping a settled PR
    # before its merge predicate so the finding self-extinguishes. Gated by
    # the (default-on) ``[audit] settlement_reconciler_enabled`` switch:
    # flag off ⇒ ``frozenset()`` everywhere ⇒ byte-identical pre-097 output.
    settled_prs: frozenset[int] = frozenset()
    if agent.config.settlement_reconciler_enabled:
        settled_prs = audit_checks_settlement.reconcile_settled_verdicts(
            agent.config.memory_dir, 20, iteration,
            is_pr_settled=agent.is_pr_settled,
            backend=agent.settled_verdicts,
        )

    findings = audit_checks.run_all_checks(
        self_model, tasks, success_rate,
        resolutions=resolutions,
        stale_entry_corpus=stale_entry_corpus,
        cited_source_task_ids=cited_source_task_ids,
        branch_promotion_pressure_enabled=(
            agent.config.branch_promotion_pressure_enabled
        ),
        evidence_bound_claims_enabled=(
            agent.config.audit_evidence_bound_claims_enabled
        ),
        settled_prs=settled_prs,
        closed_refs=(
            audit_checks.resolve_closed_refs(
                self_model, agent.evolution.code_host.get_issue_state,
            )
            if agent.config.audit_closed_reference_enabled else frozenset()
        ),
    )

    # Rule 9 (impact review) lives outside ``run_all_checks`` because it
    # needs the version archive + data dir, which that pure combiner
    # does not receive. Prepend its findings and re-clip to the cycle
    # cap so they sit early; later prepend rules can still clip them, so
    # the surfaced mark is written only after the final clip (below the
    # ``append_audit_result`` persist) for findings that survived — a
    # clipped version stays unrecorded and re-surfaces next cycle.
    if agent.config.audit_impact_review_enabled:
        impact_findings = audit_checks.check_evolution_impact_reviews(
            agent.memory.get_recent_versions(
                agent.config.audit_impact_review_window,
            ),
            agent.memory.get_recent_tasks(30),
            agent.impact_review_surfaced,
            max_age_days=agent.config.audit_impact_review_after_days,
            review_window=agent.config.audit_impact_review_window,
            enabled=agent.config.audit_impact_review_enabled,
        )
        if impact_findings:
            findings = (impact_findings + list(findings))[
                : audit_checks.MAX_FINDINGS_PER_CYCLE
            ]

    # ``settled_prs`` is computed once above ``run_all_checks`` (proposal
    # 097) and reused here by the phantom rules — one reconciler call, one
    # settled set reaching both the combiner and the out-of-combiner rules.
    # Rule 11 (orphaned cure) also lives outside ``run_all_checks`` because
    # it needs the plan archive + data dir + the audit iteration to measure
    # plan age in cycles via its first-seen sidecar. It turns the
    # unlanded-PR lens inward: an own ``implemented``-but-unmerged cure left
    # to rot while the remediations it would eliminate keep being lapped.
    # Prepend + re-clip like rules 9/10 so a surfaced orphaned cure reliably
    # reaches ``open_questions`` and the audit→directive bridge.
    if agent.config.orphaned_cure_enabled:
        orphaned_findings = audit_checks_phantom.check_orphaned_cure(
            agent.memory.get_all_plans(),
            tasks,
            agent.orphaned_cure,
            iteration,
            age_cycles=agent.config.orphaned_cure_age_cycles,
            enabled=agent.config.orphaned_cure_enabled,
            settled_prs=settled_prs,
        )
        if orphaned_findings:
            findings = (orphaned_findings + list(findings))[
                : audit_checks.MAX_FINDINGS_PER_CYCLE
            ]
        # Rule 11's dispose complement — the mechanical retire actuator.
        # Runs after ``check_orphaned_cure`` because the check seeds and
        # prunes the first-seen map the sweep measures age against — the same
        # ``agent.orphaned_cure`` backend, so the sweep reads the freshly
        # pruned/touched map whether it lives in the JSON sidecar or state.db.
        # Retires (mark_plan_obsolete via fire_and_capture) each
        # ``implemented`` cure past the retire ceiling whose PR tend has
        # directly observed as CONFLICTING; the PR itself stays open.
        if agent.config.orphaned_cure_retire_enabled:
            first_seen = agent.orphaned_cure.load()

            def _deliver_disposition(plan: EvolutionPlan) -> bool:
                # Refuse retirement (return False → sweep leaves the plan
                # ``implemented``) without a real delivery path: no message
                # store, no PR URL, or an unparseable one. Otherwise deliver
                # the close-or-keep disposition to the PR and Thomas and
                # record the standdown; only a delivered (or already-recorded)
                # disposition permits the retirement.
                if agent.message_store is None or not plan.pr_url:
                    return False
                try:
                    pr_number = agent.evolution._pr_number_from_url(
                        plan.pr_url,
                    )
                except ValueError:
                    return False
                return deliver_retirement_disposition(
                    agent.config.memory_dir,
                    pr_number,
                    backend=agent.escalation_standdowns,
                    plan_title=plan.title,
                    pr_url=plan.pr_url,
                    comment_poster=agent.evolution.code_host.comment_issue,
                    message_store=agent.message_store,
                )

            def _salvage_permits_retire(plan: EvolutionPlan) -> bool:
                # Salvage veto (proposal 094): behind
                # ``salvage_before_disengage_enabled`` (default-off), route
                # this last autonomous PR-backing-work retirement through the
                # same ``assess_pr_salvage`` oracle the stand-down writers
                # honor. Refuse (return False → the sweep leaves the plan
                # ``implemented``) only when the plan backs an open own PR
                # that reads ``SALVAGE_AND_LAND`` — a code-clearable blocker
                # remains on non-disposable work, so the loop must reconcile
                # and land it, not throw it away. Flag off ⇒ always permit
                # (today's behavior). An absent/unparseable PR URL protects
                # nothing, and a PR absent from a *confirmed* open-owned
                # snapshot is closed or merged (its plan is genuinely dead),
                # so both permit. A failed listing confirms nothing, so it
                # vetoes (#4107). Only an open, salvageable PR vetoes.
                if not agent.config.salvage_before_disengage_enabled:
                    return True
                if not plan.pr_url:
                    return True
                try:
                    pr_number = agent.evolution._pr_number_from_url(
                        plan.pr_url,
                    )
                except ValueError:
                    return True
                try:
                    states = agent._open_owned_pr_states(strict=True)
                except VCSError as exc:
                    logger.warning(
                        "orphaned-cure retire on plan %s vetoed — could "
                        "not confirm PR #%s is closed (open-PR listing "
                        "failed: %s); leaving the plan implemented.",
                        plan.plan_id, pr_number, exc,
                    )
                    return False
                branch_by_pr = {
                    state.number: head_ref
                    for head_ref, state in states.items()
                }
                branch = branch_by_pr.get(pr_number)
                if branch is None:
                    return True
                verdict = agent._assess_pr_salvage(pr_number, branch)
                if verdict is Salvage.SALVAGE_AND_LAND:
                    logger.warning(
                        "orphaned-cure retire on plan %s vetoed — it backs "
                        "salvageable open PR #%s (SALVAGE_AND_LAND): a "
                        "code-clearable blocker remains on non-disposable "
                        "work; leaving the plan implemented for tend.",
                        plan.plan_id, pr_number,
                    )
                    return False
                return True

            retired = agent.memory.sweep_orphaned_cures(
                first_seen,
                iteration,
                agent.config.orphaned_cure_retire_age_cycles,
                deliver_disposition=_deliver_disposition,
                salvage_permits_retire=_salvage_permits_retire,
                lever_attempts=agent.lever_attempts,
            )
            for plan_id in retired:
                agent.journal.record_event(
                    f"I retired my orphaned cure plan {plan_id}: "
                    f"implemented but unmerged past "
                    f"{agent.config.orphaned_cure_retire_age_cycles} "
                    f"audit cycles with a tend-confirmed conflict. "
                    f"Letting it go is the owned act; the PR stays "
                    f"open for Thomas.",
                    entry_type="reflection",
                    triggered_by="orphaned_cure_retire",
                )

    # Rule 12 (phantom escalation) also lives outside ``run_all_checks``
    # because it needs the message store to cross-reference outbound knocks.
    # It surfaces ``partial`` laps whose summary asserts "escalating" for a
    # ``#NNNN`` PR while no message to Thomas references it — escalation that
    # lived only in prose. Prepend + re-clip like rules 9/10/11 so a surfaced
    # phantom escalation reliably reaches ``open_questions`` and the
    # audit→directive bridge.
    if agent.config.phantom_escalation_enabled:
        recent_messages = (
            agent.message_store.get_recent(50)
            if agent.message_store else []
        )
        phantom_escalation_findings = audit_checks_phantom.check_phantom_escalation(
            tasks,
            recent_messages,
            enabled=agent.config.phantom_escalation_enabled,
            settled_prs=settled_prs,
        )
        if phantom_escalation_findings:
            findings = (phantom_escalation_findings + list(findings))[
                : audit_checks.MAX_FINDINGS_PER_CYCLE
            ]

        # Rule-12 delivery arm (proposal 090, default-off
        # ``[audit] phantom_escalation_delivery_enabled``). Detection alone
        # journals the phantom escalation; when the flag is set, *complete*
        # it. For each finding whose escalation prose has recurred across the
        # ≥2-lap threshold (``len(finding.cites) >= 2``; the cites cap
        # ``MAX_CITES_PER_FINDING`` is 5, so the count is a reliable ≥2 gate),
        # recover the ``#NNNN`` from the finding statement and route it
        # through ``deliver_claimed_escalation`` — one merge-or-close message
        # to Thomas + a durable key-keyed standdown (aged out by
        # ``prune_expired_standdowns`` since a plain-issue key never merges).
        # The delivered message references the number, so rule 12
        # self-extinguishes next cycle. Nesting inside the master-switch block
        # makes delivery require detection.
        if agent.config.phantom_escalation_delivery_enabled:
            for finding in phantom_escalation_findings:
                if len(finding.cites) < 2:
                    continue
                for issue_num in sorted(
                    audit_checks.extract_issue_numbers(finding.statement),
                ):
                    if deliver_claimed_escalation(
                        agent.config.memory_dir,
                        issue_num,
                        len(finding.cites),
                        backend=agent.escalation_standdowns,
                        message_store=agent.message_store,
                    ).delivered:
                        # Actuation telemetry (090): a ``handoff`` row only
                        # when the delivery actually happened — the return
                        # is ``False`` on the idempotent already-
                        # standing-down no-op, which must not inflate the
                        # actuation count.
                        audit_checks.log_gate_activation(
                            agent.gate_activations,
                            gate="phantom_escalation_delivery",
                            action="handoff",
                            detail=f"pr=#{issue_num}",
                        )

    # Honest-terminal readiness predicate used by rule 22's
    # green-ready-handoff arm. A
    # ``True`` reply means the cited PR is observed ``ready_and_waiting``
    # — open, every code-clearable blocker clear, sole remaining gate
    # Thomas's approval — so it is a merge-ready handoff candidate. The
    # predicate resolves the PR to its ``maestro/evolve/*``
    # head via the per-iteration open-owned snapshot (the same
    # ``branch_by_pr`` idiom the salvage closures use) and reads the
    # memoized ``observe_pr_readiness`` chokepoint; a PR absent from the
    # snapshot (closed / merged / foreign) resolves ``False``.
    # Defining it is free — it touches the host only when called.
    def _pr_ready_and_waiting(pr_num: int) -> bool:
        # Rule 22 populates the open-owned snapshot memo and passes only
        # owned PR numbers, so ``or {}`` and the ``branch is not None`` guard
        # are defensive folds (not separate branches) — a memo-less or
        # foreign PR reads not-ready rather than raising.
        states = agent._open_owned_pr_states_memo or {}
        branch_by_pr = {
            state.number: head_ref
            for head_ref, state in states.items()
        }
        branch = branch_by_pr.get(pr_num)
        return branch is not None and agent.observe_pr_readiness(
            pr_num, branch,
        ).ready_and_waiting

    # Rule 14 (unconfirmed own landing) also lives outside ``run_all_checks``
    # because it needs the data dir for its first-seen sidecar plus the audit
    # iteration to measure the grace age in cycles, and the agent's merge-truth
    # oracle. It is the most concrete handoff tripwire: a ``partial`` task whose
    # summary carries an on-branch handoff marker + an own committed SHA
    # (a ``#NNNN`` is now optional — a truly PR-less handoff is admitted with
    # ``pr_number=None`` and decided by SHA ancestry alone) where git shows the
    # SHA still not an ancestor of ``origin/main`` (and, for a PR-bearing
    # candidate, the PR not otherwise merged) past the grace age — handed is
    # not landed. Prepend + re-clip like the other out-of-``run_all_checks``
    # rules so a surfaced
    # contradiction reliably reaches ``open_questions`` and the audit→directive
    # bridge.
    if agent.config.unconfirmed_landing_enabled:
        unconfirmed_landing_findings = (
            audit_checks_phantom.check_unconfirmed_own_landing(
                tasks,
                agent.unconfirmed_own_landing,
                iteration,
                age_cycles=agent.config.unconfirmed_landing_age_cycles,
                enabled=agent.config.unconfirmed_landing_enabled,
                is_pr_merged=agent._host_or_git_pr_merged,
                settled_prs=settled_prs,
            )
        )
        if unconfirmed_landing_findings:
            findings = (unconfirmed_landing_findings + list(findings))[
                : audit_checks.MAX_FINDINGS_PER_CYCLE
            ]

        # Rule-14 forced arm (proposal 090, default-off
        # ``[audit] unconfirmed_landing_forced_enabled``). Detection alone
        # journals the handoff gap; when the flag is set, *complete* it. Each
        # past-grace finding above already proved its own committed SHA
        # git-unlanded, so split by whether it cites a PR:
        #  * PR-citing finding → recover the ``#NNNN`` and route it through
        #    ``deliver_handoff`` (unchanged) — one merge-or-close message to
        #    Thomas + a durable ``pr_number``-keyed standdown, idempotent
        #    under its file lock and auto-released by the Tend route's
        #    ``prune_resolved_standdowns`` once git shows the PR settled. It
        #    shares the escalation bridge's ledger (a PR already standing down is
        #    a no-op). ``head_sha`` is empty (the ledger gates on
        #    ``pr_number`` alone); ``stamp_count`` is the citing-task count.
        #  * PR-less finding (a truly unwatched on-branch commit with no
        #    ``#NNNN`` anywhere) → there is nothing to hand up, so recover the
        #    representative SHA from the finding statement and, only when the
        #    one-shot directive slot is empty, write the same fire-or-close
        #    ``_SELF_COMMITTED_FIRE_OR_CLOSE_DIRECTIVE`` the write-time
        #    self-committed bind uses: land the commit via a PR or retire the
        #    branch/plan. Mutating the local ``self_model`` here (before the
        #    generic audit→directive bridge below) pre-empts the generic
        #    ``findings[0]`` directive with this specific one, and the
        #    method's single trailing ``save_self_model`` persists it.
        # Nesting inside the master-switch block makes delivery require
        # detection and ties it to the journal arm's grace window and
        # per-rule cap (mirrors the rule-12 delivery arm).
        if agent.config.unconfirmed_landing_forced_enabled:
            for finding in unconfirmed_landing_findings:
                cited_prs = sorted(
                    audit_checks.extract_issue_numbers(finding.statement),
                )
                if cited_prs:
                    for pr_num in cited_prs:
                        if deliver_handoff_cfg(
                            agent.config,
                            UnlandedCureHandoff(
                                pr_number=pr_num,
                                head_sha="",
                                stamp_count=len(finding.cites),
                            ),
                            backend=agent.escalation_standdowns,
                            message_store=agent.message_store,
                            claim=StanddownClaim.LAP_BUDGET_EXHAUSTED,
                        ):
                            # Telemetry (090): real deliveries only.
                            audit_checks.log_gate_activation(
                                agent.gate_activations,
                                gate="unconfirmed_landing_forced",
                                action="handoff",
                                detail=f"pr=#{pr_num}",
                            )
                else:
                    shas = extract_self_committed_shas(finding.statement)
                    if shas:
                        # Route through the dropped-directive ledger (090),
                        # grounded on the finding's SHA the directive names:
                        # fill the empty slot as before (the method's single
                        # trailing ``save_self_model`` persists it), else —
                        # when the flag is on — ledger the drop for replay.
                        rep_sha = min(shas)
                        offered = offer_directive(
                            self_model,
                            _SELF_COMMITTED_FIRE_OR_CLOSE_DIRECTIVE.format(
                                sha=rep_sha,
                            ),
                            source="rule_14_pr_less",
                            grounding=DirectiveGrounding(shas=(rep_sha,)),
                            backend=agent.dropped_directives,
                            enabled=(
                                agent.config.dropped_directive_replay_enabled
                            ),
                        )
                        # Actuation telemetry (090): ``directive_set`` when
                        # the offer filled the empty slot, ``dropped`` when
                        # it lost the race; grounded on the finding's SHA.
                        audit_checks.log_gate_activation(
                            agent.gate_activations,
                            gate="rule_14_pr_less",
                            action=(
                                "directive_set" if offered else "dropped"
                            ),
                            detail=f"sha={rep_sha}",
                        )

    # Rule 16 (ungrounded question closure, proposal 088 Phase 1) lives
    # outside ``run_all_checks`` behind its own default-off flag and reads
    # the already-loaded resolution tail. It re-surfaces a ``removed`` open
    # question whose only probe did not succeed as a ``contradicted_claim``
    # finding, so the brain cannot mark answered a question the eyes never
    # saw resolved. Prepend + re-clip like rules 9-14 so a surfaced
    # ungrounded closure reliably reaches ``open_questions`` and the
    # audit→directive bridge.
    if agent.config.audit_ungrounded_closure_enabled:
        ungrounded_closure_findings = (
            audit_checks.check_ungrounded_question_closure(
                resolutions,
                enabled=agent.config.audit_ungrounded_closure_enabled,
            )
        )
        if ungrounded_closure_findings:
            findings = (ungrounded_closure_findings + list(findings))[
                : audit_checks.MAX_FINDINGS_PER_CYCLE
            ]

    # Rule 18 (owned-act drought) lives outside ``run_all_checks`` because it
    # reads the plan archive + data dir for durable event streams the agent
    # cannot narrate around. The counted set is flag-dependent: by default
    # ``obsolete`` plan-status events (mark_plan_obsolete) and CHOSEN
    # stand-down verdict rows; under ``wip_primacy_enabled`` it swaps the
    # stand-down rows for merged owned PRs (``version_archive.jsonl``
    # ``merged_at``) so landing — not standing-down — is the rewarded act.
    # When zero such owned terminal acts landed in the window while a
    # prunable ``pending_review`` backlog sits, it emits one finding
    # demanding ONE owned act. Prepend + re-clip like rules 9-14 so the
    # surfaced demand reliably reaches ``open_questions`` and the
    # audit→directive bridge — the mechanism that makes the drought bind
    # rather than merely report.
    if agent.config.owned_act_drought_enabled:
        owned_act_drought_findings = agent._owned_act_drought_findings(
            agent.memory.get_all_plans(),
        )
        if owned_act_drought_findings:
            findings = (owned_act_drought_findings + list(findings))[
                : audit_checks.MAX_FINDINGS_PER_CYCLE
            ]

    # Rule 19 (phantom-partial-prune) also lives outside ``run_all_checks``
    # because it reads ``task_history.jsonl`` plus the plan store under the
    # data dir — neither of which ``run_all_checks`` receives. The
    # affirmative counterpart to the git-keyed landing binds: a *landed* owned
    # ``mark_plan_obsolete`` prune stamped ``partial`` whose target plan_id
    # reads ``obsolete`` on disk — the prune landed, so the ``partial``
    # under-claimed it. Prepend + re-clip like rules 9-18 so the surfaced
    # contradiction reliably reaches ``open_questions`` and the
    # audit→directive bridge. Default-off, gated by the opt-in
    # ``[audit] phantom_partial_prune_enabled`` master switch, matching the
    # sibling-rule convention (the check self-suppresses on ``enabled=False``
    # too, so the flag is the single decision point). The window mirrors the
    # ``get_recent_tasks(20)`` tail the other data-dir rules scan.
    if agent.config.phantom_partial_prune_enabled:
        phantom_partial_prune_findings = (
            audit_checks_phantom.check_phantom_partial_prune(
                agent.config.memory_dir,
                20,
                enabled=agent.config.phantom_partial_prune_enabled,
            )
        )
        if phantom_partial_prune_findings:
            findings = (phantom_partial_prune_findings + list(findings))[
                : audit_checks.MAX_FINDINGS_PER_CYCLE
            ]

    # Rule 20 (under-credited-landing) is the affirmative twin of rule 14:
    # where rule 14 flags a ``partial`` handoff whose committed SHA git shows
    # is *not* on ``origin/main`` (over-claim), this flags a non-``success``
    # task whose own-committed SHA git *proves* IS an ancestor of
    # ``origin/main`` — a landing authored and stamped nothing. It reuses the
    # same ``merge-base --is-ancestor`` oracle rule 14 uses (defaulted inside
    # the check to ``is_branch_merged_into_main``), so no extra seam is wired
    # here. Prepend + re-clip like rules 9-19 so the surfaced contradiction
    # reliably reaches ``open_questions`` and the audit→directive bridge.
    # Default-off, gated by the opt-in ``[audit] undercredited_landing_enabled``
    # master switch (the check self-suppresses on ``enabled=False`` too, so
    # the flag is the single decision point). Not an auto-relabel — the
    # ``partial`` may be correct for other goals — so detection only surfaces
    # the under-credit as an ``[audit]`` question for the LLM to judge.
    if agent.config.undercredited_landing_enabled:
        undercredited_landing_findings = (
            audit_checks_phantom.check_undercredited_landing(
                tasks,
                enabled=agent.config.undercredited_landing_enabled,
            )
        )
        if undercredited_landing_findings:
            findings = (undercredited_landing_findings + list(findings))[
                : audit_checks.MAX_FINDINGS_PER_CYCLE
            ]

    # Rule 21 (reconcile-near-miss) is the *pre-landing* complement of rule
    # 20: where rule 20 credits a landing whose lineage SHA git-proves it
    # landed, rule 21 flags a ``partial`` remediation on a git-unlanded own
    # PR measured ≤ ``reconcile_near_max_behind`` behind ``origin/main``
    # whose lineage authored no ancestor-of-main reconcile SHA — the
    # actuator gap in the ≤N-behind band that keeps "awaiting review/merge"
    # structurally available for the one case that is mine to land. The three
    # git seams are dependency-injected: ``is_settled`` reads the
    # per-iteration-memoized merge-truth chokepoint (``is_pr_settled``),
    # ``commits_behind`` measures the cited PR's own head branch against
    # ``origin/main`` (``None`` when the PR is not an open owned evolution
    # PR, so an un-mappable citation cannot fire), and ``is_ancestor``
    # defaults inside the check to the same ``merge-base --is-ancestor``
    # oracle rule 20 uses (keyed by SHA). Prepend + re-clip like rules 9-20
    # so the surfaced contradiction reliably reaches ``open_questions`` and
    # the audit→directive bridge writes ``Reconcile+push #N or gh pr close
    # #N`` into an empty ``next_task_directive`` — under ``[loop]
    # owed_act_binding_enabled`` the ``owed_act`` seam drops the close clause
    # while the PR reads ``CODE_OWED``. Default-off, gated by the
    # opt-in ``[audit] reconcile_near_miss_enabled`` master switch (the check
    # self-suppresses on ``enabled=False`` too, so the flag is the single
    # decision point).
    if agent.config.reconcile_near_miss_enabled:
        reconcile_pr_branches: dict[int, str] = {
            state.number: branch
            for branch, state in
            agent.evolution.code_host.get_open_evolution_pr_states(
                "maestro/evolve/",
            ).items()
        }

        def _reconcile_commits_behind(pr_num: int) -> int | None:
            branch = reconcile_pr_branches.get(pr_num)
            if branch is None:
                return None
            _ahead, behind = agent._vcs.commits_ahead_behind(
                branch, "origin/main",
            )
            return behind

        reconcile_near_miss_findings = (
            audit_checks_phantom.check_reconcile_near_miss(
                tasks,
                enabled=agent.config.reconcile_near_miss_enabled,
                max_behind=agent.config.reconcile_near_max_behind,
                is_settled=lambda n: agent.is_pr_settled(n).settled,
                commits_behind=_reconcile_commits_behind,
                owed_act=owed_act_binding(agent.config).seam(
                    reconcile_pr_branches, agent.observe_pr_readiness,
                ),
                on_disposal_offer=make_disposal_offer_recorder(
                    reconcile_pr_branches, agent.observe_pr_readiness,
                    agent.gate_activations,
                ),
            )
        )
        if reconcile_near_miss_findings:
            findings = (reconcile_near_miss_findings + list(findings))[
                : audit_checks.MAX_FINDINGS_PER_CYCLE
            ]

    # Rule 22 (green_ready_handoff) forced arm — the earliest, most-owned
    # handoff trigger. Unlike the escalation bridge (which hands a PR up only
    # after lap-budget exhaustion) and rule 14 (which fires on a
    # verdict/SHA contradiction after the fact), this fires the
    # instant an owned, git-unlanded PR reads merge-ready — every CI check
    # SUCCESS, no code-clearable merge blocker (the shared
    # ``_pr_ready_and_waiting`` readiness fact) — and carries no
    # ``pr_number``-keyed handoff standdown. It routes each such PR through the
    # same ``deliver_handoff`` actuator + keyed-release path rule 14 uses:
    # one merge-or-close message to Thomas plus a durable standdown that
    # self-extinguishes the rule (a stood-down PR is filtered out by
    # ``green_ready_handoff_prs`` so ``deliver_handoff`` is never re-invoked)
    # and auto-releases via the Tend route's ``prune_resolved_standdowns`` the
    # moment git shows the PR merged/closed. No new push capability — the only
    # owned act is looking + one message. Default-on; gated by the
    # ``[audit] green_ready_handoff_enabled`` master switch.
    if agent.config.green_ready_handoff_enabled:
        owned_states = agent._open_owned_pr_states(strict=False)
        green_ready_prs = audit_checks_phantom.green_ready_handoff_prs(
            [state.number for state in owned_states.values()],
            enabled=agent.config.green_ready_handoff_enabled,
            is_green_ready=_pr_ready_and_waiting,
            has_standdown=lambda pr: has_standdown(
                agent.escalation_standdowns, pr,
            ),
        )
        for pr_num in green_ready_prs:
            # Delivery-truth bind: the arm's 'delivered' truth binds to a
            # ``pr_number``-keyed ``stand_down_verdicts.jsonl`` row written
            # during this fire — the one artifact narration cannot author — not
            # to ``deliver_handoff``'s return (mirroring push delivery binding
            # to a remote-head advance). ``confirm_handoff_delivery`` counts
            # that ledger's rows around the actuator and persists the row
            # directly at zero SDK cost when this fire added none (a stale row
            # from an earlier act does not count), so every fire leaves its own
            # durable artifact the STANDDOWN sensor can count.
            def _deliver(pr_num: int = pr_num) -> bool:
                return deliver_handoff_cfg(
                    agent.config,
                    UnlandedCureHandoff(
                        pr_number=pr_num,
                        head_sha="",
                        stamp_count=1,
                    ),
                    backend=agent.escalation_standdowns,
                    message_store=agent.message_store,
                    plan_lookup=agent._plan_for_pr_number,
                    claim=StanddownClaim.HANDOFF_READY,
                )

            delivered = audit_checks_phantom.confirm_handoff_delivery(
                agent.stand_down_verdicts,
                pr_num,
                _deliver,
            )
            # Actuation telemetry (090): ``handoff`` when the actuator wrote the
            # durable verdict row itself during this fire,
            # ``handoff_row_persisted`` when the bind backfilled it directly
            # (today's ``deliver_handoff`` writes no verdict row, so production
            # fires honestly read as backfilled).
            audit_checks.log_gate_activation(
                agent.gate_activations,
                gate="green_ready_handoff",
                action="handoff" if delivered else "handoff_row_persisted",
                detail=f"pr=#{pr_num}",
            )
            # Deed ledger: every PR reaching this loop passed the
            # ``has_standdown`` filter in ``green_ready_handoff_prs``, so
            # ``deliver_handoff`` genuinely dispatched (never the idempotent
            # no-op) and left a durable standdown — a fired terminal act.
            # Record exactly one deed per delivered PR; a PR already
            # stood-down (the no-op/skip branch) was filtered out before the
            # loop and records nothing. ``delivered`` only distinguishes who
            # wrote this fire's verdict row, not whether the handoff fired.
            agent.deed_ledger.record(
                deed_ledger.DeedType.HANDOFF_DELIVERED,
                pr_num,
                detail=(
                    f"green_ready_handoff handed off PR #{pr_num} "
                    f"(row {'left by actuator' if delivered else 'backfilled'})"
                ),
            )

    # Banked-owned-act-reperformance detector — the retrospective
    # artifact-id-axis complement to the prospective
    # ``check_untouched_first_target`` task gate. Where that gate steers the
    # FIRST selection away from an already-disposed target, this reads the
    # three owned-act ledgers (stand-down verdicts, obsolete-prune plans,
    # delivered handoffs) under the data dir and flags a
    # ``next_task_directive`` or queued task goal that re-performs an act
    # already banked there — re-doing it only re-banks a duplicate. Prepend +
    # re-clip like the sibling rules so the surfaced contradiction reliably
    # reaches ``open_questions`` and the audit→directive bridge. Default-off,
    # gated by the opt-in ``[audit] banked_owned_act_enabled`` master switch
    # (the check self-suppresses on ``enabled=False`` too, so the flag is the
    # single decision point). Git-free; zero SDK cost (bounded ledger reads).
    if agent.config.banked_owned_act_enabled:
        banked_owned_act_findings = (
            audit_checks.check_banked_owned_act_reperformance(
                agent.config.memory_dir,
                self_model.next_task_directive,
                [t.goal for t in agent.task_queue.load() if t.goal],
                enabled=agent.config.banked_owned_act_enabled,
                stand_down_backend=agent.stand_down_verdicts,
            )
        )
        if banked_owned_act_findings:
            findings = (banked_owned_act_findings + list(findings))[
                : audit_checks.MAX_FINDINGS_PER_CYCLE
            ]

    # Build journal-entry contents and the matching ``[audit]`` open
    # questions in one pass — write nothing yet.
    journal_contents: list[str] = []
    for finding in findings:
        cite_suffix = (
            f" — cites: {', '.join(finding.cites)}"
            if finding.cites else ""
        )
        journal_contents.append(
            f"[{finding.kind}] {finding.statement}{cite_suffix}",
        )
        # A re-firing finding is upserted to one slot (#4113).
        self_model.open_questions = upsert_open_question(
            self_model.open_questions,
            f"[audit] {finding.kind}: {finding.statement[:160]}",
        )

    # Audit→directive bridge: when new findings exist and no
    # directive is already set, auto-route the first finding into
    # ``next_task_directive`` so the next ``generate_task`` picks it
    # up without waiting for a contemplation round-trip.
    #
    # WIP-primacy stand-down (proposal 092): while owned WIP with a
    # self-clearable blocker is open, the WIP owner — not an audit finding —
    # owns the slot, so the head-of-chain ``wip_primacy`` gate is free to
    # name the PR to finish. The ``_wip_owns_slot`` conjunct is evaluated
    # last, so its ``_current_task_focus`` probe (open-PR enumeration +
    # readiness) runs only when the bridge would otherwise fire, and — since
    # ``_wip_owns_slot`` short-circuits on the flag — never at all while
    # ``wip_primacy_enabled`` is off.
    if (
        findings
        and not self_model.next_task_directive
        and not agent._wip_owns_slot()
    ):
        bridge_directive = audit_checks.finding_to_directive(findings[0])
        self_model.next_task_directive = bridge_directive
        # Actuation telemetry (090): the bridge routes the first finding
        # into the empty slot but logged nothing before, so it left no
        # record to correlate. Log ``directive_set`` with the directive
        # text as detail — any ``#NNNN`` in it doubles as settlement
        # grounding for the narration-conversion sensor; a row with none
        # simply drops out of the settlement denominator.
        audit_checks.log_gate_activation(
            agent.gate_activations,
            gate="audit_directive_bridge",
            action="directive_set",
            detail=bridge_directive,
        )

    # Undefined-coinage → directive bridge (proposal 101): the actuator half
    # of the ``UNDEFINED-COINAGE`` display sensor. Convert the top coinage the
    # recent journal is actively minting but ``docs/lexicon.md`` does not yet
    # define into a one-shot ``next_task_directive`` steering the next task
    # toward defining it (or letting it lapse). Fills only an *empty* slot —
    # deferring to the generic finding bridge above and to owned WIP — and
    # self-extinguishes as terms are defined or fall out of use. Reads the
    # lexicon doc + journal tail only when enabled; zero SDK cost, default-off.
    # Mirrors ``contemplate``'s display-sensor data plumbing exactly.
    coinage_directive_set = False
    if agent.config.audit_undefined_coinage_directive_enabled:
        lexicon_path = agent.config.workspace / "docs" / "lexicon.md"
        defined_spellings: frozenset[str] = (
            all_spellings(load_lexicon(lexicon_path))
            if lexicon_path.exists()
            else frozenset()
        )
        coinage_directive = audit_checks.check_undefined_coinage_directive(
            measure_undefined_coinages(
                [
                    entry.content
                    for entry in agent.journal.read_recent(
                        _UNDEFINED_COINAGE_JOURNAL_WINDOW,
                    )
                ],
                lexicon_terms=defined_spellings,
            ),
        )
        if (
            coinage_directive
            and not self_model.next_task_directive
            and not agent._wip_owns_slot()
        ):
            self_model.next_task_directive = coinage_directive
            coinage_directive_set = True
            # Actuation telemetry (090): mirror the generic bridge so the
            # coinage bridge's empty-slot fills correlate the same way.
            audit_checks.log_gate_activation(
                agent.gate_activations,
                gate="undefined_coinage_directive",
                action="directive_set",
                detail=coinage_directive,
            )

    # Persist the self-model BEFORE the journal event records.
    # ``atomic_write`` only guarantees per-file atomicity; ordering
    # the more authoritative write first means a crash between the
    # two leaves persisted findings without a journal entry rather
    # than the inverse (orphan journal entries citing findings the
    # self-model never recorded). ``coinage_directive_set`` widens the
    # guard so a finding-free cycle that filled the directive slot from
    # the undefined-coinage bridge above still persists the assignment.
    if findings or coinage_directive_set:
        agent.consciousness.save_self_model(self_model)

    for content in journal_contents:
        agent.journal.record_event(
            content=content,
            entry_type="audit_observation",
            triggered_by="audit_self_model",
        )

    result = AuditResult(
        iteration=iteration,
        timestamp=datetime.now(UTC).isoformat(),
        findings=list(findings),
        rules_fired=sorted({f.rule for f in findings}),
    )
    agent.memory.append_audit_result(result)

    # Mark rule-9 versions surfaced only now that they survived every
    # clip and are persisted; a crash before this re-raises, never loses.
    surfaced_impact_ids = [
        cite
        for f in findings
        if f.rule == "evolution_impact_review"
        for cite in f.cites
    ]
    if surfaced_impact_ids:
        agent.impact_review_surfaced.record(surfaced_impact_ids)

    # Shared audit tail for the two accountability actuators below
    # (proposal 129 Phase C refutation-accountability restore, then the
    # proposal 088 reality-refutation escalation). Read at most once, and
    # only when one of them is enabled, so turning both on does not
    # double-read ``audit_history.jsonl``.
    _cached_audit_tail: list[AuditResult] | None = None

    def _recent_audit_results() -> list[AuditResult]:
        nonlocal _cached_audit_tail
        if _cached_audit_tail is None:
            _cached_audit_tail = agent.memory.get_recent_audit_results(
                _REALITY_REFUTATION_AUDIT_CYCLES,
            )
        return _cached_audit_tail

    # Snapshot ``open_questions`` before the 129 refutation-accountability
    # restore below can rebind it, so the proposal 088 reality-refutation
    # actuator keys on the refuted entries as this cycle's audit left them.
    # The two actuators are additive — for a mechanical_fact PR both flags
    # cover, 088 still escalates *and* 129 restores, not one in place of the
    # other (proposal 129 non-goal). With the 129 flag off this equals the
    # live list, so the 088-only path stays byte-identical.
    _open_questions_before_restore = list(self_model.open_questions)

    # Refutation accountability (proposal 129 Phase C): the eyes win the
    # dispute across the whole mechanical-fact class, and the correction is
    # owed to the self-model, not a message queue. A refuted
    # ``[audit refuted]`` open question whose ``mechanical_fact`` rule keeps
    # re-firing on the same target across the threshold of consecutive
    # cycles is mechanically restored to its canonical ``[audit]`` text
    # (re-derived from the newest finding, truncated as the upsert stores
    # it), or dropped when already present — a duplicate would burn a
    # ``MAX_AUDIT_OPEN_QUESTIONS`` slot. One restore per refuted question;
    # journal + ``refutation_accountability`` gate row. Default-off.
    if agent.config.audit_refutation_accountability_enabled:
        rejections = audit_checks.measure_refutation_rejections(
            _recent_audit_results(),
            self_model.open_questions,
            threshold=agent.config.audit_refutation_rejection_cycles,
        )
        # One restore per refuted question (a question may name several
        # targets; keep the deepest-recurring as its representative).
        by_question: dict[str, audit_checks.RefutationRejection] = {}
        for rejection in rejections:
            current = by_question.get(rejection.question)
            if current is None or rejection.cycles > current.cycles:
                by_question[rejection.question] = rejection
        if by_question:
            present = {
                _normalize_question(q) for q in self_model.open_questions
            }
            rebuilt: list[str] = []
            restored: list[audit_checks.RefutationRejection] = []
            for question in self_model.open_questions:
                matched = by_question.get(question)
                if matched is None:
                    rebuilt.append(question)
                    continue
                restored.append(matched)
                canonical = Consciousness._truncate_item(
                    matched.restored_question, Consciousness.MAX_ITEM_CHARS,
                )
                if _normalize_question(canonical) in present:
                    continue
                rebuilt.append(canonical)
                present.add(_normalize_question(canonical))
            self_model.open_questions = rebuilt
            agent.consciousness.save_self_model(self_model)
            for overturned in restored:
                agent.journal.record_event(
                    content=(
                        f"Mechanically overturned my refutation of "
                        f"{overturned.rule} on {overturned.target}: the "
                        f"finding re-fired {overturned.cycles} consecutive "
                        f"cycles while I disputed it, so I restored the "
                        f"canonical audit question. The eyes win this "
                        f"dispute; the correction is owed to my self-model."
                    ),
                    entry_type="audit_observation",
                    triggered_by="audit_self_model",
                )
                audit_checks.log_gate_activation(
                    agent.gate_activations,
                    gate="refutation_accountability",
                    action="refutation_rejected",
                    detail=(
                        f"{overturned.rule} {overturned.target}: "
                        f"{overturned.cycles} consecutive cycles"
                    ),
                )

    # Reality-refutation actuator (proposal 088 Phase 2): the eyes win a
    # sustained dispute. After persisting this cycle, hand up exactly one
    # merge-or-close escalation (and stand the lineage down via the shared
    # ledger) for any PR whose git-grounded finding has recurred across the
    # threshold of consecutive cycles while a matching ``[audit refuted]``
    # entry disputes it and git still shows the PR unmerged. The standdown
    # suppresses further remediation minting and auto-releases when git shows
    # the PR merged/closed (``prune_resolved_standdowns``, already wired in
    # ``loop/pr_remediation``). Default-off; idempotent per target.
    if agent.config.audit_reality_refutation_enabled:
        refuted_prs: set[int] = set()
        for question in _open_questions_before_restore:
            if isinstance(question, str) and question.startswith(
                "[audit refuted",
            ):
                refuted_prs |= audit_checks.extract_issue_numbers(question)
        if refuted_prs:
            refutation_counts = measure_reality_refutation(
                _recent_audit_results(),
            )
            threshold = agent.config.audit_reality_refutation_threshold
            # Per refuted PR, the deepest consecutive-recurrence count across
            # the reality-grounded rules; only PRs the LLM has refuted and
            # that meet the threshold qualify.
            per_pr_count: dict[int, int] = {}
            for (_rule, pr), count in refutation_counts.items():
                if count >= threshold and pr in refuted_prs:
                    per_pr_count[pr] = max(per_pr_count.get(pr, 0), count)
            for pr in sorted(per_pr_count):
                if agent.is_pr_settled(pr).branch_landed:
                    continue
                deliver_reality_refutation(
                    agent.config.memory_dir,
                    pr,
                    per_pr_count[pr],
                    backend=agent.escalation_standdowns,
                    message_store=agent.message_store,
                )

    # Unwinnable-race escalation (proposal 099 step 8). The heads-up sibling
    # of the reality-refutation actuator above — but a *concession, not a
    # give-up*: for each conflict-remediation PR lineage that re-entered the
    # resolution race on base advance at/above the threshold without ever
    # landing, deliver exactly one durable message to Thomas and record
    # **no** stand-down, so the PR stays fully tend-eligible and the loop
    # keeps re-entering the race after the human is told. Mirrors the
    # reality-refutation dispatch (sorted PRs, settled-filter via the
    # ``is_pr_settled`` chokepoint, idempotent per target through the message
    # thread). The re-entry count is measured over a *dedicated* wider window
    # (``_UNWINNABLE_RACE_TASK_WINDOW``) rather than the 20-task audit tail:
    # a stuck PR interleaved with other work can otherwise age its own laps
    # out of a 20-task view before it reaches the threshold. Those records
    # are ``TaskRecord`` dataclasses, so they are ``asdict``-ed to the dict
    # shape the pure sensor reads — the same conversion ``contemplate``
    # performs for ``format_base_advance_races``.
    race_re_entries = measure_unwinnable_race_re_entries(
        [
            asdict(t)
            for t in agent.memory.get_recent_tasks(
                _UNWINNABLE_RACE_TASK_WINDOW,
            )
        ],
    )
    unwinnable_threshold = agent.config.unwinnable_race_re_entry_threshold
    for pr in sorted(race_re_entries):
        if race_re_entries[pr] < unwinnable_threshold:
            continue
        # Skip any PR that is no longer *open* — merged or closed-unmerged
        # alike. ``settled`` is the merge-truth chokepoint's not-open
        # verdict (True for MERGED or CLOSED, False only for OPEN);
        # ``branch_landed`` alone would still fire the "merge it yourself"
        # heads-up on a PR Thomas has already closed, and — unlike the
        # sibling escalations — this deliverer records no stand-down whose
        # later auto-release would retract such a message. A settled PR is
        # not an unwinnable race.
        if agent.is_pr_settled(pr).settled:
            continue
        deliver_unwinnable_race(
            agent.config.memory_dir,
            pr,
            race_re_entries[pr],
            backend=agent.escalation_standdowns,
            message_store=agent.message_store,
        )

    # On-settlement record reconciler (proposal 103). Drive the reconciler
    # registry over the settled PRs this cycle actually *observed*, so each
    # such PR's PR-keyed store traces (the escalation standdown and the
    # self-model rows) are cleared under one provenance-stamped owner every
    # audit cycle — not only when the tend pass happens to run — with every
    # clear recorded to the durable ``settlement_reconciliations.jsonl``
    # ledger. It runs *last*, after the trailing ``save_self_model`` and the
    # journal/audit-history writes above: the ``SelfModelReconciler`` does a
    # disk read-modify-write on ``self_model.json``, and the audit's own
    # trailing save persists the in-memory ``self_model`` loaded at the top
    # of this method — so a reconciler running before that save would be
    # clobbered by it. Driving here makes the reconciler's write
    # authoritative over the fully-settled self-model state of the cycle.
    # The drive is confined to PRs already in the per-iteration merge-truth
    # memo: those are exactly the ones ``reconcile_settled_verdicts`` just
    # observed (or another face observed earlier this iteration), so
    # ``is_pr_settled`` returns their memoized ``MergeTruth`` and the drive
    # costs zero extra merge-oracle calls. A ledger-only PR whose
    # ``Remediate`` record has scrolled out of the trailing window has a cold
    # memo and is skipped here — driving it would force a real
    # per-PR-per-cycle git-fetch + host-API observation (the cost 097
    # deliberately declined); a fresh remediation lap re-observes it. (Ledger
    # growth is not a reason to skip: the driver writes a row only when a
    # reconciler changes a store, so re-driving an already-clean warm-memo PR
    # each cycle adds no rows.) The reconcilers themselves re-gate the clear
    # on ``truth.settled`` so a reopened memo-warm PR is never cleared. Gated
    # by the default-on (proposal 103 step 7)
    # ``[audit] settlement_record_reconciler_enabled`` flag, which an
    # operator may disable; and because it consumes the settled set, an
    # empty set (the 097 flag off) makes the drive a no-op regardless.
    if agent.config.settlement_record_reconciler_enabled:
        for pr in sorted(settled_prs):
            if pr not in agent._merge_truth_memo:
                continue
            # Delegate to the shared single-PR drive (proposal 103). The
            # warm-memo guard stays here so this audit sweep still forces
            # no cold merge-truth observations; the helper is the same
            # three lines this loop ran before the extraction.
            agent._reconcile_settled_pr_records(pr, iteration)

    # Split-due directive bridge (proposal 147): the actuator half of the
    # ``SPLIT-DUE`` sensor. Fill an *empty* ``next_task_directive`` with a
    # concern-split directive for the worst split-due file. It runs last
    # because the blind ``save_self_model`` calls above persist the in-memory
    # snapshot loaded at the top and would revert a disk-side fill. The fill is
    # a transactional empty-slot write recorded via ``fire_and_capture`` with
    # a re-read verify, so an occupied slot adds no lever row (written once)
    # and a write that did not persist is recorded ``False`` with no gate row.
    # The flag, slot and WIP checks run first so an occupied slot never pays
    # for the tree walk and AST parse the readback does.
    split_directive = (
        split_due_directive_readback(agent.config)
        if agent.config.audit_split_due_directive_enabled
        and not agent.consciousness.load_self_model().next_task_directive
        and not agent._wip_owns_slot()
        else None
    )
    if split_directive:
        directive = audit_checks.truncate_directive(split_directive)

        def _fill(text: str) -> bool:
            def _set(model: SelfModel) -> bool:
                if model.next_task_directive:
                    return False
                model.next_task_directive = text
                return True
            return agent.consciousness.mutate_self_model(_set)

        if fire_and_capture(
            agent.lever_attempts, "split_due_directive", _fill, directive,
            verify=lambda: (
                agent.consciousness.load_self_model().next_task_directive
                == directive
            ),
        ):
            audit_checks.log_gate_activation(
                agent.gate_activations,
                gate="split_due_directive",
                action="directive_set",
                detail=directive,
            )
