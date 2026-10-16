"""Persistent operational state — task history and evolution plans."""

from __future__ import annotations

import functools
import hashlib
import json
import logging
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, get_args, runtime_checkable
from uuid import uuid4

from maestro.consciousness import (
    RESTATEMENT_THRESHOLD,
    _content_tokens,
    detect_restatement,
)
from maestro.fileutil import (
    SkipBudget,
    append_jsonl_row,
    append_jsonl_row_locked,
    atomic_write,
    file_lock,
    iter_jsonl_reverse,
)
from maestro.lever_capture import (
    JsonlLeverAttemptsLedger,
    LeverAttemptsBackend,
    fire_and_capture,
    verify_obsolete_persisted,
)
from maestro.plan_feedback import (
    FEEDBACK_RESPONSE_MAX_CHARS,
    FEEDBACK_SOURCES,
    FEEDBACK_TEXT_MAX_CHARS,
    MAX_ADDRESSED_FEEDBACK_KEPT,
    MAX_UNADDRESSED_FEEDBACK,
    PlanFeedback,
    coerce_feedback,
    truncate_hash_prefix,
    unaddressed_feedback,
)
from maestro.plan_grounding import GroundingProbe
from maestro.pr_facts import LIVE_STATUSES, LivePlan, select_live_plans
from maestro.search import bm25_rank
from maestro.types import (
    ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS,
    ACCEPTANCE_CRITERIA_MAX_ITEMS,
    LEGACY_PRIORITY_REMAP,
    Category,
    DocStatus,
    DocType,
    EvolutionProposalStatus,
    PlanStatus,
    Priority,
    ProposalStatus,
    QuestionDisposition,
    RejectionCategory,
    StepStatus,
    TaskOutcome,
    TaskSource,
    Verdict,
    remap_plan_field_aliases,
    validate_acceptance_criteria_count,
)

if TYPE_CHECKING:
    from maestro.audit_checks import AuditFinding

logger = logging.getLogger("maestro")

_UNSET = object()  # sentinel to distinguish "not provided" from None


class DuplicatePlanError(ValueError):
    """Raised when a plan with the same plan_id already exists.

    Subclasses :class:`ValueError` for backward compatibility.  Callers that
    need to treat idempotent retries differently from hard errors can catch
    this specific subclass instead of the base ``ValueError``.
    """


class DuplicateProposalError(ValueError):
    """Raised when a publication proposal with the same proposal_id already exists.

    Mirrors :class:`DuplicatePlanError` so idempotent-retry callers can
    distinguish an already-seen proposal from a schema violation.
    """


@dataclass
class PlanStep:
    step_id: str
    description: str
    status: StepStatus = "pending"
    result: str | None = None
    depends_on: list[str] = field(default_factory=list)


@dataclass
class TaskRecord:
    task_id: str
    goal: str
    plan: list[PlanStep]
    outcome: TaskOutcome
    result_summary: str
    errors: list[str]
    duration_secs: float
    timestamp: str
    source: TaskSource = "external"
    branch: str | None = None
    key_findings: str | None = None
    artifacts_dir: str | None = None
    replans: int = 0
    linked_question: str | None = None
    # ``origin/<pr_branch>`` head SHA read at the START of a ``Remediate PR
    # #<n>:`` lap — after workspace preparation, before step execution, so
    # before any in-lap push (#4567) — the delivery watermark for that lap. ``None`` for
    # non-remediation tasks and for legacy records written before the field
    # existed. The costume-signature gate discounts a counted lap whose
    # ``pr_head_sha`` is a *proper ancestor* of the PR's current origin head:
    # a head that advanced past the lap proves Tend pushed that lap's
    # resolution, so it was real work, not a costume (#2595).
    pr_head_sha: str | None = None
    # ``origin/main`` head SHA the remediation lap raced against, read
    # post-session (after step execution, unlike the lap-start ``pr_head_sha``,
    # #4567) when a ``Remediate PR #<n>:`` lap built this record
    # (proposal 099 step 1). ``None`` for non-remediation tasks and for legacy
    # records written before the field existed (the same append-only optional
    # pattern as ``pr_head_sha`` / ``push_delivered`` / ``merge_clean``). Pure
    # data plumbing for now: it records the base the resolution was made against
    # so a later increment can tell a frozen world (base unchanged across laps)
    # from a lost race (base moved underneath a correct resolution); no stop
    # condition reads it yet.
    base_head_sha: str | None = None
    # Per-criterion MET/UNMET verdicts from ``_assess_criteria_outcome``;
    # each item is ``{"criterion": str, "verdict": "MET"|"UNMET",
    # "evidence": str}``. ``None`` when no acceptance criteria were assessed
    # or the assessment fell back to the step-based outcome.
    criteria_verdicts: list[dict[str, str]] | None = None
    # Merge-readiness probe captured at task completion via
    # ``contributions.check_merge_clean`` while the kept branch is freshest:
    # ``True`` clean merge into ``origin/main``, ``False`` conflicts, ``None``
    # unknown/not-probed (the default for non-success runs, branchless runs,
    # and legacy rows). Records integration debt at the moment it is cheapest
    # to measure; feeds branch-promotion triage. See [docs/self-evolution.md].
    merge_clean: bool | None = None
    # Aggregated PR-readiness snapshot for a ``Remediate PR #<n>:`` lap — the
    # ``dataclasses.asdict`` form of the :class:`~maestro.pr_facts.PRReadiness`
    # observed when this lap's outcome was recorded (proposal 089 step 4).
    # ``None`` for non-remediation tasks and legacy records written before the
    # field existed (the same append-only optional pattern as ``criteria_verdicts``
    # / ``merge_clean``). The consecutive-no-progress lap sensor
    # (``measure_no_progress_laps``) reads it to tell a lap that cleared a fresh
    # blocker from one that ground in place.
    pr_readiness_snapshot: dict[str, object] | None = None
    # Delivery truth for a ``Remediate PR #<n>:`` lap's §5-push (proposal 091
    # step 1). ``True`` the push reached ``origin/<pr_branch>`` (after at most
    # one fetch+retry), ``False`` it failed both attempts, ``None`` for
    # non-remediation tasks, checkout-fallback runs, and legacy records (the
    # same append-only optional pattern as ``criteria_verdicts`` /
    # ``merge_clean`` / ``pr_readiness_snapshot``). Lets the FORCED-QUIET sensor
    # and the marker split tell "I delivered but the human has not merged yet"
    # from "I never delivered."
    push_delivered: bool | None = None
    # ``str(exc)`` of the push failure that left ``push_delivered`` ``False`` —
    # the retry's exception message, surfaced into ``result_summary`` so the
    # failure is visible in prior-task context. ``None`` when the push
    # succeeded, was never attempted, or on legacy records.
    push_error: str | None = None
    # Conscious-access citation provenance (proposal 130 — data model only, no
    # runtime read yet): which conscious *moment* the task-generation decision
    # that produced this task saw, and which admitted workspace *item IDs* it
    # declared it used. Pure provenance — nothing populates them yet and no stop
    # condition reads them (the producer and the fail-closed citation validator
    # land as separate increments). ``None`` (never cited / legacy row) is
    # distinct from ``[]`` (saw the moment, cited no item). The same append-only
    # optional pattern as ``criteria_verdicts`` / ``base_head_sha``: ``asdict``
    # serialises them and ``_parse_task_dict``'s ``fields(TaskRecord)`` filter
    # loads a legacy row with the defaults (no serialiser change), while
    # ``_parse_task_dict`` coerces a malformed ``workspace_item_ids`` (a non-list
    # collapses to ``None``, non-string members drop) exactly as
    # ``_parse_plan_line`` does for the plan side.
    workspace_moment_id: str | None = None
    workspace_item_ids: list[str] | None = None


@dataclass
class TaskCheckpoint:
    """Crash-recovery snapshot of an in-flight task's step-group progress.

    Written after each completed step group (and each re-plan splice) by
    ``MaestroAgent.run()`` to ``{memory_dir}/checkpoints/<task_id>.json`` via
    ``atomic_write`` — one file per task, overwritten each group, never
    appended. On entry ``run()`` reloads it when the task branch still
    resolves and resumes from the last completed group; the file is cleared
    once a ``TaskRecord`` is written.  Each ``PlanStep`` already carries
    ``status``/``result``, so completed, failed, and pending steps are all
    encoded and ``completed_ids`` is recoverable as
    ``{s.step_id for s in steps if s.status == "done"}`` — there is no
    separate step-index field.  ``replans`` carries the re-plan budget across
    a resume so a resumed task does not silently regain a fresh budget.

    ``linked_question`` and ``acceptance_criteria`` carry the run's
    classification context so ``resume_task`` can re-anchor the resumed run:
    without them a resumed task would lose criteria-anchored MET/UNMET
    outcome classification (falling back to raw step counts) and would write
    no ``QuestionResolution`` for the open question it was probing.

    The run's ``context`` (the free-text task context passed to
    ``planner.decompose``) is intentionally *not* persisted: ``resume_task``
    skips the initial decompose entirely (the steps are already here), so the
    only consumer left on the resume path is a ``planner.replan`` triggered by
    a fresh step failure *during* the resumed run, which then runs with empty
    context — an accepted fidelity gap, not a missing field.
    """

    task_id: str
    goal: str
    source: TaskSource = "external"
    branch: str | None = None
    steps: list[PlanStep] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    all_output: list[str] = field(default_factory=list)
    # Executed Bash command strings this run (the analog of ``all_output`` for
    # tool inputs) — carried across a resume so the red-read bind's
    # command-record guard (``bind_ci_failing_verdict_to_red_read``) survives a
    # crash mid-task. A pre-field checkpoint deserializes to the default ``[]``
    # (load filters unknown keys), so an old snapshot simply reads as "no
    # commands recorded" rather than crashing.
    executed_commands: list[str] = field(default_factory=list)
    # Diagnostic-only: persisted for inspection but never read back on resume.
    # ``run()`` recomputes ``artifacts_dir`` from ``config.artifacts_dir`` and
    # the (reused) ``task_id``, so the stored value is informational.
    artifacts_dir: str | None = None
    replans: int = 0
    timestamp: str = ""
    linked_question: str | None = None
    acceptance_criteria: list[str] = field(default_factory=list)
    # Conscious-access citation provenance (proposal 130) carried across a
    # crash-resume. By resume time the queued task is gone, so ``resume_task``
    # restores these from the checkpoint to keep the resumed run's
    # ``TaskRecord`` citing the same moment the original generation decision
    # saw. ``None`` on legacy checkpoints written before the fields existed
    # (``load_checkpoint`` filters unknown keys), and ``None`` (never cited)
    # stays distinct from ``[]`` (saw the moment, cited no admitted item).
    workspace_moment_id: str | None = None
    workspace_item_ids: list[str] | None = None


@dataclass
class EvolutionPlan:
    plan_id: str
    title: str
    category: Category
    rationale: str
    proposed_changes: list[str]
    files_affected: list[str]
    expected_impact: str
    acceptance_criteria: list[str] = field(default_factory=list)
    priority: Priority = "capability"
    # Target-issue linkage driving the eventual ``Fixes #N``. Excluded from
    # ``compute_plan_content_hash`` — its only downstream effect is visible
    # under the human-in-the-loop merge review; see that docstring.
    issue_number: int | None = None
    # Nominated kept task branch used as the implementation base (``None`` for
    # fresh-from-``main``). Provenance/implementation linkage, not reviewable
    # change content — excluded from ``compute_plan_content_hash`` so
    # re-targeting the base (e.g. the fallback on branch conflict) never
    # invalidates an approved plan's hash; see that docstring.
    source_task_id: str | None = None
    status: PlanStatus = "pending_review"
    created_at: str = ""
    reviewed_at: str | None = None
    # Set by ``Memory.refine_plan`` to the UTC ISO timestamp of the most
    # recent curator refinement; ``None`` until a plan is first refined.
    # Lets a reviewer see when a pending plan's body last changed.
    refined_at: str | None = None
    # Round-robin cursor for :func:`select_curation_target`. Stamped by
    # ``Memory.touch_plan_curated`` to the UTC ISO timestamp of the most
    # recent curate pass that *left the plan* ``pending_review`` (keep or a
    # deterministic skip). Never-curated plans (``None`` → sort key ``""``)
    # rank ahead of already-curated ones, so a kept plan yields its slot to
    # the rest of the backlog instead of being re-picked forever. Excluded
    # from ``compute_plan_content_hash`` (bookkeeping, not reviewable content,
    # exactly like ``refined_at``).
    last_curated_at: str | None = None
    # Proposal 155 step 6: ``compute_curation_fingerprint`` as of the last
    # curate pass that left the plan ``pending_review``; the selector skips
    # the plan while the live fingerprint still equals it. Bookkeeping,
    # excluded from ``compute_plan_content_hash``.
    curated_hash: str | None = None
    # Proposal 145: reviewer "request changes" ledger, written only through
    # ``Memory.add_plan_feedback``. Bookkeeping, not reviewable content —
    # excluded from ``compute_plan_content_hash`` like ``refined_at``.
    reviewer_feedback: list[PlanFeedback] = field(default_factory=list)
    rejection_reason: str | None = None
    pr_url: str | None = None
    rejection_learned: bool = False
    last_pr_reminder_at: str | None = None
    pr_reminder_count: int = 0
    last_conflict_resolution_attempt_at: str | None = None
    # Proposal 099: the ``origin/main`` SHA the conflict-cooldown anchor above was
    # stamped against. ``_in_conflict_cooldown`` reads it so a moved base
    # invalidates the cooldown exactly as a moved head does (#2821). ``None`` on
    # legacy rows and whenever the base could not be resolved.
    last_conflict_resolution_base_sha: str | None = None
    # Proposal 137: the rest of the signature the conflict-cooldown anchor above
    # was stamped against — the PR branch's ``origin/<branch>`` head and the CI
    # rollup conclusion the tend walk read that lap. With
    # ``[loop] cooldown_signature_keying_enabled`` on, ``_in_conflict_cooldown``
    # releases the cooldown when any component has provably changed, generalising
    # the base-SHA release above. Plain ``str | None`` fields, so ``_parse_plan_line``
    # absorbs them with no migration and legacy rows take ``None``; bookkeeping
    # only — ``compute_plan_content_hash``'s positive allowlist excludes them, so an
    # approved plan's review binding is unchanged.
    last_conflict_resolution_head_sha: str | None = None
    last_conflict_resolution_ci_conclusion: str | None = None
    # Proposal 137: how many tend laps have already stamped the conflict-cooldown anchor
    # against this same signature. Under ``[loop]
    # cooldown_signature_keying_enabled`` the cooldown window is
    # ``min(base * 2 ** attempts, ceiling)``, so a lap that changed nothing (a
    # noop, a failed push, a route that made no progress) re-routes after the
    # short first rung instead of holding the flat ceiling, while a
    # persistently-broken head converges back to it. Reset to ``0`` whenever a
    # signature component provably changes. A rung is consumed only by a lap
    # the cooldown genuinely governed, measured against a concluded rollup on a
    # measured head; a bypassed, pushing or unmeasurable lap leaves the count
    # untouched. Bookkeeping — excluded from
    # ``compute_plan_content_hash`` by its positive allowlist, absorbed by
    # ``_parse_plan_line`` with no migration, ``0`` on legacy rows.
    conflict_resolution_signature_attempts: int = 0
    # Proposal 124, step 9: an ``INHERITED`` convergence terminal holds the plan
    # ``approved`` instead of demoting it, keyed by the ``origin/main`` head the
    # base red was stamped against — mirroring
    # ``last_conflict_resolution_base_sha``'s base-SHA-keyed cooldown shape.
    # ``_implement_approved_plans`` declines to re-buy the same red while the base
    # has not moved (both SHAs truthy and equal) and re-admits the plan once it
    # has. ``inherited_hold_count`` bounds the hold: after
    # ``max_implementation_attempts - 1`` holds the next ``INHERITED`` terminal
    # demotes as today. Both are bookkeeping — excluded from
    # ``compute_plan_content_hash`` by its explicit allowlist — so a held plan's
    # approval hash is unchanged. ``None`` / ``0`` on legacy rows and whenever the
    # base could not be resolved.
    inherited_hold_main_sha: str | None = None
    inherited_hold_count: int = 0
    # Proposal 133: the approved-plan selector's environment hold. A non-``SOUND``
    # ``gate_environment`` verdict means the local gate cannot reach a verdict at
    # all, so implementing this plan would only buy a red no diff can clear;
    # ``_implement_approved_plans`` stamps the verdict's *fingerprint* here instead
    # of demoting the plan, leaving ``status`` and ``failure_count`` untouched. Keyed
    # to the fingerprint rather than the clock so the hold tracks the fact: it moves
    # when the fact moves, and the next ``SOUND`` verdict releases the plan with no
    # explicit release step. Bookkeeping — excluded from
    # ``compute_plan_content_hash`` by its explicit allowlist — so a held plan's
    # approval hash is unchanged. ``None`` on legacy rows and once released.
    environment_hold_fingerprint: str | None = None
    # Proposal 131: attempt-disposition persistence. ``resume_count`` is the
    # continuation-budget counter the RESUME arm increments each time it continues
    # a progressing attempt from its existing branch; ``resume_failing_identities``
    # is the sorted failing-identity baseline that arm's progress check measures
    # the next attempt against; and ``resume_branch_head`` is the preserved
    # evolution-branch HEAD ``implement`` confirms the branch is the one the prior
    # attempt left before continuing from it. ``blocked_question_id`` is the
    # ``ask_thomas`` message id the ``ASK_HUMAN`` arm keys a ``blocked`` plan to,
    # so the human's reply can thread back (via its ``reply_to``) and re-admit it;
    # ``blocked_answer`` is the human's reply text that re-admission stamps onto
    # the plan and ``implement`` injects into the next implementation prompt, so
    # the writer acts on Thomas's decision instead of restarting blind. All are
    # bookkeeping — excluded from ``compute_plan_content_hash`` by its explicit
    # content-field allowlist, so an approved plan's review-binding hash is
    # unchanged. ``0`` / ``[]`` / ``None`` on legacy rows.
    resume_count: int = 0
    resume_failing_identities: list[str] = field(default_factory=list)
    resume_branch_head: str | None = None
    blocked_question_id: str | None = None
    blocked_answer: str | None = None
    last_quality_fix_attempt_at: str | None = None
    # Proposal 137: the signature the quality-fix cooldown anchor above was
    # stamped against — the PR branch's ``origin/<branch>`` head, the
    # ``origin/main`` base, and the CI rollup conclusion the tend walk read that
    # lap. Under ``[loop] cooldown_signature_keying_enabled``
    # ``_in_quality_fix_cooldown`` releases on the head and CI components only —
    # the base is kept as a record but never releases, since an ``origin/main``
    # advance says nothing about whether this PR's own red changed. Otherwise it
    # mirrors the conflict cluster above (the quality-fix cooldown, written later
    # as an explicit mirror, carried none of the conflict cooldown's ad-hoc
    # releases). Same two facts hold: no loader edit is needed and the content
    # hash is unaffected.
    last_quality_fix_head_sha: str | None = None
    last_quality_fix_base_sha: str | None = None
    last_quality_fix_ci_conclusion: str | None = None
    # Proposal 137: how many tend laps have already stamped the quality-fix cooldown anchor
    # against this same signature. Under ``[loop]
    # cooldown_signature_keying_enabled`` the cooldown window is
    # ``min(base * 2 ** attempts, ceiling)``, so a lap that changed nothing (a
    # noop, a failed push, a route that made no progress) re-routes after the
    # short first rung instead of holding the flat ceiling, while a
    # persistently-broken head converges back to it. Reset to ``0`` whenever a
    # signature component provably changes. A rung is consumed only by a lap
    # the cooldown genuinely governed, measured against a concluded rollup on a
    # measured head; a bypassed, pushing or unmeasurable lap leaves the count
    # untouched. Bookkeeping — excluded from
    # ``compute_plan_content_hash`` by its positive allowlist, absorbed by
    # ``_parse_plan_line`` with no migration, ``0`` on legacy rows.
    quality_fix_signature_attempts: int = 0
    failure_count: int = 0
    last_failure_at: str | None = None
    last_failure_reason: str | None = None
    # Populated only when ``status == "obsolete"``: free-form one-line
    # explanation set by ``mark_plan_obsolete`` when the agent prunes
    # a stale ``pending_review`` plan. Distinct from ``rejection_reason``
    # so the rejection-learning cycle never confuses self-pruning with
    # a human rejection.
    obsolete_reason: str | None = None
    # ``True`` only when the submission door (``submit_for_review``) refused
    # this freshly minted plan and recorded it ``obsolete`` in the same call —
    # a refused mint, not the retirement of a plan that already existed, so
    # audit rule 18 never credits it as an owned prune (#4157). Bookkeeping —
    # excluded from ``compute_plan_content_hash`` by its positive allowlist,
    # absorbed by ``_parse_plan_line`` with no migration, ``False`` on legacy
    # rows.
    refused_at_submission: bool = False
    # SHA-256 over the plan's content fields (see
    # :func:`compute_plan_content_hash`). Stamped at every content write
    # boundary (``save_evolution_plan``, ``refine_plan``,
    # ``update_plan_proposed_changes``); ``""`` only on legacy rows that
    # predate the field — loaders backfill it in memory. Sentinel ``""``
    # rather than ``Optional`` keeps call-site usage flat (proposal 021).
    plan_content_hash: str = ""
    # Snapshot of ``plan_content_hash`` taken when the plan was last sent
    # for human review (Telegram / dashboard notification). Approval
    # callbacks compare a fresh content hash against this snapshot and
    # refuse the transition on drift. ``""`` means the plan was never
    # notified under hash binding (legacy rows) — verifiers skip the
    # drift check in that case.
    notified_content_hash: str = ""
    # Delivered head SHA awaiting CI-green confirmation for the
    # "ready for re-review" ping. Set on push-delivery in
    # ``EvolutionEngine.tend_evolution_prs``; cleared on notify /
    # CI-failure / head-staleness. ``None`` means no ping is pending.
    ready_for_review_pending_sha: str | None = None
    # Multi-phase planning linkage (proposal 098, phase 1 — data model only,
    # no behavior change). ``parent_plan_id`` is the ``proposal_id`` of the
    # parent ``EvolutionProposal`` when this plan is one *slice* of an ordered
    # multi-phase chain; ``None`` for a standalone one-shot plan (the legacy
    # default, so every pre-existing row loads unchanged). ``phase_index`` is
    # the 0-based position of this slice within its parent's ordered slice list
    # (``0`` for a standalone plan). Both are structural linkage, deliberately
    # excluded from ``compute_plan_content_hash`` (like ``status``/timestamps),
    # so linking an already-approved plan into a proposal never invalidates its
    # approval hash.
    parent_plan_id: str | None = None
    phase_index: int = 0
    # Premise-verification provenance (proposal 114, phase 1 — data model only,
    # no runtime read yet): the tree-reading probe this plan must clear before it
    # spends a human approval. ``None`` for a plan carrying no probe (the legacy
    # default, so every pre-existing row loads unchanged). Deliberately excluded
    # from ``compute_plan_content_hash`` — the hash is an explicit allowlist, so a
    # new field stays out by construction, exactly like ``source_task_id``; the
    # probe is verification provenance, not reviewable change content.
    grounding: GroundingProbe | None = None
    # Conscious-access citation provenance (proposal 130 — data model only, no
    # runtime read yet): which conscious *moment* the plan-proposal decision saw
    # and which admitted workspace *item IDs* it declared it used.
    # ``workspace_moment_id`` is the opaque moment id; ``workspace_item_ids`` is
    # the list of admitted ``item_id``s cited. Both are pure provenance — nothing
    # populates them yet and no stop condition reads them (the producer and the
    # fail-closed citation validator land as separate increments). ``None`` (never
    # cited / legacy row) is deliberately distinct from ``[]`` (the decision saw
    # the moment but cited no item). Excluded from ``compute_plan_content_hash``
    # by that function's explicit allowlist, so an already-approved plan's review
    # hash is provably unchanged when these fields appear — exactly like
    # ``grounding`` / ``source_task_id``.
    workspace_moment_id: str | None = None
    workspace_item_ids: list[str] | None = None
    # Planner read provenance (proposal 143 — data model only, nothing stamps it
    # yet): the repo-relative paths and symbols the planner read while authoring
    # this plan, feeding ``measure_planning_grounding``'s read-coverage figure.
    # ``None`` (no reads recorded / legacy row) is deliberately distinct from
    # ``[]`` (recorded, read nothing). Excluded from ``compute_plan_content_hash``
    # by its explicit allowlist, like ``workspace_item_ids``.
    grounded_reads: list[str] | None = None
    # Generator reality-grounding declarations (proposal 136): the measured
    # problem id the plan addresses (``audit:`` / ``sensor:`` / ``rejection:``)
    # and the names it will newly define, cleared against the reality inventory
    # at the submission door. ``None`` (undeclared / legacy row) stays distinct
    # from ``[]`` (declared, defines nothing new). Provenance, excluded from
    # ``compute_plan_content_hash`` by its explicit allowlist.
    problem_ref: str | None = None
    new_capability_names: list[str] | None = None


def _norm_hash_text(value: object) -> str:
    """Whitespace-normalise a string field for hashing; non-str → ``""``.

    Legacy JSONL rows can carry ``null`` for nominally-``str`` fields
    (``_parse_plan_line`` does not coerce string fields), and the hash
    is computed on exactly those rows at load-time backfill — so the
    canonicalisation must not assume the annotation holds.
    """
    return value.strip() if isinstance(value, str) else ""


