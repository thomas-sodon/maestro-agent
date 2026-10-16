"""Canonical type definitions — single source of truth for all domain literals."""

from __future__ import annotations

from typing import Any, Literal, get_args

# --- Evolution Plan Priority ---
Priority = Literal[
    "task_followup", "capability", "resilience", "experiment", "maintenance",
]
VALID_PRIORITIES: frozenset[str] = frozenset(get_args(Priority))

#: Maps deprecated priority values from on-disk JSONL rows to the current
#: taxonomy. Applied lazily at parse time in ``Memory._parse_plan_line``
#: and ``EvolutionEngine.propose_plan`` — same precedent as the
#: ``acceptance_criteria`` field-default, so no bulk JSONL migration.
LEGACY_PRIORITY_REMAP: dict[str, str] = {
    "bug_fix": "resilience",
    "security": "resilience",
    "performance": "capability",
    "feature": "capability",
}

#: Maps a legacy key a plan row may carry to the
#: :class:`~maestro.memory.EvolutionPlan` attribute it fills. Applied lazily
#: through :func:`remap_plan_field_aliases` at parse time in
#: ``Memory._parse_plan_line``; when both keys are present the attribute's own
#: key wins, and no state file is rewritten.
PLAN_FIELD_KEY_ALIASES: dict[str, str] = {
    "expected_stage_impact": "expected_impact",
}


def remap_plan_field_aliases(raw: dict[str, Any]) -> dict[str, Any]:
    """Copy each :data:`PLAN_FIELD_KEY_ALIASES` alias onto its attribute key.

    Mutates and returns *raw*; an attribute key already present wins.
    """
    for alias, attr in PLAN_FIELD_KEY_ALIASES.items():
        if alias in raw and attr not in raw:
            raw[attr] = raw[alias]
    return raw


# --- Evolution Plan field caps ---
#: Hard cap on the number of acceptance criteria persisted on an
#: :class:`~maestro.memory.EvolutionPlan`.  The schema asks the LLM for 1-10
#: binding invariants; a producer-side reply that returns a wider list is
#: truncated rather than rejected, while every write door and the prompt
#: renderer reject it loudly via
#: :func:`validate_acceptance_criteria_count`.  Single-sourced here — a leaf
#: module ``maestro.evolution``, ``maestro.memory`` and ``maestro.prompts``
#: already import — so the producer slice and the boundary raise cannot
#: drift.
ACCEPTANCE_CRITERIA_MAX_ITEMS = 10

#: Hard cap on a single acceptance-criterion string.  Mirrors the identity-
#: list per-item cap (200 chars) so the binding-citation block in
#: :meth:`EvolutionEngine.self_review` cannot bloat the prompt window.  Lives
#: in this leaf module so ``maestro.memory``'s refine boundary can enforce the
#: same bound the ``maestro.evolution`` creation path truncates to.
ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS = 200


def validate_acceptance_criteria_count(
    criteria: list[str],
    *,
    field_name: str = "acceptance_criteria",
) -> None:
    """Raise :class:`ValueError` when *criteria* exceeds the documented cap.

    The single owner of the count bound's (``ACCEPTANCE_CRITERIA_MAX_ITEMS``)
    enforcement. Both persistence
    write doors (``Memory.save_evolution_plan``, ``Memory.refine_plan``) and
    the shared prompt renderer
    (:func:`~maestro.prompts.acceptance.format_acceptance_criteria_block`)
    call this, so the bound cannot drift between the door that admits a row
    and the boundary that spends context on it. *field_name* names the
    offending field in the message — ``refine_plan`` validates a caller-named
    field, the other sites the plan attribute itself.

    An at-cap list is accepted; only ``ACCEPTANCE_CRITERIA_MAX_ITEMS + 1`` and
    above raise. Producer slices (``propose_plan``, task generation) truncate
    a wide model reply rather than calling this — model-output jitter is not
    an invariant violation.
    """
    if len(criteria) > ACCEPTANCE_CRITERIA_MAX_ITEMS:
        raise ValueError(
            f"{field_name} must contain at most "
            f"{ACCEPTANCE_CRITERIA_MAX_ITEMS} items, got {len(criteria)}"
        )


