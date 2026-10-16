"""Implementation cluster carved out of ``maestro.agent`` (proposal 147, Step 7).

Holds the approved-plan implementation path formerly on ``MaestroAgent``:
``implement_plan`` (Session C plus the post-implementation quality gate and
PR open), the ``_implement_approved_plans`` dispatcher with its slice
rollout and deferral helpers, the scope gate's ``_replan_for_scope`` and
``_slice_scope_rejected_plan``, ``_assess_criteria_outcome``,
``_snapshot_version`` and ``_demote_plan_to_failed``, plus the module-level
``next_eligible_slice`` and ``_check_uncalled_symbol_gate`` that only this
cluster uses. It is one concern moved intact to bring ``maestro/agent.py``
under its source budget.

The extraction is behaviour-preserving: the same bodies, the same call
order, the same effects — only the receiver changed from ``self`` to a first
positional ``agent`` argument, matching
:func:`maestro.loop.contemplation.contemplate`. ``MaestroAgent`` keeps a
one-line delegator per moved method, and calls between the moved functions
still dispatch through ``agent.<name>`` so instance-level patches keep
intercepting. Every module global the bodies read is imported from its
defining module, so a test patches it as
``maestro.loop.implementation.<name>``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from maestro import diagnostics
from maestro.attempt_disposition import classify_attempt
from maestro.config import Config
from maestro.evolution import _MAX_WARNING_BULLETS, ProposalParseError, _symbol_has_call_site
from maestro.evolution_submission import SUPERSEDED_BY_PROPOSAL_PREFIX
from maestro.gate_observation import render_identities
from maestro.generation_gate import GENERATION_STANDDOWN_DIRECTIVE
from maestro.loop import plan_holds
from maestro.loop.plan_refusal import dispose_over_cap_plan, journal_plan_refusal
from maestro.memory import (
    _OBSOLETE_REASON_MAX_CHARS,
    _PLAN_CHANGE_ITEM_MAX_CHARS,
    EvolutionPlan,
    EvolutionProposal,
    PlanStep,
    TaskRecord,
    VersionRecord,
    slice_ended_without_pr,
)
from maestro.planner import _find_unverified_code_files, check_plan_scope, check_plan_scope_detailed
from maestro.prompt_caps import MAX_TASK_GOAL_BYTES, cap_utf8
from maestro.prompts.defaults import CRITERIA_ASSESSMENT_PROMPT, SCOPE_REPLAN_PROMPT
from maestro.sdk import (
    LONG_CONTEXT_BETA,
    QueryOptions,
    SDKError,
    _note_session_effort_override,
    chars_complete,
    create_session,
    disconnect_session,
    is_environment_fault,
    json_complete,
    query,
    query_on_session,
)
from maestro.sdk import TRANSIENT_SDK_CATEGORIES as _TRANSIENT_SDK_CATEGORIES
from maestro.types import TaskOutcome
from maestro.uncalled_scope import UNCALLED_GREP_EXCLUDES, counts_for_uncalled_scan
from maestro.vcs import VCSError, evolution_branch

if TYPE_CHECKING:
    from maestro.agent import MaestroAgent
    from maestro.providers.base import Session

logger = logging.getLogger("maestro")

#: Matches unified-diff ``+++ b/<path>`` headers to extract the file.
_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$")
#: Matches added ``def`` or ``class`` lines (``+`` prefix, not ``++``).
_ADDED_SYMBOL_RE = re.compile(
    r"^\+(?!\+)\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)"
)


# Bounds for rendering gate failure identities into a *directive* — the two hold
# arms of ``implement_plan`` (proposal 124's inherited hold and the environment
# hold). Both render through ``gate_observation.render_identities``, and the
# inherited arm's rendering is what fills ``{identities}`` in
# ``loop/plan_holds.py::_INHERITED_BASE_REPAIR_DIRECTIVE`` — whose 133 static
# chars plus at most ``_INHERITED_HOLD_DIRECTIVE_IDENTITY_CHARS`` (60) keep the
# whole directive under ``Consciousness.MAX_DIRECTIVE_CHARS`` (200) *before*
# ``save_self_model`` (called in ``plan_holds``) truncates as a backstop.
_INHERITED_HOLD_DIRECTIVE_IDENTITY_LIMIT = 3


_INHERITED_HOLD_DIRECTIVE_IDENTITY_CHARS = 60


# Bounds for rendering gate failure identities into a durable *record*, across
# all three consumers (proposal 124): the terminal-demote ``failure_reason`` in
# ``implement_plan`` — on *both* the inherited-demote (hold budget spent) and the
# non-inherited stalled-convergence demote (ATTRIBUTED / UNATTRIBUTABLE / flag-off),
# which now records the sorted identities rather than a mid-word ``raw_output``
# byte slice — and the held-red ``partial`` notes the two record-time landing arms
# stamp (``_land_uncommitted_owned_edit`` / ``_push_committed_unpushed_owned_head``).
# Each names the gate failures that actually held or demoted the plan, so it is
# generous where the ``_INHERITED_HOLD_DIRECTIVE_*`` directive above is tight —
# sized to the ``raw_output[-500:]`` tail it replaces.
_GATE_RECORD_IDENTITY_LIMIT = 50


_GATE_RECORD_IDENTITY_CHARS = 500


def next_eligible_slice(
    proposal: EvolutionProposal,
    slices: Sequence[EvolutionPlan],
    *,
    is_settled: Callable[[EvolutionPlan], bool],
) -> EvolutionPlan | None:
    """Return the one slice of *proposal* that may be worked next, else ``None``.

    The single owner of the question *"given a multi-slice ``EvolutionProposal``,
    which slice is eligible to work now?"* (proposal 118). A multi-PR
    self-improvement lands one thin slice per PR, and a slice becomes eligible
    only once its immediate predecessor's PR has *settled* — merged or closed —
    so eligibility is a pure function of ``phase_index`` ordering plus
    merge-truth. Both are injected: *slices* are the proposal's slice plans (its
    ``EvolutionPlan``s, filtered by ``parent_plan_id``) and *is_settled* is the
    settlement seam. Keeping settlement injected (mirroring
    :func:`maestro.audit_checks_phantom.check_reconcile_near_miss`'s ``is_settled=``
    seam) leaves this predicate pure and testable; the closure that reads a
    slice's PR through the one merge-truth door (``pr_url`` →
    ``_pr_number_from_url`` → :meth:`is_pr_settled`) belongs to a later wiring
    increment, not here. Nothing in the loop calls this yet — inert scaffolding
    that establishes the single door the later behavioral increments wire to.

    Returns ``None`` when *proposal* is already terminal (``status`` is
    ``"complete"`` or ``"abandoned"``). Otherwise walks *slices* in ascending
    ``phase_index`` order and returns the lowest-``phase_index`` slice that is
    not yet settled — the head slice has no predecessor and is eligible until it
    settles, and every later slice's predecessor is settled by construction of
    "lowest unsettled". Returns ``None`` when every slice has settled (the
    proposal is complete in fact) and, defensively, when a *settled* slice sits
    at a higher ``phase_index`` than an unsettled one: a successor cannot settle
    before its predecessor under in-order rollout, so an inconsistent chain
    yields no eligible slice rather than a guessed one. Pure, zero SDK cost.
    """
    if proposal.status in ("complete", "abandoned"):
        return None
    ordered = sorted(slices, key=lambda plan: plan.phase_index)
    eligible: EvolutionPlan | None = None
    for slice_ in ordered:
        if is_settled(slice_):
            if eligible is not None:
                # A settled slice sits after an unsettled predecessor — the
                # chain cannot advance out of order, so no slice is eligible.
                return None
            continue
        if eligible is None:
            eligible = slice_
    return eligible


def _check_uncalled_symbol_gate(
    diff_text: str,
    config: Config,
) -> tuple[bool, str]:
    """Post-self-review blocking gate for unwired definitions.

    Parses *diff_text* for added ``def``/``class`` lines, then uses
    ``git grep -n`` to look for non-test call sites for each symbol.  A
    reference anywhere in non-test code — including the symbol's own module,
    but excluding its own definition line — satisfies the gate; only a symbol
    whose sole appearance is its own definition line is flagged.  Intra-module
    helpers (the normal shape of a decomposed feature) therefore pass.
    Returns ``(True, '')`` when the number of uncalled symbols is within
    the configured threshold or the gate is disabled.  Otherwise returns
    ``(False, message)`` listing the offending symbols.

    The logic mirrors :func:`~maestro.evolution_quality._find_uncalled_symbols`
    — including its batched single ``git grep`` fork (one ``-e <name>`` per
    symbol, with the rows attributed back per symbol in-process by the same
    ``--fixed-strings`` substring test; #1856) — but lives as a standalone
    function so it can be called and tested independently of the EvolutionEngine
    instance.  The two batched copies are kept in sync by hand rather than
    sharing a helper: both use module-level regexes now, differing only in that
    the engine copy reads ``engine._config`` while this function takes *config*
    (a shared extraction is a follow-up).
    """
    if not config.uncalled_symbol_gate_enabled:
        return True, ""

    # 1. Parse the diff to collect (file, symbol) pairs.
    current_file: str | None = None
    symbols: list[tuple[str, str]] = []  # (file_path, symbol_name)

    for line in diff_text.splitlines():
        file_match = _DIFF_FILE_RE.match(line)
        if file_match:
            current_file = file_match.group(1)
            continue

        sym_match = _ADDED_SYMBOL_RE.match(line)
        if sym_match and current_file is not None:
            name = sym_match.group(1)
            # Skip dunder methods — invoked implicitly by runtime/protocols.
            if name.startswith("__") and name.endswith("__"):
                continue
            # Skip private test helpers (``_test_foo``).
            if name.startswith("_test"):
                continue
            if not counts_for_uncalled_scan(current_file):
                continue
            symbols.append((current_file, name))

    # 2. Batched: one ``git grep`` for all symbols, attribute rows per symbol.
    # ``git grep`` accepts multiple ``-e <pat>`` flags in a single invocation,
    # so all symbols are matched in one fork rather than one subprocess per
    # symbol (#1856) — mirroring ``EvolutionEngine._find_uncalled_symbols``.
    workspace = str(config.workspace)
    uncalled: list[tuple[str, str]] = []  # (file_path, symbol_name)

    # Empty-symbol guard: ``git grep`` with no ``-e`` is an error / scans
    # nothing usefully, so skip the fork entirely.
    if not symbols:
        return True, ""

    grep_args = ["git", "grep", "-n", "--fixed-strings"]
    for _file_path, name in symbols:
        grep_args += ["-e", name]
    grep_args += ["--", ".", *UNCALLED_GREP_EXCLUDES]

    try:
        proc = subprocess.run(
            grep_args,
            capture_output=True,
            text=True,
            timeout=config.git_timeout,
            cwd=workspace,
        )
    except (subprocess.TimeoutExpired, OSError):
        # If the batched grep fails, skip *all* symbols rather than blocking —
        # the gate is advisory.  Note the blast radius so the debug log makes
        # clear every symbol was skipped, not just one.
        logger.debug(
            "_check_uncalled_symbol_gate: batched git grep failed for %d "
            "symbol(s); skipping uncalled-symbol gate this round",
            len(symbols),
        )
        return True, ""

    # Pre-split the batched output once into (path, content) rows so each
    # symbol's attribution is a cheap in-memory filter rather than a fork.
    rows: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        rows.append((parts[0], parts[2]))

    for file_path, name in symbols:
        # Filter to rows whose content contains this symbol as a substring —
        # the exact ``--fixed-strings -e name`` semantics a per-symbol grep
        # would apply, so behaviour is byte-for-byte identical (e.g. ``foo``
        # still matches a line containing ``foobar``).  Reconstruct a
        # ``path:lineno:content`` blob (the lineno is irrelevant to the helper)
        # and reuse the existing call-site check; pre-filtering keeps
        # ``_symbol_has_call_site``'s single-symbol contract intact so one
        # symbol's rows cannot count as a call site for another.
        relevant = "\n".join(
            f"{path}:0:{content}" for path, content in rows if name in content
        )
        # A symbol is "called" iff git grep finds a reference on a line
        # that is not a declaration of it.  See ``_symbol_has_call_site``
        # for which declaration rows are discounted.
        if not _symbol_has_call_site(relevant, name, file_path):
            uncalled.append((file_path, name))

    # 3. Apply threshold.
    if len(uncalled) <= config.uncalled_symbol_max_allowed:
        return True, ""

    # Capped: the message is persisted as ``last_failure_reason`` and the
    # uncapped diff (#4217) no longer bounds the symbol count.
    details = "; ".join(
        f"'{name}' in {fp}" for fp, name in uncalled[:_MAX_WARNING_BULLETS]
    )
    if len(uncalled) > _MAX_WARNING_BULLETS:
        details += f"; ... and {len(uncalled) - _MAX_WARNING_BULLETS} more"
    msg = (
        f"Uncalled-symbol gate: {len(uncalled)} new definition(s) lack "
        f"non-test call sites: {details}"
    )
    return False, msg


async def implement_plan(agent: MaestroAgent, plan_id: str) -> None:
    """Implement a single approved or retry-eligible failed plan.

    Writes code, creates a PR, records the journal entry. Failed plans
    with ``failure_count < max_implementation_attempts`` re-enter this
    method via ``_implement_approved_plans`` and are retried with the
    same plan definition on a fresh implementation branch.

    Callable from both the autonomous loop and the dashboard.
    """
    plan = agent.memory.get_plan(plan_id)
    if not plan or plan.status not in ("approved", "failed"):
        return

    # A failed plan whose evolve branch already carries an open or merged
    # PR (e.g. ``gh pr create`` exited non-zero after creating it) is bound
    # to that PR rather than re-implemented into a duplicate (#4553).
    if plan.status == "failed" and agent._bind_existing_pr(plan):
        return

    # Zero-SDK pre-flight refusal; see ``loop/plan_refusal.py`` (#3712).
    if dispose_over_cap_plan(
        plan,
        memory=agent.memory,
        journal=agent.journal,
    ):
        return

    # Plan-scope gate: reject multi-file plans without per-file
    # verification before any code is written.  EvolutionPlan stores
    # changes as plain strings in ``proposed_changes``; wrap them as
    # synthetic PlanStep objects so check_plan_scope can operate on
    # the uniform PlanStep interface.
    synthetic_steps = [
        PlanStep(step_id=f"pc_{i}", description=change)
        for i, change in enumerate(plan.proposed_changes)
    ]
    scope_ok, scope_reason, scope_gaps = check_plan_scope_detailed(
        synthetic_steps, files_affected=plan.files_affected,
    )
    if not scope_ok:
        # On first rejection (failure_count == 0), give the LLM one
        # bounded chance to rewrite ``proposed_changes`` into prose
        # that satisfies the gate. On retries (failure_count >= 1),
        # we already replanned once and refuse to pay again.
        replan_ok = False
        if plan.failure_count == 0:
            # Proposal 118, Step 6: with ``[loop] multi_pr_proposals_enabled``
            # offer a too-large intent to multi-PR chain emission first; a
            # chain supersedes (retires obsolete) the original, never
            # demoting it. Flag off, or a lone-plan collapse, falls through
            # to ``_replan_for_scope`` unchanged.
            #
            # Never re-slice a chain slice (``parent_plan_id`` set): slicing
            # mints a competing ``EvolutionProposal`` and retires the slice
            # obsolete, so it never opens a PR, never settles, and strands
            # the parent in ``rolling_out``. A too-large slice falls through
            # to the in-place rewrite-or-fail path instead.
            scope_call = "slice"
            try:
                if (
                    agent.config.multi_pr_proposals_enabled
                    and not plan.parent_plan_id
                    and await agent._slice_scope_rejected_plan(plan, scope_reason)
                ):
                    return
                scope_call = "replan"
                rewritten = await agent._replan_for_scope(
                    plan, scope_reason, gaps=scope_gaps,
                )
            except SDKError as exc:
                # #4063/#4162, both priced calls: a transient error or an
                # environment fault re-raises with the approval unspent;
                # anything else demotes naming the call, then re-raises —
                # ``_implement_approved_plans`` assumes it demoted (#4039).
                if (
                    exc.category in _TRANSIENT_SDK_CATEGORIES
                    or is_environment_fault(exc)
                ):
                    logger.warning(
                        "Plan %s scope-%s hit %s SDK error "
                        "(exit_code=%s); keeping approval, re-raising",
                        plan_id, scope_call, exc.category.value,
                        exc.exit_code,
                    )
                    raise
                await agent._demote_plan_to_failed(
                    plan, f"Scope {scope_call} SDK call failed: {exc.detail}",
                )
                raise
            if rewritten is not None:
                new_steps = [
                    PlanStep(step_id=f"pc_{i}", description=c)
                    for i, c in enumerate(rewritten)
                ]
                recheck_ok, _ = check_plan_scope(
                    new_steps, files_affected=plan.files_affected,
                )
                if recheck_ok:
                    if not agent.memory.update_plan_proposed_changes(
                        plan.plan_id, rewritten,
                    ):
                        # The plan vanished from disk (concurrently
                        # rejected/deleted) between get_plan and this
                        # write, so the rewrite was never persisted.
                        # Implementing against unpersisted prose would
                        # paper over the divergence; abort loudly instead.
                        logger.warning(
                            "Plan %s vanished from disk before scope-gate "
                            "replan could persist; aborting implementation",
                            plan_id,
                        )
                        return
                    plan.proposed_changes = list(rewritten)
                    synthetic_steps = new_steps
                    logger.info(
                        "Plan %s passed scope gate after replan",
                        plan_id,
                    )
                    replan_ok = True
        if not replan_ok:
            logger.warning(
                "Plan %s rejected by scope gate: %s",
                plan_id, scope_reason,
            )
            journal_plan_refusal(
                agent.journal,
                content=scope_reason,
                triggered_by=f"scope_gate:{plan_id}",
            )
            agent.memory.update_plan_status(
                plan_id, "failed",
                rejection_reason=scope_reason,
                failure_count=plan.failure_count + 1,
                last_failure_at=datetime.now(UTC).isoformat(),
                last_failure_reason=scope_reason,
            )
            return

    logger.info("Implementing plan: %s (status=%s)", plan.title, plan.status)
    prev_task = agent._current_task
    agent._current_task = f"implementing: {plan.title}"
    impl_session: Session | None = None
    try:
        # Phase 1: implement code and create PR.  Failures here are safe to
        # roll back — no remote PR exists yet.
        try:
            # Tracks whether an exception originated in the
            # post-implementation auto-fix stage (vs the implementation
            # itself) so the outer handler can attribute the failure
            # accurately. Bound first, before any statement that can
            # raise, so the outer handler never hits UnboundLocalError.
            failed_in_auto_fix = False
            # Create Session C for implementation if session reuse is enabled
            if agent.config.enable_session_reuse:
                # Long-context implementation: when both flags are set,
                # the per-plan implementation session (Session C in
                # CLAUDE.md's four-session model) flips to the 1M
                # context model with the matching beta and budget.
                # ``self_review`` and ``auto_fix`` then run on the
                # same long-context session via ``query_on_session``,
                # inheriting the model + betas the session was created
                # with — see ``evolution.self_review`` for the diff
                # cap broadening that pairs with this flip.
                impl_long_context = (
                    agent.config.long_context_implementation_enabled
                    and bool(agent.config.sdk_model_long_context)
                )
                if impl_long_context:
                    logger.info(
                        "long-context call: caller=implementation_session "
                        "model=%s",
                        agent.config.sdk_model_long_context,
                    )
                # ``implementation`` is an action-gated phase
                # (``action_gated_phases()``), and both guard-carrying
                # providers derive the gate from ``QueryOptions.hooks`` —
                # so the declaration only bites once the hooks are actually
                # installed here (issue #2914).  Built by the evolution
                # engine rather than inline: it is the sole owner of the
                # branch-scoping decision for the whole implementation
                # lineage, and this session serves the same phase over the
                # same plan as the sessionless arms it already gates.
                impl_session = await create_session(
                    system=agent._build_identity_context(),
                    options=QueryOptions(
                        effort="high",
                        allowed_tools=[
                            "Read", "Write", "Edit", "Bash", "Glob", "Grep",
                        ],
                        mcp_servers=agent.evolution.mcp_servers,
                        disallowed_tools=(
                            agent.config.external_mcp_disallowed_tools()
                        ),
                        caller="implementation_session",
                        cwd=str(agent.config.workspace),
                        model=(
                            agent.config.sdk_model_long_context
                            if impl_long_context else None
                        ),
                        betas=(
                            [LONG_CONTEXT_BETA] if impl_long_context else None
                        ),
                        hooks=agent.evolution.action_gate_hooks(plan),
                    ),
                    phase="implementation",
                )

            base_hash = await agent.evolution.implement(
                plan, session=impl_session,
            )

            # Review on impl_session, or (flag on) its own high-effort one.
            if agent.config.self_review_max_rounds > 0:
                own = agent.config.self_review_own_session_enabled
                review_approved = await agent.evolution.self_review(
                    plan, base_hash, session=None if own else impl_session,
                )
                # Proposal 110: the "proceed without LGTM" escape hatch is
                # retired.  An exhausted-but-unapproved review always
                # terminates through the same loud fail-loud path the
                # convergence owner uses — loud ERROR, branch cleanup,
                # demotion to ``failed`` — so no unapproved head ever
                # reaches the gate loop or ``create_pr``.
                if not review_approved:
                    logger.error(
                        "Self-review did not converge to LGTM (no-progress "
                        "stall or ceiling reached) — demoting plan %s (gate "
                        "convergence: no unapproved head proceeds)",
                        plan_id,
                    )
                    # Proposal 131, step 8: this observation-less terminal routes
                    # through the disposition oracle too. A self-review that never
                    # reached LGTM carries no gate observation, so
                    # ``classify_attempt`` necessarily returns ABANDON (no
                    # progress/recurrence signal, no inherited attribution) —
                    # ``_demote_plan_to_failed`` is reached only in that arm, the
                    # seam that lets a future increment give this terminal a
                    # non-ABANDON disposition in one place.
                    disposition = classify_attempt(
                        None,
                        frozenset(),
                        outcome="stuck",
                        attribution=None,
                        failure_count=plan.failure_count,
                        inherited_hold_count=plan.inherited_hold_count,
                        resume_count=plan.resume_count,
                        max_implementation_attempts=(
                            agent.config.max_implementation_attempts
                        ),
                        max_resume_attempts=agent.config.max_resume_attempts,
                    )
                    # Fail loud on the oracle contract (proposal 131; ledger r3):
                    # a None observation with no attribution and a non-green
                    # outcome can only classify as ABANDON. A future oracle change
                    # that returned a continuation verdict here would otherwise
                    # silently skip the demote and fall through past this terminal.
                    assert disposition.verdict == "ABANDON", disposition.verdict
                    agent.evolution.cleanup_branch(plan_id)
                    failure_reason = (
                        "Self-review did not converge to LGTM "
                        "(no-progress stall or ceiling reached)"
                    )
                    await agent._demote_plan_to_failed(plan, failure_reason)
                    return

            # Post-implementation quality gate / auto-fix loop.
            # Proposal 110: the fix loop routes unconditionally through the
            # convergence owner, which grinds while the failing-gate set
            # strictly shrinks and stops honestly on a stall — instead of a
            # fixed round cap. The former ``auto_fix_max_rounds`` cap is no
            # longer a terminal; it only raises the convergence ceiling's
            # floor.  Wrapped in the same try/except that tags the auto-fix
            # stage, so a non-transient ``auto_fix`` failure inside
            # convergence stays attributed there (fail-loud: attribute,
            # never swallow).
            if agent.config.post_implement_review:
                try:
                    convergence = (
                        await agent.evolution.converge_to_green(
                            plan, session=impl_session,
                            ceiling=max(
                                agent.config.gate_convergence_ceiling,
                                agent.config.auto_fix_max_rounds,
                            ),
                            base_hash=base_hash,
                        )
                    )
                except Exception:
                    failed_in_auto_fix = True
                    raise
                if convergence.outcome == "stuck":
                    # Proposal 131, step 8: every non-green quality-gate terminal
                    # is owned by the pure disposition oracle. Build its inputs
                    # once — the final gate observation, the prior failing
                    # baseline, and the plan's attempt counts/budgets — call
                    # ``classify_attempt`` (pure, zero-I/O), then dispatch on the
                    # single returned verdict:
                    #   HOLD_INHERITED — the red is the *base*'s, not this diff's;
                    #     hold the plan ``approved`` until the base moves (proposal
                    #     124). Honoured *regardless of the flag* — the oracle's
                    #     rule fires iff ``attribution == "INHERITED"`` and the hold
                    #     budget is unspent, reproducing the bespoke branch exactly.
                    #   RESUME / ASK_HUMAN — continuation verdicts honoured *only
                    #     when* ``[loop] attempt_disposition_enabled``; flag-off they
                    #     collapse to today's demote.
                    #   otherwise (ABANDON, and the flag-off RESUME / ASK_HUMAN) —
                    #     spend the plan. An ``INHERITED`` attribution reaching here
                    #     is necessarily hold-budget-spent (HOLD_INHERITED returned
                    #     above while it was unspent), so it keeps today's "held N
                    #     time(s)" wording; every other red keeps the generic
                    #     rendered-identity (flag-off raw-output) wording.
                    # Flag-off the oracle call is pure and every observable effect
                    # is byte-identical to today's scattered terminals.
                    observation = convergence.review.observation
                    prior_failing = frozenset(plan.resume_failing_identities)
                    current_failing = (
                        observation.progress_signature()
                        if observation is not None
                        else frozenset()
                    )
                    disposition = classify_attempt(
                        observation,
                        prior_failing,
                        outcome=convergence.outcome,
                        attribution=convergence.attribution,
                        failure_count=plan.failure_count,
                        inherited_hold_count=plan.inherited_hold_count,
                        resume_count=plan.resume_count,
                        max_implementation_attempts=(
                            agent.config.max_implementation_attempts
                        ),
                        max_resume_attempts=agent.config.max_resume_attempts,
                    )
                    if (
                        agent.config.gate_environment_oracle_enabled
                        and disposition.verdict == "HOLD_ENVIRONMENT"
                    ):
                        # The environment hold (proposal 133, step 5): the
                        # runnability oracle named this red its own, so the
                        # attempt bought no evidence about the diff. Tear the
                        # branch down (``implement`` recreates it on
                        # re-admission), stamp the verdict's fingerprint so the
                        # hold is keyed to the *fact* exactly as the pre-spend
                        # hold is, and stop: no ``failure_count`` bump, no
                        # ``inherited_hold_count``, no demote, no issue. The
                        # stamp self-extinguishes; nothing filters on it, so the
                        # next SOUND verdict releases the plan
                        # (``plan_holds.release_environment_hold``).
                        #
                        # The verdict is *carried* on the result, never re-read:
                        # a second read of the cached sidecar could answer
                        # ``None``, which ``update_plan_environment_hold``
                        # defines as *clearing* the stamp. ``None`` is therefore
                        # unreachable here (the attribution is derived from the
                        # verdict); ``stamp_terminal_environment_hold`` owns
                        # that guard and the ledger row.
                        agent.evolution.cleanup_branch(plan_id)
                        verdict = convergence.runnability
                        plan_holds.stamp_terminal_environment_hold(
                            agent, plan_id, verdict,
                        )
                        logger.warning(
                            "Plan %s HELD (not demoted) — the local quality "
                            "gate cannot reach a verdict (%s), so the red is "
                            "the environment's, not this diff's: %s",
                            plan_id,
                            verdict.kind if verdict else "unmeasured",
                            render_identities(
                                observation,
                                limit=(
                                    _INHERITED_HOLD_DIRECTIVE_IDENTITY_LIMIT
                                ),
                                max_chars=(
                                    _INHERITED_HOLD_DIRECTIVE_IDENTITY_CHARS
                                ),
                            ) if observation is not None else "<no identities>",
                        )
                        try:
                            model = agent.consciousness.load_self_model()
                            agent.journal.record_event(
                                content=(
                                    f"Held plan {plan.title}: the local "
                                    f"quality gate could not reach a verdict "
                                    f"mid-attempt, so the red is the "
                                    f"environment's rather than this diff's — "
                                    f"no attempt charged."
                                ),
                                entry_type="evolution_note",
                                triggered_by=f"environment_hold:{plan_id}",
                            )
                            if not model.next_task_directive and verdict:
                                model.next_task_directive = (
                                    plan_holds.environment_repair_directive(
                                        verdict,
                                    )
                                )
                                agent.consciousness.save_self_model(model)
                        except (OSError, ValueError):
                            # Narrow rather than broad: ``implement_plan``'s
                            # exempted-handler count is pinned shrink-only
                            # (``tests/test_no_silenced_exceptions.py``), and
                            # the only failures this note has to survive are
                            # the state-file ones — an unwritable journal or a
                            # corrupt self-model row. Anything else is a bug
                            # that must crash loudly; the hold's stamp is
                            # already persisted above, so it survives either
                            # way.
                            logger.warning(
                                "Failed to write environment-hold "
                                "journal entry",
                                exc_info=True,
                            )
                        return
                    if disposition.verdict == "HOLD_INHERITED":
                        # The base-inherited hold (proposal 124, step 9): clean up
                        # the branch (``implement`` recreates it from the repaired
                        # base on re-admission), stamp the ``origin/main`` head the
                        # base red was measured against so
                        # ``_implement_approved_plans`` declines to re-buy the same
                        # red until the base moves, and bridge a base-repair
                        # directive into an empty slot. Honoured regardless of the
                        # flag — the oracle returns this verdict only while the hold
                        # budget is unspent, exactly as the bespoke branch did.
                        agent.evolution.cleanup_branch(plan_id)
                        held_main_sha = agent._vcs.rev_parse("origin/main")
                        new_hold_count = plan.inherited_hold_count + 1
                        agent.memory.update_plan_inherited_hold(
                            plan_id,
                            inherited_hold_main_sha=held_main_sha,
                            inherited_hold_count=new_hold_count,
                        )
                        directive_identities = (
                            render_identities(
                                observation,
                                limit=(
                                    _INHERITED_HOLD_DIRECTIVE_IDENTITY_LIMIT
                                ),
                                max_chars=(
                                    _INHERITED_HOLD_DIRECTIVE_IDENTITY_CHARS
                                ),
                            )
                            if observation is not None else ""
                        )
                        logger.warning(
                            "Plan %s HELD (not demoted) — quality gate red "
                            "is inherited from base %s, not this diff: %s "
                            "(hold %d/%d)",
                            plan_id,
                            (held_main_sha or "unmeasured")[:8],
                            directive_identities or "<no identities>",
                            new_hold_count,
                            agent.config.max_implementation_attempts - 1,
                        )
                        # The self-model bookkeeping — note and base-repair
                        # directive — and the ``maestro:gate`` gap issue
                        # (opened or reused via
                        # ``self_health.open_inherited_gate``) belong to
                        # ``plan_holds``, which owns this hold; each of its
                        # writes is separately guarded there (#3814).
                        plan_holds.record_inherited_hold(
                            agent, plan, directive_identities, new_hold_count,
                            held_main_sha,
                        )
                        return
                    new_baseline = sorted(current_failing)
                    if (
                        agent.config.attempt_disposition_enabled
                        and disposition.verdict == "RESUME"
                    ):
                        # Keep the branch but release HEAD (#4370); stamp the
                        # carry-context so the next lap resumes from this head.
                        # ``status``/``failure_count`` stay frozen; the resume
                        # budget bounds the total continuations.
                        branch_head = agent._vcs.rev_parse(
                            f"refs/heads/{evolution_branch(plan_id)}",
                        )
                        agent.memory.update_plan_resume(
                            plan_id,
                            resume_count=plan.resume_count + 1,
                            resume_failing_identities=new_baseline,
                            resume_branch_head=branch_head,
                            last_failure_reason=disposition.reason,
                        )
                        logger.warning(
                            "Plan %s RESUMING (branch preserved) — %s: %s",
                            plan_id, disposition.reason,
                            ", ".join(new_baseline) or "<no identities>",
                        )
                        agent.evolution.release_branch(plan_id)
                        return
                    if (
                        agent.config.attempt_disposition_enabled
                        and disposition.verdict == "ASK_HUMAN"
                    ):
                        # A recurring environment/infrastructure red no code
                        # change can clear (a collection abort, a runner timeout,
                        # an unrunnable gate). Rather than burn the plan's retry
                        # budget on cold restarts of a wall the diff never caused,
                        # ask Thomas one specific question and move the plan to the
                        # inert ``blocked`` status keyed to that question's message
                        # id — no demote, no spent attempt, no GitHub issue.
                        # ``carry_context`` is ``False`` for this verdict, so the
                        # branch is disposed like today's demote. The current
                        # baseline already equals ``prior_failing`` here (it is what
                        # made the recurrence match), so no re-bank is needed.
                        message_id = await agent.ask_thomas(
                            disposition.question or "",
                        )
                        if message_id:
                            agent.evolution.cleanup_branch(plan_id)
                            agent.memory.update_plan_status(
                                plan_id, "blocked",
                                blocked_question_id=message_id,
                                last_failure_reason=disposition.reason,
                            )
                            logger.warning(
                                "Plan %s BLOCKED (asked Thomas, message %s) "
                                "— %s: %s",
                                plan_id, message_id, disposition.reason,
                                ", ".join(new_baseline)
                                or "<no identities>",
                            )
                            try:
                                agent.journal.record_event(
                                    content=(
                                        f"Blocked plan {plan.title}: a "
                                        f"recurring environment red no code "
                                        f"change can clear — asked Thomas and "
                                        f"paused pending a human decision "
                                        f"({', '.join(new_baseline) or 'no identities'})"
                                    ),
                                    entry_type="evolution_note",
                                    triggered_by=f"ask_human:{plan_id}",
                                )
                            except Exception:
                                logger.warning(
                                    "Failed to write ASK_HUMAN journal entry",
                                    exc_info=True,
                                )
                            return
                        # No reachable message channel (no message store / blank
                        # question) — fall through to today's demote so a plan is
                        # never stranded ``blocked`` with no question channel to
                        # unblock it.
                        logger.warning(
                            "Plan %s ASK_HUMAN verdict but no reachable "
                            "message channel — demoting instead",
                            plan_id,
                        )
                    # Every other verdict (ABANDON, and the flag-off RESUME /
                    # ASK_HUMAN that collapse to demote): bank the current failing
                    # baseline for the next admission (flag-on only, as today),
                    # then demote — ``_demote_plan_to_failed`` is reached only on an
                    # ABANDON-class verdict.
                    if agent.config.attempt_disposition_enabled:
                        agent.memory.update_plan_resume(
                            plan_id,
                            resume_failing_identities=new_baseline,
                        )
                    agent.evolution.cleanup_branch(plan_id)
                    if convergence.attribution == "INHERITED":
                        # Hold budget spent → demote, naming the inherited base
                        # failures that held the plan rather than a truncated
                        # raw-output tail.
                        demote_identities = (
                            render_identities(
                                observation,
                                limit=_GATE_RECORD_IDENTITY_LIMIT,
                                max_chars=_GATE_RECORD_IDENTITY_CHARS,
                            )
                            if observation is not None else ""
                        )
                        logger.error(
                            "Gate convergence stalled on inherited base "
                            "failures after %d hold(s) — demoting plan %s: %s",
                            plan.inherited_hold_count, plan_id,
                            demote_identities or "<no identities>",
                        )
                        failure_reason = (
                            "Quality gate failed on inherited base failures "
                            f"(held {plan.inherited_hold_count} time(s)):\n"
                            f"{demote_identities}"
                        )
                    else:
                        # Proposal 124: record the sorted failure identities rather
                        # than a mid-word ``raw_output`` byte slice when a gate
                        # observation is present. This reason is rendered inline
                        # into the dashboard's one-line plan-meta row, so
                        # ``render_identities`` (a single comma-joined line) is used
                        # here. Flag-off (no observation) it falls back to today's
                        # ``raw_output[-500:]`` tail, keeping the persisted-record
                        # and Telegram shapes stable.
                        demote_identities = (
                            render_identities(
                                observation,
                                limit=_GATE_RECORD_IDENTITY_LIMIT,
                                max_chars=_GATE_RECORD_IDENTITY_CHARS,
                            )
                            if observation is not None
                            else convergence.review.raw_output[-500:]
                        )
                        logger.error(
                            "Gate convergence stalled with no progress "
                            "after %d round(s) — demoting plan %s",
                            convergence.rounds_used, plan_id,
                        )
                        failure_reason = (
                            f"Quality gate failed:\n{demote_identities}"
                        )
                    await agent._demote_plan_to_failed(
                        plan, failure_reason,
                        gate_output=convergence.review.raw_output,
                    )
                    return
                # "green" → fall through to create_pr.

            # Cumulative diff (base_hash → HEAD), fetched once after
            # self-review and convergence so every auto_fix commit has
            # landed. Uncapped: both checks parse it locally (it never
            # enters a prompt), and a head+tail splice would misattribute
            # tail defs to the head's last file and hide the dropped
            # middle from the check (#4217).
            current_head = agent.evolution._vcs.get_head_hash() or ""
            gate_diff = agent.evolution._vcs.get_diff(
                base_hash, current_head, max_chars=0,
            )
            # Uncalled-symbol blocking gate.
            if agent.config.uncalled_symbol_gate_enabled and gate_diff:
                gate_ok, gate_msg = _check_uncalled_symbol_gate(
                    gate_diff, agent.config,
                )
                if not gate_ok:
                    logger.warning(
                        "Plan %s blocked by uncalled-symbol gate: %s",
                        plan_id, gate_msg,
                    )
                    # Proposal 131, step 8: route this observation-less
                    # terminal through the oracle. The uncalled-symbol gate
                    # carries no gate observation, so ``classify_attempt``
                    # necessarily returns ABANDON — the demote is reached only
                    # in that arm.
                    disposition = classify_attempt(
                        None,
                        frozenset(),
                        outcome="stuck",
                        attribution=None,
                        failure_count=plan.failure_count,
                        inherited_hold_count=plan.inherited_hold_count,
                        resume_count=plan.resume_count,
                        max_implementation_attempts=(
                            agent.config.max_implementation_attempts
                        ),
                        max_resume_attempts=agent.config.max_resume_attempts,
                    )
                    # Fail loud on the oracle contract (proposal 131; ledger
                    # r3): this observation-less terminal can only ABANDON;
                    # assert it so a future non-ABANDON verdict cannot silently
                    # skip the demote and fall through.
                    assert disposition.verdict == "ABANDON", disposition.verdict
                    agent.evolution.cleanup_branch(plan_id)
                    await agent._demote_plan_to_failed(
                        plan, gate_msg,
                    )
                    return

            # Capture evolution commit hash before create_pr switches branches
            evolution_head = agent.contributions.get_current_head()
            # Re-check status after implementation — we must still be
            # on the status we started with. A concurrent reviewer
            # action (rejection/approval) should win over the PR.
            refreshed = agent.memory.get_plan(plan_id)
            if not refreshed or refreshed.status != plan.status:
                logger.warning(
                    "Plan %s status changed during implementation",
                    plan_id,
                )
                agent.evolution.cleanup_branch(plan_id)
                return
            pr_url = await agent.evolution.create_pr(plan)
        except Exception as exc:
            # Transient SDK failures (stream stall, rate limit, CLI
            # crash, stale session) are retried in place by the
            # iteration-level handler in ``run_loop``. Demoting the
            # plan here would consume the human approval and strand
            # the plan — the retry's ``_implement_approved_plans``
            # selector would then skip it. Clean up the branch and
            # re-raise so the outer handler sees the original
            # category and can retry the phase.
            transient = (
                isinstance(exc, SDKError)
                and exc.category in _TRANSIENT_SDK_CATEGORIES
            )
            if transient:
                assert isinstance(exc, SDKError)  # for type narrowing
                logger.warning(
                    "Transient SDK failure during implement_plan(%s) "
                    "(category=%s) — leaving plan %s for "
                    "iteration-level retry",
                    plan_id, exc.category.value, plan.status,
                )
                agent.evolution.cleanup_branch(plan_id)
                raise
            # A permanent environment fault (OOM kill, missing binary) is
            # the host's doing, not the plan's: re-raise without spending
            # a plan attempt (#3488, ledger r1). Cleanup reads *exc*: a
            # dirty-tree refusal (#3711) cut no branch, discards nothing.
            if is_environment_fault(exc):
                agent.evolution.cleanup_branch(plan_id, exc)
                raise
            agent.evolution.cleanup_branch(plan_id)
            error_detail = exc.detail if isinstance(exc, SDKError) else str(exc)
            # Proposal 131, step 8 (ledger r3): both non-transient demotes
            # consult the oracle first — the re-raises above are not terminals.
            # An unwrapped/transport error surfaces here with no gate
            # observation and no attribution, so ``classify_attempt`` can only
            # return ABANDON; the assert below pins that contract rather than
            # gating each demote on it, so a future non-ABANDON verdict fails
            # loud instead of letting the ``raise`` strand the plan un-demoted.
            disposition = classify_attempt(
                None,
                frozenset(),
                outcome="stuck",
                attribution=None,
                failure_count=plan.failure_count,
                inherited_hold_count=plan.inherited_hold_count,
                resume_count=plan.resume_count,
                max_implementation_attempts=(
                    agent.config.max_implementation_attempts
                ),
                max_resume_attempts=agent.config.max_resume_attempts,
            )
            assert disposition.verdict == "ABANDON", disposition.verdict
            if failed_in_auto_fix:
                # The implementation landed; only the post-hoc auto-fix
                # stage failed. Attribute it accurately (distinct from an
                # implementation failure) and carry the gate output, so
                # reflection reasons about the right failure stage.
                logger.exception(
                    "Post-implementation auto-fix failed for plan %s",
                    plan_id,
                )
                failure_reason = (
                    f"Post-implementation auto-fix failed: {error_detail}"
                )
                # Proposal 110: the convergence path carries its own gate
                # output at its own demote site; a raise *inside*
                # ``converge_to_green`` surfaces here with no observed gate
                # result, so this handler demotes with no gate output.
                await agent._demote_plan_to_failed(
                    plan, failure_reason,
                    gate_output=None,
                )
            else:
                logger.exception(
                    "Failed to implement plan %s", plan_id,
                )
                failure_reason = f"Implementation failed: {error_detail}"
                await agent._demote_plan_to_failed(plan, failure_reason)
            raise

        # Post-PR bookkeeping — the plan is already "implemented" with
        # pr_url recorded in memory.
        if evolution_head:
            # Record every commit on the evolution branch (implementation
            # plus any self-review fix commits), not just HEAD — otherwise
            # check_for_contributions() flags the intermediate commits as
            # external work on the next iteration.
            intermediates = (
                agent.contributions.get_new_commits(base_hash, evolution_head)
                if base_hash else []
            )
            if intermediates:
                for commit in intermediates:
                    agent.contributions.record_own_commit(commit.hash)
                logger.debug(
                    "Recorded %d intermediate evolution commits",
                    len(intermediates),
                )
            else:
                agent.contributions.record_own_commit(evolution_head)
        # evolution_count is bumped when the PR actually merges (see the
        # merged-plans loop in run_loop), not here at PR-creation time —
        # a PR later closed without merging must not count as an evolution.
        try:
            agent.journal.record_event(
                content=f"Evolution implemented: {plan.title} -> {pr_url}",
                entry_type="milestone",
                triggered_by=f"evolution:{plan_id}",
            )
        except Exception:
            logger.warning("Failed to write implementation journal entry", exc_info=True)
        logger.info(
            "Evolution implemented: %s -> %s", plan.title, pr_url,
        )
    finally:
        if impl_session is not None:
            with contextlib.suppress(Exception):
                await disconnect_session(impl_session)
        agent._current_task = prev_task


async def _implement_approved_plans(
    agent: MaestroAgent, *, iteration: int | None = None,
) -> None:
    """Implement approved plans and retry eligible failed plans.

    A plan is eligible if its status is ``approved`` or if it is
    ``failed`` with ``failure_count < max_implementation_attempts``.
    Approved plans are implemented before failed plans so fresh
    work does not queue behind an old retry; among failed plans,
    the one with the lowest failure count runs first.

    When ``[loop] max_open_evolution_prs > 0`` and the last
    ``tend_evolution_prs`` sweep found at least that many open PRs,
    this method returns early without implementing anything. It writes
    no ``next_task_directive``: the integrate-existing-branches intent is
    carried at task-generation time by the promotable-backlog and
    integration-cadence bridges, and the head-of-chain ``wip_primacy``
    gate is the sole author of the finish-this-PR directive.

    *iteration* keys the proposal-133 runnability probe (see
    :mod:`maestro.loop.plan_holds`); ``None`` leaves every caller unaffected.
    """
    # Multi-PR rollout sequencing (proposal 118): promote the next eligible
    # slice of any in-flight EvolutionProposal (and complete a fully-settled
    # chain) before the WIP/PR-cap early returns. Promotion is zero-SDK and
    # opens no PR of its own; one-slice-open already prevents pile-up because
    # an unsettled predecessor keeps the next slice ineligible. No-op when
    # ``multi_pr_proposals_enabled`` is off.
    await agent._advance_slice_rollouts()
    # WIP-primacy stand-down (proposal 092): while owned WIP with a
    # self-clearable blocker is open, minting a *new* evolution PR only piles
    # more unfinished work onto the leak. Decline before the concurrency cap
    # so the deferral holds even below ``max_open_evolution_prs``. Author no
    # ``next_task_directive`` — the head-of-chain ``wip_primacy`` gate is the
    # sole author of the finish-this-PR directive; this path only declines to
    # open new work. Inert while the flag is off (``_wip_owns_slot``
    # short-circuits) and under ``Discretionary`` focus, so the count-based
    # gate below is unchanged for both parity paths.
    if agent._wip_owns_slot():
        logger.info(
            "WIP primacy: owned work is pending (%r) — deferring "
            "evolution-PR implementation this iteration",
            agent._current_task_focus(),
        )
        return
    # Pre-mint generation gate (proposal 120, Phase B). Turning an approved
    # plan into a fresh evolution PR opens net-new plan work, so route it
    # through the same generation authority ``evolve()`` consults, on the
    # same live-backlog inputs — no new threshold. This channel implements an
    # already-approved plan and has no single candidate to theme-classify, so
    # it uses the candidate-independent backlog-saturation arm only: a
    # human-approved plan is never theme-refused. On a refusal, skip implementation and write the
    # bounded prune/land directive into an empty ``next_task_directive`` slot
    # (first-writer-wins), so the next self-directed pass disposes an
    # existing plan instead of piling on. Runs *after*
    # ``_advance_slice_rollouts`` above, so in-flight multi-PR slice chains
    # keep advancing even under a saturated backlog. Inert while the flag is
    # off (verdict is ``None``); the count-based cap below is unchanged.
    verdict = agent._pre_mint_generation_verdict()
    if verdict is not None and not verdict.admit:
        logger.info(
            "Pre-mint generation gate refused a net-new evolution PR (%s) — "
            "skipping approved-plan implementation this iteration",
            verdict.reason,
        )
        model = agent.consciousness.load_self_model()
        if not model.next_task_directive:
            model.next_task_directive = GENERATION_STANDDOWN_DIRECTIVE
            agent.consciousness.save_self_model(model)
        return
    pr_cap = agent.config.max_open_evolution_prs
    if pr_cap > 0 and agent._open_evolution_pr_count >= pr_cap:
        logger.info(
            "Open evolution PR cap reached (%d >= %d) — "
            "skipping implementation to reduce maintenance surface",
            agent._open_evolution_pr_count,
            pr_cap,
        )
        return
    max_attempts = agent.config.max_implementation_attempts
    eligible = [
        p for p in agent.memory.get_all_plans()
        if p.status == "approved"
        or (p.status == "failed" and p.failure_count < max_attempts)
    ]
    # Pre-spend runnability consult (proposal 133), owned by
    # ``maestro.loop.plan_holds``: no probe is paid for an iteration with
    # nothing to implement, and none at all while the flag is off.
    environment = plan_holds.gate_environment_verdict(
        agent, iteration, eligible,
    )
    if environment is not None and environment.kind != "SOUND":
        await plan_holds.hold_plans_on_environment(
            agent, environment, eligible,
        )
        return
    if environment is not None:
        plan_holds.release_environment_hold_state(agent, eligible)
    eligible.sort(key=lambda p: (p.status != "approved", p.failure_count))
    # Implementation-time slice ordering (proposal 118): drop any slice that
    # is not the next eligible slice for its proposal *before* applying the
    # per-iteration cap, so a deferred out-of-order slice cannot occupy a cap
    # slot and starve the eligible slice — which, under an adverse
    # ``get_all_plans`` order with ``cap == 1``, it otherwise could. Non-slice
    # plans (no/dangling ``parent_plan_id``) and the flag-off path are never
    # filtered, so ordinary evolution PRs are unaffected.
    # Proposal 124, step 9: resolve the current ``origin/main`` head once —
    # no fetch, reading the remote-tracking ref as ``tend_evolution_prs``
    # last reconciled it — so ``plan_holds.plan_on_inherited_hold`` can skip
    # a plan still held against the base red it was stamped at.
    current_main = agent._vcs.rev_parse("origin/main")
    ready: list[EvolutionPlan] = []
    for plan in eligible:
        if agent._slice_defers_implementation(plan):
            logger.info(
                "Slice %r is not the next eligible slice for proposal %r — "
                "deferring implementation this iteration",
                plan.plan_id, plan.parent_plan_id,
            )
            continue
        if plan_holds.plan_on_inherited_hold(plan, current_main):
            logger.info(
                "Plan %r is on an inherited-base hold against origin/main "
                "%s — deferring implementation until the base moves",
                plan.plan_id, (current_main or "")[:8],
            )
            continue
        ready.append(plan)
    cap = agent.config.max_implementations_per_iteration
    for plan in ready[:cap]:
        if agent._ensure_shutdown_event().is_set():
            break
        plan_holds.release_environment_hold(agent, plan)
        if plan.status == "approved":
            try:
                await agent.notifier.resolve_plan(plan.plan_id, "Approved")
            except Exception:
                logger.warning("Failed to resolve Telegram buttons for plan %s", plan.plan_id, exc_info=True)
        # Defence-in-depth: ensure HEAD is on main before each
        # implement_plan call so a future cap > 1 cannot inherit a sibling
        # plan's branch as the base ref. With cap = 1 this is a no-op in
        # the steady state. Pairs with Evolution.implement()'s own
        # unconditional ensure_main_branch() at entry.
        agent.evolution.ensure_main_branch()
        # Transient SDK errors must propagate so ``run_loop``'s
        # iteration-level resume-in-place handler retries the phase.
        # Permanent environment faults (``is_environment_fault``) also
        # propagate: ``implement_plan`` re-raised them un-demoted, so the
        # loop must crash loudly rather than re-attempt the plan forever
        # (#4039). Every other failure has already been demoted +
        # notified inside ``implement_plan``; swallow it here so one
        # plan's logic error cannot crash the loop or block subsequent
        # eligible plans in the same iteration.
        try:
            await agent.implement_plan(plan.plan_id)
        except SDKError as exc:
            if exc.category in _TRANSIENT_SDK_CATEGORIES or is_environment_fault(exc):
                raise
            logger.exception(
                "Non-transient SDKError implementing plan %s — "
                "already demoted, continuing with remaining plans",
                plan.plan_id,
            )
        except Exception as exc:
            if is_environment_fault(exc):
                raise
            logger.exception(
                "Non-transient error implementing plan %s — "
                "already demoted, continuing with remaining plans",
                plan.plan_id,
            )


async def _slice_scope_rejected_plan(
    agent: MaestroAgent,
    plan: EvolutionPlan,
    scope_reason: str,
) -> bool:
    """Route a scope-rejected, too-large plan to multi-PR chain emission.

    Proposal 118, Step 6. Called from ``implement_plan`` when the plan-scope
    gate rejects a plan and ``[loop] multi_pr_proposals_enabled`` is on,
    *before* the ``_replan_for_scope`` rewrite-or-fail fallback and under the
    same once-per-lifetime (``failure_count == 0``) guard. Synthesises a
    intent from the rejected plan plus the scope gate's uncovered-file
    ``scope_reason`` and offers it to ``propose_plan_chain``:

    * On an ``(EvolutionProposal, slices)`` tuple — a genuinely-too-large
      intent that decomposes into >=2 ordered slices — submit the chain
      (head slice for review, downstream slices dormant) and, *only when the
      head is accepted and the chain persisted* (``submit_proposal_chain``
      returns ``True``), retire the original plan as ``obsolete``
      (superseded / self-pruned, never demoted to ``failed`` nor fed to
      rejection learning), journal the supersession, and return ``True``.
    * On a lone ``EvolutionPlan`` — the intent still fits one PR (fewer than
      two justified slices) — return ``False`` and leave the original plan
      untouched, so the caller runs the unchanged rewrite-or-fail path.
    * On a chain whose head is refused at the submission door (a saturation
      block / dedup / shipped-CLI guard fires, so nothing is persisted) —
      return ``False`` and leave the original plan untouched, so the caller
      still falls back to ``_replan_for_scope`` rather than dropping the
      plan for a chain that was never minted.

    Contains no bare ``query()``: the single priced LLM call lives inside
    ``propose_plan_chain`` (session-less, mirroring ``_replan_for_scope``'s
    posture). A ``ProposalParseError`` from an unparseable chain reply is
    caught and returns ``False`` — the caller then falls back to
    ``_replan_for_scope`` exactly as if the flag were off.

    Slicing mints net-new slice plans, so — like every self-initiated
    priced-propose channel — this clears the shared generation authority
    (``_pre_mint_generation_verdict``, proposal 120) *before* spending the
    priced ``propose_plan_chain`` call. On a non-admit verdict it returns
    ``False``, falling back to ``_replan_for_scope``'s in-place rewrite,
    which mints no net-new plans and so stays outside the authority's
    domain — the saturation-safe disposal for an already-approved plan.
    """
    # Pre-mint generation gate (proposal 120, Phase B). Consult the single
    # generation-authority owner before the priced chain-emission call, on
    # the candidate-independent (backlog-saturation-only) arm: this
    # transforms an already-approved plan rather than producing a fresh
    # self-measurement reflection, mirroring ``_implement_approved_plans``
    # and the doc-proposal channel. Returns ``None`` (byte-identical) when
    # ``[loop] pre_mint_generation_gate_enabled`` is off, so the default
    # path is unchanged; zero SDK cost.
    verdict = agent._pre_mint_generation_verdict()
    if verdict is not None and not verdict.admit:
        logger.info(
            "Pre-mint generation gate refused slicing scope-rejected plan "
            "%s into a chain (%s) — falling back to in-place replan",
            plan.plan_id, verdict.reason,
        )
        return False
    intent = (
        "A plan was rejected by the plan-scope gate for spanning too many "
        "files to land in one reviewable PR. It cannot be shrunk in place. "
        "Decompose its intent into an ordered chain of thin slices, each a "
        "standalone reviewable PR that builds on its predecessor.\n\n"
        f"Title: {plan.title}\n"
        f"Rationale: {plan.rationale}\n"
        "Files affected:\n"
        + "\n".join(f"- {p}" for p in plan.files_affected)
        + "\nProposed changes:\n"
        + "\n".join(
            f"{i + 1}. {c}" for i, c in enumerate(plan.proposed_changes)
        )
        + "\nAcceptance criteria:\n"
        + "\n".join(f"- {c}" for c in plan.acceptance_criteria)
        + f"\n\nScope-gate rejection: {scope_reason}"
    )
    # A plan pinned to an open door-shaped gap issue (proposal 155) keeps
    # the pin on its slices; an ``issue_number`` a planner reply chose itself
    # names no gap and is not propagated.
    gap_pinned = (
        plan.issue_number is not None
        and agent.evolution.is_open_gap_issue(plan.issue_number)
    )
    try:
        result = await agent.evolution.propose_plan_chain(
            intent, gap_pinned=gap_pinned,
        )
    except ProposalParseError as exc:
        logger.warning(
            "Plan %s scope-rejection chain emission returned an "
            "unparseable reply — falling back to replan: %s",
            plan.plan_id, exc,
        )
        return False
    if not isinstance(result, tuple):
        # Chain emission judged the intent still fits one PR (fewer than
        # two justified slices); leave the plan untouched for the caller's
        # rewrite-or-fail fallback.
        return False

    proposal, slices = result
    exempt_saturation = False
    if gap_pinned and plan.issue_number is not None:
        # A gap plan's slices stay pinned to its issue (proposal 155): the
        # chain keeps citing the gap (so it is not re-selected while live),
        # and only the final slice's PR closes it.
        # A red ``maestro:gate`` gap keeps the saturation exemption its
        # original mint carried (``fuel.bypasses_gates``) across re-slicing.
        exempt_saturation = agent.evolution.gap_issue_bypasses_gates(
            plan.issue_number,
        )
        for slice_plan in slices:
            slice_plan.issue_number = plan.issue_number
            slice_plan.category = plan.category
            slice_plan.problem_ref = None
    submitted = await agent.evolution.submit_proposal_chain(
        proposal, slices,
        exempt_saturation=exempt_saturation,
    )
    if not submitted:
        # The head slice was refused at the submission door (saturation
        # block, dedup, shipped-CLI), so submit_proposal_chain persisted
        # nothing — there is no chain to supersede the original. Leave the
        # plan untouched and fall back to the caller's rewrite-or-fail path
        # rather than dropping it with a false "superseded by proposal"
        # reason and skipping ``_replan_for_scope`` entirely.
        logger.info(
            "Scope-rejection chain for plan %s was refused at the "
            "submission door (no headless chain persisted); falling back "
            "to replan",
            plan.plan_id,
        )
        return False
    disposed = agent.memory.mark_plan_obsolete(
        plan.plan_id,
        (
            f"{SUPERSEDED_BY_PROPOSAL_PREFIX}{proposal.proposal_id} "
            f"({len(slices)} slices): too large for one PR, sliced not "
            "demoted"
        ),
        allow_approved=True,
    )
    if not disposed:
        # The chain is already submitted; the original could not be retired
        # (its status changed or it vanished between read and dispose).
        # Surface loudly rather than silently leaving it approved.
        logger.warning(
            "Scope-sliced plan %s could not be retired as obsolete "
            "(status changed or vanished); chain proposal %s already "
            "submitted",
            plan.plan_id, proposal.proposal_id,
        )
    agent.journal.record_event(
        content=(
            f"Sliced scope-rejected plan '{plan.title}' into multi-PR "
            f"proposal {proposal.proposal_id} ({len(slices)} slices); "
            "retired the original as obsolete rather than demoting to "
            "failed."
        ),
        entry_type="evolution_note",
        triggered_by=f"scope_slice:{plan.plan_id}",
    )
    return True


async def _replan_for_scope(
    agent: MaestroAgent,
    plan: EvolutionPlan,
    rejection_reason: str,
    session: Session | None = None,
    gaps: list[tuple[str, str]] | None = None,
) -> list[str] | None:
    """One bounded LLM rewrite of ``plan.proposed_changes`` to satisfy
    the scope gate.

    Called at most once per plan lifetime (when ``failure_count == 0``)
    from ``implement_plan`` after ``check_plan_scope`` rejects.
    Returns the rewritten list when the LLM produces a well-formed
    response with the same length as ``plan.proposed_changes``;
    returns ``None`` on malformed JSON, a non-dict or non-list-of-str
    payload, or a length mismatch. An ``SDKError`` propagates so the
    caller can classify it (transient / environment fault keep the
    approval; anything else demotes) rather than conflating it with a
    malformed reply (#4063). The caller is responsible for re-running
    ``check_plan_scope`` on the result before persisting it.

    ``gaps`` is the per-file gap list (``(path, step_text)`` pairs)
    the scope gate already computed; the caller passes it so this
    method need not re-walk the steps.  When omitted (e.g. a direct
    call), the gap list is self-computed from ``plan.proposed_changes``.
    """
    # Build the concrete gap list so the LLM knows exactly which
    # files need verification language added.  Reuse the caller's gap
    # list when supplied; otherwise self-compute it.
    if gaps is None:
        synthetic_steps = [
            PlanStep(step_id=f"pc_{i}", description=c)
            for i, c in enumerate(plan.proposed_changes)
        ]
        gaps = _find_unverified_code_files(
            synthetic_steps, files_affected=plan.files_affected,
        )
    if gaps:
        gap_block = (
            f"\nThe following {len(gaps)} file(s) in your plan lack "
            f"per-file verification language:\n"
            + "\n".join(
                f'- {path}: "{step_text}"' for path, step_text in gaps
            )
            + "\n\nFor each file above, add explicit verification "
            "language (e.g. tested by ..., verified by running ..., "
            "asserts ...).\n"
        )
    else:
        gap_block = ""

    prompt = SCOPE_REPLAN_PROMPT.format(
        rejection_reason=rejection_reason,
        files_affected="\n".join(
            f"- {p}" for p in plan.files_affected
        ) or "(none)",
        proposed_changes="\n".join(
            f"{i + 1}. {c}" for i, c in enumerate(plan.proposed_changes)
        ),
        n_steps=len(plan.proposed_changes),
        gap_enumeration=gap_block,
        max_chars=_PLAN_CHANGE_ITEM_MAX_CHARS,
    )
    if session is not None:
        result = await query_on_session(
            session, prompt, caller="replan_for_scope",
            complete_on=json_complete,
        )
    else:
        result = await query(
            prompt=prompt,
            system=agent._build_identity_context(),
            options=QueryOptions(
                effort="low", caller="replan_for_scope", tools=[],
            ),
            complete_on=json_complete, phase="implementation",
        )

    try:
        payload = result.parse_json()
    except json.JSONDecodeError:
        logger.debug(
            "replan_for_scope: response did not parse as JSON: %r",
            result.response[:200],
        )
        return None

    if not isinstance(payload, dict):
        return None
    rewritten = payload.get("proposed_changes")
    if not isinstance(rewritten, list) or not all(
        isinstance(c, str) for c in rewritten
    ):
        return None
    if len(rewritten) != len(plan.proposed_changes):
        logger.debug(
            "replan_for_scope: length mismatch (got %d, expected %d)",
            len(rewritten), len(plan.proposed_changes),
        )
        return None
    # Defensively clamp each entry to the same per-entry cap the sibling
    # write path (``Memory.refine_plan``) enforces, so an over-long LLM
    # rewrite cannot grow the implementation prompt past the bound. A
    # length-only overrun is truncated, not rejected, to avoid discarding
    # an otherwise well-formed replan and forcing a paid retry.
    clamped = [c[:_PLAN_CHANGE_ITEM_MAX_CHARS] for c in rewritten]
    if any(len(c) > _PLAN_CHANGE_ITEM_MAX_CHARS for c in rewritten):
        logger.debug(
            "replan_for_scope: truncated %d entr(ies) to %d chars",
            sum(
                1 for c in rewritten
                if len(c) > _PLAN_CHANGE_ITEM_MAX_CHARS
            ),
            _PLAN_CHANGE_ITEM_MAX_CHARS,
        )
    return clamped


def _bind_existing_pr(agent: MaestroAgent, plan: EvolutionPlan) -> bool:
    """Bind a failed plan to a PR already live on its branch.

    Returns ``True`` when the caller must not re-implement: either a PR
    was bound (plan ``implemented`` with its ``pr_url``), or the host
    lookup failed and the plan is left ``failed`` for the next iteration
    without spending an attempt. Merged-plan bookkeeping stays with
    ``check_merged_plans`` via the merge-truth chokepoint.
    """
    try:
        pr = agent.evolution.find_existing_pr(plan)
    except VCSError:
        logger.warning(
            "PR lookup for failed plan %s failed; deferring retry",
            plan.plan_id, exc_info=True,
        )
        return True
    if pr is None:
        return False
    agent.memory.update_plan_status(
        plan.plan_id, "implemented", pr_url=pr.url, rejection_reason=None,
    )
    logger.info(
        "Bound failed plan %s to existing PR %s (%s)",
        plan.plan_id, pr.url, pr.state,
    )
    agent.journal.record_event(
        f"Bound failed plan {plan.plan_id} to existing PR "
        f"{pr.url} ({pr.state})",
        entry_type="milestone",
        triggered_by=f"bind_existing_pr:{plan.plan_id}",
    )
    return True


async def _assess_criteria_outcome(
    agent: MaestroAgent,
    step_based_outcome: TaskOutcome,
    acceptance_criteria: list[str],
    goal: str,
    steps: list[PlanStep],
    *,
    session: Session | None = None,
) -> tuple[TaskOutcome, list[dict[str, str]] | None]:
    """Override *step_based_outcome* using per-criterion MET/UNMET assessment.

    When *acceptance_criteria* is non-empty, the LLM assesses each
    criterion against the step results.  A deterministic gate then
    maps the verdicts to an outcome:

    * all MET → ``success``
    * some MET → ``partial``
    * none MET → ``failure``

    Returns ``(outcome, verdicts)`` where *verdicts* is a list of
    ``{"criterion": ..., "verdict": "MET"|"UNMET", "evidence": ...}``
    dicts persisted on the ``TaskRecord`` so downstream consumers can
    trace the classification basis.  *verdicts* is ``None`` on every
    early-exit / fallback path (no criteria, parse undercount, LLM
    failure) so a fallback never persists a partial or misleading list.

    If the LLM call fails, falls back to *step_based_outcome* so the
    task record is never lost.
    """
    if not acceptance_criteria:
        return step_based_outcome, None

    try:
        step_results = ""
        for s in steps:
            result_text = (s.result or "")[:500]
            step_results += f"- [{s.status}] {s.description}: {result_text}\n"
        step_results = step_results[:3000]

        criteria_text = "\n".join(
            f"{i}. {c}" for i, c in enumerate(acceptance_criteria, 1)
        )
        prompt = CRITERIA_ASSESSMENT_PROMPT.format(
            goal=cap_utf8(goal, MAX_TASK_GOAL_BYTES),
            criteria=criteria_text,
            step_results=step_results or "(no step results)",
        )

        _note_session_effort_override(
            "assess_criteria_outcome", "low", session,
        )
        # Scale the early-completion threshold by criterion count so a
        # multi-criterion response with concrete per-line evidence isn't
        # truncated before the last verdict is emitted.  A realistic
        # 5-criterion reply easily exceeds 400 chars, well past a flat
        # 300-char cap; the parser counts ``MET`` verdicts and would
        # otherwise undercount, downgrading ``success`` to ``partial``.
        char_budget = max(600, 200 * len(acceptance_criteria))
        if session is not None:
            result = await query_on_session(
                session, prompt, caller="assess_criteria_outcome",
                complete_on=chars_complete(char_budget),
            )
        else:
            result = await query(
                prompt=prompt,
                options=QueryOptions(
                    effort="low", caller="assess_criteria_outcome",
                ),
                complete_on=chars_complete(char_budget),
            )

        response_text = str(result.response)
        # ``IGNORECASE`` returns the matched casing verbatim, so
        # canonicalise to upper-case before the ``== "MET"`` tally below.
        verdicts = [
            v.upper()
            for v in agent._CRITERION_VERDICT_RE.findall(response_text)
        ]
        total = len(acceptance_criteria)

        # Parse-completeness assertion: if the regex did not recover
        # one verdict per criterion the LLM's formatting drifted far
        # enough that the count is untrustworthy.  Silent undercounts
        # downgrade ``success`` → ``partial`` (or ``failure``), so
        # we conservatively fall back to the step-based outcome and
        # log a warning the operator can investigate.
        if len(verdicts) != total:
            logger.warning(
                "Criteria verdict parse incomplete: matched %d of %d; "
                "falling back to step-based outcome %s",
                len(verdicts), total, step_based_outcome,
            )
            return step_based_outcome, None

        met = sum(1 for v in verdicts if v == "MET")

        # Parsing is complete and trustworthy (one verdict per
        # criterion), so build the structured verdict list to persist
        # on the task record.  Pair each criterion positionally with
        # the verdict and evidence of the same evidence-regex match, whose
        # single-line tail keeps it aligned with the verdict tally.  Each
        # field is char-capped so a runaway LLM line cannot bloat it.
        cap = agent._CRITERION_VERDICT_FIELD_MAX_CHARS
        evidence_matches = agent._CRITERION_VERDICT_EVIDENCE_RE.findall(
            response_text,
        )
        criteria_verdicts: list[dict[str, str]] = []
        for i, criterion in enumerate(acceptance_criteria):
            verdict, evidence = (
                (evidence_matches[i][0].upper(), evidence_matches[i][1].strip())
                if i < len(evidence_matches)
                else (verdicts[i], "")
            )
            criteria_verdicts.append({
                "criterion": criterion[:cap],
                "verdict": verdict,
                "evidence": evidence[:cap],
            })

        if met == total:
            return "success", criteria_verdicts
        if met == 0:
            return "failure", criteria_verdicts
        return "partial", criteria_verdicts
    except SDKError:
        raise
    except Exception:
        logger.warning(
            "Failed to assess criteria outcome; falling back to "
            "step-based outcome %s",
            step_based_outcome,
            exc_info=True,
        )
        return step_based_outcome, None


def _snapshot_version(
    agent: MaestroAgent,
    plan_id: str,
    snapshot: diagnostics.DiagnosticSnapshot | None = None,
    recent_tasks: list[TaskRecord] | None = None,
) -> None:
    """Record a :class:`VersionRecord` for a just-merged evolution plan.

    Best-effort: any failure is logged and swallowed so the merge
    detection path still completes.  The snapshot is a derived
    observation — if we lose it for one version, we lose telemetry,
    not correctness.

    ``snapshot`` and ``recent_tasks`` are optional batch-stable
    inputs: when the caller is processing a batch of merges in one
    synchronous loop body, both values are unchanged across
    iterations and re-running them per plan is wasted work. When
    omitted (the default), each is computed locally so callers that
    snapshot a single plan keep the original behavior.
    """
    if not agent.config.version_archive_enabled:
        return
    try:
        plan = agent.memory.get_plan(plan_id)
        if plan is None:
            return

        if recent_tasks is None:
            window = agent.config.version_archive_window_iterations
            recent_tasks = agent.memory.get_recent_tasks(window)
        succeeded = sum(1 for t in recent_tasks if t.outcome == "success")
        failed = sum(1 for t in recent_tasks if t.outcome == "failure")

        if snapshot is None:
            snapshot = diagnostics.build_diagnostic_snapshot(
                agent.config.app_log_path,
                n_entries=500,
                memory_dir=agent.config.memory_dir,
            )
        sdk = snapshot.sdk_usage
        if sdk is None:
            sdk_cost_usd = 0.0
            sdk_calls = 0
            cache_hit_rate = 0.0
            rate_limit_hits = 0
        else:
            sdk_cost_usd = sdk.total_cost_usd
            sdk_calls = sdk.total_calls
            cache_hit_rate = sdk.cache_hit_rate
            rate_limit_hits = sdk.rate_limit_hits

        commit_sha = ""
        ci_conclusion = ""
        if plan.pr_url:
            try:
                pr_number = agent.evolution._pr_number_from_url(plan.pr_url)
                commit_sha = agent.evolution.code_host.get_commit_sha_of_pr(pr_number)
                ci_conclusion = agent.evolution.code_host.get_pr_check_conclusion(pr_number)
            except Exception:
                logger.warning(
                    "Could not resolve commit sha or CI conclusion for plan "
                    "%s (pr_url=%r)",
                    plan_id, plan.pr_url, exc_info=True,
                )

        now = datetime.now(UTC)
        merged_at = now.isoformat()
        # Include microseconds so two plans merged in the same wall-clock
        # second (a batch of PRs detected together) still get distinct ids.
        # strftime("%f") always emits 6 digits, avoiding the zero-microsecond
        # edge case that isoformat() drops (#1372).
        version_id = now.strftime("v%Y%m%dT%H%M%S%f")

        record = VersionRecord(
            version_id=version_id,
            plan_id=plan_id,
            plan_title=plan.title,
            commit_sha=commit_sha,
            merged_at=merged_at,
            window_tasks_total=len(recent_tasks),
            window_tasks_succeeded=succeeded,
            window_tasks_failed=failed,
            window_sdk_cost_usd=sdk_cost_usd,
            window_sdk_calls=sdk_calls,
            window_cache_hit_rate=cache_hit_rate,
            window_rate_limit_hits=rate_limit_hits,
            window_error_count=snapshot.recent_error_count,
            quality_gate_pass=(ci_conclusion == "success"),
        )
        agent.memory.record_version(record)

        agent.journal.record_event(
            content=(
                f"Version snapshot {version_id} for {plan.title}: "
                f"ok={succeeded}/{len(recent_tasks)} "
                f"cost=${sdk_cost_usd:.2f} "
                f"errors={snapshot.recent_error_count}"
            ),
            entry_type="milestone",
            triggered_by="version_snapshot",
        )
    except Exception:
        logger.warning(
            "Failed to snapshot version for plan %s", plan_id, exc_info=True,
        )


async def _demote_plan_to_failed(
    agent: MaestroAgent,
    plan: EvolutionPlan,
    failure_reason: str,
    *,
    gate_output: str | None = None,
) -> None:
    """Mark *plan* as ``failed`` (not ``rejected``), increment the
    failure counter, notify Telegram on every attempt, and open a
    GitHub issue only on the terminal attempt.

    ``failed`` is distinct from ``rejected``: a buggy implementation
    is not a signal that the idea itself was wrong, so it does NOT
    trigger rejection-learning or the 7-day cooldown that
    ``submit_for_review`` applies to rejected plans.
    """
    # Once the persisted record reaches ``implemented`` with a real
    # ``pr_url``, the PR exists on GitHub and demotion is *always*
    # wrong — it would re-qualify the plan for the next iteration's
    # ``_implement_approved_plans`` selector and create a duplicate
    # PR. Read the live persisted state (the ``plan`` argument may
    # be stale: ``create_pr`` mutates the row mid-flight) and
    # short-circuit before any failure-counter increment or
    # demotion-notification side-effect leaks.
    refreshed = agent.memory.get_plan(plan.plan_id)
    if (
        refreshed is not None
        and refreshed.status == "implemented"
        and refreshed.pr_url
    ):
        logger.warning(
            "Skipping demotion of plan %s: already persisted as "
            "implemented with pr_url=%s — post-PR work raised %r but "
            "the PR exists on GitHub and must not be re-created",
            plan.plan_id, refreshed.pr_url, failure_reason,
        )
        return

    new_count = plan.failure_count + 1
    max_attempts = agent.config.max_implementation_attempts
    terminal = new_count >= max_attempts

    agent.memory.update_plan_status(
        plan.plan_id, "failed",
        rejection_reason=failure_reason,
        failure_count=new_count,
        last_failure_at=datetime.now(UTC).isoformat(),
        last_failure_reason=failure_reason,
    )

    terminal_suffix = (
        " — no further retries will be attempted" if terminal else ""
    )
    await agent._notify_best_effort(
        f"Implementation failed (attempt {new_count}/{max_attempts}): "
        f"{plan.title}{terminal_suffix}\n\n{failure_reason}",
        failure_log="Failed to send implementation failure notification",
    )

    if terminal:
        try:
            if gate_output is not None:
                title = f"[Maestro] Quality gate failed: {plan.title}"
                body = (
                    f"Plan `{plan.plan_id}` failed post-implementation "
                    f"quality gates after {new_count} attempts.\n\n"
                    f"**Review output:**\n```\n"
                    f"{gate_output[-1000:]}\n```\n\n"
                    f"Auto-fix was attempted but did not resolve all issues."
                )
            else:
                title = f"[Maestro] Implementation failed: {plan.title}"
                body = (
                    f"Plan `{plan.plan_id}` failed during implementation "
                    f"after {new_count} attempts.\n\n"
                    f"**Error:** {failure_reason}\n\n"
                    f"**Rationale:** {plan.rationale}\n\n"
                    f"The plan has been marked `failed` and will not be "
                    f"retried."
                )
            agent.evolution.create_issue(title, body, labels=["bug"])
        except Exception:
            logger.warning(
                "Failed to create issue for terminal implementation failure",
                exc_info=True,
            )
    else:
        logger.warning(
            "Plan %s demoted to failed (attempt %d/%d) — will be retried",
            plan.plan_id, new_count, max_attempts,
        )


def _slice_pr_settled(agent: MaestroAgent, slice_plan: EvolutionPlan) -> bool:
    """Return whether *slice_plan*'s PR has settled (merged or closed).

    The single settlement closure both the rollout driver and the
    implementation-time ordering gate pass into :func:`next_eligible_slice`
    (proposal 118). A slice with no ``pr_url`` has not opened a PR, so it
    cannot have settled — returns ``False``. Otherwise the PR number is read
    through the one merge-truth door (``pr_url`` →
    :meth:`~maestro.evolution.EvolutionEngine._pr_number_from_url` →
    :meth:`is_pr_settled`); an unparseable ``pr_url`` is treated as
    unsettled rather than crashing the driver.
    """
    if not slice_plan.pr_url:
        return False
    try:
        pr_num = agent.evolution._pr_number_from_url(slice_plan.pr_url)
    except ValueError:
        return False
    return agent.is_pr_settled(pr_num).settled


async def _advance_slice_rollouts(agent: MaestroAgent) -> None:
    """Drive one-slice-open sequencing across in-flight multi-PR proposals.

    The processing arm of proposal 118. For each ``rolling_out``
    :class:`~maestro.memory.EvolutionProposal`, ask the single oracle
    :func:`next_eligible_slice` which slice may be worked next given each
    slice's PR settlement. When that slice is still dormant (``"sliced"``),
    promote it for review; when no slice is eligible yet every slice has
    settled, advance the proposal to ``complete``; when the eligible slice
    ended without a PR (:func:`~maestro.memory.slice_ended_without_pr`),
    move it to ``abandoned`` and retire its dormant slices. Every action is a
    zero-SDK status flip that opens no PR of its own and preserves per-slice
    human approval — a sequencing wrapper, never a merge or approval bypass.

    Fully behind ``[loop] multi_pr_proposals_enabled``: when off this
    early-returns before any proposal read, so the loop is byte-identical to
    today.
    """
    if not agent.config.multi_pr_proposals_enabled:
        return
    for proposal in agent.memory.get_evolution_proposals():
        if proposal.status != "rolling_out":
            continue
        slices = agent.memory.get_slices_for_proposal(proposal.proposal_id)
        eligible = next_eligible_slice(
            proposal, slices, is_settled=agent._slice_pr_settled,
        )
        if eligible is not None:
            if slice_ended_without_pr(
                eligible, agent.config.max_implementation_attempts,
            ):
                pid = proposal.proposal_id
                why = (
                    f"proposal {pid} abandoned: slice {eligible.plan_id} "
                    f"ended {eligible.status} without a PR"
                )
                for s in slices:
                    if s.status == "sliced":
                        agent.memory.mark_plan_obsolete(
                            s.plan_id, reason=why[:_OBSOLETE_REASON_MAX_CHARS],
                            allow_sliced=True,
                        )
                # Flip only after the successors retire, so a crash
                # mid-cascade leaves it rolling_out and re-fires.
                agent.memory.update_evolution_proposal_status(pid, "abandoned")
                agent._record_milestone(why, "slice_rollout_abandoned")
            elif eligible.status == "sliced":
                await agent.evolution.promote_slice(eligible)
        elif slices and all(
            agent._slice_pr_settled(s) for s in slices
        ):
            # Oracle yielded no eligible slice and every slice has settled —
            # the whole chain has landed, so the proposal is complete.
            agent.memory.update_evolution_proposal_status(
                proposal.proposal_id, "complete",
            )


def _slice_defers_implementation(agent: MaestroAgent, plan: EvolutionPlan) -> bool:
    """Return whether *plan* is a slice that must defer implementation now.

    Implementation-time slice ordering (proposal 118): a plan that is a
    valid slice of a real :class:`~maestro.memory.EvolutionProposal` may be
    implemented only when it is the slice :func:`next_eligible_slice` names
    for that proposal — so an out-of-order slice cannot land ahead of an
    unsettled predecessor, making the oracle the authority at implement time
    too. Returns ``True`` (defer) only for such an out-of-order slice.

    Non-slice plans (no ``parent_plan_id`` or one naming no real proposal)
    and the flag-off path always return ``False``, so ordinary evolution PRs
    are never gated. Pure and zero-SDK — evaluated as a *pre-cap* filter so a
    deferred slice never consumes a ``max_implementations_per_iteration``
    slot and starves the eligible slice under an adverse plan order.
    """
    if not (agent.config.multi_pr_proposals_enabled and plan.parent_plan_id):
        return False
    parent_proposal = next(
        (
            p for p in agent.memory.get_evolution_proposals()
            if p.proposal_id == plan.parent_plan_id
        ),
        None,
    )
    if parent_proposal is None:
        return False
    eligible_slice = next_eligible_slice(
        parent_proposal,
        agent.memory.get_slices_for_proposal(parent_proposal.proposal_id),
        is_settled=agent._slice_pr_settled,
    )
    return eligible_slice is None or eligible_slice.plan_id != plan.plan_id