def compute_plan_content_hash(plan: EvolutionPlan) -> str:
    """Return the canonical SHA-256 content hash for *plan*.

    Covers exactly the fields a reviewer approves: title, rationale,
    proposed changes, files affected, acceptance criteria, expected
    impact, category, and priority. Mutable bookkeeping (status,
    timestamps, PR/rejection fields, reminder counters, and the hash
    fields themselves) is excluded so a status transition never changes
    the hash. Two non-bookkeeping fields are deliberately excluded too:
    ``issue_number`` is
    target-issue linkage whose only downstream effect — the PR's
    ``Fixes #N`` — is independently visible under the human-in-the-loop
    merge review; and ``source_task_id`` is implementation-base linkage
    (the nominated task branch to build on) that may be re-targeted after
    approval, so binding it would false-mismatch an approved plan's hash.
    ``reviewer_feedback`` is bookkeeping too: new feedback must never
    invalidate the hash a reviewer is about to approve.
    This is the single canonicalisation point — every stamping and
    verification site must call it (proposal 021; proposal 023 consumes
    the same hash in its HMAC payload).
    """
    fields_: list[object] = [
        _norm_hash_text(plan.title),
        _norm_hash_text(plan.rationale),
        list(plan.proposed_changes),
        list(plan.files_affected),
        list(plan.acceptance_criteria),
        _norm_hash_text(plan.expected_impact),
        plan.category,
        plan.priority,
    ]
    canonical = json.dumps(
        fields_,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_curation_fingerprint(plan: EvolutionPlan) -> str:
    """Return the curation fingerprint for *plan* (proposal 155 step 6).

    SHA-256 over the reviewable content hash plus the sorted ids of every
    ``reviewer_feedback`` entry, addressed or not: a content edit or a new
    feedback entry moves it, while ``mark_feedback_addressed`` (which only
    stamps an existing entry) does not, so serving feedback costs no extra
    curate call.
    """
    canonical = json.dumps(
        [
            compute_plan_content_hash(plan),
            sorted(f.feedback_id for f in plan.reviewer_feedback),
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Default directional-overlap cutoff for the cluster-level themed-restatement
# guard (:meth:`Memory.find_theme_cluster_pending_plans`). A pending plan
# joins the candidate's theme cluster when more than this fraction of the
# candidate's dominant content tokens also appear in that plan. Chosen above
# the pairwise ``RESTATEMENT_THRESHOLD`` Jaccard so cluster membership requires
# strong thematic containment, not merely incidental vocabulary overlap.
THEME_CLUSTER_FRACTION: float = 0.6

# Minimum dominant-token count a candidate must have before the directional
# containment metric (``|shared| / |candidate|``) is trusted. With a tiny token
# set the denominator is so small that a couple of *incidental* shared words
# clear :data:`THEME_CLUSTER_FRACTION` (2-of-3 = 0.67), so an unrelated large
# plan that merely happens to contain those words would match — silently
# landing a genuinely-novel terse plan ``obsolete`` without human review. Unlike
# the symmetric-Jaccard sibling guards (``find_pending_plan_cluster``,
# ``find_duplicate_pending_plan``), directional containment lacks that built-in
# size correction, so a candidate below this floor is treated as having no
# trustworthy theme signature and never clusters.
THEME_CLUSTER_MIN_CANDIDATE_TOKENS: int = 4


def is_plan_restatement(
    title: str,
    description: str,
    existing_pending: list[EvolutionPlan],
    threshold: float = RESTATEMENT_THRESHOLD,
) -> EvolutionPlan | None:
    """Return the pending plan that *title*+*description* restates, else ``None``.

    Reuses the content-word Jaccard machinery behind
    :func:`maestro.consciousness.detect_restatement` at *threshold* (default
    :data:`~maestro.consciousness.RESTATEMENT_THRESHOLD`, 0.5) — the same
    accepted, zero-LLM-cost invariant that protects self-model list items.
    A candidate plan whose combined title and description overlaps an
    existing plan's combined title and rationale at or above the threshold
    is treated as a restatement.

    The predicate compares exactly the corpus it is handed — every plan in
    *existing_pending*, whatever its status. Which plans are eligible to be
    restated is the caller's decision, not this function's.
    """
    candidate = f"{title}\n{description}"
    for plan in existing_pending:
        existing_text = f"{plan.title or ''}\n{plan.rationale or ''}"
        if detect_restatement(candidate, [existing_text], threshold) is not None:
            return plan
    return None


# Plan statuses whose ``source_task_id`` citation makes a task branch "live"
# — work still in flight that has already claimed the branch: an un-ruled
# proposal (``pending_review``), an approved-but-not-yet-implemented plan
# (``approved``, queued for capped ``_implement_approved_plans`` — a
# still-open claim that can span several iterations), or a plan landed behind
# an open PR (``implemented``). A branch cited only by a terminal status
# (``rejected``/``failed``/``obsolete``, or the landed-and-done ``merged``) is
# free to re-cite: that attempt did not leave an open claim. This mirrors the
# canonical live-work corpus exactly, so it reuses its single named owner
# :data:`~maestro.pr_facts.LIVE_STATUSES` rather than re-listing the statuses
# inline (keeping the re-cite guard aligned if that set ever changes).
RECITE_LIVE_STATUSES: frozenset[str] = frozenset(LIVE_STATUSES)

#: The only plan statuses :meth:`Memory.archive` may rotate to cold storage —
#: the single named owner of "which plan history is finished". Derived as the
#: complement of every status that still owes work: the canonical live set
#: :data:`~maestro.pr_facts.LIVE_STATUSES` (``pending_review`` awaiting a human,
#: ``approved`` queued for an implementation slot, ``implemented`` behind an open
#: PR), the two deliberately-inert dormant statuses ``sliced`` (a held slice of a
#: multi-PR ``EvolutionProposal``) and ``blocked`` (paused on a human question,
#: re-admitted when the reply threads back), and ``failed`` — retry-eligible
#: while ``failure_count < max_implementation_attempts``, a bound ``Memory``
#: cannot read itself and so is handed by the caller (see
#: :func:`_plan_row_archivable`), leaving a failed row hot until that budget is
#: provably spent. Two members are themselves conditional in
#: :func:`_plan_row_archivable`: ``rejected`` rotates only once
#: ``rejection_learned`` is set (``_process_rejected_plans`` reads the hot tier),
#: and a retired ``rejected``/``obsolete`` row whose PR is still open — or whose
#: PR state is unknown — stays hot for Tend. Membership is an
#: allowlist, so a status added to :data:`~maestro.types.PlanStatus` defaults to
#: *hot* (kept, and surfaced to its consumers) rather than to silent deletion.
ARCHIVABLE_PLAN_STATUSES: frozenset[str] = frozenset(
    {"merged", "rejected", "obsolete"}
)

#: The :class:`EvolutionProposal` statuses that no longer own their slice chain.
#: A plan row whose ``parent_plan_id`` names a proposal *outside* this set stays
#: hot even when its own status is archivable, so ``get_slices_for_proposal`` can
#: still reconstruct the ordered chain the sequencing arm walks. Named as the
#: *finished* set rather than the live one for the same reason
#: :data:`ARCHIVABLE_PLAN_STATUSES` is an allowlist: a value later added to
#: ``EvolutionProposalStatus`` then reads as unfinished, holding its slices hot
#: rather than silently shedding them.
FINISHED_PROPOSAL_STATUSES: frozenset[str] = frozenset(
    {"complete", "abandoned"}
)

#: The :data:`~maestro.types.ProposalStatus` values that no longer reach
#: ``Publisher._publish``'s hot-tier task lookup. A task a publication proposal
#: *outside* this set cites as ``task_id`` stays hot so its artifacts dir still
#: resolves on a later approval. An allowlist like the two above: a status later
#: added to ``ProposalStatus`` defaults to holding its task hot.
FINISHED_PUBLICATION_STATUSES: frozenset[str] = frozenset(
    {"published", "rejected", "failed"}
)


def _plan_row_archivable(
    row: dict[str, object],
    open_proposal_ids: set[str],
    retry_budget: int | None = None,
    open_pr_plan_ids: set[str] | None = None,
) -> bool:
    """Whether one raw ``evolution_plans.jsonl`` row is finished history.

    Pure predicate over the decoded row, shared by :meth:`Memory.archive`'s
    rotation loop and its direct unit tests. ``True`` only when no hot path
    still owes work on the row: its status is in
    :data:`ARCHIVABLE_PLAN_STATUSES`, or it is a ``failed`` row whose
    ``failure_count`` has provably spent *retry_budget* (see
    :func:`_retry_budget_spent`); a ``rejected`` row has also been learned from
    (``rejection_learned``); a retired ``rejected``/``obsolete`` row's PR is not
    among *open_pr_plan_ids* — Tend still services it — where ``None`` means PR
    state is unknown, so any such row carrying a ``pr_url`` stays hot; *and* it
    is not a slice of a proposal in *open_proposal_ids*. A missing or
    unrecognised status reads as unfinished.
    """
    status = row.get("status")
    if not isinstance(status, str):
        return False
    if status not in ARCHIVABLE_PLAN_STATUSES and not (
        status == "failed" and _retry_budget_spent(row, retry_budget)
    ):
        return False
    if status == "rejected" and row.get("rejection_learned") is not True:
        return False
    if status in ("rejected", "obsolete"):
        if open_pr_plan_ids is None:
            if row.get("pr_url"):
                return False
        elif row.get("plan_id") in open_pr_plan_ids:
            return False
    parent = row.get("parent_plan_id")
    return not (isinstance(parent, str) and parent in open_proposal_ids)


def _retry_budget_spent(row: dict[str, object], retry_budget: int | None) -> bool:
    """Whether a ``failed`` plan row can no longer be retried.

    Mirrors ``_implement_approved_plans``'s ``failure_count <
    max_implementation_attempts`` re-admission test: once the count reaches that
    budget the selector never picks the plan up again, so the row is terminal in
    practice even though its status is not — without this, every failed plan
    would accumulate in the hot file forever. An unknown budget (``None``, what
    an un-supplied caller gets) or a non-``int`` ``failure_count`` reads as *not*
    spent, keeping the row hot.
    """
    if retry_budget is None:
        return False
    count = row.get("failure_count", 0)
    if isinstance(count, bool) or not isinstance(count, int):
        return False
    return count >= retry_budget


#: Plan statuses at which a slice has ended for good without opening a PR
#: (``failed`` joins them only once its retry budget is spent).
SLICE_DEAD_STATUSES: frozenset[str] = frozenset({"rejected", "obsolete"})


def slice_ended_without_pr(plan: EvolutionPlan, retry_budget: int) -> bool:
    """Whether a proposal-118 slice ended without ever opening a PR.

    ``True`` iff *plan* has no ``pr_url`` and is ``rejected``/``obsolete``, or
    ``failed`` with its retry budget spent (the same count semantics as
    :func:`_retry_budget_spent`). A slice with a ``pr_url`` is never "dead"
    here: its PR's settlement owns its fate. Pure, zero SDK cost.
    """
    if plan.pr_url:
        return False
    if plan.status in SLICE_DEAD_STATUSES:
        return True
    return plan.status == "failed" and _retry_budget_spent(
        {"failure_count": plan.failure_count}, retry_budget,
    )


def _live_cited_source_task_ids(plans: list[EvolutionPlan]) -> set[str]:
    """Pure extraction: non-null ``source_task_id``s cited by live plans.

    Shared by :meth:`Memory.live_cited_source_task_ids` (which supplies the
    on-disk plan list) and :func:`is_source_task_recited` (which is handed a
    plan list) so both agree on exactly one notion of "live citation".
    """
    return {
        p.source_task_id
        for p in plans
        if p.status in RECITE_LIVE_STATUSES
        and isinstance(p.source_task_id, str)
        and p.source_task_id
    }


def is_source_task_recited(
    source_task_id: str | None,
    plans: list[EvolutionPlan],
) -> bool:
    """Return ``True`` iff *source_task_id* is cited by a live plan in *plans*.

    A pure predicate reusing :func:`_live_cited_source_task_ids`: the branch
    is "re-cited" when a live plan (``pending_review``/``approved``/
    ``implemented`` — the canonical :data:`~maestro.pr_facts.LIVE_STATUSES`
    set) already names it as its :attr:`EvolutionPlan.source_task_id`. A
    ``None``/empty *source_task_id* is never recited — a plan basing itself
    fresh from ``main`` cannot collide with an existing claim.
    """
    if not source_task_id:
        return False
    return source_task_id in _live_cited_source_task_ids(plans)


def find_pending_plan_cluster(
    memory: Memory,
    new_plan_text: str,
    threshold: float = RESTATEMENT_THRESHOLD,
) -> list[EvolutionPlan]:
    """Return every open plan that thematically clusters with *new_plan_text*.

    Where :meth:`Memory.find_duplicate_pending_plan` answers "is this a
    near-verbatim copy of *one* pending plan" at a strict pairwise cutoff,
    this answers the cluster question: how many open pending plans share a
    *theme* with the candidate at a looser *threshold*. Every pending plan
    whose content-word Jaccard (the same zero-LLM-cost machinery behind
    :func:`~maestro.consciousness.detect_restatement`) against *new_plan_text*
    meets *threshold* is returned, in load order.

    The failure mode this guards is plan proliferation: several variants
    that each sit just *below* the strict duplicate threshold yet
    collectively form one theme-cluster choking the review queue. The corpus is
    the whole live-work set (``pending_review``/``approved``/``implemented``, via
    :meth:`Memory.get_live_plan_records`) so an approved or open-PR-backed plan is
    visible too. A candidate with no content tokens clusters with nothing and
    yields an empty list.
    """
    candidate_tokens = _content_tokens(new_plan_text)
    if not candidate_tokens:
        return []
    corpus = memory.get_live_plan_records()
    cluster: list[EvolutionPlan] = []
    for plan in corpus:
        existing_text = f"{plan.title or ''}\n{plan.rationale or ''}"
        existing_tokens = _content_tokens(existing_text)
        if not existing_tokens:
            continue
        intersection = candidate_tokens & existing_tokens
        union = candidate_tokens | existing_tokens
        if len(intersection) / len(union) >= threshold:
            cluster.append(plan)
    return cluster


_VERSION_NOTES_MAX_CHARS = 280

# Caps for ``EvolutionPlan`` body fields rewritten by
# :meth:`Memory.refine_plan` and :meth:`Memory.mark_plan_obsolete`.
# Enforced at the write boundary so a confused curator cannot drop
# multi-KB strings into the JSONL — mirrors the CLAUDE.md
# "Every list/string entering a prompt has a cap" invariant.
#
# These caps are two-sided: the creation path (``_plan_from_spec``)
# truncates raw model output to the same limits, while refinement rejects
# an over-cap value. ``acceptance_criteria`` items are bound by the
# tighter ``ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS`` (in ``maestro.types``),
# not ``_PLAN_CHANGE_ITEM_MAX_CHARS``. A legacy oversized plan stays
# loadable but cannot be refined without being summarized down.
_OBSOLETE_REASON_MAX_CHARS = 280
_PLAN_RATIONALE_MAX_CHARS = 2000
_PLAN_CHANGE_ITEM_MAX_CHARS = 1000
#: ``files_affected`` bounds for refinement: each entry is a repo-relative
#: path, and the list enters the implement and self-review prompts.
_PLAN_FILE_PATH_MAX_CHARS = 300
_PLAN_FILES_AFFECTED_MAX_ITEMS = 30

def refine_cap_violation(fields: Mapping[str, object]) -> str | None:
    """Describe the first cap a curator ``refine`` field breaks, else ``None``.

    Pure pre-check for :meth:`Memory.refine_plan`'s length, blank-path and
    count caps, read only for correctly-typed values so a wrong-typed or empty
    field still reaches ``refine_plan``'s own validators. The curator skips a
    violating verdict with a WARNING instead of crashing the loop (#3339).
    """
    rationale = fields.get("rationale")
    if isinstance(rationale, str) and len(rationale) > _PLAN_RATIONALE_MAX_CHARS:
        return (
            f"rationale is {len(rationale)} chars "
            f"(max {_PLAN_RATIONALE_MAX_CHARS})"
        )
    caps = (
        ("proposed_changes", _PLAN_CHANGE_ITEM_MAX_CHARS, None),
        (
            "acceptance_criteria",
            ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS,
            ACCEPTANCE_CRITERIA_MAX_ITEMS,
        ),
        (
            "files_affected",
            _PLAN_FILE_PATH_MAX_CHARS,
            _PLAN_FILES_AFFECTED_MAX_ITEMS,
        ),
    )
    for name, item_cap, count_cap in caps:
        value = fields.get(name)
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            continue
        if any(len(item) > item_cap for item in value):
            return f"{name} has an item over {item_cap} chars"
        if name == "files_affected" and any(not i.strip() for i in value):
            return f"{name} has a blank item"
        if count_cap is not None and len(value) > count_cap:
            return f"{name} has {len(value)} items (max {count_cap})"
    return None


#: ``plan_id``s already warned about for carrying an over-cap
#: ``acceptance_criteria`` list, so ``_parse_plan_line`` emits one WARNING per
#: legacy plan per process rather than one per parse.  Module-level because the
#: parser is a ``@staticmethod`` shared by the full scan and the indexed seek.
_WARNED_OVER_CAP_PLAN_IDS: set[str] = set()

# Fixed ``obsolete_reason`` stamped by
# :meth:`Memory.sweep_clustered_pending_plans` on every member it retires.
# Held well under ``_OBSOLETE_REASON_MAX_CHARS`` so the write never raises.
_CLUSTER_SWEEP_OBSOLETE_REASON = (
    "Mechanical pending-plan cluster sweep: redundant member of a "
    "near-restated cluster; the newest member is kept canonical."
)

# Fixed ``obsolete_reason`` stamped by
# :meth:`Memory.sweep_restatement_pending_plans` on every plan it retires.
# Held well under ``_OBSOLETE_REASON_MAX_CHARS`` so the write never raises.
_RESTATEMENT_SWEEP_OBSOLETE_REASON = (
    "Mechanical restatement sweep: this pending plan restates a "
    "strictly-newer surviving pending plan; the newest is kept canonical."
)

# Fixed ``obsolete_reason`` stamped by
# :meth:`Memory.sweep_expired_pending_plans` on every stale plan it retires.
# Held well under ``_OBSOLETE_REASON_MAX_CHARS`` so the write never raises.
_EXPIRY_SWEEP_OBSOLETE_REASON = (
    "Mechanical pending-plan expiry sweep: this plan sat in pending_review "
    "past the age horizon un-reviewed; auto-obsoleted as stale (not rejected)."
)

# Fixed ``obsolete_reason`` stamped by :meth:`Memory.sweep_orphaned_cures`
# on every implemented-but-unmerged cure it retires. Held well under
# ``_OBSOLETE_REASON_MAX_CHARS`` so the write never raises.
_ORPHANED_CURE_SWEEP_OBSOLETE_REASON = (
    "Mechanical orphaned-cure sweep: implemented but unmerged past the "
    "retire ceiling with a tend-confirmed conflict; the PR stays open "
    "for human disposition."
)


@dataclass
class VersionRecord:
    version_id: str
    plan_id: str
    plan_title: str
    commit_sha: str
    merged_at: str
    window_tasks_total: int
    window_tasks_succeeded: int
    window_tasks_failed: int
    window_sdk_cost_usd: float
    window_sdk_calls: int
    window_cache_hit_rate: float
    window_rate_limit_hits: int
    window_error_count: int
    # True iff the merge commit's CI rollup conclusion was "success";
    # False when CI failed, was unknown, or the PR was force-merged.
    quality_gate_pass: bool
    notes: str | None = None


# Ledger filename, a sibling of the other JSONL state files under the memory
# directory. Append-only; one ``VersionRecord`` row per merged owned evolution PR.
VERSION_ARCHIVE_FILENAME = "version_archive.jsonl"


@runtime_checkable
class VersionArchiveBackend(Protocol):
    """The version-archive persistence surface (proposal 106).

    ``Memory.record_version`` writes one row per merged owned evolution PR and
    three independent readers load every row: ``Memory`` itself (its
    ``get_recent_versions`` / ``read_version_records`` / ``_load_all_versions``
    tail/all readers), the owned-act-drought sensor in ``audit_checks`` (each
    row's ``merged_at``), and the merge-truth corroboration reader in
    ``remediation_tracker`` (each row's ``plan_id``). Routing all three through
    this seam lets the archive live in ``version_archive.jsonl`` or in
    ``state.db`` behind ``[paths] sqlite_state_enabled`` with no call-site
    branching. Mirrors the append-only, single-writer idiom of
    :class:`RejectionHistoryBackend`: one row per merged plan, append-only,
    oldest-first load. ``@runtime_checkable`` enables a structural ``isinstance``
    check in the backend-selection tests.
    """

    def record(self, record: VersionRecord) -> None:
        """Append exactly one merged-plan row."""
        ...

    def load_all(self) -> list[VersionRecord]:
        """Return every recorded version oldest-first."""
        ...


def load_all_versions(memory_dir: Path) -> list[VersionRecord]:
    """Parse every well-formed row from ``version_archive.jsonl`` oldest-first.

    The shared reader — the SQLite seed source and the JSONL adapter's
    ``load_all`` — reuses the corrupt-tolerant :meth:`Memory._parse_version_dict`
    coercion: it **skips and warns on every corrupt row and never raises** (a
    malformed line anywhere, interior or trailing, is dropped, not propagated),
    and returns ``[]`` on a missing file or an ``OSError``. Order is oldest-first,
    so ``load_all()[-1]`` is the most recent merged plan and ``load_all()[-n:]``
    the tail window.
    """
    archive_path = memory_dir / VERSION_ARCHIVE_FILENAME
    if not archive_path.exists():
        return []
    try:
        text = archive_path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[VersionRecord] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Skipping corrupted version record in %s", archive_path)
            continue
        record = Memory._parse_version_dict(data)
        if record is None:
            logger.warning("Skipping corrupted version record in %s", archive_path)
            continue
        records.append(record)
    return records


class JsonlVersionArchiveLedger:
    """JSONL-backed :class:`VersionArchiveBackend` — the default, flag-off store.

    Binds a ``memory_dir`` and keeps the ledger on ``version_archive.jsonl``
    (behaviour-preserving). ``record`` appends exactly one row via
    :func:`fileutil.append_jsonl_row` (under ``file_lock``, cutting a crash-left
    partial tail with a WARNING first) in the same
    ``json.dumps(asdict(record)) + "\\n"`` shape ``Memory.record_version`` wrote
    before the fold; ``load_all`` reuses the corrupt-tolerant
    forward parse via :func:`load_all_versions`.
    """

    def __init__(self, memory_dir: Path) -> None:
        self._memory_dir = memory_dir
        self._archive_path = memory_dir / VERSION_ARCHIVE_FILENAME

    def record(self, record: VersionRecord) -> None:
        append_jsonl_row(self._archive_path, asdict(record))

    def load_all(self) -> list[VersionRecord]:
        return load_all_versions(self._memory_dir)


@dataclass
class RejectionRecord:
    plan_id: str
    title: str
    category: Category
    priority: Priority
    rejection_reason: str
    timestamp: str
    rejection_category: RejectionCategory = "other"


# Ledger filename, a sibling of the other JSONL state files under the memory
# directory. Append-only; one ``RejectionRecord`` row per human-rejected plan.
REJECTION_HISTORY_FILENAME = "rejection_history.jsonl"


@runtime_checkable
class RejectionHistoryBackend(Protocol):
    """The rejection-history persistence surface (proposal 106).

    ``Memory.record_rejection`` writes one row per human-rejected evolution plan
    and the two readers (``get_recent_rejections`` — consumed by the reality
    inventory's rejection categories — and ``_load_all_rejections``,
    the migration reader) load every row, so the history can live in
    ``rejection_history.jsonl`` or in ``state.db`` behind
    ``[paths] sqlite_state_enabled`` with no call-site branching. Mirrors the
    append-only, single-writer idiom of :class:`AuditHistoryBackend`: one row per
    rejection, append-only, oldest-first load. ``@runtime_checkable`` enables a
    structural ``isinstance`` check in the backend-selection tests.
    """

    def record(self, record: RejectionRecord) -> None:
        """Append exactly one rejection row."""
        ...

    def load_all(self) -> list[RejectionRecord]:
        """Return every recorded rejection oldest-first."""
        ...


def _parse_rejection_dict(
    data: dict[str, Any], *, source: Path | None = None,
) -> RejectionRecord | None:
    """Coerce an already-decoded JSON row into a :class:`RejectionRecord`.

    Carries the exact per-row corrupt-tolerance the two rejection readers
    (``get_recent_rejections`` / ``_load_all_rejections``) always had:
    field-filtering against ``{f.name for f in fields(RejectionRecord)}`` (so an
    unknown future field is dropped, not fatal) and the ``LEGACY_PRIORITY_REMAP``
    priority coercion that keeps a legacy ``priority`` string in lock-step with
    ``Memory._parse_plan_line``. Returns ``None`` on any structural failure
    (``KeyError`` / ``TypeError`` / ``AttributeError``); the ``json.loads`` that
    can raise ``JSONDecodeError`` lives in the shared reader
    :func:`load_all_rejection_history`, mirroring the audit split. *source*, when
    given, names the ledger in the warning.
    """
    known = {f.name for f in fields(RejectionRecord)}
    try:
        filtered = {k: v for k, v in data.items() if k in known}
        if isinstance(filtered.get("priority"), str):
            filtered["priority"] = LEGACY_PRIORITY_REMAP.get(
                filtered["priority"], filtered["priority"],
            )
        return RejectionRecord(**filtered)
    except (KeyError, TypeError, AttributeError):
        logger.warning("Skipping corrupted rejection record in %s", source)
        return None


def load_all_rejection_history(memory_dir: Path) -> list[RejectionRecord]:
    """Parse every well-formed row from ``rejection_history.jsonl`` oldest-first.

    The shared reader — the SQLite seed source and the JSONL adapter's
    ``load_all`` — carrying the exact tolerance the two rejection readers had
    before the fold: it **skips and warns on every corrupt row and never raises**
    (a malformed line anywhere, interior or trailing, is dropped, not
    propagated), and returns ``[]`` on a missing file or an ``OSError``. Order is
    oldest-first, so ``load_all()[-1]`` is the most recent rejection and
    ``load_all()[-n:]`` the tail window.
    """
    history_path = memory_dir / REJECTION_HISTORY_FILENAME
    if not history_path.exists():
        return []
    try:
        text = history_path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[RejectionRecord] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            logger.warning(
                "Skipping corrupted rejection record in %s", history_path,
            )
            continue
        record = _parse_rejection_dict(data, source=history_path)
        if record is not None:
            records.append(record)
    return records


class JsonlRejectionHistoryLedger:
    """JSONL-backed :class:`RejectionHistoryBackend` — the default, flag-off store.

    Binds a ``memory_dir`` and keeps the ledger on ``rejection_history.jsonl``
    (behaviour-preserving). ``record`` appends exactly one row under
    ``open(path, "a", encoding="utf-8")`` with the same
    ``json.dumps(asdict(record)) + "\\n"`` shape ``Memory.record_rejection`` wrote
    before the fold (parent-dir-created); ``load_all`` reuses the
    corrupt-tolerant forward parse via :func:`load_all_rejection_history`.
    """

    def __init__(self, memory_dir: Path) -> None:
        self._memory_dir = memory_dir
        self._history_path = memory_dir / REJECTION_HISTORY_FILENAME

    def record(self, record: RejectionRecord) -> None:
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record)) + "\n")

    def load_all(self) -> list[RejectionRecord]:
        return load_all_rejection_history(self._memory_dir)