# --- Evolution Plan Category ---
Category = Literal[
    "capability", "awareness", "purpose", "infrastructure",
    # ``maintenance`` is what ``pin_gap_plan`` stamps on every gap plan.
    # ``revenue_enabler`` is retired: kept only so persisted rows still load.
    "revenue_enabler", "maintenance",
]
VALID_CATEGORIES: frozenset[str] = frozenset(get_args(Category))
#: The categories a planner reply may still choose (the retired one excluded).
REPLY_CATEGORIES: frozenset[str] = VALID_CATEGORIES - {"revenue_enabler"}

# --- Evolution Plan Status ---
# Three terminal statuses with distinct dedup / learning semantics:
# - ``rejected``: human verdict (PR closed without merge); feeds
#   ``learn_from_rejection`` and triggers the M1 cooldown window in
#   ``submit_for_review``.
# - ``failed``: implementation bug or quality-gate failure (LLM/system
#   error, not a human verdict); excluded from active dedup so the
#   same idea can be re-proposed once the underlying bug is fixed.
# - ``obsolete``: agent-pruned ``pending_review`` plan from the future
#   ``curate_pending_plans`` phase (stale rationale, superseded, or no
#   longer relevant). Distinct from ``rejected`` so self-pruning never
#   feeds the rejection-learning cycle.
PlanStatus = Literal[
    "pending_review", "approved", "rejected", "implemented", "merged", "failed",
    "obsolete",
    # Proposal 118: the status a downstream (``phase_index > 0``) slice of a
    # multi-PR ``EvolutionProposal`` chain carries after it is minted but before
    # it is up for review. Deliberately excluded from every existing status
    # *allowlist* (``pending_review``, ``LIVE_STATUSES``, ``approved``, the
    # terminal states), so a held slice is inert on the allowlist-keyed
    # surfaces: ``get_pending_plans``, ``_implement_approved_plans``, the
    # dashboard, and ``submit_for_review``'s content/cluster/live-corpus and
    # saturation guards (which read the ``pending_review``-only or
    # ``LIVE_STATUSES`` corpus) all skip it. One layer is *not* allowlist-keyed:
    # ``submit_for_review``'s exact-title dedup reads a denylist (every status
    # except ``rejected``/``failed``/``obsolete``) and so does see a held slice —
    # a slice titled X dedups a later same-title plan like any other
    # non-terminal plan. Exempting sibling slices from that layer is deferred
    # with the rest of the sequencing arm. ``VALID_PLAN_STATUSES`` (dashboard)
    # and the tool status-filter description derive from ``get_args(PlanStatus)``,
    # so both pick this value up automatically.
    "sliced",
    # Proposal 131: the status a plan carries once the future ``ASK_HUMAN``
    # disposition arm pauses it on a human-decidable blocker, keyed to the
    # question message by ``EvolutionPlan.blocked_question_id`` and re-admitted
    # when the human's reply threads back to it. Inert exactly like ``sliced``:
    # deliberately excluded from every existing status *allowlist* so a blocked
    # plan is dormant until re-admitted. ``LIVE_STATUSES`` omits it (so
    # ``get_live_plan_records`` and the ``submit_for_review`` live-corpus guards
    # skip it), ``get_pending_plans`` selects only ``pending_review``, and
    # ``_implement_approved_plans``'s eligibility filter selects only
    # ``approved``/``failed``, so a blocked plan is never attempted. Like
    # ``sliced`` it is *not* excluded from ``submit_for_review``'s denylist-keyed
    # exact-title dedup (every status except ``rejected``/``failed``/``obsolete``),
    # which is intended: a paused plan should dedup a later same-title
    # resubmission rather than spawning a duplicate. This slice adds *only* the
    # inert status; nothing sets it yet. ``VALID_PLAN_STATUSES`` (dashboard) and
    # the tool status-filter description derive from ``get_args(PlanStatus)``, so
    # both accept this value automatically.
    "blocked",
]

# --- Plan Step Status ---
StepStatus = Literal["pending", "running", "done", "failed"]

# --- Task Outcome ---
TaskOutcome = Literal["success", "partial", "failure"]

