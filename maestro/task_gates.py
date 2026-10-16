"""Declarative registry for ``generate_task``'s pre-call gate chain (069).

``generate_task`` historically inlined every blocking gate and directive
bridge as a hand-copied block: guard on ``if not
model.next_task_directive``, call a ``check_*`` predicate, set the
directive, ``save_self_model``, record a journal event, and
``log_gate_activation``. Precedence lived only in source order. This
module makes precedence data: :data:`GATE_CHAIN` is the ordered registry,
:func:`run_gate_chain` is the single runner that applies it, and
:func:`gate_order` exposes the precedence for docs and the
``maestro list-gates`` CLI.

The chain covers the *pre-call* gates only — blocking gates short-circuit
the SDK call entirely; directive bridges fill the shared
``next_task_directive`` slot (first writer wins, a pre-existing directive
wins over all of them). The *post-call content* gates are owned by
:func:`~maestro.loop.task_generation._vet_goal_content` (applied at both the
``enqueue`` and ``edit`` queue-mutation sites); :func:`content_gate_order` is
their single ordered projection — read it, not a hand-copied list, for the
current membership.

Predicates are imported from their defining modules, and tests patch them on
this module (``maestro.task_gates.<name>``); ``check_task_cost_risk`` is read
through the :mod:`maestro.task_cost_gate` module attribute.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from maestro import (
    audit_checks,
    deed_ledger,
    directive_ledger,
    escalation_bridge,
    lever_capture,
    pr_facts,
    task_cost_gate,
)
from maestro.audit_checks import (
    INSTRUMENT_CONSUMPTION_THRESHOLD,
    SELF_DIRECTED_WINDOW,
    GateActivationBackend,
    check_integration_cadence,
    check_investigation_streak,
    check_partial_construction_gap,
    check_promotable_backlog_directive,
    check_self_directed_success_rate,
    check_task_goal_overlap,
    classify_task_shape,
    compute_shape_investigation_rate,
    extract_issue_numbers,
    find_unconsumed_instruments,
    log_gate_activation,
    truncate_directive,
)
from maestro.frame_discharge import (
    is_investigation_frame,
    measure_frame_discharge_costumes,
)
from maestro.loop.owed_act_binding import (
    failing_checks_for,
    owed_act_for_readiness,
)
from maestro.memory import Memory
from maestro.remediation_tracker import (
    MERGE_BLOCK_CONFLICTING,
    OWNED_BLOCKERS,
    check_forced_read_directive,
    check_readmission_construction_directive,
    format_handoff_directive_detail,
    is_owned_obsolete_row,
    measure_no_progress_laps,
)
from maestro.standdown_validity import StanddownClaim
from maestro.wip_primacy import TendOwnedPR

if TYPE_CHECKING:
    from maestro.agent import MaestroAgent
    from maestro.config import Config
    from maestro.consciousness import Consciousness, SelfModel
    from maestro.memory import TaskRecord
    from maestro.pr_facts import PRReadiness
    from maestro.remediation_tracker import StandDownVerdictBackend
    from maestro.task_queue import QueuedTask

logger = logging.getLogger("maestro")


GateKind = Literal["block", "directive"]


@dataclass(frozen=True)
class GateOutcome:
    """One gate firing — everything the runner needs to apply it.

    ``detail`` feeds ``log_gate_activation``; ``log_message`` is the
    operator-facing INFO line. ``directive`` carries the text a
    directive-kind gate wants in ``next_task_directive``. The optional
    journal pair is recorded as a journal event (``Journal.record_event``),
    its trigger defaulting to the gate's name.
    """

    detail: str
    log_message: str
    directive: str | None = None
    journal_content: str | None = None
    journal_trigger: str | None = None


@dataclass
class GateContext:
    """Shared state handed to every gate predicate.

    ``recent()`` / ``recent_dicts()`` lazily load and cache the 10-task
    window so the chain's I/O profile matches the historical inline code:
    the quiet-skip gate fired before ``get_recent_tasks`` was ever read,
    so a quiet-skip block must not pay for the read. The window size (10)
    is load-bearing — it also bounds the post-call follow-up
    ``source_task_id`` validation in ``generate_task``.
    """

    agent: MaestroAgent
    config: Config
    model: SelfModel
    _recent: list[TaskRecord] | None = field(default=None, repr=False)
    _recent_dicts: list[dict[str, Any]] | None = field(
        default=None, repr=False,
    )

    def recent(self) -> list[TaskRecord]:
        """Return the cached 10-task history window, loading it once."""
        if self._recent is None:
            self._recent = self.agent.memory.get_recent_tasks(10)
        return self._recent

    def recent_dicts(self) -> list[dict[str, Any]]:
        """Return :meth:`recent` as dicts (cached), for dict-based predicates."""
        if self._recent_dicts is None:
            self._recent_dicts = [asdict(t) for t in self.recent()]
        return self._recent_dicts


@dataclass(frozen=True)
class TaskGate:
    """One registry entry: a named, toggleable, ordered gate.

    ``rewrites`` opts a ``directive``-kind gate out of the empty-slot guard
    in :func:`run_gate_chain`: a normal directive bridge only *fills* an
    empty ``next_task_directive`` (a pre-set directive wins), but a rewrite
    gate runs on a *non-empty* directive too so it can divert an already-
    authored owned-act directive away from an already-disposed artifact.
    Defaults to ``False`` — the historical fill-empty-only behaviour.
    """

    name: str
    kind: GateKind
    enabled: Callable[[Config], bool]
    run: Callable[[GateContext], GateOutcome | None]
    rewrites: bool = False


def _always_enabled(config: Config) -> bool:
    """Enable predicate for gates with no config toggle."""
    return True


def _apply_gate_directive(
    ctx: GateContext,
    directive: str | None,
    *,
    expected: str | None,
) -> bool:
    """Write *directive* into the live self-model slot; return whether it landed.

    Runs inside :meth:`Consciousness.mutate_self_model` so only the
    gate-owned delta is applied to the re-read live model. With *expected*
    ``None`` the write fills an empty slot only; otherwise (a ``rewrites=True``
    gate) it replaces the slot only while it still holds *expected*.
    ``ctx.model`` is rebound to the persisted (cap-normalized) copy either
    way, so later guards and the directive consumer see what is on disk.
    """

    def _write(live: SelfModel) -> bool:
        current = live.next_task_directive
        allowed = current == expected if expected is not None else not current
        if allowed:
            live.next_task_directive = directive
        return allowed

    wrote, persisted = ctx.agent.consciousness._mutate_self_model_persisted(
        _write,
    )
    ctx.model = persisted
    return wrote


def run_gate_chain(
    gates: Sequence[TaskGate],
    ctx: GateContext,
) -> bool:
    """Evaluate *gates* in list order; return ``True`` when one blocked.

    - A gate whose ``enabled(ctx.config)`` is false is skipped.
    - A directive gate is skipped — its ``run`` never called — while
      ``ctx.model.next_task_directive`` is non-empty, reproducing the
      historical ``if not model.next_task_directive:`` guards (a pre-set
      directive is never overwritten, and lower-precedence predicates pay
      no evaluation cost). A ``rewrites=True`` directive gate is exempt: it
      runs on a non-empty directive too, so it can divert an already-
      authored owned-act directive off an already-disposed artifact.
    - The first ``block`` outcome short-circuits the chain. A block outcome
      whose ``directive`` is non-``None`` also sets
      ``ctx.model.next_task_directive`` (and persists it) before the
      ``blocked`` record — the WIP-primacy gate hands the loop a bounded
      instruction to finish the owned PR while still stopping generation —
      but only into an *empty* slot: a pre-set contemplation/fire-or-close
      directive is preserved, honouring the same "a pre-set directive is
      never overwritten" contract the directive gates obey (the block still
      short-circuits either way). Backward compatible: every other block
      gate returns ``directive=None``.
    - Every directive write is one :meth:`Consciousness.mutate_self_model`
      transaction that re-checks the *live* slot (#4302): a fill lands only
      into a live-empty slot, and a ``rewrites=True`` write only while the
      live directive still equals the one the gate evaluated. ``ctx.model``
      is then rebound to the persisted copy, so no concurrently committed field
      is reverted and later guards read the live slot.
    - Exactly one ``log_gate_activation`` record is emitted per firing,
      ``action="blocked"`` or ``action="directive_set"``; a directive gate
      whose write did not land emits nothing and the chain continues.
    """
    for gate in gates:
        if not gate.enabled(ctx.config):
            continue
        if (
            gate.kind == "directive"
            and ctx.model.next_task_directive
            and not gate.rewrites
        ):
            continue
        evaluated = ctx.model.next_task_directive
        outcome = gate.run(ctx)
        if outcome is None:
            continue
        if gate.kind == "block":
            # A block gate may carry a bounded directive (the WIP-primacy gate
            # names the owned PR to finish), but never at the cost of an
            # already-pending one: guard on the empty slot exactly as the
            # directive-gate branch does, so a pre-set contemplation /
            # fire-or-close directive survives the block rather than being
            # discarded. The block short-circuits the chain regardless.
            if outcome.directive is not None:
                _apply_gate_directive(ctx, outcome.directive, expected=None)
            logger.info("%s", outcome.log_message)
            if outcome.journal_content is not None:
                ctx.agent.journal.record_event(
                    content=outcome.journal_content,
                    entry_type="reflection",
                    triggered_by=outcome.journal_trigger or gate.name,
                )
            log_gate_activation(
                ctx.agent.gate_activations,
                gate=gate.name,
                action="blocked",
                detail=outcome.detail,
            )
            return True
        if not _apply_gate_directive(
            ctx,
            outcome.directive,
            expected=(evaluated or None) if gate.rewrites else None,
        ):
            continue
        if outcome.journal_content is not None:
            ctx.agent.journal.record_event(
                content=outcome.journal_content,
                entry_type="reflection",
                triggered_by=outcome.journal_trigger or gate.name,
            )
        logger.info("%s", outcome.log_message)
        log_gate_activation(
            ctx.agent.gate_activations,
            gate=gate.name,
            action="directive_set",
            detail=outcome.detail,
        )
    return False


# ---------------------------------------------------------------------------
# Blocking gates (short-circuit the SDK call)
# ---------------------------------------------------------------------------


# Owned merge-block codes naming code-clearable *git* work — resolve the
# conflict, re-run the gates — as opposed to the residual "I never handed it
# off" disposition. ``MERGE_BLOCK_CONFLICTING`` (and the raw ``DIRTY`` host
# state it derives from) keep a PR off the merge-ready path. ``THREADS_UNRESOLVED``
# is *not* listed here yet is still never merge-ready: it names an open review
# thread I must answer, and :func:`_wip_pr_merge_ready`'s ``unresolved_threads
# == 0`` requirement routes it to 'Finish PR #N' — answering the thread is owned
# code work, not a merge-or-close handoff. The subset of
# :data:`~maestro.remediation_tracker.OWNED_BLOCKERS` that is *git* work.
_WIP_CODE_GATE_BLOCKERS = frozenset({MERGE_BLOCK_CONFLICTING, "DIRTY"})


def _wip_primacy_directive_text(pr_num: int, blocker: str) -> str:
    """Render the finish-owned-work directive for *pr_num*, cap-bounded.

    A bounded one-shot ``next_task_directive`` naming the open own PR whose
    *blocker* (``CONFLICTING`` / ``THREADS_UNRESOLVED``) is mine to clear: merge
    ``origin/main`` and resolve the conflict, answer the review thread, or close
    the PR — finish started work before starting anything new. The literal
    ``"Finish PR #"`` prefix is the signature the WIP-primacy chokepoint test
    keys on; keep it if you edit the wording. The merge-ready sibling variant is
    :func:`_wip_primacy_handoff_directive_text`.
    """
    directive = (
        f"Finish PR #{pr_num} yourself — clear the {blocker}: merge "
        f"origin/main and resolve conflicts, answer the review thread, or "
        f"close it. Don't start new work while it is open."
    )
    return truncate_directive(directive)


def _wip_primacy_handoff_directive_text(pr_num: int) -> str:
    """Render the merge-ready *handoff* directive for *pr_num*, cap-bounded.

    The variant :func:`_wip_pr_merge_ready` selects: a bounded one-shot
    ``next_task_directive`` for an owned PR measured green, 0-behind, 0-conflict
    with no blocker left that is mine to clear. Rather than the blockable
    ``Finish PR #`` lever — which I have converted into yet another CI-deferred
    "awaiting review/merge" lap across ~11 firings — it names the one act I
    actually control from contemplation: send Thomas a single merge-or-close
    message about the PR. Carries the ``"Hand off PR #"`` marker (never
    ``"Finish PR #"``) so the WIP chokepoint guards tell the two variants apart.
    """
    directive = (
        f"Hand off PR #{pr_num} to Thomas — it is merge-ready, no blocker is "
        f"mine to clear. Send one message: merge it or close it. Don't start "
        f"new work while it is open."
    )
    return truncate_directive(directive)


def _wip_primacy_code_red_directive_text(
    pr_num: int, checks: Sequence[str] = (),
) -> str:
    """Render the code-clearable-*red* first-act directive for *pr_num*, cap-bounded.

    The variant :func:`_wip_primacy_run` selects when the owned PR's sole
    remaining blocker is a code-clearable CI *red* — a failing quality gate /
    brevity over-cap whose fix is a local edit, not a merge-conflict resolution
    (``CONFLICTING``) or a review-thread reply (``THREADS_UNRESOLVED``). The
    bounded ``Finish PR #N`` verb names no act, so my act phase opens with a
    fork-read (the documented pre-mutation-read tell), produces a "nothing to
    push" artifact, and closes on an "awaiting review/merge" verdict over a red
    I actually own (this is exactly what happened to #3264, whose sole red was
    ``test_claude_md_word_count_under_cap``). This directive instead names the
    unfakeable first mutating act — Edit the failing file/gate to clear the red,
    then push — and forbids the survey-first move outright. It carries neither
    the ``"Finish PR #"`` nor the ``"Hand off PR #"`` marker, so the WIP
    chokepoint guards still tell all three variants apart.

    *checks* (proposal 137, step 11) are the names of the checks actually
    failing, resolved by
    :func:`~maestro.loop.owed_act_binding.failing_checks_for` off the CI-truth
    owner. When non-empty they are appended as a bounded ``(failing: <names>)``
    clause, so the act phase opens knowing *which* gate to edit rather than
    going looking; an empty list — an unreadable or name-less rollup — yields
    today's text verbatim. The whole directive still flows through
    :func:`~maestro.audit_checks.truncate_directive`, so the clause can never
    push it past the shared cap.
    """
    directive = (
        f"PR #{pr_num}: first act = Edit the failing file/gate to clear the "
        f"red, then push. Do NOT fork-read or survey first — the red is "
        f"code-clearable and mine to fix."
    )
    if checks:
        directive = f"{directive} (failing: {', '.join(checks)})"
    return truncate_directive(directive)


def _wip_is_code_clearable_red(
    readiness: PRReadiness | None, owned_blocker: str,
) -> bool:
    """Is the owned WIP PR's sole blocker a code-clearable CI *red*?

    ``True`` iff a readiness observation is present and shows the PR open
    (``not branch_landed``), CI concluded ``FAILING``, the branch 0-behind and
    0-conflict, and **no** unresolved review thread / unaddressed overflow
    finding remains — the failing quality gate is the *only* thing between the
    branch and green. The ``owned_blocker`` that fired the focus must not be one
    of the structural owned blockers
    (:data:`~maestro.remediation_tracker.OWNED_BLOCKERS` —
    ``CONFLICTING`` / ``DIRTY`` / ``THREADS_UNRESOLVED``): a git conflict or an
    un-answered review thread is code-clearable work, but not the *red* whose
    first act is an Edit-then-push (it is a merge-conflict resolution or a thread
    reply), so those keep the ``Finish PR #N`` text. This partition is what keeps
    a ``THREADS_UNRESOLVED`` focus whose CI is also failing on the finish path.

    A missing observation, a landed PR, a CI-not-``FAILING`` (green / pending)
    read, a behind/conflicting branch, or an open thread / overflow is **not** a
    bare code-clearable red — so the caller keeps the existing text. Pure; zero
    SDK cost.
    """
    if readiness is None or readiness.branch_landed:
        return False
    if owned_blocker.strip().upper() in OWNED_BLOCKERS:
        return False
    return (
        readiness.ci_state == "FAILING"
        and not readiness.branch_behind
        and not readiness.branch_conflicting
        and readiness.unresolved_threads == 0
        and readiness.unaddressed_overflow == 0
    )


def _wip_pr_merge_ready(
    readiness: PRReadiness | None, owned_blocker: str,
) -> bool:
    """Is the owned WIP PR *measured* merge-ready — hand off, not more code work?

    Answers proposal-092's follow-on question from
    ``classify_blocker_ownership`` (via :data:`_WIP_CODE_GATE_BLOCKERS`, the
    owned/git subset) *plus* the git-measured
    :class:`~maestro.pr_facts.PRReadiness` fact the focus decision already
    observed: ``True`` iff a readiness observation is present and shows the PR
    open (``not branch_landed``), the branch 0-behind and 0-conflict, the CI
    axis cleared (:func:`~maestro.pr_facts.ci_state_clears_ci_axis` — ``GREEN``,
    or ``NONE`` for a PR with no CI configured), **no unresolved review thread and no unaddressed
    overflow finding remain** (both are owned code work — answer the thread /
    address the finding — not a merge-or-close), **and** the ``owned_blocker``
    that fired the focus is not itself a code-clearable git conflict. In that
    state no blocker is mine to clear — the sole outstanding disposition is a
    human merge-or-close — so the caller hands off to Thomas instead of handing
    me the blockable finish-it lever.

    A missing observation (memo miss), a landed PR, a
    CONFLICTING / behind / CI-failing-or-pending measurement, **or an open review thread
    (the reachable state a ``THREADS_UNRESOLVED`` focus always carries, since
    :func:`~maestro.remediation_tracker.classify_owned_blocker` only emits that
    code when ``unresolved_threads > 0``) / unaddressed overflow** is *not*
    merge-ready — that is genuine code/gate work — so the caller keeps the
    existing 'Finish PR #N' text. Pure; zero SDK cost.
    """
    if readiness is None or readiness.branch_landed:
        return False
    if owned_blocker.strip().upper() in _WIP_CODE_GATE_BLOCKERS:
        return False
    return (
        not readiness.branch_behind
        and not readiness.branch_conflicting
        and pr_facts.ci_state_clears_ci_axis(readiness.ci_state)
        and readiness.unresolved_threads == 0
        and readiness.unaddressed_overflow == 0
    )


def _wip_primacy_failing_checks(
    ctx: GateContext, pr_num: int,
) -> tuple[str, ...]:
    """The failing check names to quote for *pr_num*, or ``()`` (137, step 11).

    Consulted only on the code-clearable-red branch of :func:`_wip_primacy_run`,
    and only while ``[loop] owed_act_binding_enabled`` is on — so the flag-off
    path pays for no extra ``gh`` read and renders today's name-free text. The
    read itself is the agent's existing ``_observe_pr_check_runs``, routed
    through :func:`~maestro.loop.owed_act_binding.failing_checks_for` (and thus
    the CI-truth owner), so no new host seam is introduced.
    """
    if not ctx.config.owed_act_binding_enabled:
        return ()
    return failing_checks_for(pr_num, ctx.agent._observe_pr_check_runs)


def _wip_primacy_run(ctx: GateContext) -> GateOutcome | None:
    """Finish owned WIP before starting new (proposal 092, head of the chain).

    Consults the single Work-In-Progress owner via
    :meth:`~maestro.agent.MaestroAgent._current_task_focus`. On
    :class:`~maestro.wip_primacy.TendOwnedPR` — an open own PR whose merge
    blocker is mine to clear (``CONFLICTING`` / ``THREADS_UNRESOLVED``) — it
    blocks discretionary generation *and* carries a bounded directive naming that
    PR, so the loop converges started work before minting new. On
    :class:`~maestro.wip_primacy.Discretionary` it returns ``None`` and the chain
    proceeds. Enabled only under ``[loop] wip_primacy_enabled``; zero SDK cost.

    The directive *text* branches three ways over the measured readiness fact
    :meth:`_current_task_focus` already observed into ``self._pr_readiness_memo``.
    (1) When the PR is measured merge-ready — green, 0-behind, 0-conflict with no
    code/gate blocker left (:func:`_wip_pr_merge_ready`) — the sole outstanding
    disposition is a human merge-or-close, so it writes the handoff-framed
    directive naming the PR and instructing that message to Thomas, removing the
    decoy lever the loop kept lapping. (2) When the sole remaining blocker is a
    code-clearable CI *red* (:func:`_wip_is_code_clearable_red` — a failing
    quality gate / brevity over-cap, distinct from a git conflict or a review
    thread), it names the unfakeable first mutating act (Edit the failing
    file/gate, then push; do not survey first), so the act phase cannot open with
    a fork-read that produces a "nothing to push" artifact over a red I own
    (#3264) — and, under ``[loop] owed_act_binding_enabled``, names the checks
    that are actually failing (:func:`_wip_primacy_failing_checks`) in both the
    directive and the activation row, so the act phase does not have to go
    looking for which gate is red. (3) Otherwise it keeps the ``Finish PR #N``
    text for the genuine conflict / thread blocker. It stays the *sole* writer of all three variants.
    """
    focus = ctx.agent._current_task_focus()
    if not isinstance(focus, TendOwnedPR):
        return None
    readiness = ctx.agent._pr_readiness_memo.get(focus.pr_number)
    if readiness is not None and _wip_pr_merge_ready(
        readiness, focus.owned_blocker,
    ):
        owed = owed_act_for_readiness(readiness)
        return GateOutcome(
            detail=format_handoff_directive_detail(
                focus.pr_number, owed.value if owed is not None else "",
            ),
            log_message=(
                "generate_task: wip-primacy gate engaged — owned PR "
                f"#{focus.pr_number} is measured merge-ready; handing it off to "
                "Thomas (merge-or-close) rather than lapping it again"
            ),
            directive=_wip_primacy_handoff_directive_text(focus.pr_number),
        )
    if _wip_is_code_clearable_red(readiness, focus.owned_blocker):
        checks = _wip_primacy_failing_checks(ctx, focus.pr_number)
        named = f" failing={','.join(checks)}" if checks else ""
        return GateOutcome(
            detail=(
                f"pr=#{focus.pr_number} blocker={focus.owned_blocker} "
                f"code_red=first_act{named}"
            ),
            log_message=(
                "generate_task: wip-primacy gate engaged — owned PR "
                f"#{focus.pr_number} has a code-clearable CI red{named}; "
                "directing the first act (Edit the failing gate, then push) "
                "instead of a survey-first 'Finish PR' lap"
            ),
            directive=_wip_primacy_code_red_directive_text(
                focus.pr_number, checks,
            ),
        )
    return GateOutcome(
        detail=f"pr=#{focus.pr_number} blocker={focus.owned_blocker}",
        log_message=(
            "generate_task: wip-primacy gate engaged — owned PR "
            f"#{focus.pr_number} has a self-clearable {focus.owned_blocker} "
            "blocker; finishing owned work before starting new"
        ),
        directive=_wip_primacy_directive_text(focus.pr_number, focus.owned_blocker),
    )


def _quiet_skip_run(ctx: GateContext) -> GateOutcome | None:
    """Skip the LLM call while the queue holds work and nothing external waits.

    Creates natural drain-then-refill cycles: the queue empties as
    ``_run_task_phase`` consumes items, then ``generate_task`` runs with
    fresher context. Pure-read decision before any state mutation, safe
    under ``_LOOP_PHASES`` resume.
    """
    queue = ctx.agent.task_queue.load()
    if (
        len(queue) > 0
        and not ctx.model.pending_user_requests
        and not ctx.model.pending_pr_requests
    ):
        return GateOutcome(
            detail=f"queue_size={len(queue)}",
            log_message=(
                "generate_task: quiet-skip gate engaged — "
                f"queue has {len(queue)} items and no external requests "
                "pending; skipping SDK call"
            ),
        )
    return None


def _audit_stall_run(ctx: GateContext) -> GateOutcome | None:
    """Block generation while an ``[audit]`` question stalls unintegrated.

    Short-circuits before the SDK call so generation pressure converts
    into integration pressure; clears automatically when the LLM removes
    the entry from ``open_questions``. Question-lineage resolutions fold
    in so a recent REFINE doesn't zero the counter. ``next_task_directive``
    is preserved across firings — directives are not a bypass.
    """
    threshold = ctx.config.audit_stall_threshold
    recent_audits = ctx.agent.memory.get_recent_audit_results(threshold)
    try:
        recent_resolutions = ctx.agent.memory.get_recent_question_resolutions(
            ctx.agent._STALL_LINEAGE_RESOLUTION_LOOKBACK,
        )
    except (OSError, sqlite3.Error):
        # Best-effort lineage signal, bounded to the two wired backends'
        # recoverable storage failures: ``OSError`` (the JSONL adapter, whose
        # ``load_all_question_resolutions`` never raises past it) and
        # ``sqlite3.Error`` (``SqliteQuestionResolutionLedger`` under ``[paths]
        # sqlite_state_enabled``, whose storage failures are the sqlite3 family
        # — its row decoding drops a corrupt blob rather than raising, exactly
        # as the JSONL walk does, so neither backend surfaces a decode error
        # here).
        # Softening to ``[]`` folds ``compute_audit_stall_counts`` into legacy
        # single-key matching rather than crashing generation on a read hiccup.
        # Any *other* exception is an adapter defect, not a storage hiccup —
        # swallowing it would silently zero a renamed question's accumulated
        # stall count, so it propagates into the fail-loud path.
        logger.warning(
            "generate_task: audit-stall lineage read failed; falling back "
            "to legacy single-key matching",
            exc_info=True,
        )
        recent_resolutions = []
    stall_counts = audit_checks.compute_audit_stall_counts(
        recent_audits,
        ctx.model.open_questions,
        resolutions=recent_resolutions,
    )
    if not stall_counts:
        return None
    worst_key, worst_count = max(stall_counts.items(), key=lambda kv: kv[1])
    if worst_count < threshold:
        return None
    return GateOutcome(
        detail=f"{worst_key} fired {worst_count} cycles",
        log_message=(
            f"generate_task: audit-stall gate engaged — {worst_key!r} has "
            f"fired {worst_count} consecutive cycles "
            f"(threshold={threshold}); skipping SDK call"
        ),
    )


#: Gate name the cost-risk gate's ``blocked`` rows (via ``GATE_CHAIN``) and
#: ``released`` rows are stamped under, and the release counter reads — one
#: constant so a rename cannot desynchronise them (#4071).
_COST_RISK_GATE = "cost_risk"

#: Number of byte-identical consecutive ``cost_risk``/``blocked`` activations
#: after which the gate lets exactly one curation call through and stamps a
#: ``released`` activation (#2566). This accelerates recovery below the 24h
#: ``COST_RISK_DECAY_HORIZON`` and makes the release observable in
#: ``gate_activations.jsonl``. Self-extinguishing: the ``released`` row breaks
#: the run. Only ``cost_risk`` rows count toward or break it — other gates'
#: rows in the shared trace are skipped (#4071).
_COST_RISK_RELEASE_STREAK = 5


def _trailing_identical_blocks(
    backend: GateActivationBackend, gate: str, detail: str, limit: int,
) -> int:
    """Count the trailing run of identical *gate*/``blocked`` activations.

    Walks *gate*'s **own** rows newest-first and counts consecutive rows that
    are ``action == "blocked"`` with a ``detail`` byte-identical to *detail*; a
    ``released`` row or a differing detail breaks the run. Other gates' rows
    are skipped, neither breaking nor extending it: the trace is shared by every
    phase, and tend's cooldown-skip rows land every iteration ahead of
    ``generate_task``, so an unscoped tail would read 0 and wedge the valve shut
    (#4071). Zero SDK cost.

    Only the last *limit* rows of *gate* can ever matter — a release valve
    fires (and breaks the run) the moment the count reaches its streak — so
    this takes a *bounded*, gate-scoped tail read (``read_gate_tail``) that
    hydrates at most *limit* records (#2566 review note).
    """
    count = 0
    tail = backend.read_gate_tail(gate, limit)
    for record in reversed(tail):
        if record.action == "blocked" and record.detail == detail:
            count += 1
        else:
            break
    return count


def _cost_risk_run(ctx: GateContext) -> GateOutcome | None:
    """Block generation on cost-risk patterns in recent task history.

    Consecutive expensive failures, escalating failure durations, high
    failure-cost ratio. Pure logic, deterministic. ``check_task_cost_risk``
    is reached through the :mod:`maestro.task_cost_gate` module attribute
    so existing patches keep intercepting it.

    ``datetime.now(UTC)`` is handed in as the horizon clock so a static task
    history that has aged past ``COST_RISK_DECAY_HORIZON`` can no longer wedge
    ``generate_task`` forever (#2566): the gate is the only thing that curates
    the queue, so an idle loop appends no new record to refresh the window.

    A second, faster release valve layers on top: after
    :data:`_COST_RISK_RELEASE_STREAK` byte-identical consecutive blocks the gate
    lets one curation call through (returns ``None``) and stamps a ``released``
    activation, so recovery beats the 24h horizon and the release is observable.

    Evidence is scoped to ``self_directed`` records (#4573), mirroring
    :func:`_consecutive_partial_breaker_run`: external PR-remediation laps and
    user requests legitimately end ``partial`` (success iff landed), and they
    reach the task slot through ``pending_pr_requests``, which this block cannot
    throttle — counting them would wedge discretionary generation on history it
    can never advance.
    """
    discretionary = [t for t in ctx.recent() if t.source == "self_directed"]
    cost_verdict = task_cost_gate.check_task_cost_risk(
        discretionary, reference_time=datetime.now(UTC),
    )
    if cost_verdict is None:
        return None
    detail = str(cost_verdict)[:200]
    identical_blocks = _trailing_identical_blocks(
        ctx.agent.gate_activations, _COST_RISK_GATE, detail,
        _COST_RISK_RELEASE_STREAK,
    )
    if identical_blocks >= _COST_RISK_RELEASE_STREAK:
        log_gate_activation(
            ctx.agent.gate_activations,
            gate=_COST_RISK_GATE,
            action="released",
            detail=f"released after {identical_blocks} identical blocks: {detail}",
        )
        return None
    return GateOutcome(
        detail=detail,
        log_message=f"generate_task: cost gate engaged — {cost_verdict}",
        journal_content=f"Cost gate blocked task generation: {cost_verdict}",
        journal_trigger="cost_gate",
    )


def _escalation_bridge_run(ctx: GateContext) -> GateOutcome | None:
    """Hand an unlanded cure up to Thomas once, then suppress remediation.

    When an UNLANDED-REMEDIATION lineage has crossed the lap budget
    (``[loop] pr_remediation_lap_budget``) and no ``pr_number``-keyed standdown
    is recorded, deliver exactly one merge-or-close message to Thomas and block
    this iteration's task generation — re-minting a remediation lap for a cure
    only Thomas can land is the wheel this gate replaces with a single human
    knock. Idempotent by construction: once the standdown is recorded, detection
    returns ``None`` and the gate is a no-op, so the handoff fires exactly once
    per PR regardless of head changes (proposal 080 Phase 3).
    """
    # Progress-aware lap budget (proposal 089 step 5): when enabled, the handoff
    # triggers on *consecutive no-progress* laps instead of the gross unlanded
    # count, so a PR clearing a freshly-arrived blocker every lap (the healthy
    # churn of a fast main) never escalates while a genuinely stuck one still
    # does. With the flag off the gross-count trigger is unchanged.
    no_progress_laps = None
    if ctx.config.progress_aware_lap_budget_active:
        no_progress_laps = measure_no_progress_laps(
            ctx.config.memory_dir, escalation_bridge.HANDOFF_SCAN_CYCLES,
        )
    handoff = escalation_bridge.detect_unlanded_cure_handoff(
        ctx.config.memory_dir,
        backend=ctx.agent.escalation_standdowns,
        threshold=ctx.config.pr_remediation_lap_budget,
        is_pr_merged=ctx.agent._host_or_git_pr_merged,
        # Symmetric settled guard (#2967): a PR Thomas closes in response to the
        # handoff must not re-knock. Read merge state through the same
        # per-iteration-memoized is_pr_settled chokepoint the standdown release
        # uses (pr_remediation._pr_resolved) so detection and release share one
        # merged-or-closed oracle — never a bespoke closed-state check.
        is_pr_settled=lambda n: ctx.agent.is_pr_settled(n).settled,
        no_progress_laps=no_progress_laps,
    )
    if handoff is None:
        return None
    delivered = escalation_bridge.deliver_handoff_cfg(
        ctx.config, handoff, backend=ctx.agent.escalation_standdowns,
        plan_lookup=ctx.agent._plan_for_pr_number,
        claim=StanddownClaim.LAP_BUDGET_EXHAUSTED,
    )
    if not delivered:
        return None
    return GateOutcome(
        detail=f"PR #{handoff.pr_number} handoff delivered (x{handoff.stamp_count})",
        log_message=(
            "generate_task: escalation-bridge gate engaged — delivered one "
            f"merge-or-close handoff to Thomas for unlanded PR "
            f"#{handoff.pr_number}; suppressing remediation minting"
        ),
        journal_content=(
            f"No-escalation-bridge: PR #{handoff.pr_number} cure stamped "
            f"{handoff.stamp_count}x but unmerged per git-merge-truth; handed "
            f"up to Thomas once (merge-or-close) and stood down rather than "
            f"re-minting a remediation lap."
        ),
        journal_trigger="escalation_bridge_gate",
    )


# Number of trailing *consecutive* ``partial`` task outcomes that trips the
# circuit-breaker. Module constant by design — no new TOML; the treadmill this
# halts (a run of ``partial`` build-tasks orbiting one symbol, each grinding out
# real cost before the duration-based cost gate finally stops me) is a property
# of my own behaviour, not an operator-tunable policy. Mirrors
# :data:`_UNLOSEABLE_STREAK_THRESHOLD` in spirit.
_CONSECUTIVE_PARTIAL_THRESHOLD = 4

#: Gate name the circuit-breaker's ``blocked`` rows (via ``GATE_CHAIN``) and
#: ``released`` rows are stamped under, and its release counter reads — one
#: constant so a rename cannot desynchronise them (#4071).
_CONSECUTIVE_PARTIAL_GATE = "consecutive_partial_breaker"

#: Number of byte-identical consecutive ``consecutive_partial_breaker``/
#: ``blocked`` activations after which the breaker lets exactly one curation
#: call through and stamps a ``released`` activation (#4108). The block itself
#: stops ``generate_task`` from queueing the ``self_directed`` task whose
#: outcome would reset the run, so on an idle loop the window never advances;
#: this valve admits one ``generate_task`` curation call per streak (which may
#: queue up to its free slots) so the run can be re-evaluated.
#: Module constant for the same reason as the threshold.
_CONSECUTIVE_PARTIAL_RELEASE_STREAK = 5

# The bounded directive the circuit-breaker carries into an *empty*
# ``next_task_directive`` slot when it fires. Fixed template (no run-count
# interpolation) so it is provably within ``Consciousness.MAX_DIRECTIVE_CHARS``
# — the exact run count rides in the activation detail / log line instead. It
# routes the loop out of the build-task mill toward evolve/contemplate/human
# input rather than minting another doomed ``partial``.
_CONSECUTIVE_PARTIAL_DIRECTIVE = (
    "Recent tasks keep ending `partial` — a treadmill. Do NOT mint another "
    "build-task. Route to evolve/contemplate, or ask Thomas for direction, "
    "instead of grinding out one more partial."
)


def count_trailing_partial_outcomes(
    records: Sequence[Mapping[str, Any]],
) -> int:
    """Return the length of the trailing run of consecutive ``partial`` outcomes.

    Walks *records* — the ``generate_task`` 10-task history window, oldest-first
    (newest last, matching :func:`_unloseable_streak_run`'s tail convention) —
    from the newest backwards, counting ``partial`` task outcomes and stopping at
    the first non-``partial`` (``success`` / ``failure``). A single trailing
    non-partial outcome yields ``0`` — that stop-at-first-non-partial reset is
    how the circuit-breaker falls silent once a released task completes
    cleanly. An empty window yields
    ``0``. Pure function, zero SDK cost.
    """
    run = 0
    for rec in reversed(records):
        if rec.get("outcome") != "partial":
            break
        run += 1
    return run


def _consecutive_partial_breaker_run(ctx: GateContext) -> GateOutcome | None:
    """Halt the discretionary-task treadmill after a run of ``partial`` outcomes.

    Counts the trailing run of consecutive ``partial`` task outcomes over the
    shared 10-task history window (:func:`count_trailing_partial_outcomes`) and,
    once it reaches :data:`_CONSECUTIVE_PARTIAL_THRESHOLD`, short-circuits
    discretionary task generation — carrying :data:`_CONSECUTIVE_PARTIAL_DIRECTIVE`
    into an *empty* ``next_task_directive`` slot only (``run_gate_chain``'s block
    branch never overwrites a pre-set directive). No existing gate keys on the
    outcome-run itself: ``max_consecutive_remediation`` caps remediation, the
    cost gate is duration-based, rule-18 is owned-act drought — this deterministic
    circuit-breaker stops me minting the doomed build-task N+1 earlier and cheaper.
    A single trailing ``success``/``failure`` ``self_directed`` outcome resets
    the run to zero and the gate falls silent — but the block itself stops
    ``generate_task`` from queueing the task that would record that outcome, so
    on an idle loop the window is frozen (#4108). A release valve breaks that
    livelock: after :data:`_CONSECUTIVE_PARTIAL_RELEASE_STREAK` byte-identical
    consecutive blocks the gate lets one curation call through (returns
    ``None``) and stamps a ``released`` activation; a clean outcome from the
    admitted task resets the run, another ``partial`` re-arms the streak. Zero
    SDK cost.

    Scoped to the *discretionary* treadmill: only ``self_directed`` task records
    feed the run. PR-remediation and user-request tasks are recorded ``external``
    and legitimately/normally end ``partial`` (``outcome_authority``: ``success``
    iff the PR landed, else ``partial``), so a routine multi-lap remediation
    would otherwise pad the run and — because this is a *block* gate that stops
    ``generate_task`` from recording any fresh ``self_directed`` outcome — wedge
    discretionary generation on history it can never advance. Filtering to
    ``self_directed`` (mirroring ``_unloseable_streak_run``'s source/shape
    filtering) keeps the self-extinguishing property inside the channel the gate
    actually governs.
    """
    run = count_trailing_partial_outcomes(
        [r for r in ctx.recent_dicts() if r.get("source") == "self_directed"],
    )
    if run < _CONSECUTIVE_PARTIAL_THRESHOLD:
        return None
    detail = (
        f"{run} consecutive partial task outcomes "
        f"(threshold={_CONSECUTIVE_PARTIAL_THRESHOLD})"
    )
    identical_blocks = _trailing_identical_blocks(
        ctx.agent.gate_activations, _CONSECUTIVE_PARTIAL_GATE, detail,
        _CONSECUTIVE_PARTIAL_RELEASE_STREAK,
    )
    if identical_blocks >= _CONSECUTIVE_PARTIAL_RELEASE_STREAK:
        log_gate_activation(
            ctx.agent.gate_activations,
            gate=_CONSECUTIVE_PARTIAL_GATE,
            action="released",
            detail=f"released after {identical_blocks} identical blocks: {detail}",
        )
        return None
    return GateOutcome(
        detail=detail,
        log_message=(
            "generate_task: consecutive-partial circuit-breaker engaged — "
            f"{run} trailing partial task outcomes "
            f"(threshold={_CONSECUTIVE_PARTIAL_THRESHOLD}); halting "
            "discretionary generation and routing out of the build-task mill"
        ),
        directive=_CONSECUTIVE_PARTIAL_DIRECTIVE,
        journal_content=(
            f"Consecutive-partial circuit-breaker: {run} trailing partial task "
            "outcomes with no clean completion — the treadmill. Halted "
            "discretionary generation and routed toward evolve/contemplate or "
            "human input rather than minting another doomed build-task."
        ),
        journal_trigger="consecutive_partial_breaker",
    )


# ---------------------------------------------------------------------------
# No-repeat-failed-branch-target gate (block a doomed task-path re-attempt)
# ---------------------------------------------------------------------------
#
# A ``maestro/task/<hash>`` branch reaches ``main`` *only* through an evolution
# PR that nominates it via ``source_task_id`` — never from the task-execution
# path, where the action gate blocks every PR/push/merge. Repeatedly re-minting
# a task that re-attacks a branch which already recorded a non-success outcome
# ('finish', 'promote', 'wire', 'open a real PR') therefore burns the full task
# budget on an impossible main-delivery and only trips the duration-based cost
# gate after the waste is already spent. This gate keys on the *structured*
# ``maestro/task/<hash>`` token (not brittle NL verb classification, the failure
# class the rejection ledger warns against) in a candidate queue task, matched
# against the branch tokens of recent non-success ``task_history`` entries. It is
# deliberately distinct from the count-keyed consecutive-partial breaker and the
# verb-keyed branch-promotion redirect: it fires per-target, on any verb, on the
# first repeat. Self-extinguishing — when no queued candidate references a failed
# target (the failed branch aged out of the window, or the candidate is gone) it
# returns ``None``.

#: Bound on the ``task_history`` tail :func:`_failed_branch_targets` reads —
#: matches the 10-task window the rest of the pre-call chain uses (see
#: :meth:`GateContext.recent`), so "recent" means the same thing everywhere.
_FAILED_BRANCH_WINDOW = 10

#: Public name for :data:`_FAILED_BRANCH_WINDOW`, read by the admission
#: (``_vet_goal_content``) and dequeue (``_run_task_phase``) refusals so all
#: three failed-branch sites scan the same history tail.
FAILED_BRANCH_WINDOW = _FAILED_BRANCH_WINDOW

#: The structured owned-task-branch token. ``run()`` names task branches
#: ``maestro/task/<task_id>`` where ``task_id`` is a ≥6-hex uuid slice; this
#: matches that literal shape so detection never rides on NL classification.
_BRANCH_TOKEN_RE = re.compile(r"maestro/task/[0-9a-f]{6,}")


def _referenced_branch_tokens(text: str) -> set[str]:
    """Return the ``maestro/task/<hash>`` branch tokens named in *text*.

    Zero-cost structured extraction (:data:`_BRANCH_TOKEN_RE`) over any
    candidate task text; tolerant of ``None``/empty. The set form lets the gate
    intersect a candidate's referenced tokens with the failed-target set.
    """
    return set(_BRANCH_TOKEN_RE.findall(text or ""))


def _failed_branch_targets_from_records(
    records: Sequence[TaskRecord],
) -> set[str]:
    """Union the failed-branch tokens named across already-loaded *records*.

    Core of :func:`_failed_branch_targets`, split out so the gate can scan the
    shared :meth:`GateContext.recent` window instead of a private
    ``Memory`` read — preserving the single-read I/O profile the
    ``GateContext`` cache was built for. Unions the ``maestro/task/<hash>``
    tokens found in each entry whose ``outcome`` is not ``"success"``. Tokens
    are drawn from the entry's structured ``branch`` field *and* its ``goal`` /
    ``key_findings`` / ``result_summary`` narrative, so a failed *re-attempt*
    whose own branch differs still contributes the original target it named.
    Pure in-memory scan; zero I/O, zero SDK cost.
    """
    targets: set[str] = set()
    for rec in records:
        if rec.outcome == "success":
            continue
        text = " ".join(
            str(value)
            for value in (
                rec.branch,
                rec.goal,
                rec.key_findings,
                rec.result_summary,
            )
            if value
        )
        targets |= _referenced_branch_tokens(text)
    return targets


def failed_branch_target_named(
    text: str, records: Sequence[TaskRecord],
) -> str | None:
    """Return the failed-branch token *text* names, else ``None``.

    The one predicate shared by the three failed-branch sites — the
    generation-time block (:func:`_no_repeat_failed_branch_target_run`), the
    admission content gate (``_vet_goal_content``), and the dequeue refusal in
    ``_run_task_phase`` (issue #4049). Intersects the ``maestro/task/<hash>``
    tokens in *text* with :func:`_failed_branch_targets_from_records` over
    *records*; the lowest-sorted match wins so the answer is deterministic.
    Pure in-memory scan; zero I/O, zero SDK cost.
    """
    referenced = _referenced_branch_tokens(text)
    if not referenced:
        return None
    matched = referenced & _failed_branch_targets_from_records(records)
    if not matched:
        return None
    return sorted(matched)[0]


def drop_failed_branch_head(
    agent: MaestroAgent, head: QueuedTask,
) -> bool:
    """Refuse a just-popped queue *head* that re-attacks a failed branch.

    The pop-time door of the failed-branch check (issue #4049): the
    generation-time gate only skips curation, so a head queued before its
    target branch failed would otherwise run the doomed re-attempt. On a
    match logs a ``rejected`` ``no_repeat_failed_branch_target`` activation
    row, journals the drop, and returns ``True`` — the caller returns without
    running it. The head is dropped, not re-queued: a re-queue would re-fire
    this check on every later pop. Zero SDK cost.
    """
    token = failed_branch_target_named(
        f"{head.goal} {head.rationale or ''}",
        agent.memory.get_recent_tasks(FAILED_BRANCH_WINDOW),
    )
    if token is None:
        return False
    audit_checks.log_gate_activation(
        agent.gate_activations,
        gate="no_repeat_failed_branch_target",
        action="rejected",
        detail=f"dropped queue head {head.task_id}: {token}",
    )
    logger.info(
        "run_task: dropped queue head %s — re-attacks failed task branch %s",
        head.task_id, token,
    )
    agent.journal.record_event(
        content=(
            f"Dropped queued task {head.task_id} ({head.goal[:120]!r}) "
            f"without running it: it re-attacks {token}, which already "
            "recorded a non-success outcome. That branch reaches main only "
            "via an evolution PR."
        ),
        entry_type="audit_observation",
        triggered_by="no_repeat_failed_branch_target_gate",
    )
    return True


def _failed_branch_targets(
    data_dir: Path, window: int = _FAILED_BRANCH_WINDOW,
) -> set[str]:
    """Return the branch tokens of recent non-success task_history entries.

    Reads only the *window*-task tail of ``task_history.jsonl`` under *data_dir*
    via :meth:`Memory.get_recent_tasks` (the existing reader — bounded, reverse-
    streamed) and delegates the token extraction to
    :func:`_failed_branch_targets_from_records`. Pure file read; zero SDK cost.
    """
    if window <= 0:
        return set()
    return _failed_branch_targets_from_records(
        Memory(data_dir).get_recent_tasks(window),
    )


def _source_task_id_eligible(data_dir: Path, task_id: str) -> bool:
    """Return whether *task_id* would survive ``_validate_source_task_id``.

    Mirrors :meth:`EvolutionPlanner._validate_source_task_id` exactly so the
    redirect never recommends a ``source_task_id`` the plan proposer will drop:
    a match in the recent-30 window (newest-first, latest retry wins) whose
    outcome is ``"success"`` or ``"partial"`` — the outcomes validation accepts
    — with a non-empty ``branch``. Only a ``failure`` outcome (the primary
    target of this gate) or a branchless/window-aged record is *not* eligible,
    so a known-broken branch is never offered as an implementation base, while a
    kept ``success`` branch that a *later* re-attempt named — the validator's
    single best base — is still recommended. Pure file read; zero SDK cost.
    """
    if not task_id:
        return False
    for record in reversed(Memory(data_dir).get_recent_tasks(30)):
        if record.task_id != task_id:
            continue
        return record.outcome in ("success", "partial") and bool(record.branch)
    return False


def _failed_branch_redirect_directive(
    task_id: str, *, source_eligible: bool,
) -> str:
    """Render the fresh-target redirect for a failed branch, cap-bounded.

    Names the one path a task branch reaches ``main`` — an evolution PR —
    instead of another doomed task-path re-attempt. Only recommends
    ``source_task_id={task_id}`` when *source_eligible* (a ``partial``-outcome
    branch validation will actually accept, per :func:`_source_task_id_eligible`);
    for a ``failure``-outcome branch it steers toward a *fresh-from-HEAD*
    evolution PR, since :meth:`_validate_source_task_id` would drop the id and
    basing a plan on a known-broken branch is undesirable anyway.
    Bounded by :func:`~maestro.audit_checks.truncate_directive`, so the cap
    moves with :data:`~maestro.audit_checks._DIRECTIVE_MAX_CHARS` rather than
    a local literal; ``save_self_model`` re-truncates on persist anyway, so
    the explicit cap only keeps the log/detail text tidy.
    """
    if source_eligible:
        directive = (
            f"Branch {task_id} already failed a recent task attempt; it reaches "
            f"main only via an evolution PR — propose a plan with "
            f"source_task_id={task_id}"
        )
    else:
        directive = (
            f"Branch {task_id} already failed a recent task attempt; it reaches "
            f"main only via a fresh-from-HEAD evolution PR — propose a plan for "
            f"this work (no source_task_id; the branch is not reusable)"
        )
    return truncate_directive(directive)


def _no_repeat_failed_branch_target_run(ctx: GateContext) -> GateOutcome | None:
    """Skip generation while a queued candidate re-attacks a failed branch.

    Skips the curation call and writes a bounded ``source_task_id`` redirect
    into an *empty* directive slot (a pre-set directive survives, per the block
    branch's empty-slot guard) when any queued candidate task references — via
    the structured ``maestro/task/<hash>`` token — a branch in
    :func:`_failed_branch_targets`. Deterministic: candidates are scanned in
    FIFO order and the lowest-sorted matching token wins, so the same history +
    queue always names the same target. Zero SDK cost; strict no-op (returns
    ``None``) when no failed targets exist or no candidate references one.

    This gate does not remove the candidate from the queue: the re-attempt
    itself is refused at admission (the ``failed_branch_target`` content gate
    in ``_vet_goal_content``) and at pop (``_run_task_phase``), both through
    :func:`failed_branch_target_named` (issue #4049).

    Reuses the shared :meth:`GateContext.recent` 10-task window for the
    failed-target scan (rather than a private ``Memory`` read), so a downstream
    ``ctx.recent()`` consumer does not pay for the tail a second time; the
    separate ``data_dir``-based recent-30 read stays in
    :func:`_source_task_id_eligible`, which needs a wider window than the chain
    caches.
    """
    records = ctx.recent()
    if not _failed_branch_targets_from_records(records):
        return None
    for task in ctx.agent.task_queue.load():
        token = failed_branch_target_named(
            f"{task.goal} {task.rationale or ''}", records,
        )
        if token is None:
            continue
        task_id = token.rsplit("/", 1)[-1]
        source_eligible = _source_task_id_eligible(
            ctx.config.memory_dir, task_id,
        )
        return GateOutcome(
            detail=f"candidate re-attacks failed branch {token}",
            log_message=(
                "generate_task: no-repeat-failed-branch-target gate engaged — "
                f"a queued candidate re-attacks failed task branch {token}; "
                "skipping generation and redirecting to an evolution PR"
            ),
            directive=_failed_branch_redirect_directive(
                task_id, source_eligible=source_eligible,
            ),
            journal_content=(
                f"No-repeat-failed-branch: branch {token} already recorded a "
                "non-success task outcome; a task branch reaches main only via "
                "an evolution PR (source_task_id), never the task path. Skipped "
                "generation and wrote a redirect; the queued re-attempt is "
                "refused at admission and at pop, not here."
            ),
            journal_trigger="no_repeat_failed_branch_target_gate",
        )
    return None


# ---------------------------------------------------------------------------
# Directive bridges (fill the empty ``next_task_directive`` slot)
# ---------------------------------------------------------------------------


def _instrument_consumption_run(ctx: GateContext) -> GateOutcome | None:
    """Direct review of the oldest unconsumed instrument-shipping task.

    Self-extinguishing: once instruments get consumed, the count drops
    below threshold and the gate stops firing. Experiment.
    """
    unconsumed = find_unconsumed_instruments(ctx.recent_dicts())
    if len(unconsumed) < INSTRUMENT_CONSUMPTION_THRESHOLD:
        return None
    oldest = unconsumed[0]
    inst_id = str(oldest.get("task_id", ""))
    inst_goal = str(oldest.get("goal", ""))[:100]
    directive = f"Review output of task {inst_id}: {inst_goal}"
    directive = truncate_directive(directive)
    return GateOutcome(
        detail=f"{len(unconsumed)} unconsumed; reviewing {inst_id}",
        log_message=(
            "generate_task: instrument-consumption bridge set "
            f"directive — {directive}"
        ),
        directive=directive,
        journal_content=(
            f"Instrument-consumption redirect: {len(unconsumed)} "
            f"unconsumed instruments detected; directing review "
            f"of {inst_id}"
        ),
        journal_trigger="instrument_consumption_gate",
    )


def _integration_cadence_run(ctx: GateContext) -> GateOutcome | None:
    """Direct integration work when no recent task performed any.

    Self-extinguishing: once integration work happens, the cadence resets.

    Gated by the branch-promotion-pressure master switch (proposal 116,
    step 6): with ``[audit] branch_promotion_pressure_enabled`` off this
    directive bridge is silent. The shared ``is_integration_task``
    classifier (used by the investigation-streak / rate gates) is untouched.
    """
    if not ctx.config.branch_promotion_pressure_enabled:
        return None
    directive = check_integration_cadence(
        ctx.recent_dicts(),
        cadence=ctx.config.integration_cadence,
    )
    if not directive:
        return None
    return GateOutcome(
        detail=directive,
        log_message=(
            f"generate_task: integration-cadence gate set directive — "
            f"{directive}"
        ),
        directive=directive,
    )


def _investigation_streak_run(ctx: GateContext) -> GateOutcome | None:
    """Require construction work after N consecutive investigation tasks.

    Self-extinguishing: one construction task resets the streak.
    """
    directive = check_investigation_streak(
        ctx.recent(),
        threshold=ctx.config.investigation_streak_threshold,
    )
    if not directive:
        return None
    return GateOutcome(
        detail=directive,
        log_message=(
            f"generate_task: investigation-streak gate set directive — "
            f"{directive}"
        ),
        directive=directive,
    )


def _investigation_rate_run(ctx: GateContext) -> GateOutcome | None:
    """Windowed complement to the consecutive-streak check.

    Catches a distributed investigation pattern (mechanical tasks
    interleaved) that the streak gate misses.
    """
    inv_rate = compute_shape_investigation_rate(
        ctx.config.memory_dir,
        window=ctx.config.investigation_rate_window,
    )
    if inv_rate <= ctx.config.investigation_rate_threshold:
        return None
    directive = (
        f"{inv_rate:.0%} of the last "
        f"{ctx.config.investigation_rate_window} tasks were "
        "investigation-shaped (measurement/investigation/other), "
        "above the "
        f"{ctx.config.investigation_rate_threshold:.0%} "
        "threshold. Next task must be construction-shaped: build, "
        "fix, wire, integrate, or promote a branch."
    )
    return GateOutcome(
        detail=directive,
        log_message=(
            f"generate_task: investigation-rate gate set directive — "
            f"{directive}"
        ),
        directive=directive,
    )


def _branchless_streak_run(ctx: GateContext) -> GateOutcome | None:
    """Force code-changing work after N consecutive branchless tasks.

    Structural complement to the investigation-streak/rate gates: those
    classify task *shape* from goal text, while this reads the ``branch``
    field directly as a proxy for whether a task produced file changes. A
    run of branchless tasks is the analysis-only streak — work where I am
    sole judge of completion and ship gitignored artifacts instead of merged
    PRs. Self-extinguishing: one branch-producing task resets the streak.
    Experiment priority — may be reverted if the proxy proves too noisy.
    """
    threshold = ctx.config.branchless_streak_threshold
    streak = audit_checks.count_consecutive_branchless_tasks(
        ctx.recent_dicts(),
    )
    if streak < threshold:
        return None
    directive = (
        "Your next task MUST target specific code files for modification "
        "and produce a branch with file changes. No analysis-only tasks."
    )
    return GateOutcome(
        detail=f"{streak} consecutive branchless tasks (threshold={threshold})",
        log_message=(
            "generate_task: branchless-streak gate set directive — "
            f"{directive}"
        ),
        directive=directive,
    )


def _partial_construction_run(ctx: GateContext) -> GateOutcome | None:
    """Carry a partial task's findings into the next task's directive.

    Self-extinguishing: a subsequent completed task pushes the partial out
    of tail position.
    """
    directive = check_partial_construction_gap(ctx.recent_dicts())
    if not directive:
        return None
    return GateOutcome(
        detail=directive,
        log_message=(
            f"generate_task: partial-construction bridge set directive — "
            f"{directive}"
        ),
        directive=directive,
    )


def _promotable_backlog_run(ctx: GateContext) -> GateOutcome | None:
    """Name the oldest uncited branch when the promotable backlog piles up.

    Directive-bridge complement to the advisory PROMOTABLE BACKLOG prompt
    hints — the backlog signal actuates behavior instead of only informing
    it. Self-extinguishing once branches are promoted or merged.

    Gated by the branch-promotion-pressure master switch (proposal 116,
    step 6): with ``[audit] branch_promotion_pressure_enabled`` off this
    directive bridge is silent.
    """
    if not ctx.config.branch_promotion_pressure_enabled:
        return None
    directive = check_promotable_backlog_directive(
        ctx.recent_dicts(),
        ctx.config.promotable_backlog_threshold,
    )
    if not directive:
        return None
    return GateOutcome(
        detail=directive,
        log_message=(
            f"generate_task: promotable-backlog bridge set directive — "
            f"{directive}"
        ),
        directive=directive,
    )


def _self_directed_success_run(ctx: GateContext) -> GateOutcome | None:
    """Steer toward bounded construction when self-directed tasks keep failing.

    Keeps its window local — the shared 10-task ``ctx.recent()`` is bound
    to the post-call growth gates, so the declared ``SELF_DIRECTED_WINDOW``
    (15) tasks are fetched separately.
    """
    sd_directive_tasks = ctx.agent.memory.get_recent_tasks(
        SELF_DIRECTED_WINDOW,
    )
    directive = check_self_directed_success_rate(
        sd_directive_tasks,
    )
    if not directive:
        return None
    return GateOutcome(
        detail=directive,
        log_message=(
            f"generate_task: self-directed success-rate gate set "
            f"directive — {directive}"
        ),
        directive=directive,
    )


def _forced_read_run(ctx: GateContext) -> GateOutcome | None:
    """Force a real-blocker read for a repeatedly-serviced unmerged PR.

    Consumes merge truth through the ``is_pr_settled`` chokepoint rather than
    adding sensor eleven; forbids a fresh remediation lap. Self-extinguishing
    once the PR is settled (merged *or* closed). Experimental.
    """
    directive = check_forced_read_directive(
        ctx.recent_dicts(),
        ctx.model,
        # Symmetric-settled via the chokepoint (#2967): a closed PR is done too.
        is_pr_merged=lambda n: ctx.agent.is_pr_settled(n).settled,
    )
    if not directive:
        return None
    return GateOutcome(
        detail=directive,
        log_message=(
            f"generate_task: forced-read bridge set directive — {directive}"
        ),
        directive=directive,
    )


def _readmission_construction_run(ctx: GateContext) -> GateOutcome | None:
    """Force a construction shape when already-merged PRs keep re-lapping.

    Actuates the landed ``measure_pr_readmission_rate`` sensor: when the
    readmission ratio crosses the configured threshold, fill the directive
    to steer the next task toward building something that lands rather than
    another remediation lap — turning the dial I already read into a lever.
    Default-off, self-extinguishing, lowest directive precedence.
    Experimental.
    """
    directive = check_readmission_construction_directive(
        ctx.recent_dicts(),
        ratio_threshold=(
            ctx.agent.config.readmission_construction_ratio_threshold
        ),
    )
    if not directive:
        return None
    return GateOutcome(
        detail=directive,
        log_message=(
            f"generate_task: readmission→construction bridge set "
            f"directive — {directive}"
        ),
        directive=directive,
    )


def _comfort_cluster_run(ctx: GateContext) -> GateOutcome | None:
    """Break a comfort-zone cluster when recent goals overlap too tightly.

    Gives the comfort-cluster overlap check behavioural teeth: it ran only
    as a deferred audit advisory that redirected nothing. As a *directive*
    (not a block) it nudges the next task off the cluster without risking
    deadlock — a hard block would prevent any new task, so the overlap
    could never decrease. Lowest-precedence directive bridge: a concrete
    bridge (instrument-consumption, partial-construction, …) that names a
    specific follow-up already breaks repetition, so this catch-all yields
    to them via list order.

    The overlap computation is pure token math, but any unexpected failure
    is swallowed at debug level so a single bad record never blocks task
    generation — the bridge is best-effort context enrichment, not a gate.
    Self-extinguishing: once diverse goals land, mean overlap drops below
    the threshold and the bridge stops firing.
    """
    try:
        findings = check_task_goal_overlap(ctx.model, ctx.recent())
    except Exception:
        logger.debug(
            "comfort-cluster gate: overlap check failed; proceeding",
            exc_info=True,
        )
        return None
    if not findings:
        return None
    directive = (
        f"{findings[0].statement} Break the cluster — pick a different "
        "module, shape, or problem area than the recent run."
    )
    directive = truncate_directive(directive)
    return GateOutcome(
        detail=directive,
        log_message=(
            f"generate_task: comfort-cluster bridge set directive — "
            f"{directive}"
        ),
        directive=directive,
    )


# Task shapes whose completion is gated by an owned ruff/mypy/pytest exit
# code on code I wrote — work I can actually fail at. Everything else
# (measurement, investigation, external_request, other) is read/analysis/doc
# shaped: *unloseable*, because nothing of my own code is put at risk. Derived
# by reusing ``classify_task_shape`` so a new shape only needs adding here.
_LOSEABLE_SHAPES: frozenset[str] = frozenset(
    {
        "github_issue",
        "branch_promotion",
        "module_extension",
        "remediation",
        "construction",
    },
)

# Number of trailing all-unloseable tasks that trips the streak gate. Module
# constant by design — no new TOML; the avoidance this interrupts is a property
# of my own behaviour, not an operator-tunable policy.
_UNLOSEABLE_STREAK_THRESHOLD = 4


def classify_task_loseable(task: Mapping[str, Any]) -> bool:
    """Return ``True`` when *task* is a code-changing (loseable) shape.

    Reuses :func:`classify_task_shape` on the task's ``goal`` text: the
    implementation/bugfix shapes (construction, module_extension,
    branch_promotion, github_issue, remediation) are *loseable* — their
    completion rides on an owned ruff/mypy/pytest exit code over code I
    wrote. The analysis/doc/read shapes (measurement, investigation,
    external_request) and the residual ``other`` are *unloseable*: nothing
    of my own code is put at risk, so I cannot fail at them. Pure function,
    zero SDK cost.
    """
    goal = str(task.get("goal", ""))
    return classify_task_shape(goal) in _LOSEABLE_SHAPES


def _unloseable_streak_run(ctx: GateContext) -> GateOutcome | None:
    """Demand a verified code change after a run of unloseable tasks.

    When the last :data:`_UNLOSEABLE_STREAK_THRESHOLD` recorded tasks are
    *all* unloseable (read/analysis/doc — see :func:`classify_task_loseable`),
    set a bounded directive demanding a small, concrete code change whose
    completion is verified by running ruff/mypy/pytest to a real exit code.
    This is the actuator that interrupts task-selection avoidance at the
    choice point rather than another read-only sensor watching the cycle.
    Self-extinguishing: a single loseable task within the window releases it.
    """
    recent = ctx.recent_dicts()
    if len(recent) < _UNLOSEABLE_STREAK_THRESHOLD:
        return None
    window = recent[-_UNLOSEABLE_STREAK_THRESHOLD:]
    if any(classify_task_loseable(task) for task in window):
        return None
    directive = (
        f"Last {_UNLOSEABLE_STREAK_THRESHOLD} tasks were all unloseable "
        "(read/analysis only). Next task MUST make a small code change to a "
        "specific file, verified by running ruff, mypy, and pytest to a real "
        "exit code."
    )
    return GateOutcome(
        detail=(
            f"{_UNLOSEABLE_STREAK_THRESHOLD} consecutive unloseable tasks "
            f"(threshold={_UNLOSEABLE_STREAK_THRESHOLD})"
        ),
        log_message=(
            "generate_task: unloseable-streak gate set directive — "
            f"{directive}"
        ),
        directive=directive,
    )


# ---------------------------------------------------------------------------
# Untouched-first-target rewrite gate (steer the opening selection)
# ---------------------------------------------------------------------------
#
# The retrospective artifact-id-axis shield (``check_banked_owned_act_reperformance``)
# detects re-performance of an owned terminal act *after* I write it. Nothing
# steers the FIRST selection
# away from artifacts I have already disposed. This gate is the prospective
# complement: at directive-authoring time it reads the recent owned-terminal-act
# ledger (chosen/mechanical stand-down verdicts + obsolete plans + closed-PR
# lineages), and when the owned-act directive names a member of that
# already-disposed set it rewrites the directive to name a distinct *untouched*
# target — landing the steer upstream of will, at the selection seam the
# self-model names ``selection-not-steered``. A strict no-op otherwise.

_EVOLUTION_PLANS_FILE = "evolution_plans.jsonl"

# Bound on the deed-ledger read the PR-close arm performs.
_PR_CLOSE_DEED_LOOKBACK = 50


def _read_jsonl_records(path: Path) -> list[dict[str, Any]]:
    """Return the dict records of a JSONL file, corrupted-tail tolerant.

    Mirrors the audit/remediation read convention: returns an empty list when
    the file is absent or unreadable, and skips malformed lines and non-dict
    records rather than raising. Zero SDK cost; one bounded file read.
    """
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lines = list(f)
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def _issue_id(number: int) -> str:
    """Normalise a GitHub issue/PR number to its ``#NNNN`` identity token."""
    return f"#{number}"


def _issue_sort_key(identity: str) -> int:
    """Sort ``#NNNN`` identity tokens by their numeric value (lowest first)."""
    return int(identity.lstrip("#"))


def _stand_down_touched_ids(backend: StandDownVerdictBackend) -> set[str]:
    """Return ``#NNNN`` ids of every stand-down verdict PR (chosen + mechanical).

    Both the LLM-authored CHOSEN stand-down and the loop-authored mechanical
    stand-down append to the stand-down ledger; each verdict row's positive-int
    ``pr`` is an owned terminal act keyed to that artifact. Release rows are
    excluded: a release voids suppression rather than recording a disposal, so a
    release-only PR (e.g. one Thomas merely asked about, #4314) must never read as
    already disposed. The positive-int ``pr`` extraction lives with *backend*
    (mirroring how :func:`_dropped_directive_replay_run` reaches
    ``ctx.agent.dropped_directives``); this reader just formats each as ``#NNNN``.
    """
    return {_issue_id(row.pr) for row in backend.verdict_rows()}


def _obsolete_plan_touched_ids(data_dir: Path) -> set[str]:
    """Return ``#NNNN`` ids cited by every ``obsolete`` plan record.

    ``mark_plan_obsolete`` rewrites the plan status to ``obsolete`` in
    ``evolution_plans.jsonl`` — an owned terminal act. The citation-bearing
    fields of the :class:`~maestro.memory.EvolutionPlan` schema are read: the
    ``title`` and ``rationale`` prose, the ``proposed_changes`` list, and the
    ``obsolete_reason`` the disposal stamped. The structured ``issue_number``
    linkage is folded in directly — it holds a bare int, which the ``#NNNN``
    prose miner could never match. Validate against the dataclass, not against
    prose — a key the schema never writes can never match. A door-refused mint
    (:func:`~maestro.remediation_tracker.is_owned_obsolete_row`) disposed of
    nothing, so its ids are skipped.
    """
    ids: set[str] = set()
    for rec in _read_jsonl_records(data_dir / _EVOLUTION_PLANS_FILE):
        if not is_owned_obsolete_row(rec):
            continue
        parts = [
            str(rec.get(key) or "")
            for key in ("title", "rationale", "obsolete_reason")
        ]
        changes = rec.get("proposed_changes")
        if isinstance(changes, list):
            parts.extend(str(change) for change in changes)
        elif changes:
            # Legacy row whose list field holds a bare scalar: read it too,
            # rather than silently dropping the ids it cites.
            parts.append(str(changes))
        ids.update(_issue_id(n) for n in extract_issue_numbers(" ".join(parts)))
        issue = rec.get("issue_number")
        if isinstance(issue, int) and not isinstance(issue, bool) and issue > 0:
            ids.add(_issue_id(issue))
    return ids


def _pr_close_deed_touched_ids(backend: deed_ledger.DeedLedgerBackend) -> set[str]:
    """Return ``#NNNN`` ids of every recent ``PR_CLOSE`` deed.

    A ``gh pr close`` disposal the loop honored is ledgered as a
    :attr:`~maestro.deed_ledger.DeedType.PR_CLOSE` deed keyed by its
    ``pr_number`` — a structured disposal fact. Task prose is never scanned:
    a remediation lap's goal is its composed prompt (merge-truth preamble
    included) and its outcome may carry loop-authored "not closed" clauses, so
    close wording there would mark the lap's own live PR disposed (#4070).
    Other deed types (handoff, merge, owned prune) dispose no PR here.
    """
    ids: set[str] = set()
    for deed in backend.recent(_PR_CLOSE_DEED_LOOKBACK):
        if deed.deed_type != deed_ledger.DeedType.PR_CLOSE.value:
            continue
        pr = deed.pr_number
        if isinstance(pr, int) and not isinstance(pr, bool) and pr > 0:
            ids.add(_issue_id(pr))
    return ids


def _build_touched_artifact_ids(ctx: GateContext) -> set[str]:
    """Union the recent owned-terminal-act targets into a ``#NNNN`` id set.

    The already-disposed set the opening selection must steer away from:
    stand-down verdict PRs + ``PR_CLOSE`` deeds + obsolete plan citations.
    Bounded reads, zero SDK cost.
    """
    data_dir = ctx.config.memory_dir
    ids = _stand_down_touched_ids(ctx.agent.stand_down_verdicts)
    ids |= _pr_close_deed_touched_ids(ctx.agent.deed_ledger)
    ids |= _obsolete_plan_touched_ids(data_dir)
    return ids


def _build_untouched_candidate_ids(
    ctx: GateContext, touched: frozenset[str],
) -> list[str]:
    """Return actionable ``#NNNN`` targets in my orbit that are *not* disposed.

    Candidate fresh targets are the issue/PR numbers cited by recent task goals
    and by queued ``pending_pr_requests`` directives, minus the already-disposed
    *touched* set. Numeric-sorted (lowest first) for deterministic selection.
    """
    seen: set[str] = set()
    for rec in ctx.recent_dicts():
        seen.update(
            _issue_id(n) for n in extract_issue_numbers(str(rec.get("goal", "")))
        )
    for req in ctx.model.pending_pr_requests:
        seen.update(_issue_id(n) for n in extract_issue_numbers(req.directive))
    fresh = [identity for identity in seen if identity not in touched]
    return sorted(fresh, key=_issue_sort_key)


def _divert_touched_directive(
    directive: str,
    touched: frozenset[str],
    untouched: Sequence[str],
) -> str | None:
    """Rewrite a directive naming a disposed artifact toward an untouched target.

    Pure decision core (injectable for tests). Returns the rewritten directive
    only when *directive* names an id in *touched* **and** an untouched
    candidate distinct from every named target is available; returns ``None``
    (a strict no-op) when the directive names no disposed artifact, or when no
    untouched target exists. The rewrite is bounded by
    :func:`~maestro.audit_checks.truncate_directive`, so it moves with
    :data:`~maestro.audit_checks._DIRECTIVE_MAX_CHARS` instead of pinning the
    number in prose.
    """
    named = {_issue_id(n) for n in extract_issue_numbers(directive)}
    disposed = named & touched
    if not disposed:
        return None
    fresh = sorted(
        (c for c in untouched if c not in touched and c not in named),
        key=_issue_sort_key,
    )
    if not fresh:
        return None
    target = fresh[0]
    disposed_list = ", ".join(sorted(disposed, key=_issue_sort_key))
    rewritten = (
        f"Do NOT re-select {disposed_list} — already-disposed owned terminal "
        f"act(s). Author a FRESH first target instead: {target}."
    )
    return truncate_directive(rewritten)


def check_untouched_first_target(ctx: GateContext) -> GateOutcome | None:
    """Steer the opening owned-act selection off an already-disposed artifact.

    The prospective complement to rule 19's after-write re-performance
    detection: when the (already-authored) ``next_task_directive`` names an
    artifact in the recent owned-terminal-act set, rewrite it to name a
    distinct untouched target. A strict no-op when there is no directive, no
    disposed target, or no untouched candidate to divert to — the existing
    directive is preserved untouched in every no-op branch.
    """
    directive = ctx.model.next_task_directive
    if not directive:
        return None
    touched = frozenset(_build_touched_artifact_ids(ctx))
    if not touched:
        return None
    untouched = _build_untouched_candidate_ids(ctx, touched)
    rewritten = _divert_touched_directive(directive, touched, untouched)
    if rewritten is None or rewritten == directive:
        return None
    return GateOutcome(
        detail=rewritten,
        log_message=(
            "generate_task: untouched-first-target gate rewrote the "
            f"directive off an already-disposed artifact — {rewritten}"
        ),
        directive=rewritten,
        journal_content=(
            "Untouched-first-target: the owned-act directive named an "
            "already-disposed artifact; steered the FIRST selection to a "
            "fresh target upstream of will, where the leak lives."
        ),
        journal_trigger="untouched_first_target_gate",
    )


# ---------------------------------------------------------------------------
# Stale-obsolete-directive guard (clear a target-empty obsolete-lever directive)
# ---------------------------------------------------------------------------
#
# A ``next_task_directive`` naming the ``mark_plan_obsolete`` lever is only worth
# firing while ``select_distinct_obsolete_target`` can still hand the actuator an
# un-banked ``pending_review`` id to stamp. Cycles stood down at net=+0 because a
# such a directive was minted and the lever fired *blind* after the selector had
# already exhausted every distinct target — industriousness aimed at nothing.
# This gate moves the verify-the-target-before-the-bet value into a mechanical
# floor: when the directive names the obsolete lever *and* the selector returns
# ``None``, it clears the directive (self-extinguishing) so the cycle falls
# through to queue-head remediation instead of staking on a provably-empty
# target. A live selector id, or a directive that does not name the lever, is a
# strict no-op — the directive is preserved untouched.


def _stale_obsolete_directive_run(ctx: GateContext) -> GateOutcome | None:
    """Clear an obsolete-lever directive whose selector target is exhausted.

    Fires only when ``next_task_directive`` names the ``mark_plan_obsolete``
    lever (case-insensitive substring) **and**
    :func:`~maestro.lever_capture.select_distinct_obsolete_target` returns
    ``None`` — proof no un-banked ``pending_review`` id remains to stamp. In
    that case it returns a :class:`GateOutcome` whose ``directive`` is ``None``,
    so the runner clears the slot and the cycle falls through to queue-head
    remediation rather than firing the lever blind at net=+0. A live selector
    target, or a directive that does not name the lever, is a strict no-op
    (returns ``None``, leaving the directive untouched). Zero SDK cost; one
    bounded ledger read.
    """
    directive = ctx.model.next_task_directive
    if not directive or lever_capture.OBSOLETE_LEVER_SIGNATURE not in directive.lower():
        return None
    target = lever_capture.select_distinct_obsolete_target(
        ctx.agent.memory, ctx.agent.lever_attempts,
    )
    if target is not None:
        return None
    return GateOutcome(
        detail="no un-banked obsolete target; cleared directive",
        log_message=(
            "generate_task: stale-obsolete-directive guard cleared the "
            "mark_plan_obsolete directive — select_distinct_obsolete_target "
            "returned no un-banked pending_review id to stamp"
        ),
        directive=None,
        journal_content=(
            "Stale-obsolete-directive guard: the directive named "
            "mark_plan_obsolete but no un-banked pending_review target remained "
            "to stamp; cleared it rather than firing the lever blind at net=+0."
        ),
        journal_trigger="stale_obsolete_directive_guard",
    )


def _dropped_directive_replay_run(ctx: GateContext) -> GateOutcome | None:
    """Re-offer the oldest still-live dropped fire-or-close directive (090).

    Lowest-precedence bridge: it fires only into an empty slot (``run_gate_chain``
    skips a directive gate while the slot is full), so it never pre-empts a
    fresher bridge — a dropped directive is only replayed when nothing more
    urgent claimed the slot this cycle. Liveness is re-verified at offer time
    through the merge-truth chokepoint (``is_pr_settled``) and ``git`` ancestry
    (``is_branch_merged_into_main``): an entry whose PR has since settled or whose
    SHA has landed is pruned, never re-offered. Zero SDK cost.
    """
    text = directive_ledger.replay_dropped_directive(
        ctx.agent.dropped_directives,
        is_pr_settled=lambda pr: ctx.agent.is_pr_settled(pr).settled,
        is_sha_landed=audit_checks.is_branch_merged_into_main,
        now=datetime.now(UTC).isoformat(),
    )
    if text is None:
        return None
    return GateOutcome(
        detail=text,
        log_message=(
            f"generate_task: dropped-directive replay gate re-offered a "
            f"previously-dropped fire-or-close directive — {text}"
        ),
        directive=text,
    )


# Precedence is list order: blocking gates first (a block pre-empts every
# bridge), then the directive bridges in their historical source order —
# the first bridge to fire wins the shared ``next_task_directive`` slot.
GATE_CHAIN: tuple[TaskGate, ...] = (
    TaskGate(
        name="wip_primacy",
        kind="block",
        enabled=lambda config: config.wip_primacy_enabled,
        run=_wip_primacy_run,
    ),
    TaskGate(
        name="no_repeat_failed_branch_target",
        kind="block",
        enabled=_always_enabled,
        run=_no_repeat_failed_branch_target_run,
    ),
    TaskGate(
        name="quiet_skip",
        kind="block",
        enabled=lambda config: config.quiet_skip_enabled,
        run=_quiet_skip_run,
    ),
    TaskGate(
        name="audit_stall",
        kind="block",
        enabled=lambda config: config.audit_stall_block_enabled,
        run=_audit_stall_run,
    ),
    TaskGate(
        name=_COST_RISK_GATE,
        kind="block",
        enabled=_always_enabled,
        run=_cost_risk_run,
    ),
    TaskGate(
        name="escalation_bridge",
        kind="block",
        enabled=_always_enabled,
        run=_escalation_bridge_run,
    ),
    TaskGate(
        name=_CONSECUTIVE_PARTIAL_GATE,
        kind="block",
        enabled=_always_enabled,
        run=_consecutive_partial_breaker_run,
    ),
    TaskGate(
        name="stale_obsolete_directive_guard",
        kind="directive",
        enabled=_always_enabled,
        run=_stale_obsolete_directive_run,
        rewrites=True,
    ),
    TaskGate(
        name="instrument_consumption",
        kind="directive",
        enabled=_always_enabled,
        run=_instrument_consumption_run,
    ),
    TaskGate(
        name="integration_cadence",
        kind="directive",
        enabled=_always_enabled,
        run=_integration_cadence_run,
    ),
    TaskGate(
        name="investigation_streak",
        kind="directive",
        enabled=lambda config: config.investigation_streak_enabled,
        run=_investigation_streak_run,
    ),
    TaskGate(
        name="investigation_rate",
        kind="directive",
        enabled=lambda config: config.investigation_rate_enabled,
        run=_investigation_rate_run,
    ),
    TaskGate(
        name="branchless_streak",
        kind="directive",
        enabled=_always_enabled,
        run=_branchless_streak_run,
    ),
    TaskGate(
        name="partial_construction",
        kind="directive",
        enabled=_always_enabled,
        run=_partial_construction_run,
    ),
    TaskGate(
        name="promotable_backlog_directive",
        kind="directive",
        enabled=lambda config: config.promotable_backlog_directive_enabled,
        run=_promotable_backlog_run,
    ),
    TaskGate(
        name="self_directed_success",
        kind="directive",
        enabled=_always_enabled,
        run=_self_directed_success_run,
    ),
    TaskGate(
        name="forced_read",
        kind="directive",
        enabled=_always_enabled,
        run=_forced_read_run,
    ),
    TaskGate(
        name="readmission_construction",
        kind="directive",
        enabled=lambda config: config.readmission_construction_directive_enabled,
        run=_readmission_construction_run,
    ),
    TaskGate(
        name="unloseable_streak",
        kind="directive",
        enabled=_always_enabled,
        run=_unloseable_streak_run,
    ),
    TaskGate(
        name="comfort_cluster",
        kind="directive",
        enabled=_always_enabled,
        run=_comfort_cluster_run,
    ),
    TaskGate(
        name="untouched_first_target",
        kind="directive",
        enabled=_always_enabled,
        run=check_untouched_first_target,
        rewrites=True,
    ),
    TaskGate(
        name="dropped_directive_replay",
        kind="directive",
        enabled=lambda config: config.dropped_directive_replay_enabled,
        run=_dropped_directive_replay_run,
    ),
)


def gate_order() -> list[tuple[str, GateKind]]:
    """Return ``(name, kind)`` pairs in chain precedence order.

    The single readable source of the gate taxonomy — consumed by the
    ``maestro list-gates`` CLI; CLAUDE.md points here instead of
    hand-maintaining the list.
    """
    return [(gate.name, gate.kind) for gate in GATE_CHAIN]


#: The post-call content gates, in the order
#: :func:`~maestro.loop.task_generation._vet_goal_content` applies them to a
#: candidate goal. Sibling to :data:`GATE_CHAIN` for the *post-call* family:
#: whereas the chain's blocking gates and directive bridges act *before* the
#: SDK call, these reject an individual goal *after* it. The literal below *is*
#: the membership — kept here (not inline in ``_vet_goal_content``) so every
#: surface reads one ordered declaration instead of hand-copying the names; the
#: binding test in ``tests/test_task_gates.py`` pins it to the applier's actual
#: behaviour so the two cannot drift.
CONTENT_GATE_ORDER: tuple[str, ...] = (
    "pr_mutation_goal",
    "terrain_exhaustion",
    "compound_scope",
    "concreteness",
    "phantom_premise",
    "failed_branch_target",
)


def content_gate_order() -> list[str]:
    """Return the post-call content-gate names in application order.

    The single readable projection of :data:`CONTENT_GATE_ORDER`, mirroring
    :func:`gate_order`'s role for the pre-call chain: consumed by the
    ``maestro list-gates`` CLI and by prose that would otherwise recite the
    membership. The gates are applied by
    :func:`~maestro.loop.task_generation._vet_goal_content` at *both* queue-
    mutation sites — the ``enqueue`` pass vets a new goal and the ``edit`` pass
    vets a rewrite — so read this, not a hand-copied list, for the current
    membership.
    """
    return list(CONTENT_GATE_ORDER)


# ---------------------------------------------------------------------------
# Investigation-observable post-call gate (task-selection actuator)
# ---------------------------------------------------------------------------
#
# Unlike the pre-call ``GATE_CHAIN`` entries, this gate runs on the *generated*
# enqueue entries after the SDK call, alongside the sibling post-call content
# gates ``_vet_goal_content`` applies (their membership lives in
# ``content_gate_order``, not a hand-copied list here) in
# ``generate_task``'s enqueue loop. It is the actuator for the reflex the
# self-model names ``delta-not-file``: fleeing an investigation I already
# understand to an *additive* artifact (a fresh markdown writeup, an
# always-green test file) instead of reading a net signal. Naming the reflex
# gave no traction on it; this fires at the phase where task shape is chosen —
# when a generated task is investigation-framed AND commits to creating a new
# artifact file WHILE naming no existing observable instrument signal to
# read/report, it steers the directive toward reading a named signal instead.

# A commitment to CREATE a new artifact file — the additive flee. A creation
# verb followed (within the same clause) by an artifact-document noun, or the
# explicit "new <file>" / "artifacts/<path>" forms. Deliberately does not list
# a bare "file" without a creation verb, so a net-diff edit to an existing file
# ("refactor the loop in maestro/foo.py") is not swept in. The verb suffixes are
# bounded to real inflections — open-ended ``\w*`` would over-match lookalike
# nouns/adjectives ("authorization", "creative", "product") that are not creation
# verbs, spuriously tripping the gate on an ordinary investigation goal.
_ARTIFACT_CREATION_RE = re.compile(
    r"\b(?:creat(?:e|es|ed|ing|ion)?|writ(?:e|es|ing|ten)?"
    r"|produc(?:e|es|ed|ing|tion)?|generat(?:e|es|ed|ing|ion)?"
    r"|draft(?:s|ed|ing)?|author(?:s|ed|ing)?)\b"
    r"[^.\n]{0,80}?"
    r"\b(?:file|artifact|report|document|doc|markdown|\.md|\.txt|\.rst|notes?"
    r"|summary|writeup|write-up|analysis)\b"
    r"|\bnew\s+(?:file|artifact|report|document|markdown|test\s+file)\b"
    r"|artifacts/",
    re.IGNORECASE,
)

# An existing observable instrument signal the task already commits to reading
# and reporting — the reframe the gate demands. When one is named the task is
# reading a net signal, not fleeing to a fresh artifact, so the gate stays
# silent. The examples the directive text names (jsonl ledger, diff-stat,
# gate-activation count, cost-gate value) plus the concrete ledger filenames.
_OBSERVABLE_SIGNAL_RE = re.compile(
    r"\.jsonl\b|\bledger\b|diff[\s-]?stat|git\s+diff|gate[\s_-]?activation"
    r"|cost[\s-]?gate|exit\s+code|task_history|usage\.jsonl|deed[\s-]?ledger"
    r"|remediation_tracker|version_archive",
    re.IGNORECASE,
)

# The reframe directive (within ``Consciousness.MAX_DIRECTIVE_CHARS``).
_INVESTIGATION_OBSERVABLE_DIRECTIVE = (
    "Reframe as reading a named observable: name which existing instrument "
    "signal (.jsonl ledger, diff-stat, gate-activation count, cost-gate value) "
    "you will read and report, not a new artifact file."
)

# The discharge-arm reframe directive (likewise within
# ``Consciousness.MAX_DIRECTIVE_CHARS``). Distinct from the goal-string
# reframe above: it fires on *recorded* evidence — the most recent completed task
# was an owner-stamped COSTUME (an investigation frame discharged as tracked
# construction), not just a goal-string guess.
_INVESTIGATION_DISCHARGE_COSTUME_DIRECTIVE = (
    "Your last task wore an investigation costume — framed read-only but "
    "discharged as tracked construction. Name the net signal you will read and "
    "report, or reframe the goal as the build it was."
)


def is_investigation_observable_flee(goal: str) -> bool:
    """Return ``True`` when *goal* is an investigation that flees to a new file.

    The losing shape this gate interrupts: a goal whose frame is a read-only
    investigation — decided by the frame-discharge owner's single frame
    predicate :func:`~maestro.frame_discharge.is_investigation_frame` (the same
    ``classify_task_shape`` frame the record-time verdict reads, so the choice
    gate and the record-time cap share one frame definition — the rival regex
    is retired) — **and** that commits to creating a new artifact file
    (:data:`_ARTIFACT_CREATION_RE`) **while** naming no existing observable
    instrument signal to read/report (:data:`_OBSERVABLE_SIGNAL_RE`). Naming a
    net signal, committing to a net diff of an existing file, or not being
    investigation-framed all return ``False``. Zero SDK cost.

    Because the frame test routes through ``classify_task_shape``'s shape
    *precedence*, an investigation-verb goal that a higher-precedence
    deliverable-bearing shape claims first (``github_issue`` on ``#NNN``,
    ``measurement`` on ``audit``/``count``, ``construction`` on a build verb) is
    intentionally *not* investigation-framed and stays uncaught here: those
    shapes carry a concrete, loseable deliverable, so they are not the loseless
    investigation costume this gate interrupts. The record-time ``COSTUME``
    verdict reads the same frame set, so those goals fall outside its domain too
    — the narrowing is uniform across both faces of the frame-discharge owner,
    not a coverage gap unique to this gate.
    """
    if not goal:
        return False
    if not is_investigation_frame(goal):
        return False
    if not _ARTIFACT_CREATION_RE.search(goal):
        return False
    return not _OBSERVABLE_SIGNAL_RE.search(goal)


def investigation_observable_gate(
    goals: Sequence[str],
    config: Config,
    consciousness: Consciousness,
    recent: list[TaskRecord],
    gate_activations: GateActivationBackend,
) -> bool:
    """Steer an investigation→new-artifact task toward reading a named signal.

    Post-call actuator dispatched from ``generate_task``'s enqueue loop over the
    surviving new entries. Gated by ``[loop]
    investigation_observable_gate_enabled``. When enabled and the directive slot
    is empty, it fires on either of two arms and returns ``True``, appending
    exactly one ``gate_activations.jsonl`` row:

    - **Goal-string flee** — some *goal* is
      :func:`is_investigation_observable_flee`-shaped: it fills
      ``next_task_directive`` with :data:`_INVESTIGATION_OBSERVABLE_DIRECTIVE`.
    - **Discharge evidence** — no goal is flee-shaped, but the most recent
      completed record in *recent* is an owner-stamped ``COSTUME`` (read through
      :func:`~maestro.frame_discharge.measure_frame_discharge_costumes` over
      ``recent[-1:]``, which consults the owner verdict rather than re-deriving
      the discharge relation): it fills ``next_task_directive`` with
      :data:`_INVESTIGATION_DISCHARGE_COSTUME_DIRECTIVE`, naming the costume
      ``task_id`` in the ledger detail. Keying on the most-recent record makes
      this arm self-extinguishing — one subsequent clean investigation (or
      honest construction) drops the costume out of tail position and silences
      it.

    Strict no-op (returns ``False``, writes nothing) when the flag is off, the
    slot is already occupied (a pre-set directive is never overwritten — a
    concreteness re-arm or contemplation steer keeps precedence), no goal is
    flee-shaped, and the latest record is not a costume. Zero SDK cost.

    The empty-slot check and the fill share one
    :meth:`~maestro.consciousness.Consciousness.mutate_self_model` transaction,
    so the check reads the live directive and the write preserves every field a
    concurrent writer committed (#4040).
    """
    if not config.investigation_observable_gate_enabled:
        return False
    flee_goal = next(
        (g for g in goals if is_investigation_observable_flee(g)), None,
    )
    if flee_goal is not None:
        directive = _INVESTIGATION_OBSERVABLE_DIRECTIVE
        detail = flee_goal[:200]
    else:
        # Discharge-evidence arm: no goal-string flee among the new goals, but
        # the single most-recent completed record wore an investigation costume.
        costume_ids = measure_frame_discharge_costumes(recent[-1:])
        if not costume_ids:
            return False
        directive = _INVESTIGATION_DISCHARGE_COSTUME_DIRECTIVE
        detail = f"discharge-costume: {costume_ids[0]}"[:200]

    def _fill_empty_slot(live: SelfModel) -> bool:
        if live.next_task_directive:
            return False
        live.next_task_directive = directive
        return True

    if not consciousness.mutate_self_model(_fill_empty_slot):
        return False
    log_gate_activation(
        gate_activations,
        gate="investigation_observable",
        action="directive_set",
        detail=detail,
    )
    return True