@dataclass
class AuditResult:
    """Outcome of a single ``audit_self_model`` phase invocation.

    Even a zero-finding cycle is recorded: ``findings == []`` is a valid,
    observable signal that the mechanical rules did not detect any
    divergence on this pass. ``rules_fired`` is the sorted-unique set of
    rule names that produced at least one finding; callers can use it
    as a quick telemetry tag without having to walk ``findings``.
    """

    iteration: int
    timestamp: str
    findings: list[AuditFinding] = field(default_factory=list)
    rules_fired: list[str] = field(default_factory=list)


# Ledger filename, a sibling of the other JSONL state files under the memory
# directory. Append-only; one ``AuditResult`` row per ``audit_self_model`` cycle.
AUDIT_HISTORY_FILENAME = "audit_history.jsonl"


@runtime_checkable
class AuditHistoryBackend(Protocol):
    """The audit-history persistence surface (proposal 106).

    ``Memory.append_audit_result`` writes one row per ``audit_self_model``
    cycle and the two readers (``get_most_recent_audit`` /
    ``get_recent_audit_results``) load every row, so the history can live in
    ``audit_history.jsonl`` or in ``state.db`` behind
    ``[paths] sqlite_state_enabled`` with no call-site branching. One row per
    cycle, append-only, oldest-first load. ``@runtime_checkable`` enables a structural
    ``isinstance`` check in the backend-selection tests.
    """

    def record(self, result: AuditResult) -> None:
        """Append exactly one audit-result row."""
        ...

    def load_all(self) -> list[AuditResult]:
        """Return every recorded audit result oldest-first."""
        ...


def _parse_audit_dict(
    data: dict[str, Any], *, source: Path | None = None,
) -> AuditResult | None:
    """Coerce an already-decoded JSON row into an :class:`AuditResult`.

    Carries the exact per-row corrupt-tolerance the JSONL audit reader has
    always had: finding-dict field filtering with ``AuditKind``-membership
    validation (a finding whose ``kind`` falls outside ``AuditKind`` is dropped,
    not the whole cycle), ``cites`` / ``rules_fired`` sanitisation, and the
    "declared findings but every one dropped ⇒ corrupt" rule (returns ``None``
    so the caller falls through to an older valid row rather than masking
    corruption as a legitimate zero-finding cycle). A genuine zero-finding
    cycle (empty ``findings``) parses cleanly. Returns ``None`` on any
    structural failure; *source*, when given, names the ledger in the warning.

    The ``AuditFinding`` / ``AuditKind`` import is lazy to keep the
    ``audit_checks → memory → audit_checks`` import cycle broken at module load.
    """
    from maestro.audit_checks import AuditFinding, AuditKind

    finding_known = {f.name for f in fields(AuditFinding)}
    result_known = {f.name for f in fields(AuditResult)}
    # Derive the valid-kind vocabulary from the ``AuditKind`` Literal so it stays
    # in lock-step with the type automatically. A frozen dataclass does not
    # enforce ``Literal`` membership at runtime, so a stored finding carrying an
    # unknown kind would otherwise deserialise cleanly and poison the prompt.
    valid_kinds = frozenset(get_args(AuditKind))
    try:
        raw_findings = data.get("findings") or []
        findings: list[AuditFinding] = []
        for entry in raw_findings:
            if not isinstance(entry, dict):
                continue
            filtered = {k: v for k, v in entry.items() if k in finding_known}
            if filtered.get("kind") not in valid_kinds:
                # Drop the offending finding (not the whole cycle) and keep the
                # failure visible, matching the per-finding ``cites`` sanitisation.
                logger.warning(
                    "Dropping audit finding with invalid kind %r in %s",
                    filtered.get("kind"),
                    source,
                )
                continue
            cites = filtered.get("cites") or []
            filtered["cites"] = [c for c in cites if isinstance(c, str)]
            findings.append(AuditFinding(**filtered))
        if raw_findings and not findings:
            # The row declared findings but every one was dropped as malformed.
            # Treat it as corrupt so the caller falls through to an older valid
            # row, rather than surfacing an empty-but-valid cycle that masks
            # corruption as "no findings fired". A genuine zero-finding cycle has
            # an empty ``raw_findings`` and is preserved below.
            logger.warning(
                "Skipping audit record with no salvageable findings in %s",
                source,
            )
            return None
        result_fields = {k: v for k, v in data.items() if k in result_known}
        result_fields["findings"] = findings
        rules_fired = result_fields.get("rules_fired") or []
        result_fields["rules_fired"] = [
            r for r in rules_fired if isinstance(r, str)
        ]
        return AuditResult(**result_fields)
    except (KeyError, TypeError, AttributeError):
        logger.warning("Skipping corrupted audit record in %s", source)
        return None


def load_all_audit_history(memory_dir: Path) -> list[AuditResult]:
    """Parse every well-formed row from ``audit_history.jsonl`` oldest-first.

    The shared reader — the SQLite seed source and the JSONL adapter's
    ``load_all`` — carrying the exact tolerance the two audit readers had before
    the fold: it **skips and warns on every corrupt row and never raises** (a
    malformed line anywhere, interior or trailing, is dropped, not
    propagated), and returns ``[]`` on a
    missing file or an ``OSError``. Order is oldest-first, so ``load_all()[-1]``
    is the most recent cycle and ``load_all()[-limit:]`` the tail window.
    """
    history_path = memory_dir / AUDIT_HISTORY_FILENAME
    if not history_path.exists():
        return []
    try:
        text = history_path.read_text(encoding="utf-8")
    except OSError:
        return []
    results: list[AuditResult] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Skipping corrupted audit record in %s", history_path)
            continue
        result = _parse_audit_dict(data, source=history_path)
        if result is not None:
            results.append(result)
    return results


class JsonlAuditHistoryLedger:
    """JSONL-backed :class:`AuditHistoryBackend` — the default, flag-off store.

    Binds a ``memory_dir`` and keeps the ledger on ``audit_history.jsonl``
    (behaviour-preserving). ``record`` appends exactly one row under
    ``open(path, "a", encoding="utf-8")`` with the same
    ``json.dumps(asdict(result))`` shape ``Memory.append_audit_result`` wrote
    before the fold (parent-dir-created); ``load_all`` reuses the
    corrupt-tolerant forward parse via :func:`load_all_audit_history`.
    """

    def __init__(self, memory_dir: Path) -> None:
        self._memory_dir = memory_dir
        self._history_path = memory_dir / AUDIT_HISTORY_FILENAME

    def record(self, result: AuditResult) -> None:
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(result)) + "\n")

    def load_all(self) -> list[AuditResult]:
        return load_all_audit_history(self._memory_dir)


# Hard cap on the ``finding`` field of a :class:`QuestionResolution`.
# Mirrors the implicit ceiling on ``key_findings`` consumers (150-500 chars
# per call site); 500 keeps the row useful for stalled-question audits
# without bloating the JSONL when many resolutions accumulate.
_QUESTION_RESOLUTION_FINDING_MAX_CHARS = 500


@dataclass
class QuestionResolution:
    """One LLM verdict on a ``(question, finding)`` pair.

    Recorded once per pair fed to ``Consciousness.update_self_model`` so the
    self-directed-task feedback loop (``contemplate`` → ``update_self_model``
    → ``open_questions`` diff) leaves a durable audit trail. Used by the
    ``check_stalled_question`` audit rule to surface questions that have
    been targeted ≥ 3 times without being closed.

    *question*, *finding*, and *refined_to* are head-truncated at
    :data:`_QUESTION_RESOLUTION_FINDING_MAX_CHARS` on append so a
    pathological caller cannot break lineage walks or bloat the JSONL.

    *finding*'s verbatim text is recoverable from ``task_history.jsonl``
    via *task_id*.

    *refined_to* is populated only when *disposition* is ``"refined"`` —
    it carries the LLM's successor wording so the lineage chain
    ``Q1 → Q1' → Q1''`` can be walked at audit time. ``None`` (the
    default) preserves backward compatibility with rows written before
    the field existed; the loader strips unknown keys, and missing keys
    parse to the default.
    """

    resolution_id: str
    iteration: int
    timestamp: str
    question: str
    task_id: str
    outcome: TaskOutcome
    disposition: QuestionDisposition
    finding: str
    refined_to: str | None = None


# Ledger filename, a sibling of the other JSONL state files under the memory
# directory. Append-only; one ``QuestionResolution`` row per ``(question,
# finding)`` pair fed to ``Consciousness.update_self_model``.
QUESTION_RESOLUTIONS_FILENAME = "question_resolutions.jsonl"


@runtime_checkable
class QuestionResolutionBackend(Protocol):
    """The question-resolution persistence surface (proposal 106).

    ``Memory.append_question_resolution`` writes one row per ``(question,
    finding)`` pair fed to ``Consciousness.update_self_model`` and the loop-side
    readers (``get_recent_question_resolutions`` — consumed by the
    stalled-question audit lineage walk in ``agent._resolve_open_questions`` and
    by the ``dropped_directive_replay`` task gate) plus the migration reader
    (``_load_all_question_resolutions``) load every row, so the ledger can live in
    ``question_resolutions.jsonl`` or in ``state.db`` behind
    ``[paths] sqlite_state_enabled`` with no call-site branching. Mirrors the
    append-only, single-writer idiom of :class:`RejectionHistoryBackend`: one row
    per resolution, append-only, oldest-first load. ``@runtime_checkable`` enables
    a structural ``isinstance`` check in the backend-selection tests.
    """

    def record(self, record: QuestionResolution) -> None:
        """Append exactly one question-resolution row."""
        ...

    def load_all(self) -> list[QuestionResolution]:
        """Return every recorded resolution oldest-first."""
        ...


def _parse_question_resolution_dict(
    data: dict[str, Any], *, source: Path | None = None,
) -> QuestionResolution | None:
    """Coerce an already-decoded JSON row into a :class:`QuestionResolution`.

    Carries the exact per-row corrupt-tolerance the question-resolution readers
    always had: field-filtering against ``{f.name for f in
    fields(QuestionResolution)}`` (so an unknown future field is dropped, not
    fatal, and a missing ``refined_to`` parses to its default). Returns ``None``
    on any structural failure (``KeyError`` / ``TypeError`` / ``AttributeError``);
    the ``json.loads`` that can raise ``JSONDecodeError`` lives in the shared
    reader :func:`load_all_question_resolutions`, mirroring the rejection split.
    Unlike :func:`_parse_rejection_dict` there is no ``LEGACY_PRIORITY_REMAP``
    coercion — question resolutions carry no remapped field. *source*, when given,
    names the ledger in the warning.
    """
    known = {f.name for f in fields(QuestionResolution)}
    try:
        filtered = {k: v for k, v in data.items() if k in known}
        return QuestionResolution(**filtered)
    except (KeyError, TypeError, AttributeError):
        logger.warning(
            "Skipping corrupted question_resolution row in %s", source,
        )
        return None


def load_all_question_resolutions(
    memory_dir: Path,
) -> list[QuestionResolution]:
    """Parse every well-formed row from ``question_resolutions.jsonl`` oldest-first.

    The shared reader — the SQLite seed source and the JSONL adapter's
    ``load_all`` — carrying the exact tolerance the readers had before the fold:
    it **skips and warns on every corrupt row and never raises** (a malformed
    line anywhere, interior or trailing, is dropped, not propagated), and returns
    ``[]`` on a missing file or an ``OSError``. Order is oldest-first, so
    ``load_all()[-1]`` is the most recent resolution and ``load_all()[-n:]`` the
    tail window ``get_recent_question_resolutions`` returns.
    """
    history_path = memory_dir / QUESTION_RESOLUTIONS_FILENAME
    if not history_path.exists():
        return []
    try:
        text = history_path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[QuestionResolution] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            logger.warning(
                "Skipping corrupted question_resolution row in %s",
                history_path,
            )
            continue
        record = _parse_question_resolution_dict(data, source=history_path)
        if record is not None:
            records.append(record)
    return records


class JsonlQuestionResolutionLedger:
    """JSONL-backed :class:`QuestionResolutionBackend` — the default, flag-off store.

    Binds a ``memory_dir`` and keeps the ledger on ``question_resolutions.jsonl``
    (behaviour-preserving). ``record`` reproduces the exact pre-fold write — an
    advisory :func:`file_lock` around an ``open(path, "a", encoding="utf-8")``
    append of ``json.dumps(asdict(record)) + "\\n"`` (parent-dir-created) — so the
    JSONL substrate is bit-for-bit unchanged. The ``file_lock`` stays in this
    adapter (the sibling :class:`JsonlRejectionHistoryLedger` needs none) because
    concurrent question-resolution appends must not interleave partial lines;
    ``Memory.append_question_resolution`` keeps only the record normalisation
    above the backend call, so the SQLite adapter — which serialises through its
    own transaction — takes no ``file_lock``. ``load_all`` reuses the
    corrupt-tolerant forward parse via :func:`load_all_question_resolutions`.
    """

    def __init__(self, memory_dir: Path) -> None:
        self._memory_dir = memory_dir
        self._history_path = memory_dir / QUESTION_RESOLUTIONS_FILENAME

    def record(self, record: QuestionResolution) -> None:
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        with (
            file_lock(self._history_path),
            open(self._history_path, "a", encoding="utf-8") as f,
        ):
            f.write(json.dumps(asdict(record)) + "\n")

    def load_all(self) -> list[QuestionResolution]:
        return load_all_question_resolutions(self._memory_dir)


@dataclass
class ArtifactReview:
    review_id: str
    task_id: str
    verdict: Verdict
    title: str
    reasoning: str
    bundle_file_names: list[str]
    proposed_name: str | None
    proposed_description: str | None
    timestamp: str
    proposed_readme: str | None = None


@dataclass
class PublicationProposal:
    proposal_id: str
    task_id: str
    review_id: str
    proposed_name: str
    description: str
    proposed_readme: str | None
    # Each entry is treated as a path relative to the task artifacts dir;
    # _stage rejects entries whose resolved src/dest escape the staging tree.
    publishable_files: list[str]
    status: ProposalStatus = "pending_approval"
    created_at: str = ""
    reviewed_at: str | None = None
    rejection_reason: str | None = None
    scrub_findings: list[str] = field(default_factory=list)
    executing_started_at: str | None = None
    published_url: str | None = None
    error: str | None = None


@dataclass
class DocumentRecord:
    """One historical row per outcome of a retired doc-proposal decision.

    The doc-proposals pipeline was retired (proposal 149); no writer remains.
    The rows already on disk stay readable through
    :meth:`Memory.get_document_records` and ``maestro docs``.
    """

    doc_id: str
    doc_type: DocType
    title: str
    linked_task_ids: list[str]
    target_path: str | None
    doc_branch: str | None
    pr_url: str | None
    bundle_file_names: list[str]
    reasoning: str
    status: DocStatus
    error: str | None
    created_at: str


#: Per-cycle behavioral-sensor scalar readings sidecar. Written once per
#: ``contemplate`` cycle, read back tail-bounded by the SENSOR_THRESHOLD
#: calibration-evidence path. Deliberately *not* a member of the tiered-memory
#: archive set (``Memory.archive``): the calibration scorer only reads a bounded
#: tail, so the file is never rewritten and its append needs no rotation.
SENSOR_READINGS_FILENAME = "sensor_readings.jsonl"


@dataclass(frozen=True)
class SensorReading:
    """One per-cycle behavioral-sensor scalar reading.

    ``timestamp`` is an ISO-8601 UTC string (lexical order equals chronological,
    matching :class:`~maestro.stats.BacklogSnapshot` and the reflection-bet
    ``timestamp`` the calibration scorer filters against). ``tag`` is the
    uppercase behavioral-sensor tag the reading is for — the same token the
    reflection sees in ``contemplate``'s measured-data block, so a
    ``sensor <tag> goes above/below <n>`` bet resolves against the number the
    block showed. ``value`` is this cycle's scalar reading. ``crossed`` records
    only whether the reading was a non-baseline (non-zero) value — informational
    for a later human/CLI read; the SENSOR_THRESHOLD scorer
    (:func:`maestro.self_calibration.score_loss_condition`) recomputes crossing
    against each bet's own threshold from ``value`` and never reads this field.
    """

    timestamp: str
    tag: str
    value: float
    crossed: bool


@dataclass
class EvolutionProposal:
    """Runtime owner of a multi-PR self-improvement (proposal 118).

    An ``EvolutionProposal`` groups an ordered chain of thin,
    individually-reviewed plan *slices* that share one goal. Slice content
    lives in the plan store — a proposal's slices are its ``EvolutionPlan``s
    filtered by ``parent_plan_id`` and ordered by ``phase_index`` — so no
    slice-content field lives here. Nothing in the loop reads or writes a
    proposal yet; this is inert persistence scaffolding.
    """

    proposal_id: str
    title: str
    goal: str
    status: EvolutionProposalStatus = "slicing"
    created_at: str = ""