# --- SDK Transcript Turn Kind ---
# One value per turn the SDK write path produces: a query prompt / session
# follow-up, a thinking block, an answer text block, a tool-use input, a
# tool-result content block, and a lifecycle-event line. Serializes as a bare
# string — the house pattern for a persisted discriminator.
TranscriptTurnKind = Literal[
    "prompt", "thinking", "text", "tool_call", "tool_result", "event",
]
VALID_TRANSCRIPT_TURN_KINDS: frozenset[str] = frozenset(
    get_args(TranscriptTurnKind),
)

# --- Task Source ---
TaskSource = Literal["external", "self_directed"]

# --- Journal Entry Type ---
JournalEntryType = Literal[
    "reflection",
    "realization",
    "question",
    "milestone",
    "evolution_note",
    "contribution",
    "audit_observation",
]

VALID_JOURNAL_ENTRY_TYPES: frozenset[str] = frozenset(get_args(JournalEntryType))

# --- Inner-Thought Source ---
# The only ``triggered_by`` tags the journal accepts: each names a seam whose
# entry text the model authored in its own voice. Everything code-templated —
# deploy notices, merges, lifecycle milestones, plan bookkeeping, audit
# findings, task outcomes — is an operational record and goes through
# ``Journal.record_event`` instead, because the journal tail is read into the
# planning prompts and an operational row there is mistaken for a thought.
InnerThoughtSource = Literal[
    "contemplation",  # contemplate()'s introspection
    "agent_tool",  # the agent's own journal tool
]

INNER_THOUGHT_SOURCES: frozenset[str] = frozenset(get_args(InnerThoughtSource))

# --- Message Sender ---
MessageSender = Literal["maestro", "thomas"]
VALID_MESSAGE_SENDERS: frozenset[str] = frozenset(get_args(MessageSender))

# --- Message Type ---
# The types the generic ``POST /api/messages`` body may write; a
# ``plan_feedback`` row (proposal 145) is folded into a plan's reviewer
# ledger, so it enters only through a validated surface, never that route.
DashboardMessageType = Literal["question", "instruction", "answer", "info"]
MessageType = Literal[DashboardMessageType, "plan_feedback"]
VALID_MESSAGE_TYPES: frozenset[str] = frozenset(get_args(MessageType))

# --- Evolution Rejection Category ---
RejectionCategory = Literal[
    "scope_too_large",
    "wrong_approach",
    "not_needed",
    "quality_issue",
    "duplicate",
    "other",
]
VALID_REJECTION_CATEGORIES: frozenset[str] = frozenset(get_args(RejectionCategory))

# --- Artifact Review Verdict ---
Verdict = Literal["publish", "improve", "reuse", "skip"]

# --- Publication Proposal Status ---
ProposalStatus = Literal[
    "pending_approval",
    "approved",
    "rejected",
    "executing",
    "published",
    "failed",
]

# --- Evolution Proposal Status (proposal 118: multi-PR evolution) ---
# The four lifecycle states an ``EvolutionProposal`` — the runtime owner of an
# ordered chain of thin, individually-reviewed plan slices sharing one goal —
# moves through. No consumer branches on them yet; this is inert scaffolding.
EvolutionProposalStatus = Literal[
    "slicing",
    "rolling_out",
    "complete",
    "abandoned",
]

# --- Document Proposal Type (proposal: docs-from-tasks pipeline) ---
# One value per ``docs/proposals/`` subfolder. ``technical_plan`` uses an
# underscore in code; the on-disk folder name is ``technical-plan``. Kept to
# read historical ``DocumentRecord`` rows (the pipeline was retired, proposal 149).
DocType = Literal[
    "core",
    "security",
    "publication",
    "audience",
    "income",
    "research",
    "ux",
    "technical_plan",
    "operations",
]

# --- Document Proposal Status ---
DocStatus = Literal["pending", "pr_opened", "skipped", "failed"]

# --- Question Resolution Disposition ---
# Set by ``_classify_disposition`` from the diff of ``open_questions`` across
# an ``update_self_model`` call: ``removed`` (LLM dropped it from the list),
# ``refined`` (replaced by a token-overlapping successor), ``kept`` (still
# present unchanged), ``unresolved`` (orphan — was not in the pre-update list
# either, so the LLM had nothing to act on).
QuestionDisposition = Literal["removed", "refined", "kept", "unresolved"]