class Memory:
    def __init__(
        self,
        memory_dir: Path,
        *,
        audit_history: AuditHistoryBackend | None = None,
        rejection_history: RejectionHistoryBackend | None = None,
        question_resolutions: QuestionResolutionBackend | None = None,
        version_archive: VersionArchiveBackend | None = None,
    ) -> None:
        self._dir = memory_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._tasks_path = self._dir / "task_history.jsonl"
        self._plans_path = self._dir / "evolution_plans.jsonl"
        # High-frequency telemetry sidecars — one row per SDK call
        # (``cost_samples.jsonl``, the default JSONL cost store) and one per
        # loop iteration (``usage.jsonl``). Both are rotated by ``archive``
        # into the same hot/cold tiers so they cannot grow without bound
        # (mirroring ``config.cost_samples_path`` / ``config.usage_log_path``,
        # which resolve to the same ``memory_dir`` files).
        self._cost_samples_path = self._dir / "cost_samples.jsonl"
        self._usage_path = self._dir / "usage.jsonl"
        self._rejections_path = self._dir / REJECTION_HISTORY_FILENAME
        # Persistence seam (proposal 106). An un-injected caller (and every
        # existing test) keeps today's JSONL behaviour; the loop-owning
        # ``_build_agent`` site injects the flag-selected backend. The
        # ``_rejections_path`` above is retained as the warning-source path,
        # mirroring how ``_audit_history_path`` is kept alongside the injected
        # audit backend.
        self._rejections: RejectionHistoryBackend = (
            rejection_history or JsonlRejectionHistoryLedger(memory_dir)
        )
        # Version-archive persistence seam (proposal 106): an un-injected caller
        # (and every existing test) keeps today's JSONL behaviour; the loop-owning
        # ``cli.main`` site injects the flag-selected backend.
        self._versions: VersionArchiveBackend = (
            version_archive or JsonlVersionArchiveLedger(memory_dir)
        )
        self._reviews_path = self._dir / "artifact_reviews.jsonl"
        self._proposals_path = self._dir / "publication_proposals.jsonl"
        self._audit_history_path = self._dir / AUDIT_HISTORY_FILENAME
        # Persistence seam (proposal 106). An un-injected caller (and every
        # existing test) keeps today's JSONL behaviour; the loop-owning
        # ``_build_agent`` site injects the flag-selected backend. The
        # ``_audit_history_path`` above is retained as the warning-source path
        # for the ``_parse_audit_line`` wrapper.
        self._audit_history: AuditHistoryBackend = (
            audit_history or JsonlAuditHistoryLedger(memory_dir)
        )
        self._documents_path = self._dir / "document_records.jsonl"
        self._evolution_proposals_path = (
            self._dir / "evolution_proposals.jsonl"
        )
        self._question_resolutions_path = (
            self._dir / QUESTION_RESOLUTIONS_FILENAME
        )
        # Per-cycle behavioral-sensor readings sidecar (SENSOR_THRESHOLD
        # calibration scoring). Append-only, read back tail-bounded; never
        # rotated by ``archive`` (see ``SENSOR_READINGS_FILENAME``).
        self._sensor_readings_path = self._dir / SENSOR_READINGS_FILENAME
        # Persistence seam (proposal 106). An un-injected caller (and every
        # existing test) keeps today's JSONL behaviour; the loop-owning
        # ``_build_agent`` site injects the flag-selected backend. The
        # ``_question_resolutions_path`` above is retained as the warning-source
        # path, mirroring how ``_rejections_path`` / ``_audit_history_path`` are
        # kept alongside their injected backends.
        self._question_resolutions: QuestionResolutionBackend = (
            question_resolutions
            or JsonlQuestionResolutionLedger(memory_dir)
        )
        # Per-task crash-recovery checkpoints (one JSON file per task).
        # Created lazily on first ``save_checkpoint`` to keep construction
        # cheap — most ``Memory`` instances never write a checkpoint.
        self._checkpoints_dir = self._dir / "checkpoints"
        # Lazy line-offset cache for ``get_plan``; ``None`` means "rebuild
        # on next read". Process-local: only this instance's writes
        # invalidate it, so a plan appended by another process (loop vs.
        # dashboard) is caught by ``get_plan`` rebuilding on a miss — a
        # miss is never authoritative. Pure speedup — no on-disk format
        # change.
        self._plan_index: dict[str, int] | None = None

    # --- Task history ---

    def record_task(self, record: TaskRecord) -> None:
        """Append one :class:`TaskRecord` to ``task_history.jsonl``.

        The append takes ``file_lock`` because :meth:`archive` rewrites this
        same file under that lock (``read_text`` -> ``atomic_write``).  An
        advisory ``flock`` only excludes writers who also take it, so an
        unlocked append landing inside a rotation is destroyed by the rename
        — silently, with no error and no corrupted-row log.  A ``run-task`` /
        ``resume-task`` CLI process recording a task while the loop
        container's dream phase archives is the concrete race.  Validation
        stays above the lock so a rejected call never contends.
        """
        if not record.task_id:
            raise ValueError("task_id must not be empty")
        append_jsonl_row(self._tasks_path, asdict(record))

    @staticmethod
    def _parse_task_dict(d: dict) -> TaskRecord | None:
        """Coerce an already-decoded JSON dict into a ``TaskRecord``.

        Returns ``None`` for corrupted rows so callers can decide whether
        to log/skip.  Shared between line-based (``get_task_record``) and
        reverse-streamed (``get_recent_tasks``) parsing paths so coercion
        stays in one place.
        """
        try:
            step_known = {f.name for f in fields(PlanStep)}
            d["plan"] = [
                PlanStep(**{k: v for k, v in s.items() if k in step_known})
                for s in (d.get("plan") or [])
            ]
            if d.get("errors") is None:
                d["errors"] = []
            # Conscious-access citation list (proposal 130). Coerce the task read
            # path the same way ``_parse_plan_line`` coerces the plan side: a
            # present non-list value collapses to ``None`` (never cited) while a
            # present list is filtered to its string members, preserving the
            # ``None`` (never cited) vs ``[]`` (cited nothing) distinction. An
            # absent key is dropped by the ``task_known`` filter below and takes
            # the dataclass default (``None``). Unlike the plan path this never
            # feeds a line-splitter — ``workspace_item_ids`` has no legacy string
            # form.
            if "workspace_item_ids" in d:
                citations = d["workspace_item_ids"]
                if isinstance(citations, list):
                    d["workspace_item_ids"] = [
                        item for item in citations if isinstance(item, str)
                    ]
                else:
                    d["workspace_item_ids"] = None
            task_known = {f.name for f in fields(TaskRecord)}
            return TaskRecord(
                **{k: v for k, v in d.items() if k in task_known},
            )
        except (KeyError, TypeError, AttributeError):
            return None

    @staticmethod
    def _coerce_plan_steps_lenient(raw_plan: Any) -> list[PlanStep]:
        """Coerce a raw plan list into ``PlanStep``s, dropping malformed steps.

        Unlike :meth:`_parse_task_dict`, which discards the *whole* record when
        any single step is broken, this coerces step-by-step: a step missing a
        required field (``TypeError``) or that is not a mapping (``AttributeError``
        on ``.items()``) is logged and skipped while its siblings survive.  Used
        only by :meth:`_load_all_tasks`, the "preserve every record" migration
        path where losing a whole task to one bad step is unacceptable.

        A ``plan`` that is not a list at all (a truthy non-iterable such as an
        ``int``/``bool``, or a ``str``/``dict`` that is not a list of steps) is
        treated as an empty plan: it is logged and yields ``[]`` rather than
        letting the iteration raise and drop the whole record — otherwise the
        rarer container-level malformation would reintroduce the same silent
        record loss this path exists to prevent.
        """
        step_known = {f.name for f in fields(PlanStep)}
        steps: list[PlanStep] = []
        if not isinstance(raw_plan, list):
            if raw_plan:
                logger.warning("Ignoring non-list plan container: %r", raw_plan)
            return steps
        for s in raw_plan:
            try:
                steps.append(PlanStep(**{k: v for k, v in s.items() if k in step_known}))
            except (TypeError, AttributeError):
                logger.warning("Skipping malformed plan step: %r", s)
                continue
        return steps

    @staticmethod
    def _parse_task_line(line: str) -> TaskRecord | None:
        """Parse a single ``task_history.jsonl`` line into ``TaskRecord``.

        Returns ``None`` for blank or corrupted rows so callers can decide
        whether to log/skip.
        """
        if not line.strip():
            return None
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(d, dict):
            return None
        return Memory._parse_task_dict(d)

    def get_task_record(self, task_id: str) -> TaskRecord | None:
        """Return the most recent ``TaskRecord`` matching *task_id*, or ``None``.

        Linear scan over ``task_history.jsonl``; sufficient for the
        ``source_task_id`` lookup path which runs at most once per
        ``implement()`` call.
        """
        if not task_id or not self._tasks_path.exists():
            return None
        match: TaskRecord | None = None
        for line in self._tasks_path.read_text(encoding="utf-8").splitlines():
            record = self._parse_task_line(line)
            if record is not None and record.task_id == task_id:
                match = record
        return match

    def get_recent_tasks(self, n: int = 30) -> list[TaskRecord]:
        """Return the *n* most recent task records, oldest-first.

        Walks ``task_history.jsonl`` backwards.  Corrupted records are
        skipped up to a cap of *n*; after that the walk terminates with
        a partial result so a burst of corrupted lines in the file tail
        never causes a full-file scan.  Blank padding lines are free: they
        cost no decode, so they cannot make the walk expensive.  Callers already handle short
        returns gracefully.

        The cap is a single :class:`~maestro.fileutil.SkipBudget` shared with
        ``iter_jsonl_reverse``, so rows rejected by the *decoder* (invalid
        JSON, a non-object value, invalid UTF-8) charge the same counter as
        rows rejected by :meth:`_parse_task_dict`.  Without that sharing a
        parser-level corrupt tail is invisible here and the bound does not
        hold.
        """
        if n <= 0:
            return []
        if not self._tasks_path.exists():
            return []
        entries: list[TaskRecord] = []
        budget = SkipBudget(remaining=n)
        for data in iter_jsonl_reverse(self._tasks_path, skip_budget=budget):
            record = self._parse_task_dict(data)
            if record is None:
                logger.warning(
                    "Skipping corrupted task record in %s", self._tasks_path,
                )
                if not budget.consume():
                    break
                continue
            entries.append(record)
            if len(entries) >= n:
                break
        if budget.remaining <= 0:
            logger.warning(
                "Stopped scanning task_history.jsonl after skipping "
                "%d corrupted records; returning %d of %d requested "
                "tasks",
                n,
                len(entries),
                n,
            )
        entries.reverse()  # public contract: oldest-first.
        return entries

    # --- Per-cycle behavioral-sensor readings ---

    def record_sensor_readings(self, readings: Iterable[SensorReading]) -> None:
        """Append per-cycle sensor *readings* to ``sensor_readings.jsonl``.

        One JSONL row per :class:`SensorReading`.  An empty iterable is a no-op
        (no file is created).  The append takes ``file_lock`` per the JSONL
        discipline — even though ``archive`` never rotates this sidecar, a
        concurrent ``run-task`` / ``resume-task`` CLI process could append at the
        same instant, and the advisory ``flock`` serialises the two writers so
        neither interleaves a half-written line.
        """
        rows = list(readings)
        if not rows:
            return
        with (
            file_lock(self._sensor_readings_path),
            open(self._sensor_readings_path, "a", encoding="utf-8") as f,
        ):
            for reading in rows:
                f.write(json.dumps(asdict(reading)) + "\n")

    def get_recent_sensor_readings(self, n: int = 500) -> list[SensorReading]:
        """Return the *n* most recent sensor readings, oldest-first.

        Tail-bounded, mirroring :meth:`get_recent_tasks`: walks
        ``sensor_readings.jsonl`` backwards via ``iter_jsonl_reverse`` and stops
        once *n* well-formed rows are collected, so a long history never forces a
        full-file scan.  Corrupted rows are skipped up to a cap of *n*; after
        that the walk terminates with a partial result so a burst of corrupted
        tail lines can't defeat the bound either.

        The cap is a single :class:`~maestro.fileutil.SkipBudget` shared with
        ``iter_jsonl_reverse``, so rows rejected by the *decoder* (invalid JSON,
        a non-object value, invalid UTF-8) charge the same counter as rows
        rejected by :meth:`_parse_sensor_reading`.  Without that sharing a
        decode-level corrupt tail is invisible here — those rows are dropped
        inside the generator and never reach this loop — and the bound does not
        hold.
        """
        if n <= 0:
            return []
        entries: list[SensorReading] = []
        budget = SkipBudget(remaining=n)
        for data in iter_jsonl_reverse(
            self._sensor_readings_path, skip_budget=budget
        ):
            reading = self._parse_sensor_reading(data)
            if reading is None:
                logger.warning(
                    "Skipping corrupted sensor reading in %s",
                    self._sensor_readings_path,
                )
                if not budget.consume():
                    break
                continue
            entries.append(reading)
            if len(entries) >= n:
                break
        entries.reverse()  # public contract: oldest-first.
        return entries

    def get_iteration_starts(self, n: int) -> dict[int, str]:
        """Return ``{iteration: started_at}`` for the *n* most recent iterations in ``usage.jsonl``.

        Tail-bounded like :meth:`get_recent_sensor_readings`: walks the file
        backwards and stops before a well-formed row of an *(n+1)*-th distinct
        iteration, with corrupted rows
        (decoder- or shape-level) charging one shared :class:`SkipBudget` of *n*.
        A row lacking an integer ``iteration`` or a non-empty string
        ``started_at`` is corrupt. An iteration with several rows (a frozen
        counter flushes one per body) maps to its *earliest* start, and counts
        once toward *n*, so a long freeze cannot crowd older iterations out.
        Feeds the calibration scorer's window end.
        """
        if n <= 0:
            return {}
        starts: dict[int, str] = {}
        budget = SkipBudget(remaining=n)
        for data in iter_jsonl_reverse(self._usage_path, skip_budget=budget):
            iteration = data.get("iteration")
            started_at = data.get("started_at")
            if (
                not isinstance(iteration, int)
                or isinstance(iteration, bool)
                or not isinstance(started_at, str)
                or not started_at
            ):
                logger.warning("Skipping corrupted usage row in %s", self._usage_path)
                if not budget.consume():
                    break
                continue
            if iteration not in starts and len(starts) >= n:
                break
            # Reverse walk: the last assignment is the iteration's earliest row.
            starts[iteration] = started_at
        return starts

    @staticmethod
    def _parse_sensor_reading(d: dict) -> SensorReading | None:
        """Coerce an already-decoded JSON dict into a ``SensorReading``.

        Returns ``None`` for a row missing a field or carrying an
        unconvertible value, so :meth:`get_recent_sensor_readings` can skip it.
        """
        try:
            return SensorReading(
                timestamp=str(d["timestamp"]),
                tag=str(d["tag"]),
                value=float(d["value"]),
                crossed=bool(d["crossed"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    # --- Task checkpoints (crash recovery) ---

    def save_checkpoint(self, checkpoint: TaskCheckpoint) -> None:
        """Persist an in-flight task snapshot for crash recovery.

        One JSON file per task under ``checkpoints/``, overwritten each step
        group via ``atomic_write`` — never appended.  The directory is
        created lazily here so a ``Memory`` that never checkpoints pays no
        construction cost.
        """
        self._checkpoints_dir.mkdir(parents=True, exist_ok=True)
        path = self._checkpoints_dir / f"{checkpoint.task_id}.json"
        atomic_write(path, json.dumps(asdict(checkpoint)))

    def load_checkpoint(self, task_id: str) -> TaskCheckpoint | None:
        """Return the checkpoint for *task_id*, or ``None`` if absent/corrupt.

        Reconstructs the nested ``PlanStep`` list and filters unknown keys
        with the same ``{f.name for f in fields(...)}`` idiom as
        ``_parse_task_dict`` so a schema that drifted under the file is
        tolerated.  A corrupt checkpoint is logged and treated as absent (the
        task starts fresh); the specific-exception form stays within the
        no-silenced-exceptions doctrine.  The caught set maps to concrete
        sources: ``OSError`` (read race / permissions), ``ValueError``
        (``JSONDecodeError`` from bad JSON and ``UnicodeDecodeError`` from
        non-UTF-8 bytes, both ``ValueError`` subclasses), ``AttributeError``
        (top-level JSON or a step entry is not a mapping, so ``.get``/
        ``.items`` is absent), and ``TypeError`` (a ``PlanStep`` row missing a
        required field).
        """
        path = self._checkpoints_dir / f"{task_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            step_known = {f.name for f in fields(PlanStep)}
            data["steps"] = [
                PlanStep(**{k: v for k, v in s.items() if k in step_known})
                for s in (data.get("steps") or [])
            ]
            cp_known = {f.name for f in fields(TaskCheckpoint)}
            return TaskCheckpoint(
                **{k: v for k, v in data.items() if k in cp_known},
            )
        except (OSError, ValueError, AttributeError, TypeError):
            logger.warning("Corrupted checkpoint for %s, ignoring", task_id)
            return None

    def clear_checkpoint(self, task_id: str) -> None:
        """Delete the checkpoint for *task_id* (no-op when absent)."""
        (self._checkpoints_dir / f"{task_id}.json").unlink(missing_ok=True)

    def cleanup_stale_checkpoints(self, max_age_days: int) -> None:
        """Delete checkpoints older than *max_age_days* (and unusable ones).

        Bounded startup sweep so orphaned checkpoints — left by a branch that
        was manually deleted, or by schema drift that makes a file unloadable
        — cannot accumulate.  Disabled when ``max_age_days <= 0``.  A file
        whose JSON or ``timestamp`` cannot be parsed is itself unusable as a
        resume source, so it is deleted too; the ISO-timestamp parse mirrors
        ``_record_timestamp``/``archive``.
        """
        if max_age_days <= 0:
            return
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        # ``glob`` on a not-yet-created checkpoints dir yields nothing, so no
        # existence guard is needed.
        for path in self._checkpoints_dir.glob("*.json"):
            ts: datetime | None = None
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                raw = data.get("timestamp") if isinstance(data, dict) else None
                if isinstance(raw, str) and raw:
                    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    ts = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except (json.JSONDecodeError, OSError, ValueError, TypeError):
                ts = None
            if ts is None or ts < cutoff:
                path.unlink(missing_ok=True)

    def task_count(self) -> int:
        """Count task records by counting non-empty lines in the JSONL file.

        Uses a fast non-empty line count rather than JSON parsing on every
        line.  This is a hot path called from metrics endpoints; full JSON
        validation would make it O(n) with a high per-line constant.  To
        detect and count only structurally valid entries use
        ``validate_task_history()`` instead.
        """
        if not self._tasks_path.exists():
            return 0
        try:
            text = self._tasks_path.read_text(encoding="utf-8")
        except OSError:
            return 0
        return sum(1 for line in text.splitlines() if line.strip())

    def validate_task_history(self) -> int:
        """Count only valid JSON task records, skipping corrupted lines.

        Slower than ``task_count()`` because it calls ``json.loads()`` on
        every line.  Prefer ``task_count()`` for metrics / hot-path callers.
        """
        if not self._tasks_path.exists():
            return 0
        try:
            text = self._tasks_path.read_text(encoding="utf-8")
        except OSError:
            return 0
        if not text.strip():
            return 0
        count = 0
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                json.loads(line)
                count += 1
            except json.JSONDecodeError:
                continue
        return count

    def _load_all_tasks(self) -> list[TaskRecord]:
        """Load every task record from disk without any count limit.

        Prefer this method when all records must be preserved (e.g. migration).
        Use :meth:`get_recent_tasks` only when a bounded tail is intentional.
        """
        if not self._tasks_path.exists():
            return []
        lines = self._tasks_path.read_text(encoding="utf-8").strip().splitlines()
        entries = []
        for line in lines:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                d["plan"] = self._coerce_plan_steps_lenient(d.get("plan"))
                if d.get("errors") is None:
                    d["errors"] = []
                task_known = {f.name for f in fields(TaskRecord)}
                entries.append(TaskRecord(**{k: v for k, v in d.items() if k in task_known}))
            except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
                logger.warning("Skipping corrupted task record in %s", self._tasks_path)
                continue
        return entries

    def get_success_rate(self, n: int = 30) -> float:
        tasks = self.get_recent_tasks(n=n)
        if not tasks:
            return 0.0
        successes = sum(1 for t in tasks if t.outcome == "success")
        return successes / len(tasks)

    def get_task(self, task_id: str) -> TaskRecord | None:
        """Return the most recent ``TaskRecord`` matching *task_id*, else ``None``.

        Sibling to :meth:`get_proposal` for the publication pipeline:
        the executor needs a public read path to look up a task's
        ``artifacts_dir`` from a proposal, without reaching across the
        ``_load_all_tasks`` underscore boundary.
        """
        if not task_id:
            return None
        match: TaskRecord | None = None
        for task in self._load_all_tasks():
            if task.task_id == task_id:
                match = task
        return match

    # --- Evolution plans ---

    def save_evolution_plan(
        self,
        plan: EvolutionPlan,
        enforce_restatement_gate: bool = False,
    ) -> EvolutionPlan | None:
        """Persist *plan*; return ``None`` on write, the matched plan on block.

        When *enforce_restatement_gate* is set, the plan is first compared
        against the current ``pending_review`` plans via
        :func:`is_plan_restatement`. A near-verbatim restatement of a plan
        already awaiting Thomas's ruling is skipped (logged, no row written)
        and the matched pending plan is returned as a truthy sentinel so the
        caller can suppress its notify/journal side effects. The gate is
        opt-in because :meth:`save_evolution_plan` is the low-level creation
        primitive shared by hundreds of test fixtures and other internal
        saves; only the proposal path (``Evolution.submit_for_review``) — the
        sole production caller and the exact site where the duplicate pile-up
        was minted — passes ``True``. Reading pending plans happens *before*
        the file lock so the "no ``_load_all_plans`` in the hot path"
        invariant (lock criticality) is preserved for the default path.
        """
        if not plan.plan_id:
            raise ValueError("plan_id must not be empty")
        # The low-level creation door shares the documented 1-5 count cap with
        # ``refine_plan``: a row admitted here survives every restart and flows
        # into all four prompt renderings, so the bound has to hold at ingress
        # and not only at the producer slices that happen to truncate.
        #
        # Deliberate asymmetry with the read door, which admits a legacy
        # over-cap row intact: this is a *creation* primitive, never a rewrite.
        # Every caller passes a freshly minted plan; the in-place mutators
        # (``update_plan_status``, ``refine_plan``, ``update_plan_*``) rewrite
        # the store directly and do not round-trip a loaded plan back through
        # here, so no legacy row can be stranded by this raise.
        validate_acceptance_criteria_count(plan.acceptance_criteria)
        if enforce_restatement_gate:
            restated = is_plan_restatement(
                plan.title, plan.rationale, self.get_pending_plans(),
            )
            if restated is not None:
                logger.info(
                    "Skipping plan %r: restates pending plan %r "
                    "(content-word Jaccard ≥ %.2f) — not persisted",
                    plan.plan_id, restated.plan_id, RESTATEMENT_THRESHOLD,
                )
                return restated
        # Creation is the authoritative stamp: overwrite whatever the
        # caller set so the persisted hash always matches the content.
        plan.plan_content_hash = compute_plan_content_hash(plan)
        with file_lock(self._plans_path):
            # O(1) per line duplicate check — avoid full dataclass construction
            # inside the lock to keep the critical section O(n) only in bytes,
            # not in allocation cost.
            duplicate = False
            if self._plans_path.exists():
                with open(self._plans_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            # Mirror the reader-side tolerance in
                            # ``_parse_plan_line``: a valid-JSON but non-dict
                            # row is skipped, not crashed on.
                            if not isinstance(data, dict):
                                continue
                            if data.get("plan_id") == plan.plan_id:
                                duplicate = True
                                break
                        except json.JSONDecodeError:
                            continue
            if duplicate:
                raise DuplicatePlanError(
                    f"duplicate plan_id: plan with id {plan.plan_id!r} already exists"
                )
            append_jsonl_row_locked(self._plans_path, asdict(plan))
        self._invalidate_plan_index()
        return None

    @staticmethod
    def _parse_plan_line(line: str) -> EvolutionPlan | None:
        """Parse a single ``evolution_plans.jsonl`` line into ``EvolutionPlan``.

        Returns ``None`` for blank, corrupted, or empty-``plan_id`` lines so
        callers can decide whether to log/skip.  Shared by ``_load_all_plans``
        (full scan) and ``get_plan`` (indexed seek) so coercion stays in one
        place, including the ``PLAN_FIELD_KEY_ALIASES`` remap of an alias key
        onto the attribute it fills.
        """
        if not line.strip():
            return None
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, dict):
            return None
        remap_plan_field_aliases(raw)
        plan_known = {f.name for f in fields(EvolutionPlan)}
        filtered = {k: v for k, v in raw.items() if k in plan_known}
        if "issue_number" in filtered and isinstance(filtered["issue_number"], float):
            filtered["issue_number"] = int(filtered["issue_number"])
        if "phase_index" in filtered and isinstance(filtered["phase_index"], float):
            filtered["phase_index"] = int(filtered["phase_index"])
        if "inherited_hold_count" in filtered and isinstance(
            filtered["inherited_hold_count"], float,
        ):
            filtered["inherited_hold_count"] = int(filtered["inherited_hold_count"])
        if "resume_count" in filtered and isinstance(filtered["resume_count"], float):
            filtered["resume_count"] = int(filtered["resume_count"])
        if isinstance(filtered.get("priority"), str):
            filtered["priority"] = LEGACY_PRIORITY_REMAP.get(
                filtered["priority"], filtered["priority"],
            )
        for list_field in (
            "proposed_changes",
            "files_affected",
            "acceptance_criteria",
            "resume_failing_identities",
        ):
            if list_field in filtered:
                val = filtered[list_field]
                if val is None:
                    filtered[list_field] = []
                elif isinstance(val, list):
                    filtered[list_field] = [
                        item for item in val if isinstance(item, str)
                    ]
                elif isinstance(val, str):
                    filtered[list_field] = [
                        s for s in val.splitlines() if s.strip()
                    ]
                else:
                    filtered[list_field] = []
        # A legacy row can carry more than the documented
        # ``ACCEPTANCE_CRITERIA_MAX_ITEMS`` acceptance criteria: it was written before the boundary raises landed, or
        # imported into the append-only store. Surface it loudly but load it
        # intact — truncating here would silently rewrite an already-reviewed
        # contract and desynchronise its ``plan_content_hash``. The prompt
        # renderer is the choke that keeps the over-cap list out of context.
        # Deduped by ``plan_id`` for the life of the process: this parser runs
        # on every line of every ``_load_all_plans`` call (many per loop
        # iteration), so an undeduped warning would drown the signal it exists
        # to carry. The set is bounded by the number of distinct over-cap
        # legacy rows in the corpus, which only shrinks as they are re-scoped.
        over_cap = filtered.get("acceptance_criteria")
        over_cap_id = filtered.get("plan_id")
        if (
            isinstance(over_cap, list)
            and len(over_cap) > ACCEPTANCE_CRITERIA_MAX_ITEMS
            and over_cap_id not in _WARNED_OVER_CAP_PLAN_IDS
        ):
            _WARNED_OVER_CAP_PLAN_IDS.add(str(over_cap_id))
            logger.warning(
                "Plan %s carries %d acceptance_criteria (max %d) — loading "
                "intact; re-scope it to at most %d before implementation",
                filtered.get("plan_id"), len(over_cap),
                ACCEPTANCE_CRITERIA_MAX_ITEMS, ACCEPTANCE_CRITERIA_MAX_ITEMS,
            )
        # Conscious-access citation list (proposal 130). Kept out of the loop
        # above deliberately: that loop collapses ``None`` to ``[]``, which would
        # erase the ``None`` (never cited) vs ``[]`` (cited nothing) distinction
        # this field carries. A present list is filtered to its string members; a
        # present non-list value collapses to ``None``; an absent key is not in
        # ``filtered`` and takes the dataclass default (``None``).
        if "workspace_item_ids" in filtered:
            citations = filtered["workspace_item_ids"]
            if isinstance(citations, list):
                filtered["workspace_item_ids"] = [
                    item for item in citations if isinstance(item, str)
                ]
            else:
                filtered["workspace_item_ids"] = None
        # Planner read provenance (proposal 143): same ``None``-vs-``[]``
        # preserving coercion as ``workspace_item_ids`` above.
        if "grounded_reads" in filtered:
            reads = filtered["grounded_reads"]
            if isinstance(reads, list):
                filtered["grounded_reads"] = [
                    item for item in reads if isinstance(item, str)
                ]
            else:
                filtered["grounded_reads"] = None
        # Reality-grounding declarations (proposal 136): the same
        # ``None``-vs-``[]`` preserving coercion for the name list, and a
        # non-``str`` ``problem_ref`` collapses to ``None``.
        if "new_capability_names" in filtered:
            names = filtered["new_capability_names"]
            if isinstance(names, list):
                filtered["new_capability_names"] = [
                    item for item in names if isinstance(item, str)
                ]
            else:
                filtered["new_capability_names"] = None
        if "reviewer_feedback" in filtered:
            filtered["reviewer_feedback"] = coerce_feedback(
                filtered["reviewer_feedback"],
            )
        if not isinstance(filtered.get("problem_ref"), (str, type(None))):
            filtered["problem_ref"] = None
        if not isinstance(filtered.get("curated_hash"), (str, type(None))):
            filtered["curated_hash"] = None
        try:
            if isinstance(filtered.get("grounding"), dict):
                probe_known = {f.name for f in fields(GroundingProbe)}
                filtered["grounding"] = GroundingProbe(
                    **{k: v for k, v in filtered["grounding"].items() if k in probe_known},
                )
            plan = EvolutionPlan(**filtered)
        except (TypeError, KeyError, AttributeError):
            return None
        if not plan.plan_id:
            return None
        if not plan.plan_content_hash:
            # Legacy row predating proposal 021: backfill in memory so
            # every loaded plan carries a hash. A non-empty stored hash
            # is never recomputed here — verification recomputes from
            # content at decision time, so a stale stored hash is
            # evidence, not something to paper over.
            plan.plan_content_hash = compute_plan_content_hash(plan)
        return plan

    def _load_all_plans(self) -> list[EvolutionPlan]:
        if not self._plans_path.exists():
            return []
        plans = []
        for line in self._plans_path.read_text(encoding="utf-8").strip().splitlines():
            if not line.strip():
                continue
            plan = self._parse_plan_line(line)
            if plan is None:
                logger.warning("Skipping corrupted plan record in %s", self._plans_path)
                continue
            plans.append(plan)
        return plans

    def _save_all_plans(self, plans: list[EvolutionPlan]) -> None:
        atomic_write(
            self._plans_path,
            "".join(json.dumps(asdict(p)) + "\n" for p in plans),
        )
        self._invalidate_plan_index()

    def get_pending_plans(self) -> list[EvolutionPlan]:
        return [p for p in self._load_all_plans() if p.status == "pending_review"]

    def get_live_plans(self) -> list[LivePlan]:
        """Return every *live* plan (``pending_review``/``approved``/``implemented``).

        The single accessor for the live-work corpus (proposal 100): the plans
        that represent work in flight, which the ``pending_review``-only
        :meth:`get_pending_plans` cannot see once Thomas approves them or they
        land behind an open PR. Delegates to the pure
        :func:`~maestro.pr_facts.select_live_plans` oracle.
        """
        return select_live_plans(self._load_all_plans())

    def get_live_plan_records(self) -> list[EvolutionPlan]:
        """Return the live ``EvolutionPlan`` rows (``LIVE_STATUSES`` members).

        The ``EvolutionPlan``-typed sibling of :meth:`get_live_plans`: same
        live-work corpus (``pending_review``/``approved``/``implemented``, the
        single named owner :data:`~maestro.pr_facts.LIVE_STATUSES`), but it
        preserves the full ``EvolutionPlan`` records rather than projecting them
        into the frozen ``LivePlan`` view. The submission-time similarity guards
        are ``EvolutionPlan``-typed and one shared consumer reads ``.created_at``
        (which ``LivePlan`` deliberately omits), so feeding them these records
        keeps every guard's element type stable whether it reads the live or the
        ``pending_review``-only corpus.
        """
        return [p for p in self._load_all_plans() if p.status in LIVE_STATUSES]

    def find_duplicate_pending_plan(
        self,
        title: str,
        problem: str,
        threshold: float = RESTATEMENT_THRESHOLD,
        exclude_ids: Collection[str] | None = None,
    ) -> str | None:
        """Return the id of an open plan this one near-restates.

        Combines *title* and *problem* into one candidate text and compares
        it — via the zero-LLM-cost content-word Jaccard machinery behind
        :func:`is_plan_restatement` / :func:`~maestro.consciousness.detect_restatement`
        — against the combined title + rationale of every open plan in the
        corpus. Returns the first matching plan's ``plan_id`` when the overlap
        meets *threshold*, else ``None`` (including when the corpus is empty or
        the candidate has no content tokens).

        The corpus is the whole live-work set (via
        :meth:`get_live_plan_records`) — the proposal-layer guard against minting a
        copy of a plan Thomas has not yet ruled on, widened so an approved or
        open-PR-backed plan is also caught. Twin of the self-model restatement
        gate, pointed at the in-flight pile.

        *exclude_ids*, when given, drops those plan ids from the corpus before
        matching. This lets a caller ask *"is there a duplicate that is not one
        of these ids?"* — the sibling-slice dedup exemption (proposal 118) passes
        the proposal's sibling set so a co-colliding *non-sibling* plan still
        surfaces even when the first match would have been a sibling (this
        accessor returns only the first match, unlike the full-set cluster
        guards). ``None``/empty leaves the corpus untouched.
        """
        corpus = self.get_live_plan_records()
        if exclude_ids:
            excluded = set(exclude_ids)
            corpus = [p for p in corpus if p.plan_id not in excluded]
        match = is_plan_restatement(
            title, problem, corpus, threshold,
        )
        return match.plan_id if match is not None else None

    def find_theme_cluster_pending_plans(
        self,
        title: str,
        description: str,
        threshold: int = 2,
        cluster_fraction: float = THEME_CLUSTER_FRACTION,
    ) -> list[EvolutionPlan]:
        """Return the cluster of open plans sharing this theme.

        The pairwise :meth:`find_duplicate_pending_plan` guard compares the
        candidate against each pending plan in isolation, so a backlog of
        near-restatements — each one individually below the Jaccard cutoff,
        yet all *meaning the same thing* — can accumulate one plan at a time.
        This cluster-level guard closes that loophole at the minting root.

        It extracts the candidate's dominant content-word tokens (lowercased,
        stop-word- and short-token-filtered, via
        :func:`~maestro.consciousness._content_tokens`) and counts every open
        plan whose own dominant tokens cover more than *cluster_fraction* of the
        candidate's (directional containment: ``|shared| / |candidate|``). When
        that count reaches *threshold*, the matching plans are returned as the
        themed cluster.

        The corpus is the whole live-work set
        (``pending_review``/``approved``/``implemented``, via
        :meth:`get_live_plan_records`).

        Returns an empty list — signalling "no cluster, mint normally" — when
        the candidate has no content tokens (empty or stop-word/short-only
        text), when the candidate carries fewer than
        :data:`THEME_CLUSTER_MIN_CANDIDATE_TOKENS` dominant tokens (too few for
        directional containment to be trustworthy — see that constant), when the
        corpus is empty, or when fewer than *threshold* corpus plans share the
        theme.
        """
        candidate_tokens = _content_tokens(f"{title}\n{description}")
        if len(candidate_tokens) < THEME_CLUSTER_MIN_CANDIDATE_TOKENS:
            return []
        corpus = self.get_live_plan_records()
        cluster: list[EvolutionPlan] = []
        for plan in corpus:
            plan_tokens = _content_tokens(
                f"{plan.title or ''}\n{plan.rationale or ''}",
            )
            if not plan_tokens:
                continue
            shared = candidate_tokens & plan_tokens
            containment = len(shared) / len(candidate_tokens)
            if containment > cluster_fraction:
                cluster.append(plan)
        if len(cluster) >= threshold:
            return cluster
        return []

    def sweep_clustered_pending_plans(
        self,
        threshold: float = RESTATEMENT_THRESHOLD,
        min_cluster_size: int = 2,
        lever_attempts: LeverAttemptsBackend | None = None,
    ) -> list[str]:
        """Mechanically drain redundant ``pending_review`` cluster members.

        A deterministic, zero-SDK, no-approval sweep that extends the
        *submit-time* duplicate-guard detectors
        (:func:`find_pending_plan_cluster`) into a *periodic* backlog drain.
        It repeatedly locates a cluster of at least *min_cluster_size*
        ``pending_review`` plans whose content-word Jaccard meets *threshold*,
        keeps exactly the newest member of that cluster (by ``created_at``,
        ``plan_id`` as a stable tiebreak), and marks every older member
        ``obsolete`` via the owned terminal act :meth:`mark_plan_obsolete`.

        Returns the list of obsoleted ``plan_id``\\ s, in obsoletion order.

        The loop re-reads pending plans each pass and exits only when no
        qualifying cluster remains, so the post-sweep backlog contains no
        cluster of size ≥ *min_cluster_size* — which makes the sweep
        **idempotent**: a second run over the drained set finds nothing and
        writes nothing. It is also a strict no-op (no writes) when fewer than
        *min_cluster_size* plans are pending, when *min_cluster_size* < 2, or
        when no plans cluster. Each pass removes at least one member from the
        pending set — retired, or refused because a concurrent writer (the
        dashboard) already moved it out of ``pending_review`` — so the loop
        terminates after at most one pass per pending plan.

        Although :func:`find_pending_plan_cluster` reads the whole live-work
        corpus (``pending_review``/``approved``/``implemented``, since proposals
        100/111), this sweep restricts each cluster to its ``pending_review``
        members before retiring: an approved or implemented plan is never a
        sweep target (:meth:`mark_plan_obsolete` refuses a non-``pending_review``
        plan) and never counts toward *min_cluster_size*. Each retire fires
        through :func:`~maestro.lever_capture.fire_and_capture` into
        *lever_attempts* (defaulting to the JSONL ledger) with a
        :func:`~maestro.lever_capture.verify_obsolete_persisted` re-read, and
        only a fire recorded ``True`` is appended — so the returned list can
        never name a prune that did not happen, even when a member left
        ``pending_review`` between this pass's snapshot and the locked write.

        Reuses the same content-word Jaccard machinery as the minting-time
        guards — no LLM call, no new threshold. ``generate_task`` wires the
        *threshold* / *min_cluster_size* arguments to ``[loop]
        pending_cluster_threshold`` / ``pending_cluster_max``.
        """
        obsoleted: list[str] = []
        if min_cluster_size < 2:
            return obsoleted
        ledger = lever_attempts or JsonlLeverAttemptsLedger(self._dir)
        # Each qualifying pass removes ≥1 plan from the pending set (retired,
        # or already moved out by a concurrent writer), so the initial pending count
        # is a hard upper bound on the number of passes — a guaranteed-
        # terminating ceiling that needs no separate progress sentinel.
        for _ in range(len(self.get_pending_plans())):
            pending = self.get_pending_plans()
            if len(pending) < min_cluster_size:
                break
            # ``find_pending_plan_cluster`` reads the whole live-work corpus
            # (proposals 100/111), but this periodic sweep only ever retires
            # ``pending_review`` members — ``mark_plan_obsolete`` refuses an
            # approved/implemented plan, so counting one toward the cluster or
            # naming one a retire target would report a prune that never
            # happened. Restrict each cluster to the current pending set (the
            # authoritative ``get_pending_plans`` accessor, not a status literal)
            # so the size gate, the kept-newest pick, and the retire loop all
            # stay on the pending backlog this sweep owns.
            pending_ids = {plan.plan_id for plan in pending}
            cluster: list[EvolutionPlan] = []
            for seed in pending:
                seed_text = f"{seed.title or ''}\n{seed.rationale or ''}"
                candidate = [
                    plan
                    for plan in find_pending_plan_cluster(self, seed_text, threshold)
                    if plan.plan_id in pending_ids
                ]
                if len(candidate) >= min_cluster_size:
                    cluster = candidate
                    break
            if not cluster:
                break
            newest = max(cluster, key=lambda p: (p.created_at or "", p.plan_id))
            for member in cluster:
                if member.plan_id == newest.plan_id:
                    continue
                fired = fire_and_capture(
                    ledger,
                    "mark_plan_obsolete",
                    self.mark_plan_obsolete,
                    member.plan_id,
                    _CLUSTER_SWEEP_OBSOLETE_REASON,
                    verify=functools.partial(
                        verify_obsolete_persisted, self, member.plan_id,
                    ),
                )
                if fired:
                    obsoleted.append(member.plan_id)
        return obsoleted

    def sweep_restatement_pending_plans(
        self,
        threshold: float = RESTATEMENT_THRESHOLD,
        lever_attempts: LeverAttemptsBackend | None = None,
    ) -> list[str]:
        """Retire ``pending_review`` plans restating a strictly-newer plan.

        The pairwise complement of :meth:`sweep_clustered_pending_plans`.
        Where the cluster sweep drains *N-member clusters*, this drains
        *pairwise restatement chains*: a plan A that restates a strictly-
        newer plan B at or above *threshold* content-word Jaccard, yet whose
        pair never forms a cluster of the required size, would otherwise slip
        through and pile up in the review queue.

        Orders the ``pending_review`` backlog newest-first by ``(created_at,
        plan_id)`` — the same total order that :meth:`sweep_clustered_pending_plans`
        uses to pick the canonical member — and walks it newest → oldest,
        accumulating *survivors*. A plan that :func:`is_plan_restatement` of
        any already-seen (hence strictly-newer) survivor is retired via the
        owned terminal act :meth:`mark_plan_obsolete`, its literal return
        captured by :func:`~maestro.lever_capture.fire_and_capture` through
        *lever_attempts* (the injected :class:`~maestro.lever_capture
        .LeverAttemptsBackend`, defaulting to a JSONL adapter over this memory
        directory); every other plan becomes a survivor. The newest member of
        each restatement chain is thus always kept, and a genuinely unique plan
        is never touched.

        Returns the retired ``plan_id``\\ s in obsoletion order (newest-first
        walk, so oldest chain members first per chain).

        Zero SDK cost and **idempotent**: after one pass no survivor restates
        a strictly-newer survivor (Jaccard is symmetric, so the older member
        of any restating pair is always the one retired), so a second run over
        the drained backlog matches nothing, fires nothing, and writes no
        ledger row. Reuses the same content-word Jaccard machinery as the
        minting-time duplicate guard — no LLM call, no new threshold.
        """
        ledger = lever_attempts or JsonlLeverAttemptsLedger(self._dir)
        pending = self.get_pending_plans()
        ordered = sorted(
            pending,
            key=lambda p: (p.created_at or "", p.plan_id),
            reverse=True,
        )
        survivors: list[EvolutionPlan] = []
        obsoleted: list[str] = []
        for plan in ordered:
            match = is_plan_restatement(
                plan.title or "", plan.rationale or "", survivors, threshold,
            )
            if match is None:
                survivors.append(plan)
                continue
            fired = fire_and_capture(
                ledger,
                "mark_plan_obsolete",
                self.mark_plan_obsolete,
                plan.plan_id,
                _RESTATEMENT_SWEEP_OBSOLETE_REASON,
                verify=functools.partial(
                    verify_obsolete_persisted, self, plan.plan_id,
                ),
            )
            if fired:
                obsoleted.append(plan.plan_id)
        return obsoleted

    def sweep_expired_pending_plans(
        self,
        max_age_days: int,
        lever_attempts: LeverAttemptsBackend | None = None,
    ) -> list[str]:
        """Retire ``pending_review`` plans older than *max_age_days*.

        The age-based complement of the content-based cluster/restatement
        sweeps: where those retire *redundant* plans, this retires plans that
        have simply aged out of the review window un-reviewed. A genuinely
        unique plan the content sweeps deliberately keep still rots against a
        moving tree if it is never reviewed — approving one implements against
        premises later-merged work may have superseded — so an unconditional
        age horizon disposes of it.

        Walks :meth:`get_pending_plans` and, for every plan whose
        ``created_at`` is strictly older than *max_age_days* before
        :func:`datetime.now`, fires the owned terminal act
        :meth:`mark_plan_obsolete`, its literal return captured by
        :func:`~maestro.lever_capture.fire_and_capture` through
        *lever_attempts* (the injected
        :class:`~maestro.lever_capture.LeverAttemptsBackend`, defaulting to a
        JSONL adapter over this memory directory) — exactly as the restatement
        sweep does. Auto-obsoleting an un-reviewed plan is disposal, not a code
        change, and ``mark_plan_obsolete`` skips ``rejection_reason`` so the
        expiry is never read as a human rejection. An approval moves a plan out
        of ``pending_review`` (to ``approved``), which this sweep never sees —
        the approval is the explicit hold.

        A plan whose ``created_at`` is empty or unparseable is skipped, not
        crashed — its age is unknown, so it is left for a human. Returns the
        retired ``plan_id``\\ s in walk order.

        Guarded as a no-op when ``max_age_days <= 0`` (returns ``[]``, fires
        nothing, writes no ledger row). Zero SDK cost and **idempotent**: a
        retired plan leaves ``pending_review``, so a second run over the drained
        backlog matches nothing and writes no ledger row.
        """
        if max_age_days <= 0:
            return []
        ledger = lever_attempts or JsonlLeverAttemptsLedger(self._dir)
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        obsoleted: list[str] = []
        for plan in self.get_pending_plans():
            raw = plan.created_at
            if not raw:
                continue
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                # Unparseable timestamp: age is unknown, leave it for a human.
                continue
            created = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            if created >= cutoff:
                continue
            fired = fire_and_capture(
                ledger,
                "mark_plan_obsolete",
                self.mark_plan_obsolete,
                plan.plan_id,
                _EXPIRY_SWEEP_OBSOLETE_REASON,
                verify=functools.partial(
                    verify_obsolete_persisted, self, plan.plan_id,
                ),
            )
            if fired:
                obsoleted.append(plan.plan_id)
        return obsoleted

    def sweep_orphaned_cures(
        self,
        first_seen: dict[str, int],
        iteration: int,
        retire_age_cycles: int,
        *,
        deliver_disposition: Callable[[EvolutionPlan], bool],
        salvage_permits_retire: Callable[[EvolutionPlan], bool] = (
            lambda _plan: True
        ),
        lever_attempts: LeverAttemptsBackend | None = None,
    ) -> list[str]:
        """Mechanically retire dead ``implemented``-but-unmerged cures.

        The dispose complement of audit rule 11 (``check_orphaned_cure``),
        which is detection-only: the same aged cures re-flag every cycle
        with no retirement path, because :meth:`mark_plan_obsolete`
        refuses non-``pending_review`` plans by default. This sweep closes
        that detect→retire gap for cures that are demonstrably dead:
        a plan is retired only when **both**

        * its age in audit cycles — ``iteration - first_seen[plan_id]``,
          measured against rule 11's first-seen sidecar (a plan absent
          from *first_seen* has age 0 and is never touched) — is at least
          *retire_age_cycles*, a ceiling set well above the rule-11 flag
          threshold; and
        * ``last_conflict_resolution_attempt_at`` is set — tend stamps it
          each time it directly observes a genuine local conflict on the
          PR branch and clears it on head-advance, so a set value is live
          conflict evidence at zero network cost.

        Before any retirement fires, two caller-supplied preconditions
        are consulted in order. First, *salvage_permits_retire* — a
        ``Callable[[EvolutionPlan], bool]`` (permissive by default) — must
        return ``True`` for the candidate (proposal 094): a ``False``
        means the plan backs a still-fixable, non-disposable open own PR
        (``SALVAGE_AND_LAND``) that the loop should reconcile and land
        rather than retire, so the candidate is skipped *before any
        disposition is delivered* — no delivery, no fire, no ledger row —
        and the plan stays ``implemented`` for tend to keep servicing.
        Second, *deliver_disposition* — a
        ``Callable[[EvolutionPlan], bool]`` supplied by the caller — must
        return ``True`` for the candidate. It delivers the explicit
        close-or-keep disposition (a PR comment, a durable message to
        Thomas, and a ``pr_number``-keyed standdown) that makes silent
        retirement of an open-PR plan impossible; when it returns
        ``False`` (no PR URL, unparseable URL, absent message store, or a
        failed comment post) the candidate is skipped without firing —
        the plan stays ``implemented`` and tend keeps servicing it, so
        nothing is orphaned. Retirement and both preconditions are
        thus atomic per plan.

        Each retirement fires :meth:`mark_plan_obsolete` with
        ``allow_implemented=True`` through
        :func:`~maestro.lever_capture.fire_and_capture`, so every fire
        lands exactly one row through *lever_attempts* (the injected
        :class:`~maestro.lever_capture.LeverAttemptsBackend`, defaulting
        to a JSONL adapter over this memory directory) and the
        owned-act-drought sensor (rule 18) self-extinguishes on the
        resulting ``obsolete`` status event. Returns the retired
        ``plan_id``\\ s in iteration order.

        The PR itself stays open — external blockers remain human-owned;
        this prunes Maestro's plan state only. Accepted telemetry gap:
        retirement removes the plan from ``check_merged_plans``'
        implemented-only view, so a human who later resolves the conflict
        and merges the still-open PR leaves the plan ``obsolete`` — no
        ``merged`` status, no ``VersionRecord``, invisible to
        impact-review rule 9. Accepted by design: the ceiling plus live
        conflict evidence make such a rescue deliberately rare, and PR
        disposition stays human-owned. Zero SDK cost and
        **idempotent**: a retired plan leaves ``implemented`` status, so
        a second run matches nothing and writes no ledger row. Plans in
        any other status (including ``pending_review``) are never touched.
        """
        def _retire(pid: str, reason: str) -> bool:
            return self.mark_plan_obsolete(
                pid, reason, allow_implemented=True,
            )

        ledger = lever_attempts or JsonlLeverAttemptsLedger(self._dir)
        retired: list[str] = []
        for plan in self.get_all_plans():
            if plan.status != "implemented":
                continue
            if plan.last_conflict_resolution_attempt_at is None:
                continue
            age = iteration - first_seen.get(plan.plan_id, iteration)
            if age < retire_age_cycles:
                continue
            if not salvage_permits_retire(plan):
                # Salvage veto (proposal 094): the plan backs a still-fixable,
                # non-disposable open own PR (``SALVAGE_AND_LAND``) — the loop
                # should reconcile and land it, not throw the work away. Skip
                # *before* any disposition is delivered, leaving the plan
                # ``implemented`` so tend keeps servicing it: no delivery, no
                # fire, no ledger row.
                continue
            if not deliver_disposition(plan):
                # Delivery could not complete — leave the plan ``implemented``
                # so tend keeps servicing it. Retirement and its disposition
                # handoff are atomic per plan: no delivery, no fire, no row.
                continue
            fired = fire_and_capture(
                ledger,
                "mark_plan_obsolete",
                _retire,
                plan.plan_id,
                _ORPHANED_CURE_SWEEP_OBSOLETE_REASON,
                verify=functools.partial(
                    verify_obsolete_persisted, self, plan.plan_id,
                ),
            )
            if fired:
                retired.append(plan.plan_id)
        return retired

    def get_all_plans(self) -> list[EvolutionPlan]:
        return self._load_all_plans()

    def get_cited_source_task_ids(self) -> set[str]:
        """Return every non-empty ``source_task_id`` across all plan rows.

        Reads ``evolution_plans.jsonl`` once and collects citations
        across every plan ``status`` (see :data:`maestro.types.PlanStatus`
        for the full set). Status filtering would let "rejected
        promotion attempts" re-trigger downstream rules; the union
        semantics treat any LLM nomination as proof the task was
        already noticed. Used by
        :func:`audit_checks.check_kept_task_not_promoted` and
        :meth:`EvolutionEngine._gap_context_inputs` to produce identically
        shaped evidence.
        """
        return {
            p.source_task_id
            for p in self._load_all_plans()
            if isinstance(p.source_task_id, str) and p.source_task_id
        }

    def live_cited_source_task_ids(self) -> set[str]:
        """Return non-null ``source_task_id``s cited by *live* plans.

        A "live" citation is one made by a plan whose status is
        ``pending_review``, ``approved`` or ``implemented`` (see
        :data:`RECITE_LIVE_STATUSES`, which mirrors the canonical
        :data:`~maestro.pr_facts.LIVE_STATUSES`) — a task branch already
        claimed by an in-flight proposal, an approved-but-unimplemented plan,
        or a plan sitting behind an open PR. Unlike
        :meth:`get_cited_source_task_ids` (which unions across *every*
        status), this excludes terminal citations
        (``rejected``/``failed``/``obsolete``/``merged``): a branch a
        terminal attempt named is free to be re-cited because that attempt
        left no open claim. Empty when no live plan cites a branch. Zero LLM
        cost — one file read.
        """
        return _live_cited_source_task_ids(self._load_all_plans())

    def get_promotable_branches(
        self,
        recent_n: int = 30,
        max_results: int = 5,
    ) -> list[dict[str, str]]:
        """Return successful task branches not yet cited by any evolution plan.

        Reads recent tasks via :meth:`get_recent_tasks`, filters to
        those with ``outcome == "success"`` and a non-empty ``branch``
        field, then removes any whose ``task_id`` appears in the set
        returned by :meth:`get_cited_source_task_ids`. Returns at most
        *max_results* dicts with keys ``task_id``, ``branch``, and
        ``goal`` (truncated to 120 chars), **most recent first**: when
        more than *max_results* branches qualify, the freshest ones are
        kept (the oldest branches have rotted most against
        ``origin/main`` and are the worst promotion candidates). Zero
        LLM cost — pure file reads and set difference.
        """
        tasks = self.get_recent_tasks(recent_n)
        cited = self.get_cited_source_task_ids()
        results: list[dict[str, str]] = []
        # get_recent_tasks returns oldest-first; iterate in reverse so the
        # max_results cap keeps the most recent qualifying branches.
        for t in reversed(tasks):
            if (
                t.outcome == "success"
                and t.branch
                and t.task_id not in cited
            ):
                results.append({
                    "task_id": t.task_id,
                    "branch": t.branch,
                    "goal": (t.goal or "")[:120],
                })
                if len(results) >= max_results:
                    break
        return results

    @staticmethod
    def _plan_search_text(plan: EvolutionPlan) -> str:
        """Build the BM25-indexed text for a plan.

        Indexes the four narrative text fields (title, rationale,
        proposed_changes, expected_impact) and, when present,
        the rejection_reason — the latter is critical for "why was X
        rejected?" lookups.  Categorical metadata (category,
        priority, status, files_affected) is intentionally excluded so
        it does not pollute BM25 scores with uninformative tokens.
        """
        parts = [
            plan.title,
            plan.rationale,
            " ".join(plan.proposed_changes),
            plan.expected_impact,
        ]
        if plan.rejection_reason:
            parts.append(plan.rejection_reason)
        return "\n".join(parts)

    def search_plans(self, query: str) -> list[EvolutionPlan]:
        """Return evolution plans relevant to ``query``, ranked by BM25.

        Indexes each plan's title, rationale, proposed_changes,
        expected_impact, and rejection_reason (when set).
        Returns an empty list for empty queries, empty plan history,
        or when no plan contains any of the query's tokens.
        """
        plans = self._load_all_plans()
        if not plans:
            return []
        corpus = [self._plan_search_text(p) for p in plans]
        return [plans[i] for i in bm25_rank(corpus, query)]

    def _build_plan_index(self) -> dict[str, int]:
        """Stream ``_plans_path`` once and record each plan_id's seek cookie.

        The map is ``plan_id -> opaque seek position`` returned by
        ``f.tell()`` *before* the line was read.  Storing the cookie
        (rather than a byte offset) keeps the index portable across text
        mode and binary mode and round-trips through any
        ``IOBase``-compatible stream.  When duplicate ``plan_id`` values
        appear (which ``save_evolution_plan`` actively prevents but
        legacy files may carry), the *last* occurrence wins so
        ``get_plan`` agrees with ``_load_all_plans``'s "later wins"
        iteration order.  Corrupted lines are skipped silently — they
        contribute no entry, so a stale lookup falls through to the
        linear scan via ``get_plan``'s fallback.

        The resulting cache is process-local: another process appending
        to ``_plans_path`` does not invalidate it, so ``get_plan`` treats
        a cached miss as stale and rebuilds once before reporting absence.
        """
        index: dict[str, int] = {}
        if not self._plans_path.exists():
            return index
        try:
            with open(self._plans_path, encoding="utf-8") as f:
                while True:
                    cookie = f.tell()
                    line = f.readline()
                    if not line:
                        break
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        data = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    # Mirror the reader-side tolerance in ``_parse_plan_line``:
                    # a valid-JSON but non-dict row contributes no index entry
                    # rather than crashing with ``AttributeError``.
                    if not isinstance(data, dict):
                        continue
                    plan_id = data.get("plan_id")
                    if isinstance(plan_id, str) and plan_id:
                        index[plan_id] = cookie
        except OSError:
            return {}
        return index

    def _invalidate_plan_index(self) -> None:
        """Reset the cached index so the next ``get_plan`` rebuilds it."""
        self._plan_index = None

    def get_plan(self, plan_id: str) -> EvolutionPlan | None:
        if not plan_id or not self._plans_path.exists():
            return None
        just_built = self._plan_index is None
        if self._plan_index is None:
            self._plan_index = self._build_plan_index()
        offset = self._plan_index.get(plan_id)
        if offset is None and not just_built:
            # A miss on a warm index may be a plan another process
            # appended since the build — re-read the file once. This
            # makes every warm miss one full-file scan, so a hot path
            # probing unknown ids pays O(n) per probe; correctness wins.
            self._plan_index = self._build_plan_index()
            offset = self._plan_index.get(plan_id)
        if offset is None:
            return None
        try:
            with open(self._plans_path, encoding="utf-8") as f:
                f.seek(offset)
                line = f.readline()
        except OSError:
            return None
        plan = self._parse_plan_line(line)
        if plan is None or plan.plan_id != plan_id:
            # Stale offset — file was rewritten without invalidation
            # (e.g. concurrent archive rotation between two reads).
            # Drop the cache and fall back to a linear scan.
            self._plan_index = None
            for p in self._load_all_plans():
                if p.plan_id == plan_id:
                    return p
            return None
        return plan

    def update_plan_status(
        self,
        plan_id: str,
        status: PlanStatus,
        pr_url: object = _UNSET,
        rejection_reason: object = _UNSET,
        reviewed_at: object = _UNSET,
        last_pr_reminder_at: object = _UNSET,
        pr_reminder_count: object = _UNSET,
        last_conflict_resolution_attempt_at: object = _UNSET,
        last_quality_fix_attempt_at: object = _UNSET,
        failure_count: object = _UNSET,
        last_failure_at: object = _UNSET,
        last_failure_reason: object = _UNSET,
        notified_content_hash: object = _UNSET,
        ready_for_review_pending_sha: object = _UNSET,
        blocked_question_id: object = _UNSET,
        blocked_answer: object = _UNSET,
        *,
        expect_status: PlanStatus | None = None,
        expect_content_hash: str | None = None,
    ) -> bool:
        """Update a plan's status.

        Returns ``True`` when the plan was found *and* — if ``expect_status``
        is supplied — its current status matched. Returns ``False`` when the
        plan is missing or the lock-checked status does not match.
        ``expect_status`` closes the cross-surface TOCTOU window: the
        dashboard and Telegram both read-then-update, and without an
        under-lock recheck two concurrent surfaces could each pass the
        read-time ``pending_review`` check and then stomp each other's
        write, landing a status the human never chose. The ``None`` default
        keeps the internal implementation / merge / tend callers — which
        re-pass ``plan.status`` for side-channel field updates —
        unconditional.

        ``expect_content_hash`` closes the *sibling* window, the one
        ``expect_status`` alone cannot: :meth:`refine_plan` rewrites a
        plan's body while deliberately leaving it ``pending_review``, so
        a review surface that compared the reviewer's hash snapshot
        outside this lock can still land an ``approved`` (or
        ``rejected``) status on content the human never saw. Supplying
        the snapshot makes the content precondition part of the same
        transaction as the decision — the **Plan content-hash binding**
        invariant. Like ``expect_status`` it defaults to ``None`` so the
        internal implementation / merge / tend callers, which change
        side-channel fields rather than record a human decision, stay
        unconditional.

        ``"obsolete"`` is rejected here: the obsolete path must go through
        :meth:`mark_plan_obsolete` so ``obsolete_reason`` is always
        populated (the dataclass field's invariant). Without this guard
        a caller could leave ``obsolete_reason=None`` while flipping the
        status, silently breaking the audit trail the dashboard renders
        from ``obsolete_reason``.

        A transition *into* ``"rejected"`` without an explicit
        ``reviewed_at`` stamps the rejection moment, so the M1 rejection
        cooldown runs from a PR close rather than the earlier approval; a
        rejected→rejected re-write keeps the original stamp.
        """
        if status == "obsolete":
            raise ValueError(
                "update_plan_status cannot set 'obsolete' — call "
                "Memory.mark_plan_obsolete(plan_id, reason) so "
                "obsolete_reason is populated."
            )
        if pr_url is not _UNSET and pr_url is not None and not isinstance(pr_url, str):
            raise TypeError(f"pr_url must be a string or None, got {type(pr_url).__name__}")
        if rejection_reason is not _UNSET and rejection_reason is not None and not isinstance(rejection_reason, str):
            raise TypeError(f"rejection_reason must be a string or None, got {type(rejection_reason).__name__}")
        if reviewed_at is not _UNSET and reviewed_at is not None and not isinstance(reviewed_at, str):
            raise TypeError(f"reviewed_at must be a string or None, got {type(reviewed_at).__name__}")
        if last_pr_reminder_at is not _UNSET and last_pr_reminder_at is not None and not isinstance(last_pr_reminder_at, str):
            raise TypeError(f"last_pr_reminder_at must be a string or None, got {type(last_pr_reminder_at).__name__}")
        if pr_reminder_count is not _UNSET and not isinstance(pr_reminder_count, int):
            raise TypeError(f"pr_reminder_count must be an int, got {type(pr_reminder_count).__name__}")
        if last_conflict_resolution_attempt_at is not _UNSET and last_conflict_resolution_attempt_at is not None and not isinstance(last_conflict_resolution_attempt_at, str):
            raise TypeError(f"last_conflict_resolution_attempt_at must be a string or None, got {type(last_conflict_resolution_attempt_at).__name__}")
        if last_quality_fix_attempt_at is not _UNSET and last_quality_fix_attempt_at is not None and not isinstance(last_quality_fix_attempt_at, str):
            raise TypeError(f"last_quality_fix_attempt_at must be a string or None, got {type(last_quality_fix_attempt_at).__name__}")
        if failure_count is not _UNSET and not isinstance(failure_count, int):
            raise TypeError(f"failure_count must be an int, got {type(failure_count).__name__}")
        if last_failure_at is not _UNSET and last_failure_at is not None and not isinstance(last_failure_at, str):
            raise TypeError(f"last_failure_at must be a string or None, got {type(last_failure_at).__name__}")
        if last_failure_reason is not _UNSET and last_failure_reason is not None and not isinstance(last_failure_reason, str):
            raise TypeError(f"last_failure_reason must be a string or None, got {type(last_failure_reason).__name__}")
        if notified_content_hash is not _UNSET and not isinstance(notified_content_hash, str):
            raise TypeError(f"notified_content_hash must be a string, got {type(notified_content_hash).__name__}")
        if ready_for_review_pending_sha is not _UNSET and ready_for_review_pending_sha is not None and not isinstance(ready_for_review_pending_sha, str):
            raise TypeError(f"ready_for_review_pending_sha must be a string or None, got {type(ready_for_review_pending_sha).__name__}")
        if blocked_question_id is not _UNSET and blocked_question_id is not None and not isinstance(blocked_question_id, str):
            raise TypeError(f"blocked_question_id must be a string or None, got {type(blocked_question_id).__name__}")
        if blocked_answer is not _UNSET and blocked_answer is not None and not isinstance(blocked_answer, str):
            raise TypeError(f"blocked_answer must be a string or None, got {type(blocked_answer).__name__}")
        with file_lock(self._plans_path):
            plans = self._load_all_plans()
            found = False
            for p in plans:
                if p.plan_id == plan_id:
                    if expect_status is not None and p.status != expect_status:
                        # A rival surface already resolved this plan while we
                        # held only a read-time check. Refuse rather than
                        # overwrite the decision it recorded.
                        return False
                    if (
                        expect_content_hash is not None
                        and compute_plan_content_hash(p) != expect_content_hash
                    ):
                        # Plan content-hash binding: the decision and the
                        # content it is a decision *about* must be validated
                        # under this one lock. A concurrent refine_plan() can
                        # rewrite the body while leaving the status
                        # ``pending_review``, so a status check alone would
                        # let an approval land on unseen content. Keep this
                        # check inside the lock.
                        return False
                    found = True
                    prev_status = p.status
                    p.status = status
                    if pr_url is not _UNSET:
                        p.pr_url = pr_url  # type: ignore[assignment]
                    if rejection_reason is not _UNSET:
                        p.rejection_reason = rejection_reason  # type: ignore[assignment]
                    if reviewed_at is not _UNSET:
                        p.reviewed_at = reviewed_at  # type: ignore[assignment]
                    elif status == "rejected" and prev_status != "rejected":
                        p.reviewed_at = datetime.now(UTC).isoformat()
                    if last_pr_reminder_at is not _UNSET:
                        p.last_pr_reminder_at = last_pr_reminder_at  # type: ignore[assignment]
                    if pr_reminder_count is not _UNSET:
                        p.pr_reminder_count = pr_reminder_count  # type: ignore[assignment]
                    if last_conflict_resolution_attempt_at is not _UNSET:
                        p.last_conflict_resolution_attempt_at = last_conflict_resolution_attempt_at  # type: ignore[assignment]
                    if last_quality_fix_attempt_at is not _UNSET:
                        p.last_quality_fix_attempt_at = last_quality_fix_attempt_at  # type: ignore[assignment]
                    if failure_count is not _UNSET:
                        p.failure_count = failure_count  # type: ignore[assignment]
                    if last_failure_at is not _UNSET:
                        p.last_failure_at = last_failure_at  # type: ignore[assignment]
                    if last_failure_reason is not _UNSET:
                        p.last_failure_reason = last_failure_reason  # type: ignore[assignment]
                    if notified_content_hash is not _UNSET:
                        p.notified_content_hash = notified_content_hash  # type: ignore[assignment]
                    if ready_for_review_pending_sha is not _UNSET:
                        p.ready_for_review_pending_sha = ready_for_review_pending_sha  # type: ignore[assignment]
                    if blocked_question_id is not _UNSET:
                        p.blocked_question_id = blocked_question_id  # type: ignore[assignment]
                    if blocked_answer is not _UNSET:
                        p.blocked_answer = blocked_answer  # type: ignore[assignment]
                    break
            if found:
                self._save_all_plans(plans)
            return found

    def update_plan_tend_fields(
        self,
        plan_id: str,
        *,
        last_pr_reminder_at: object = _UNSET,
        pr_reminder_count: object = _UNSET,
        last_conflict_resolution_attempt_at: object = _UNSET,
        last_conflict_resolution_base_sha: object = _UNSET,
        last_conflict_resolution_head_sha: object = _UNSET,
        last_conflict_resolution_ci_conclusion: object = _UNSET,
        last_quality_fix_attempt_at: object = _UNSET,
        last_quality_fix_head_sha: object = _UNSET,
        last_quality_fix_base_sha: object = _UNSET,
        last_quality_fix_ci_conclusion: object = _UNSET,
        conflict_resolution_signature_attempts: object = _UNSET,
        quality_fix_signature_attempts: object = _UNSET,
        ready_for_review_pending_sha: object = _UNSET,
    ) -> bool:
        """Update a plan's mechanical tend-flow fields without touching status.

        The evolution-PR tend flow persists reminder, cooldown, and
        ready-for-review markers as it works.  :meth:`update_plan_status`
        would do this, but it *requires* a status positional and refuses to
        write ``"obsolete"`` — so the tend flow's habit of re-passing
        ``plan.status`` to satisfy that positional raises ``ValueError`` the
        moment it services a retired (``obsolete``) plan's still-open PR.

        This status-agnostic updater mirrors :meth:`update_plan_status`'s
        read-modify-write (``file_lock`` → ``_load_all_plans`` → mutate →
        ``_save_all_plans``) and per-field type validation, but never touches
        ``status`` or ``obsolete_reason``, so it works for a plan of *any*
        status.  Returns ``True`` when the plan is found, ``False`` otherwise.
        """
        if last_pr_reminder_at is not _UNSET and last_pr_reminder_at is not None and not isinstance(last_pr_reminder_at, str):
            raise TypeError(f"last_pr_reminder_at must be a string or None, got {type(last_pr_reminder_at).__name__}")
        if pr_reminder_count is not _UNSET and not isinstance(pr_reminder_count, int):
            raise TypeError(f"pr_reminder_count must be an int, got {type(pr_reminder_count).__name__}")
        if last_conflict_resolution_attempt_at is not _UNSET and last_conflict_resolution_attempt_at is not None and not isinstance(last_conflict_resolution_attempt_at, str):
            raise TypeError(f"last_conflict_resolution_attempt_at must be a string or None, got {type(last_conflict_resolution_attempt_at).__name__}")
        if last_conflict_resolution_base_sha is not _UNSET and last_conflict_resolution_base_sha is not None and not isinstance(last_conflict_resolution_base_sha, str):
            raise TypeError(f"last_conflict_resolution_base_sha must be a string or None, got {type(last_conflict_resolution_base_sha).__name__}")
        if last_conflict_resolution_head_sha is not _UNSET and last_conflict_resolution_head_sha is not None and not isinstance(last_conflict_resolution_head_sha, str):
            raise TypeError(f"last_conflict_resolution_head_sha must be a string or None, got {type(last_conflict_resolution_head_sha).__name__}")
        if last_conflict_resolution_ci_conclusion is not _UNSET and last_conflict_resolution_ci_conclusion is not None and not isinstance(last_conflict_resolution_ci_conclusion, str):
            raise TypeError(f"last_conflict_resolution_ci_conclusion must be a string or None, got {type(last_conflict_resolution_ci_conclusion).__name__}")
        if last_quality_fix_attempt_at is not _UNSET and last_quality_fix_attempt_at is not None and not isinstance(last_quality_fix_attempt_at, str):
            raise TypeError(f"last_quality_fix_attempt_at must be a string or None, got {type(last_quality_fix_attempt_at).__name__}")
        if last_quality_fix_head_sha is not _UNSET and last_quality_fix_head_sha is not None and not isinstance(last_quality_fix_head_sha, str):
            raise TypeError(f"last_quality_fix_head_sha must be a string or None, got {type(last_quality_fix_head_sha).__name__}")
        if last_quality_fix_base_sha is not _UNSET and last_quality_fix_base_sha is not None and not isinstance(last_quality_fix_base_sha, str):
            raise TypeError(f"last_quality_fix_base_sha must be a string or None, got {type(last_quality_fix_base_sha).__name__}")
        if last_quality_fix_ci_conclusion is not _UNSET and last_quality_fix_ci_conclusion is not None and not isinstance(last_quality_fix_ci_conclusion, str):
            raise TypeError(f"last_quality_fix_ci_conclusion must be a string or None, got {type(last_quality_fix_ci_conclusion).__name__}")
        if conflict_resolution_signature_attempts is not _UNSET and not isinstance(conflict_resolution_signature_attempts, int):
            raise TypeError(f"conflict_resolution_signature_attempts must be an int, got {type(conflict_resolution_signature_attempts).__name__}")
        if quality_fix_signature_attempts is not _UNSET and not isinstance(quality_fix_signature_attempts, int):
            raise TypeError(f"quality_fix_signature_attempts must be an int, got {type(quality_fix_signature_attempts).__name__}")
        if ready_for_review_pending_sha is not _UNSET and ready_for_review_pending_sha is not None and not isinstance(ready_for_review_pending_sha, str):
            raise TypeError(f"ready_for_review_pending_sha must be a string or None, got {type(ready_for_review_pending_sha).__name__}")
        with file_lock(self._plans_path):
            plans = self._load_all_plans()
            found = False
            for p in plans:
                if p.plan_id == plan_id:
                    found = True
                    if last_pr_reminder_at is not _UNSET:
                        p.last_pr_reminder_at = last_pr_reminder_at  # type: ignore[assignment]
                    if pr_reminder_count is not _UNSET:
                        p.pr_reminder_count = pr_reminder_count  # type: ignore[assignment]
                    if last_conflict_resolution_attempt_at is not _UNSET:
                        p.last_conflict_resolution_attempt_at = last_conflict_resolution_attempt_at  # type: ignore[assignment]
                    if last_conflict_resolution_base_sha is not _UNSET:
                        p.last_conflict_resolution_base_sha = last_conflict_resolution_base_sha  # type: ignore[assignment]
                    if last_conflict_resolution_head_sha is not _UNSET:
                        p.last_conflict_resolution_head_sha = last_conflict_resolution_head_sha  # type: ignore[assignment]
                    if last_conflict_resolution_ci_conclusion is not _UNSET:
                        p.last_conflict_resolution_ci_conclusion = last_conflict_resolution_ci_conclusion  # type: ignore[assignment]
                    if last_quality_fix_attempt_at is not _UNSET:
                        p.last_quality_fix_attempt_at = last_quality_fix_attempt_at  # type: ignore[assignment]
                    if last_quality_fix_head_sha is not _UNSET:
                        p.last_quality_fix_head_sha = last_quality_fix_head_sha  # type: ignore[assignment]
                    if last_quality_fix_base_sha is not _UNSET:
                        p.last_quality_fix_base_sha = last_quality_fix_base_sha  # type: ignore[assignment]
                    if last_quality_fix_ci_conclusion is not _UNSET:
                        p.last_quality_fix_ci_conclusion = last_quality_fix_ci_conclusion  # type: ignore[assignment]
                    if conflict_resolution_signature_attempts is not _UNSET:
                        p.conflict_resolution_signature_attempts = conflict_resolution_signature_attempts  # type: ignore[assignment]
                    if quality_fix_signature_attempts is not _UNSET:
                        p.quality_fix_signature_attempts = quality_fix_signature_attempts  # type: ignore[assignment]
                    if ready_for_review_pending_sha is not _UNSET:
                        p.ready_for_review_pending_sha = ready_for_review_pending_sha  # type: ignore[assignment]
                    break
            if found:
                self._save_all_plans(plans)
            return found

    def update_plan_inherited_hold(
        self,
        plan_id: str,
        *,
        inherited_hold_main_sha: object = _UNSET,
        inherited_hold_count: object = _UNSET,
    ) -> bool:
        """Update a plan's proposal-124 inherited-hold fields without touching status.

        An ``INHERITED`` convergence terminal holds the plan ``approved`` rather
        than demoting it (``MaestroAgent.implement_plan``), stamping the
        ``origin/main`` head the base red was measured against and the
        incremented hold count.  Like :meth:`update_plan_tend_fields`, this is
        status-agnostic — :meth:`update_plan_status` would require a status
        positional and rewrite the very status this arm exists to preserve — so
        it mirrors that method's read-modify-write (``file_lock`` →
        ``_load_all_plans`` → mutate → ``_save_all_plans``) and per-field type
        validation, never touching ``status`` or ``failure_count``.  Returns
        ``True`` when the plan is found, ``False`` otherwise.
        """
        if inherited_hold_main_sha is not _UNSET and inherited_hold_main_sha is not None and not isinstance(inherited_hold_main_sha, str):
            raise TypeError(f"inherited_hold_main_sha must be a string or None, got {type(inherited_hold_main_sha).__name__}")
        if inherited_hold_count is not _UNSET and not isinstance(inherited_hold_count, int):
            raise TypeError(f"inherited_hold_count must be an int, got {type(inherited_hold_count).__name__}")
        with file_lock(self._plans_path):
            plans = self._load_all_plans()
            found = False
            for p in plans:
                if p.plan_id == plan_id:
                    found = True
                    if inherited_hold_main_sha is not _UNSET:
                        p.inherited_hold_main_sha = inherited_hold_main_sha  # type: ignore[assignment]
                    if inherited_hold_count is not _UNSET:
                        p.inherited_hold_count = inherited_hold_count  # type: ignore[assignment]
                    break
            if found:
                self._save_all_plans(plans)
            return found

    def update_plan_environment_hold(
        self,
        plan_id: str,
        *,
        environment_hold_fingerprint: object = _UNSET,
    ) -> bool:
        """Update a plan's proposal-133 environment-hold field without touching status.

        A non-``SOUND`` ``gate_environment`` verdict holds the plan where it is
        rather than spending an implementation attempt against an environment that
        cannot answer (``MaestroAgent._implement_approved_plans``), stamping the
        verdict's fingerprint so the hold is keyed to the fact. Like
        :meth:`update_plan_inherited_hold`, this is status-agnostic —
        :meth:`update_plan_status` would require a status positional and rewrite the
        very status this arm exists to preserve — so it mirrors that method's
        read-modify-write (``file_lock`` -> ``_load_all_plans`` -> mutate ->
        ``_save_all_plans``) and per-field type validation, never touching ``status``
        or ``failure_count``. Passing ``None`` clears the stamp, which is how a
        released plan sheds a fingerprint that would otherwise outlive its hold.
        Returns ``True`` when the plan is found, ``False`` otherwise.
        """
        if (
            environment_hold_fingerprint is not _UNSET
            and environment_hold_fingerprint is not None
            and not isinstance(environment_hold_fingerprint, str)
        ):
            raise TypeError(
                "environment_hold_fingerprint must be a string or None, got "
                f"{type(environment_hold_fingerprint).__name__}"
            )
        with file_lock(self._plans_path):
            plans = self._load_all_plans()
            found = False
            for p in plans:
                if p.plan_id == plan_id:
                    found = True
                    if environment_hold_fingerprint is not _UNSET:
                        p.environment_hold_fingerprint = environment_hold_fingerprint  # type: ignore[assignment]
                    break
            if found:
                self._save_all_plans(plans)
            return found

    def update_plans_environment_hold(
        self,
        plan_ids: Sequence[str],
        *,
        environment_hold_fingerprint: str | None,
    ) -> int:
        """Stamp (or clear) the environment hold across *plan_ids* in one write.

        The proposal-133 hold covers every plan the selector would have taken, so
        the stamp is set-shaped: calling :meth:`update_plan_environment_hold` once
        per plan would take the lock and rewrite the whole plan file N times for a
        single verdict. This takes one ``file_lock`` and does one
        ``_load_all_plans`` -> mutate -> ``_save_all_plans`` round-trip over the
        whole set, which is the same read-modify-write discipline at a fraction of
        the I/O. ``status`` and ``failure_count`` are never touched; passing
        ``None`` clears the stamp. Unknown ids are ignored. Returns the number of
        plans actually updated, and writes nothing when that number is zero.
        """
        if (
            environment_hold_fingerprint is not None
            and not isinstance(environment_hold_fingerprint, str)
        ):
            raise TypeError(
                "environment_hold_fingerprint must be a string or None, got "
                f"{type(environment_hold_fingerprint).__name__}"
            )
        wanted = set(plan_ids)
        if not wanted:
            return 0
        with file_lock(self._plans_path):
            plans = self._load_all_plans()
            updated = 0
            for p in plans:
                if p.plan_id in wanted:
                    p.environment_hold_fingerprint = environment_hold_fingerprint
                    updated += 1
            if updated:
                self._save_all_plans(plans)
            return updated

    def update_plan_resume(
        self,
        plan_id: str,
        *,
        resume_count: object = _UNSET,
        resume_failing_identities: object = _UNSET,
        resume_branch_head: object = _UNSET,
        last_failure_reason: object = _UNSET,
    ) -> bool:
        """Update a plan's proposal-131 RESUME-arm fields without touching status.

        The ``RESUME`` disposition (``MaestroAgent.implement_plan``'s non-inherited
        ``converge_to_green`` stuck terminal) preserves a strictly-progressing
        attempt's branch and carries its memory into the next admission, stamping
        the incremented ``resume_count``, the current attempt's sorted
        failing-identity baseline, the preserved evolution-branch HEAD, and the
        failure reason — while deliberately leaving ``status`` (``failed``) and
        ``failure_count`` frozen so the retry selector re-admits the plan next
        lap. Like :meth:`update_plan_inherited_hold`, this is status-agnostic —
        :meth:`update_plan_status` would rewrite the very status this arm exists
        to preserve — so it mirrors that method's read-modify-write (``file_lock``
        → ``_load_all_plans`` → mutate → ``_save_all_plans``) and per-field type
        validation, never touching ``status`` or ``failure_count``. Returns
        ``True`` when the plan is found, ``False`` otherwise.
        """
        if resume_count is not _UNSET and not isinstance(resume_count, int):
            raise TypeError(f"resume_count must be an int, got {type(resume_count).__name__}")
        if resume_failing_identities is not _UNSET and (
            not isinstance(resume_failing_identities, list)
            or not all(isinstance(i, str) for i in resume_failing_identities)
        ):
            raise TypeError("resume_failing_identities must be list[str]")
        if resume_branch_head is not _UNSET and resume_branch_head is not None and not isinstance(resume_branch_head, str):
            raise TypeError(f"resume_branch_head must be a string or None, got {type(resume_branch_head).__name__}")
        if last_failure_reason is not _UNSET and last_failure_reason is not None and not isinstance(last_failure_reason, str):
            raise TypeError(f"last_failure_reason must be a string or None, got {type(last_failure_reason).__name__}")
        with file_lock(self._plans_path):
            plans = self._load_all_plans()
            found = False
            for p in plans:
                if p.plan_id == plan_id:
                    found = True
                    if resume_count is not _UNSET:
                        p.resume_count = resume_count  # type: ignore[assignment]
                    if resume_failing_identities is not _UNSET:
                        p.resume_failing_identities = resume_failing_identities  # type: ignore[assignment]
                    if resume_branch_head is not _UNSET:
                        p.resume_branch_head = resume_branch_head  # type: ignore[assignment]
                    if last_failure_reason is not _UNSET:
                        p.last_failure_reason = last_failure_reason  # type: ignore[assignment]
                    break
            if found:
                self._save_all_plans(plans)
            return found

    def update_plan_proposed_changes(
        self, plan_id: str, proposed_changes: list[str],
    ) -> bool:
        """Replace ``proposed_changes`` for a plan.

        Used by the scope-gate one-shot replan path in
        ``MaestroAgent.implement_plan`` when the LLM rewrites step prose
        to satisfy ``check_plan_scope``. Returns ``True`` when the plan
        is found, ``False`` otherwise.
        """
        if not isinstance(proposed_changes, list) or not all(
            isinstance(c, str) for c in proposed_changes
        ):
            raise TypeError("proposed_changes must be list[str]")
        with file_lock(self._plans_path):
            plans = self._load_all_plans()
            found = False
            for p in plans:
                if p.plan_id == plan_id:
                    p.proposed_changes = list(proposed_changes)
                    p.plan_content_hash = compute_plan_content_hash(p)
                    found = True
                    break
            if found:
                self._save_all_plans(plans)
            return found

    def refine_plan(
        self,
        plan_id: str,
        *,
        rationale: object = _UNSET,
        proposed_changes: object = _UNSET,
        acceptance_criteria: object = _UNSET,
        files_affected: object = _UNSET,
    ) -> bool:
        """Partially rewrite a ``pending_review`` plan's body.

        Returns ``True`` when the plan exists *and* is still
        ``pending_review``; returns ``False`` (without mutation) when the
        plan is missing or has already left ``pending_review``. Only
        fields explicitly passed are updated; ``_UNSET`` sentinels leave
        the existing value in place. On every successful refinement
        ``refined_at`` is stamped with the current UTC ISO timestamp so a
        reviewer can see when a pending plan's body last changed.
        Intended for the future ``curate_pending_plans`` phase that
        refreshes stale plans before they reach human review. A refine also
        stamps ``curated_hash`` from the post-refine row (proposal 155 step
        6), so the curator does not pay a second pass on its own output.

        Raises ``ValueError`` when no field is passed (the call would do
        a no-op rewrite of the JSONL) and when any field exceeds its
        length cap (``_PLAN_RATIONALE_MAX_CHARS`` for ``rationale``,
        ``_PLAN_CHANGE_ITEM_MAX_CHARS`` per ``proposed_changes`` item,
        ``ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS`` per ``acceptance_criteria``
        item, ``_PLAN_FILE_PATH_MAX_CHARS`` per ``files_affected`` item), or
        when ``files_affected`` holds more than
        ``_PLAN_FILES_AFFECTED_MAX_ITEMS`` items. The ``rationale``,
        ``proposed_changes`` and ``acceptance_criteria`` caps are two-sided:
        the creation path
        (:meth:`~maestro.evolution.EvolutionEngine._plan_from_spec`) enforces
        the same limits by *truncating* raw model output, while refinement —
        which rewrites an already-reviewable plan — *rejects* an over-cap value
        rather than silently shortening it. The ``files_affected`` caps are
        refinement-only: creation leaves that list uncapped. Neither side
        count-caps ``proposed_changes``: the whole list is the input the
        CLI-grounding and plan-scope guards read.

        ``proposed_changes``, ``acceptance_criteria`` and ``files_affected``
        must be non-empty (with non-blank items for ``files_affected``) when
        passed — refinement is a *replace*, not a *wipe*.
        A curator that wants to drop stale criteria must replace them
        with a meaningful value rather than clearing the list.
        """
        if (
            rationale is _UNSET
            and proposed_changes is _UNSET
            and acceptance_criteria is _UNSET
            and files_affected is _UNSET
        ):
            raise ValueError(
                "refine_plan requires at least one field to update"
            )
        if rationale is not _UNSET:
            if not isinstance(rationale, str):
                raise TypeError(
                    f"rationale must be a string, got {type(rationale).__name__}"
                )
            if not rationale.strip():
                raise ValueError("rationale must be non-empty when refining")
            if len(rationale) > _PLAN_RATIONALE_MAX_CHARS:
                raise ValueError(
                    f"rationale exceeds {_PLAN_RATIONALE_MAX_CHARS} chars"
                )
        for name, value in (
            ("proposed_changes", proposed_changes),
            ("acceptance_criteria", acceptance_criteria),
            ("files_affected", files_affected),
        ):
            if value is _UNSET:
                continue
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise TypeError(f"{name} must be list[str]")
            if not value:
                raise ValueError(f"{name} must be non-empty when refining")
            item_cap = {
                "acceptance_criteria": ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS,
                "files_affected": _PLAN_FILE_PATH_MAX_CHARS,
            }.get(name, _PLAN_CHANGE_ITEM_MAX_CHARS)
            if any(len(item) > item_cap for item in value):
                raise ValueError(
                    f"{name} items must each be <= {item_cap} chars"
                )
            # acceptance_criteria carries a documented count cap
            # (CLAUDE.md self-evolution); proposed_changes does not. The shared
            # validator owns the bound so this boundary cannot drift from
            # ``save_evolution_plan`` or the prompt renderer.
            if name == "acceptance_criteria":
                validate_acceptance_criteria_count(value, field_name=name)
            if name == "files_affected":
                if any(not item.strip() for item in value):
                    raise ValueError("files_affected items must be non-blank")
                if len(value) > _PLAN_FILES_AFFECTED_MAX_ITEMS:
                    raise ValueError(
                        f"files_affected must hold at most "
                        f"{_PLAN_FILES_AFFECTED_MAX_ITEMS} items"
                    )
        with file_lock(self._plans_path):
            plans = self._load_all_plans()
            found = False
            for p in plans:
                if p.plan_id != plan_id:
                    continue
                if p.status != "pending_review":
                    return False
                if rationale is not _UNSET:
                    assert isinstance(rationale, str)
                    p.rationale = rationale
                if proposed_changes is not _UNSET:
                    assert isinstance(proposed_changes, list)
                    p.proposed_changes = list(proposed_changes)
                if acceptance_criteria is not _UNSET:
                    assert isinstance(acceptance_criteria, list)
                    p.acceptance_criteria = list(acceptance_criteria)
                if files_affected is not _UNSET:
                    assert isinstance(files_affected, list)
                    p.files_affected = list(files_affected)
                p.refined_at = datetime.now(UTC).isoformat()
                p.plan_content_hash = compute_plan_content_hash(p)
                p.curated_hash = compute_curation_fingerprint(p)
                found = True
                break
            if found:
                self._save_all_plans(plans)
            return found

    def touch_plan_curated(self, plan_id: str) -> bool:
        """Advance the curation round-robin cursor on a left-pending plan.

        Stamps ``last_curated_at`` with the current UTC ISO timestamp when
        *plan_id* exists and is still ``pending_review``, so
        :func:`select_curation_target` sorts it behind never-curated plans
        and a kept (or deterministically-skipped) plan yields its curation
        slot to the rest of the backlog rather than being re-picked every
        cadence (#2885). Returns ``True`` on a successful stamp; ``False``
        when the plan is missing or has already left ``pending_review``.

        Unlike :meth:`refine_plan` this does **not** restamp
        ``plan_content_hash``: ``last_curated_at`` is a curation cursor, not
        reviewable plan content, and is deliberately excluded from
        :func:`compute_plan_content_hash` (exactly like ``refined_at``), so
        plan-content-hash approval-drift detection is unaffected. It does
        stamp ``curated_hash`` from the live row (proposal 155 step 6), so
        the selector skips the plan until its content or feedback changes.
        """
        with file_lock(self._plans_path):
            plans = self._load_all_plans()
            found = False
            for p in plans:
                if p.plan_id != plan_id:
                    continue
                if p.status != "pending_review":
                    return False
                p.last_curated_at = datetime.now(UTC).isoformat()
                p.curated_hash = compute_curation_fingerprint(p)
                found = True
                break
            if found:
                self._save_all_plans(plans)
            return found

    def add_plan_feedback(
        self,
        plan_id: str,
        text: str,
        *,
        source: str,
        content_hash_seen: str,
        feedback_id: str | None = None,
    ) -> bool:
        """Append one reviewer feedback entry to a ``pending_review`` plan.

        The single write door for ``EvolutionPlan.reviewer_feedback``
        (proposal 145). Raises ``TypeError``/``ValueError`` on non-string,
        empty, or over-cap *text* and an unknown *source*. Returns ``False``
        without writing when the plan is missing, has left ``pending_review``,
        or already holds ``MAX_UNADDRESSED_FEEDBACK`` unaddressed entries.
        Like :meth:`touch_plan_curated`, leaves ``plan_content_hash`` and
        ``refined_at`` untouched. A caller-supplied *feedback_id* makes the
        write idempotent: when the plan already holds an entry with that id,
        returns ``True`` without appending, so a crash re-drain lands once.
        """
        if not isinstance(text, str):
            raise TypeError("feedback text must be a str")
        stripped = text.strip()
        if not stripped:
            raise ValueError("feedback text must be non-empty")
        if len(stripped) > FEEDBACK_TEXT_MAX_CHARS:
            raise ValueError(
                f"feedback text exceeds {FEEDBACK_TEXT_MAX_CHARS} chars",
            )
        if source not in FEEDBACK_SOURCES:
            raise ValueError(f"unknown feedback source: {source!r}")
        with file_lock(self._plans_path):
            plans = self._load_all_plans()
            plan = next((p for p in plans if p.plan_id == plan_id), None)
            if plan is not None and feedback_id is not None and any(
                f.feedback_id == feedback_id for f in plan.reviewer_feedback
            ):
                return True
            if plan is None or plan.status != "pending_review":
                return False
            if len(unaddressed_feedback(plan)) >= MAX_UNADDRESSED_FEEDBACK:
                return False
            plan.reviewer_feedback.append(PlanFeedback(
                feedback_id=feedback_id or uuid4().hex,
                text=stripped,
                submitted_at=datetime.now(UTC).isoformat(),
                source=source,
                content_hash_seen=truncate_hash_prefix(content_hash_seen),
            ))
            self._save_all_plans(plans)
            return True

    def record_feedback_attempt(
        self, plan_id: str, feedback_ids: Collection[str],
    ) -> dict[str, int] | None:
        """Count one failed curate pass against the given unaddressed entries.

        Increments ``curate_attempts`` on each entry in *feedback_ids* that is
        still unaddressed and returns each matched entry's own resulting count
        by ``feedback_id`` (empty when none matched), so a caller can release
        only the entries that spent their budget. Returns ``None`` for a
        missing or non-``pending_review`` plan.
        """
        ids = set(feedback_ids)
        with file_lock(self._plans_path):
            plans = self._load_all_plans()
            plan = next((p for p in plans if p.plan_id == plan_id), None)
            if plan is None or plan.status != "pending_review":
                return None
            counts: dict[str, int] = {}
            for entry in plan.reviewer_feedback:
                if entry.feedback_id in ids and entry.addressed_at is None:
                    entry.curate_attempts += 1
                    counts[entry.feedback_id] = entry.curate_attempts
            self._save_all_plans(plans)
            return counts

    def mark_feedback_addressed(
        self,
        plan_id: str,
        feedback_ids: Collection[str],
        response: str,
    ) -> bool:
        """Stamp the given unaddressed feedback entries as addressed.

        Only entries whose id is in *feedback_ids* and still unaddressed are
        stamped, so an entry that arrived mid-curation stays open. Addressed
        entries are then pruned to the newest ``MAX_ADDRESSED_FEEDBACK_KEPT``.
        Raises ``ValueError`` on an over-cap *response*; returns ``False``
        for a missing or non-``pending_review`` plan.
        """
        if len(response) > FEEDBACK_RESPONSE_MAX_CHARS:
            raise ValueError(
                f"feedback response exceeds {FEEDBACK_RESPONSE_MAX_CHARS} chars",
            )
        ids = set(feedback_ids)
        with file_lock(self._plans_path):
            plans = self._load_all_plans()
            plan = next((p for p in plans if p.plan_id == plan_id), None)
            if plan is None or plan.status != "pending_review":
                return False
            now = datetime.now(UTC).isoformat()
            for entry in plan.reviewer_feedback:
                if entry.feedback_id in ids and entry.addressed_at is None:
                    entry.addressed_at = now
                    entry.response = response
            # Oldest-addressed first, so the entries just stamped (``now``)
            # are the last to go — list position is submission order.
            addressed = sorted(
                (f for f in plan.reviewer_feedback if f.addressed_at is not None),
                key=lambda f: f.addressed_at or "",
            )
            dropped = {
                id(f) for f in addressed[:-MAX_ADDRESSED_FEEDBACK_KEPT]
            }
            plan.reviewer_feedback = [
                f for f in plan.reviewer_feedback if id(f) not in dropped
            ]
            self._save_all_plans(plans)
            return True

    def mark_plan_obsolete(
        self,
        plan_id: str,
        reason: str,
        allow_implemented: bool = False,
        allow_approved: bool = False,
        allow_blocked: bool = False,
        allow_failed: bool = False,
        refused_at_submission: bool = False,
        allow_sliced: bool = False,
    ) -> bool:
        """Transition a ``pending_review`` plan to terminal ``obsolete``.

        Returns ``True`` only when the plan exists *and* was still in an
        accepted status: ``pending_review`` by default, widened to add
        ``"implemented"`` when *allow_implemented* is ``True`` (the
        orphaned-cure dispose sweep's opt-in), ``"approved"`` when
        *allow_approved* is ``True`` (proposal 118's scope-rejection slicing
        opt-in — an already-approved plan whose too-large intent is superseded
        by an ``EvolutionProposal`` chain is retired as ``obsolete`` rather than
        demoted to ``failed``), and/or ``"blocked"`` when *allow_blocked* is
        ``True`` (proposal 131's abandonment opt-in — a ``blocked`` plan whose
        blocking question Thomas answers with a decision to abandon is retired
        as ``obsolete`` rather than re-admitted, via
        ``MaestroAgent._abandon_blocked_plan``), and/or ``"failed"`` when
        *allow_failed* is ``True`` (the over-cap acceptance-criteria disposal,
        #3712 / #4123) so a ``failed`` legacy row that can never be rendered is
        retired rather than re-selected forever),
        and/or ``"sliced"`` when *allow_sliced* is ``True`` (proposal 118's
        abandonment opt-in — ``MaestroAgent._advance_slice_rollouts`` retires
        the dormant successors of a slice that ended without a PR).
        Every other caller keeps the narrow default.
        *refused_at_submission* stamps the plan's ``refused_at_submission``
        provenance; its sole ``True`` caller is the submission door
        (``evolution_submission._record_obsolete_duplicate``), which refuses a
        just-minted plan rather than retiring an existing one.
        Sets ``status="obsolete"``, ``reviewed_at`` to now, and
        ``obsolete_reason`` to *reason*. Skips ``rejection_reason`` so the
        rejection-learning cycle never treats a self-prune as a human
        rejection. Callers: the pending-plan curation phase
        (``curate_pending_plans``), the cluster/restatement pending-plan
        sweeps, ``lever_capture.fire_directive_named_obsolete`` (the
        directive-named obsolete actuator), and — via the opt-ins —
        :meth:`sweep_orphaned_cures`,
        ``MaestroAgent._slice_scope_rejected_plan`` and
        ``MaestroAgent._advance_slice_rollouts``.

        Raises ``ValueError`` when *reason* is empty/whitespace or
        exceeds ``_OBSOLETE_REASON_MAX_CHARS``.
        """
        if not isinstance(reason, str):
            raise TypeError(
                f"reason must be a string, got {type(reason).__name__}"
            )
        if not reason.strip():
            raise ValueError("reason must be non-empty when marking obsolete")
        if len(reason) > _OBSOLETE_REASON_MAX_CHARS:
            raise ValueError(
                f"reason exceeds {_OBSOLETE_REASON_MAX_CHARS} chars"
            )
        accepted_statuses = {"pending_review"}
        if allow_implemented:
            accepted_statuses.add("implemented")
        if allow_approved:
            accepted_statuses.add("approved")
        if allow_blocked:
            accepted_statuses.add("blocked")
        if allow_failed:
            accepted_statuses.add("failed")
        if allow_sliced:
            accepted_statuses.add("sliced")
        with file_lock(self._plans_path):
            plans = self._load_all_plans()
            found = False
            for p in plans:
                if p.plan_id != plan_id:
                    continue
                if p.status not in accepted_statuses:
                    return False
                p.status = "obsolete"
                p.obsolete_reason = reason
                p.refused_at_submission = refused_at_submission
                p.reviewed_at = datetime.now(UTC).isoformat()
                found = True
                break
            if found:
                self._save_all_plans(plans)
            return found

    def mark_rejection_learned(self, plan_id: str) -> bool:
        """Mark a rejected plan's rejection as learned. Returns ``True`` if found."""
        with file_lock(self._plans_path):
            plans = self._load_all_plans()
            found = False
            for p in plans:
                if p.plan_id == plan_id:
                    p.rejection_learned = True
                    found = True
                    break
            if found:
                self._save_all_plans(plans)
            return found

    # --- Archive rotation ---

    @staticmethod
    def _record_timestamp(data: dict[str, object]) -> datetime | None:
        """Best-effort ISO-8601 timestamp extraction from a JSONL record.

        Tries ``timestamp`` (TaskRecord / cost-sample rows), ``created_at``
        (EvolutionPlan), then ``started_at`` (IterationUsage rows in
        ``usage.jsonl``, which carry no ``timestamp`` key). Returns ``None``
        when none is present, all are unparseable, or the value is not a
        string — caller treats unparseable as "stay hot" so schema drift
        never silently archives data.

        The naive-is-UTC reading below is the same rule
        ``cost_tracking._as_utc`` owns for cost-sample stamps — a named
        sibling of it, since rotation dates the very rows those aggregators
        read — kept here for the three-key / ``None``-on-unparseable contract
        above (and to spare ``memory`` a ``cost_tracking`` import). Changing
        one side's reading of a naive stamp must change both.
        """
        for key in ("timestamp", "created_at", "started_at"):
            raw = data.get(key)
            if not isinstance(raw, str) or not raw:
                continue
            try:
                # ``fromisoformat`` accepts ``Z`` suffix on Python 3.11+.
                ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            return ts
        return None

    @classmethod
    def _plan_row_stays_hot(
        cls,
        data: dict[str, object],
        cutoff: datetime,
        open_proposal_ids: set[str],
        retry_budget: int | None,
        open_pr_plan_ids: set[str] | None,
    ) -> bool:
        """Whether :meth:`archive` keeps this ``evolution_plans.jsonl`` row hot.

        The single definition both archive passes share: the plan partition
        and the ``source_task_id`` hold-set, so every retained plan's base
        task stays resolvable (#4301).
        """
        ts = cls._record_timestamp(data)
        if ts is None or ts >= cutoff:
            return True
        if not _plan_row_archivable(
            data, open_proposal_ids, retry_budget, open_pr_plan_ids
        ):
            # Lifecycle-aware: an old plan the loop or a human still owes an
            # action on stays hot. An unrecognised status is treated as
            # unfinished (allowlist, not denylist), so a new ``PlanStatus`` is
            # never silently deleted before a consumer learns about it.
            return True
        # The rejection cooldown dates a rejected plan by its ``reviewed_at``
        # (the rejection moment), not its creation, so a long-lived PR closed
        # recently must stay visible to that hot read.
        if data.get("status") != "rejected":
            return False
        rejected_at = cls._record_timestamp({"created_at": data.get("reviewed_at")})
        return rejected_at is not None and rejected_at >= cutoff

    def archive(
        self,
        threshold_days: int,
        *,
        retry_budget: int | None = None,
        open_pr_plan_ids: set[str] | None = None,
    ) -> None:
        """Split task, plan, and telemetry JSONL files into hot/cold tiers.

        For each of ``_tasks_path``, ``_plans_path``, ``_cost_samples_path``
        (``cost_samples.jsonl``), and ``_usage_path`` (``usage.jsonl``): under
        ``file_lock``, stream the lines into a hot list (records whose
        timestamp is newer than ``now - threshold_days``) and a cold
        list (everything older).  Rewrite the hot file via
        ``atomic_write`` first, then append the cold list to
        ``<path>.archive.jsonl`` (plain append-only; the archive is
        unordered cold storage and does not need ``atomic_write``).
        Hot-first ordering means a crash between the two steps loses
        the most recent cold batch from the archive but never
        duplicates it; cold storage is never read by hot paths, so the
        trade-off favors de-duplication over completeness.  Records
        with an unparseable or missing timestamp stay hot — losing
        data on schema drift would be far worse than letting one stale
        row ride in the hot file until the schema is fixed.

        ``threshold_days`` MUST exceed the longest hot-read window over
        the telemetry sidecars so no hot read ever loses in-window rows
        to cold.  For ``cost_samples.jsonl`` the widest hot reader is the
        dashboard, not a loop-internal one: ``/api/cost-rates`` and
        ``/api/budget`` (``dashboard/routes.py``) call ``build_cost_rates``
        / ``build_budget_breakdown(agent.cost_samples, window_hours)`` with
        ``window_hours`` accepted up to ``168.0`` (7 days) over the very
        file this rotates, so a day-scale ``threshold_days <= 7`` sheds
        in-window rows to cold and the dashboard's up-to-7-day budget /
        cost-rate view silently under-reports spend.  The loop-internal
        readers share that hot file: the burn-rate ``_iter_samples_in_window``
        (``budget_rate_window_hours``, default one hour but operator-
        configurable) and the count-bounded phase-report ``_tail_cost_samples``.
        So the minimum safe ``threshold_days`` for this file is **> 7 days**,
        raised further if ``budget_rate_window_hours`` is set wider; only above that floor does every
        ``cost_samples.jsonl`` hot read stay byte-identical across the split.
        ``evolution_plans.jsonl`` is *not* telemetry: it is the authoritative
        store for plans the loop must still act on, so age alone never sheds a
        plan row.  A plan is eligible for cold only when its status is in
        :data:`ARCHIVABLE_PLAN_STATUSES` — the single owner of the
        finished-history set, so a status later added to ``PlanStatus`` defaults
        to *hot* — **and** it is not a slice of a proposal outside
        :data:`FINISHED_PROPOSAL_STATUSES`.  One status is conditional:
        a ``failed`` plan rotates only once ``failure_count`` has spent
        *retry_budget* (``max_implementation_attempts``, which ``Memory`` cannot
        read itself — an un-supplied caller passes ``None`` and every failed row
        stays hot), the point past which ``_implement_approved_plans`` never
        re-admits it; without that arm exhausted failures would accumulate in
        the hot file forever.  Every other plan row stays hot
        regardless of age: an ``approved`` plan awaiting an implementation slot,
        a ``failed`` plan still inside its retry budget, a ``blocked`` plan
        awaiting a human answer, and a dormant ``sliced`` row all age past any
        legal threshold in normal operation, and shedding one would silently end
        its lifecycle with no terminal decision recorded.  Three further holds
        keep rows a hot path still owes work on: a ``rejected`` plan not yet
        ``rejection_learned``; a ``rejected``/``obsolete`` plan whose PR is in
        *open_pr_plan_ids* (Tend still services it — ``None``, PR state unknown,
        holds every such row carrying a ``pr_url``); and a
        ``task_history.jsonl`` row cited as ``source_task_id`` by any plan row
        that stays hot, so ``get_task_record`` still resolves the base branch
        ``implement`` cuts from, or as ``task_id`` by a publication proposal
        outside ``FINISHED_PUBLICATION_STATUSES``, so ``Publisher._publish``
        still resolves its artifacts.  ``plan_review_expiry_days``
        — the horizon the expiry sweep scans for aged ``pending_review`` plans —
        still lifts the floor the same way, a second, independent guard over the
        same rows.  ``Config`` enforces the resulting
        maximum of the three windows at validation time.  The operator-facing
        ``usage.jsonl`` readers are outside that constraint by design —
        window-less ``cost_for_task`` (``maestro cost-per-task``) and the
        operator-chosen ``cost-report --days N`` /
        ``burn-baseline --since``/``--compare`` windows — can select a span
        wider than ``threshold_days``, so a task or date past the threshold
        is shed to cold and reads as absent (``cost-per-task`` then prints
        "No usage data").  That is the accepted trade-off of opting archival
        in: cold rows are operator-managed, never auto-read back.  No reader
        reads ``.archive.jsonl``, preserving the "cold storage never read by
        hot paths" invariant.

        The exclusion is two-sided: every appender to a file this rotates
        takes the same ``file_lock`` — :meth:`record_task`
        (``task_history.jsonl``), :meth:`save_evolution_plan`
        (``evolution_plans.jsonl``), ``cost_tracking.append_cost_sample``
        (``cost_samples.jsonl``), and ``sdk.write_iteration_usage``
        (``usage.jsonl``).  ``flock`` is advisory, so a lock only excludes
        writers who also take it; an unlocked append landing between the
        ``read_text`` and the ``atomic_write`` below is destroyed by the
        rename, silently and without a corrupted-row log.

        Disabled when ``threshold_days <= 0``.  Best-effort: callers
        wrap this in ``try/except`` so an I/O failure here never crashes
        the loop.  After a successful rotation the plan index is
        invalidated so the next ``get_plan`` rebuilds against the
        trimmed file.
        """
        if threshold_days <= 0:
            return
        cutoff = datetime.now(UTC) - timedelta(days=threshold_days)
        rotated_plans = False
        # Only pay for the proposal read when plan rotation can actually run.
        open_proposal_ids: set[str] = set()
        if self._plans_path.exists():
            open_proposal_ids = {
                proposal.proposal_id
                for proposal in self.get_evolution_proposals()
                if proposal.status not in FINISHED_PROPOSAL_STATUSES
            }
        # Task rows a hot plan still cites as its implementation base stay
        # hot so ``get_task_record`` keeps resolving them.
        held_task_ids: set[str] = set()
        if self._plans_path.exists() and self._tasks_path.exists():
            for raw in self._plans_path.read_text(encoding="utf-8").splitlines():
                try:
                    plan_row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(plan_row, dict):
                    continue
                source = plan_row.get("source_task_id")
                if isinstance(source, str) and self._plan_row_stays_hot(
                    plan_row, cutoff, open_proposal_ids, retry_budget,
                    open_pr_plan_ids,
                ):
                    held_task_ids.add(source)
        # So do task rows an unfinished publication proposal still cites:
        # ``Publisher._publish`` resolves artifacts through the hot-only
        # ``get_task``, and approval carries no expiry.
        if self._tasks_path.exists():
            held_task_ids.update(
                proposal.task_id
                for proposal in self._load_all_proposals()
                if isinstance(proposal.task_id, str)
                and proposal.task_id
                # A malformed (non-str) status fails safe: it holds.
                and not (
                    isinstance(proposal.status, str)
                    and proposal.status in FINISHED_PUBLICATION_STATUSES
                )
            )
        for path in (
            self._tasks_path,
            self._plans_path,
            self._cost_samples_path,
            self._usage_path,
        ):
            if not path.exists():
                continue
            archive_path = path.with_suffix(path.suffix + ".archive.jsonl")
            with file_lock(path):
                hot_lines: list[str] = []
                cold_lines: list[str] = []
                for raw in path.read_text(encoding="utf-8").splitlines():
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        # Corrupted line — keep in hot file. Archival is
                        # not a corruption-recovery tool.
                        hot_lines.append(line)
                        continue
                    if not isinstance(data, dict):
                        hot_lines.append(line)
                        continue
                    if path == self._plans_path:
                        keep = self._plan_row_stays_hot(
                            data, cutoff, open_proposal_ids, retry_budget,
                            open_pr_plan_ids,
                        )
                    else:
                        ts = self._record_timestamp(data)
                        # A task cited as a hot plan's ``source_task_id``,
                        # or by an unfinished publication, stays hot
                        # regardless of age.
                        keep = ts is None or ts >= cutoff or (
                            path == self._tasks_path
                            and data.get("task_id") in held_task_ids
                        )
                    (hot_lines if keep else cold_lines).append(line)
                if not cold_lines:
                    # Nothing to rotate; leave the hot file untouched
                    # to avoid an unnecessary atomic rewrite.
                    continue
                atomic_write(
                    path,
                    "".join(line + "\n" for line in hot_lines),
                )
                with open(archive_path, "a", encoding="utf-8") as f:
                    for line in cold_lines:
                        f.write(line + "\n")
                if path == self._plans_path:
                    rotated_plans = True
        if rotated_plans:
            self._invalidate_plan_index()

    # --- Version archive ---

    def record_version(self, record: VersionRecord) -> None:
        if not record.version_id:
            raise ValueError("version_id must not be empty")
        if record.notes is not None and len(record.notes) > _VERSION_NOTES_MAX_CHARS:
            record = VersionRecord(
                **{**asdict(record), "notes": record.notes[:_VERSION_NOTES_MAX_CHARS]}
            )
        self._versions.record(record)

    def get_recent_versions(self, n: int = 10) -> list[VersionRecord]:
        if n <= 0:
            return []
        return self._versions.load_all()[-n:]

    @staticmethod
    def _parse_version_dict(d: dict) -> VersionRecord | None:
        """Coerce an already-decoded JSON dict into a ``VersionRecord``.

        Returns ``None`` for corrupted rows so callers can decide whether to
        log/skip — mirrors ``_parse_task_dict`` so the no-silenced-exceptions
        doctrine holds (the caught set maps to concrete corruption sources:
        ``KeyError``/``TypeError`` from a row missing a required field,
        ``AttributeError`` when the row is not a mapping).
        """
        try:
            version_known = {f.name for f in fields(VersionRecord)}
            filtered = {k: v for k, v in d.items() if k in version_known}
            return VersionRecord(**filtered)
        except (KeyError, TypeError, AttributeError):
            return None

    def read_version_records(self, n: int = 30) -> list[VersionRecord]:
        """Return the *n* most recent merged ``VersionRecord``s, oldest-first.

        Delegates to the injected :class:`VersionArchiveBackend` — the JSONL
        default (``version_archive.jsonl``) or the shared SQLite store behind
        ``[paths] sqlite_state_enabled``. ``load_all`` already applies the
        corrupt-tolerant forward parse (malformed rows skipped, never raised), so
        the tail slice ``[-n:]`` returns the *n* most recent rows oldest-first.
        Returns an empty list when *n* is non-positive. Read surface for
        ``maestro list-versions`` — every row reflects a commit git actually
        merged, never a narrated outcome.
        """
        if n <= 0:
            return []
        return self._versions.load_all()[-n:]

    def _load_all_versions(self) -> list[VersionRecord]:
        """Load every version record. Used by migration."""
        return self._versions.load_all()

    # --- Rejection history ---

    def record_rejection(self, record: RejectionRecord) -> None:
        if not record.plan_id:
            raise ValueError("plan_id must not be empty")
        self._rejections.record(record)

    def get_recent_rejections(self, n: int = 50) -> list[RejectionRecord]:
        """Return the last *n* rejection records oldest-first.

        Delegates to the injected :class:`RejectionHistoryBackend` (JSONL by
        default, SQLite behind ``[paths] sqlite_state_enabled``). ``load_all()``
        is oldest-first and drops corrupt rows, so ``load_all()[-n:]`` reproduces
        today's tail window; ``n <= 0`` short-circuits before any read (``[-0:]``
        would otherwise be the whole list).
        """
        if n <= 0:
            return []
        return self._rejections.load_all()[-n:]

    def _load_all_rejections(self) -> list[RejectionRecord]:
        """Load every rejection record from disk. Used by migration."""
        return self._rejections.load_all()

    # --- Audit history ---

    def append_audit_result(self, result: AuditResult) -> None:
        """Append a single audit cycle's outcome to the audit-history ledger.

        Delegates to the injected :class:`AuditHistoryBackend` (JSONL by
        default, SQLite behind ``[paths] sqlite_state_enabled``). Zero-finding
        cycles are still persisted — the next contemplation pulling
        ``get_most_recent_audit()`` should see the empty result and know the
        rules didn't fire, rather than treating an absent file as "audit never
        ran".
        """
        self._audit_history.record(result)

    def _parse_audit_line(self, line: str) -> AuditResult | None:
        """Parse a single JSONL line into an :class:`AuditResult`.

        A thin wrapper over the module-level :func:`_parse_audit_dict` core
        (which carries the finding-field filtering, ``AuditKind``-membership
        validation, ``cites`` / ``rules_fired`` sanitisation, and the
        "declared-findings-but-all-dropped ⇒ corrupt" rule): it decodes the
        line and delegates. Returns the parsed :class:`AuditResult` on success,
        ``None`` on any parse failure (a malformed line, a non-dict payload, or
        a structurally-corrupt row).
        """
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            logger.warning(
                "Skipping corrupted audit record in %s",
                self._audit_history_path,
            )
            return None
        return _parse_audit_dict(data, source=self._audit_history_path)

    def get_most_recent_audit(self) -> AuditResult | None:
        """Return the latest :class:`AuditResult`, or ``None``.

        Loads the whole ledger via the injected backend and returns only the
        most recent entry so contemplations between audits re-see the same
        finding set until the next audit fires. ``load_all()`` is oldest-first
        and drops corrupt rows, so the last element is the newest well-formed
        cycle; an empty ledger (missing file, all rows corrupt) yields ``None``.
        """
        results = self._audit_history.load_all()
        return results[-1] if results else None

    def get_recent_audit_results(self, limit: int) -> list[AuditResult]:
        """Return the last *limit* :class:`AuditResult` rows in file order.

        Generalises :meth:`get_most_recent_audit` to a tail-window read. The
        returned list is *newest-last* — the same order callers see when
        scanning the ledger top-to-bottom — so a ``reversed()`` walk visits
        cycles newest→oldest. ``load_all()`` is oldest-first and drops corrupt
        rows, so ``load_all()[-limit:]`` reproduces today's window; ``limit <=
        0`` short-circuits to an empty list before any read (``[-0:]`` would
        otherwise be the whole list).
        """
        if limit <= 0:
            return []
        return self._audit_history.load_all()[-limit:]

    # --- Question resolutions ---

    def append_question_resolution(self, record: QuestionResolution) -> None:
        """Append a :class:`QuestionResolution` row through the injected backend.

        Head-truncates ``question``, ``finding``, and ``refined_to`` at
        :data:`_QUESTION_RESOLUTION_FINDING_MAX_CHARS` and rejects an empty
        ``resolution_id`` to keep telemetry queries stable — this record
        normalisation stays in ``Memory`` so it applies to both substrates —
        then delegates the write to :attr:`_question_resolutions`. The JSONL
        adapter reproduces the pre-fold :func:`file_lock`-guarded append so
        concurrent writers don't interleave partial lines; the SQLite adapter
        serialises through its own transaction.
        """
        if not record.resolution_id:
            raise ValueError("resolution_id must not be empty")
        overrides: dict[str, str | None] = {}
        if len(record.question) > _QUESTION_RESOLUTION_FINDING_MAX_CHARS:
            overrides["question"] = record.question[
                :_QUESTION_RESOLUTION_FINDING_MAX_CHARS
            ]
        if len(record.finding) > _QUESTION_RESOLUTION_FINDING_MAX_CHARS:
            overrides["finding"] = record.finding[
                :_QUESTION_RESOLUTION_FINDING_MAX_CHARS
            ]
        if (
            record.refined_to is not None
            and len(record.refined_to)
            > _QUESTION_RESOLUTION_FINDING_MAX_CHARS
        ):
            overrides["refined_to"] = record.refined_to[
                :_QUESTION_RESOLUTION_FINDING_MAX_CHARS
            ]
        if overrides:
            record = QuestionResolution(**{**asdict(record), **overrides})
        self._question_resolutions.record(record)

    def _load_all_question_resolutions(self) -> list[QuestionResolution]:
        """Load every resolution row oldest-first; corrupt rows are skipped.

        Delegates to the injected backend's ``load_all`` — the JSONL adapter
        walks ``question_resolutions.jsonl`` via
        :func:`load_all_question_resolutions` and the SQLite adapter reads the
        ``question_resolutions`` table; both are oldest-first and drop corrupt
        rows, so the tail slice in :meth:`get_recent_question_resolutions`
        reproduces today's window.
        """
        return self._question_resolutions.load_all()

    def get_recent_question_resolutions(
        self, limit: int,
    ) -> list[QuestionResolution]:
        """Return the last *limit* resolutions in file order."""
        if limit <= 0:
            return []
        entries = self._load_all_question_resolutions()
        return entries[-limit:]

    # --- Artifact reviews ---

    @staticmethod
    def _coerce_str_list(value: object) -> list[str]:
        """Coerce a JSON value into ``list[str]`` for defensive loading.

        Mirrors the coercion applied to ``proposed_changes`` / ``files_affected``
        in :meth:`_load_all_plans` so malformed rows do not crash the loader.
        """
        if value is None:
            return []
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str)]
        if isinstance(value, str):
            return [s.strip() for s in value.splitlines() if s.strip()]
        return []

    def write_artifact_review(self, review: ArtifactReview) -> None:
        if not review.review_id:
            raise ValueError("review_id must not be empty")
        with (
            file_lock(self._reviews_path),
            open(self._reviews_path, "a", encoding="utf-8") as f,
        ):
            f.write(json.dumps(asdict(review)) + "\n")

    def _save_all_reviews(self, reviews: list[ArtifactReview]) -> None:
        atomic_write(
            self._reviews_path,
            "".join(json.dumps(asdict(r)) + "\n" for r in reviews),
        )

    def update_artifact_review_verdict(
        self,
        review_id: str,
        verdict: Verdict,
        reasoning: str,
    ) -> bool:
        """Rewrite a persisted review's verdict + reasoning.

        Used by the 032 emission path to demote a ``publish`` row whose
        drafted README carries a hard-block credential or whose proposed
        name already exists on the host. The publish-only fields
        (``proposed_name``, ``proposed_description``, ``proposed_readme``)
        are cleared so the demoted row cannot be re-interpreted as
        publishable later. Returns ``True`` when a matching row was found.
        """
        if not isinstance(reasoning, str):
            raise TypeError(
                f"reasoning must be a string, got {type(reasoning).__name__}"
            )
        with file_lock(self._reviews_path):
            reviews = self._load_all_reviews()
            found = False
            for r in reviews:
                if r.review_id == review_id:
                    found = True
                    r.verdict = verdict
                    r.reasoning = reasoning
                    r.proposed_name = None
                    r.proposed_description = None
                    r.proposed_readme = None
                    break
            if found:
                self._save_all_reviews(reviews)
            return found

    def _load_all_reviews(self) -> list[ArtifactReview]:
        """Load every artifact review from disk without a count limit."""
        if not self._reviews_path.exists():
            return []
        lines = self._reviews_path.read_text(encoding="utf-8").strip().splitlines()
        entries: list[ArtifactReview] = []
        known = {f.name for f in fields(ArtifactReview)}
        for line in lines:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                filtered = {k: v for k, v in raw.items() if k in known}
                if "bundle_file_names" in filtered:
                    filtered["bundle_file_names"] = self._coerce_str_list(
                        filtered["bundle_file_names"],
                    )
                review = ArtifactReview(**filtered)
                if not review.review_id:
                    logger.warning(
                        "Skipping review with empty review_id in %s",
                        self._reviews_path,
                    )
                    continue
                entries.append(review)
            except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
                logger.warning("Skipping corrupted artifact review in %s", self._reviews_path)
                continue
        return entries

    def get_artifact_reviews(self, n: int = 30) -> list[ArtifactReview]:
        """Return the last ``n`` artifact reviews in file order.

        Pass ``n <= 0`` to get an empty list.  For full-scan access use
        :meth:`_load_all_reviews` (used by :meth:`get_reviewed_task_ids`).
        """
        if n <= 0:
            return []
        all_reviews = self._load_all_reviews()
        return all_reviews[-n:]

    def get_reviewed_task_ids(self) -> set[str]:
        """Return the set of ``task_id`` values that have been reviewed.

        Full-scan — review volume is bounded by ``review_every`` pacing so
        this is acceptable. Used at task-selection time to avoid re-reviewing
        the same task.
        """
        return {r.task_id for r in self._load_all_reviews()}

    # --- Publication proposals ---

    def write_publication_proposal(self, proposal: PublicationProposal) -> None:
        if not proposal.proposal_id:
            raise ValueError("proposal_id must not be empty")
        with file_lock(self._proposals_path):
            duplicate = False
            if self._proposals_path.exists():
                with open(self._proposals_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            # Mirror the reader-side tolerance in
                            # ``_load_all_proposals``: a valid-JSON but non-dict
                            # row is skipped, not crashed on.
                            if not isinstance(data, dict):
                                continue
                            if data.get("proposal_id") == proposal.proposal_id:
                                duplicate = True
                                break
                        except json.JSONDecodeError:
                            continue
            if duplicate:
                raise DuplicateProposalError(
                    f"duplicate proposal_id: proposal with id {proposal.proposal_id!r} already exists"
                )
            with open(self._proposals_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(proposal)) + "\n")

    def _load_all_proposals(self) -> list[PublicationProposal]:
        if not self._proposals_path.exists():
            return []
        entries: list[PublicationProposal] = []
        known = {f.name for f in fields(PublicationProposal)}
        for line in self._proposals_path.read_text(encoding="utf-8").strip().splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                filtered = {k: v for k, v in raw.items() if k in known}
                for list_field in ("publishable_files", "scrub_findings"):
                    if list_field in filtered:
                        filtered[list_field] = self._coerce_str_list(filtered[list_field])
                proposal = PublicationProposal(**filtered)
                if not proposal.proposal_id:
                    logger.warning(
                        "Skipping proposal with empty proposal_id in %s",
                        self._proposals_path,
                    )
                    continue
                entries.append(proposal)
            except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
                logger.warning(
                    "Skipping corrupted proposal record in %s",
                    self._proposals_path,
                )
                continue
        return entries

    def _save_all_proposals(self, proposals: list[PublicationProposal]) -> None:
        atomic_write(
            self._proposals_path,
            "".join(json.dumps(asdict(p)) + "\n" for p in proposals),
        )

    def get_publication_proposals(
        self,
        n: int | None = None,
        status_filter: ProposalStatus | None = None,
    ) -> list[PublicationProposal]:
        """Return publication proposals in file order, optionally filtered.

        ``status_filter`` selects only proposals matching a specific status.
        ``n`` tails to the most recent N after filtering; ``None`` returns
        everything matching.
        """
        proposals = self._load_all_proposals()
        if status_filter is not None:
            proposals = [p for p in proposals if p.status == status_filter]
        if n is None:
            return proposals
        if n <= 0:
            return []
        return proposals[-n:]

    def get_proposal(self, proposal_id: str) -> PublicationProposal | None:
        for p in self._load_all_proposals():
            if p.proposal_id == proposal_id:
                return p
        return None

    def update_proposal_status(
        self,
        proposal_id: str,
        status: ProposalStatus,
        *,
        expect_status: ProposalStatus | None = None,
        reviewed_at: object = _UNSET,
        rejection_reason: object = _UNSET,
        executing_started_at: object = _UNSET,
        published_url: object = _UNSET,
        scrub_findings: object = _UNSET,
        error: object = _UNSET,
    ) -> bool:
        """Update a proposal's status.

        Returns ``True`` when the row was found *and* — if ``expect_status``
        is supplied — its current status matched. Returns ``False`` when
        the row is missing or the lock-checked status does not match.
        ``expect_status`` closes the cross-surface TOCTOU window: the
        dashboard and Telegram both read-then-update, and without an
        under-lock recheck two concurrent surfaces could each pass the
        read-time check and then stomp each other's write.
        """
        if reviewed_at is not _UNSET and reviewed_at is not None and not isinstance(reviewed_at, str):
            raise TypeError(f"reviewed_at must be a string or None, got {type(reviewed_at).__name__}")
        if rejection_reason is not _UNSET and rejection_reason is not None and not isinstance(rejection_reason, str):
            raise TypeError(f"rejection_reason must be a string or None, got {type(rejection_reason).__name__}")
        if executing_started_at is not _UNSET and executing_started_at is not None and not isinstance(executing_started_at, str):
            raise TypeError(f"executing_started_at must be a string or None, got {type(executing_started_at).__name__}")
        if published_url is not _UNSET and published_url is not None and not isinstance(published_url, str):
            raise TypeError(f"published_url must be a string or None, got {type(published_url).__name__}")
        if scrub_findings is not _UNSET and not (
            isinstance(scrub_findings, list)
            and all(isinstance(item, str) for item in scrub_findings)
        ):
            raise TypeError(
                f"scrub_findings must be a list[str], got {type(scrub_findings).__name__}"
            )
        if error is not _UNSET and error is not None and not isinstance(error, str):
            raise TypeError(f"error must be a string or None, got {type(error).__name__}")
        with file_lock(self._proposals_path):
            proposals = self._load_all_proposals()
            mutated = False
            for p in proposals:
                if p.proposal_id == proposal_id:
                    if expect_status is not None and p.status != expect_status:
                        return False
                    mutated = True
                    p.status = status
                    if reviewed_at is not _UNSET:
                        p.reviewed_at = reviewed_at  # type: ignore[assignment]
                    if rejection_reason is not _UNSET:
                        p.rejection_reason = rejection_reason  # type: ignore[assignment]
                    if executing_started_at is not _UNSET:
                        p.executing_started_at = executing_started_at  # type: ignore[assignment]
                    if published_url is not _UNSET:
                        p.published_url = published_url  # type: ignore[assignment]
                    if scrub_findings is not _UNSET:
                        p.scrub_findings = scrub_findings  # type: ignore[assignment]
                    if error is not _UNSET:
                        p.error = error  # type: ignore[assignment]
                    break
            if mutated:
                self._save_all_proposals(proposals)
            return mutated

    # --- Document records (docs-from-tasks pipeline) ---

    def _load_all_documents(self) -> list[DocumentRecord]:
        if not self._documents_path.exists():
            return []
        entries: list[DocumentRecord] = []
        known = {f.name for f in fields(DocumentRecord)}
        for line in self._documents_path.read_text(encoding="utf-8").strip().splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                filtered = {k: v for k, v in raw.items() if k in known}
                for list_field in ("linked_task_ids", "bundle_file_names"):
                    if list_field in filtered:
                        filtered[list_field] = self._coerce_str_list(
                            filtered[list_field],
                        )
                record = DocumentRecord(**filtered)
                if not record.doc_id:
                    logger.warning(
                        "Skipping document record with empty doc_id in %s",
                        self._documents_path,
                    )
                    continue
                entries.append(record)
            except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
                logger.warning(
                    "Skipping corrupted document record in %s",
                    self._documents_path,
                )
                continue
        return entries

    # --- Evolution proposals (multi-PR self-improvement, proposal 118) ---

    def write_evolution_proposal(self, record: EvolutionProposal) -> None:
        """Append an :class:`EvolutionProposal`. Raises on empty/duplicate id."""
        if not record.proposal_id:
            raise ValueError("proposal_id must not be empty")
        with file_lock(self._evolution_proposals_path):
            existing = {
                r.proposal_id for r in self._load_all_evolution_proposals()
            }
            if record.proposal_id in existing:
                raise ValueError(
                    f"duplicate proposal_id: {record.proposal_id!r} "
                    "already exists"
                )
            with open(
                self._evolution_proposals_path, "a", encoding="utf-8"
            ) as f:
                f.write(json.dumps(asdict(record)) + "\n")

    def _load_all_evolution_proposals(self) -> list[EvolutionProposal]:
        if not self._evolution_proposals_path.exists():
            return []
        entries: list[EvolutionProposal] = []
        known = {f.name for f in fields(EvolutionProposal)}
        for line in self._evolution_proposals_path.read_text(
            encoding="utf-8"
        ).strip().splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                filtered = {k: v for k, v in raw.items() if k in known}
                record = EvolutionProposal(**filtered)
                if not record.proposal_id:
                    logger.warning(
                        "Skipping evolution proposal with empty "
                        "proposal_id in %s",
                        self._evolution_proposals_path,
                    )
                    continue
                entries.append(record)
            except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
                logger.warning(
                    "Skipping corrupted evolution proposal in %s",
                    self._evolution_proposals_path,
                )
                continue
        return entries

    def _save_all_evolution_proposals(
        self, records: list[EvolutionProposal]
    ) -> None:
        atomic_write(
            self._evolution_proposals_path,
            "".join(json.dumps(asdict(r)) + "\n" for r in records),
        )

    def get_evolution_proposals(self) -> list[EvolutionProposal]:
        """Return every persisted :class:`EvolutionProposal` in file order."""
        return self._load_all_evolution_proposals()

    def update_evolution_proposal_status(
        self, proposal_id: str, status: EvolutionProposalStatus
    ) -> bool:
        """Read-modify-write an :class:`EvolutionProposal` ``status``.

        A ``file_lock`` read-modify-write with a hit flag:
        loads every proposal under the store lock, flips the matching record's
        ``status`` in place, rewrites the whole file via
        :meth:`_save_all_evolution_proposals`, and returns whether a record
        matched (``False`` on no match). The one-slice-open sequencing driver
        (proposal 118) uses this to advance a ``rolling_out`` proposal to
        ``complete`` once every slice has settled, or to ``abandoned`` once a
        slice ends without a PR (:func:`slice_ended_without_pr`).
        """
        with file_lock(self._evolution_proposals_path):
            records = self._load_all_evolution_proposals()
            mutated = False
            for r in records:
                if r.proposal_id == proposal_id:
                    r.status = status
                    mutated = True
                    break
            if mutated:
                self._save_all_evolution_proposals(records)
            return mutated

    def get_slices_for_proposal(
        self, proposal_id: str
    ) -> list[EvolutionPlan]:
        """Return *proposal_id*'s slice plans, ordered by ``phase_index``.

        A proposal's slices are its :class:`EvolutionPlan`s filtered by
        ``parent_plan_id`` (proposal 118). This is the single accessor the
        sequencing arm and the sibling-exemption helper share, rather than each
        re-deriving the ``parent_plan_id``/``phase_index`` filter inline.
        """
        return sorted(
            (p for p in self.get_all_plans() if p.parent_plan_id == proposal_id),
            key=lambda plan: plan.phase_index,
        )

    def get_document_records(
        self,
        n: int | None = None,
        *,
        doc_type: DocType | None = None,
        status: DocStatus | None = None,
        task_id: str | None = None,
    ) -> list[DocumentRecord]:
        """Return document records, optionally filtered. Filters AND together."""
        records = self._load_all_documents()
        if doc_type is not None:
            records = [r for r in records if r.doc_type == doc_type]
        if status is not None:
            records = [r for r in records if r.status == status]
        if task_id is not None:
            records = [r for r in records if task_id in r.linked_task_ids]
        if n is None:
            return records
        if n <= 0:
            return []
        return records[-n:]
