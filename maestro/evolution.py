"""Self-Evolution Engine — propose (gap-fuelled), implement, create PRs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import subprocess
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, cast, get_args

from maestro import (
    evolution_quality,
    evolution_submission,
    evolution_tend,
    gate_environment,
    plan_grounding,
    tree_retrieval,
)
from maestro.audit_checks import (
    GateActivationBackend,
    JsonlGateActivationLedger,
    _check_criteria_verifiability,
    select_curation_target,
)
from maestro.call_policy import evolution_slot_system, single_use_call
from maestro.cost_tracking import (
    CostSampleBackend,
    JsonlCostSampleStore,
)
from maestro.evolution_fuel import (
    GATE_OWNER_ID,
    EvolutionFuel,
    GapContextInputs,
    classify_gap_trust,
    effective_operator_logins,
    owner_id_of_title,
    render_gap_block,
)
from maestro.evolution_quality import (
    _MAX_WARNING_BULLETS as _MAX_WARNING_BULLETS,
)
from maestro.evolution_quality import (
    QualityGateResult,
    _ScopeTrace,
)
from maestro.evolution_quality import (
    _symbol_has_call_site as _symbol_has_call_site,
)
from maestro.evolution_tend import TendPRsResult, count_eligible_threads_fail_safe
from maestro.gap_store import build_gap_store
from maestro.gate_convergence import (
    ConvergenceDecision,
    ConvergenceResult,
)
from maestro.gate_observation import (
    Attribution,
    GateNonAnswer,
    GateObservation,
    GateRunObservation,
    GateTool,
)
from maestro.issues import Issue, IssueTracker
from maestro.memory import (
    _OBSOLETE_REASON_MAX_CHARS,
    _PLAN_CHANGE_ITEM_MAX_CHARS,
    _PLAN_RATIONALE_MAX_CHARS,
    DuplicatePlanError,
    EvolutionPlan,
    EvolutionProposal,
    Memory,
    RejectionRecord,
    refine_cap_violation,
)
from maestro.plan_feedback import render_feedback_section, unaddressed_feedback
from maestro.plan_feedback_curation import (
    curate_with_feedback,
    notify_feedback_response,
)
from maestro.plan_review_message import compose_review_message
from maestro.pr_facts import (
    MergeTruth,
    PrStaleness,
    select_live_plans,
)
from maestro.pr_ledger import PRLedger, PRLedgerBackend
from maestro.prompts.acceptance import format_acceptance_criteria_block
from maestro.prompts.curate_plan import render_curate_prompt
from maestro.prompts.defaults import claude_md_guidance
from maestro.prompts.grounded_planning import (
    _GROUNDED_CURATE_PROMPT,
    _GROUNDED_PLANNING_CHAIN_PROMPT,
    _GROUNDED_PLANNING_PROMPT,
    reread_affected_prompt,
)
from maestro.prompts.plan_fields import (
    _GROUNDING_SCHEMA_PROPERTY,
    _PLAN_GROUNDING_PROMPT,
    _REALITY_NAMES_FIELDS_PROMPT,
    _REALITY_NAMES_SCHEMA_PROPERTIES,
    _WORKSPACE_CITATION_PROMPT,
    _WORKSPACE_ITEM_IDS_SCHEMA_PROPERTY,
)
from maestro.providers.anthropic_tool_adapter import AnthropicToolAdapter
from maestro.reality_inventory import (
    MAX_CLEARANCE_NAMES,
    RealityInventory,
    load_reality_inventory,
    make_inventory_reader,
)
from maestro.remediation_tracker import (
    planning_base_branch,
    recent_planning_tasks,
)
from maestro.sdk import (
    TRANSIENT_SDK_CATEGORIES,
    ErrorCategory,
    QueryOptions,
    QueryResult,
    SDKError,
    _note_session_effort_override,
    json_complete,
    query,
    query_on_session,
)
from maestro.tool_gating import make_task_tool_hooks
from maestro.types import (
    ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS,
    ACCEPTANCE_CRITERIA_MAX_ITEMS,
    LEGACY_PRIORITY_REMAP,
    REPLY_CATEGORIES,
    VALID_PRIORITIES,
    VALID_REJECTION_CATEGORIES,
    Category,
    PlanStatus,
    Priority,
    RejectionCategory,
)
from maestro.vcs import (
    DirtyWorkingTreeError,
    PrMergeState,
    SubprocessCodeHost,
    SubprocessVCS,
    VCSError,
    evolution_branch,
)

if TYPE_CHECKING:
    from maestro.config import Config
    from maestro.consciousness import Consciousness
    from maestro.journal import Journal
    from maestro.messaging import MessageStore
    from maestro.notifications.base import Notifier
    from maestro.providers.base import Session
    from maestro.vcs import CodeHost, VCSClient

logger = logging.getLogger("maestro")


#: Substrings that mark a ``git push`` rejection caused by the remote branch
#: carrying history unrelated to (or ahead of) the local branch — the
#: signature of the stale-orphan collision reconciled in
#: :meth:`EvolutionEngine.create_pr` (#2813). Matched case-insensitively against the
#: :class:`VCSError` message, which carries git's stderr (see
#: ``SubprocessVCS._run``). Only this class of push failure triggers the
#: orphan-delete reconciliation; any other ``VCSError`` re-raises unchanged.
_NON_FAST_FORWARD_MARKERS = (
    "non-fast-forward",
    "[rejected]",
    "fetch first",
)


def _is_non_fast_forward_rejection(message: str) -> bool:
    """Return ``True`` when *message* looks like a non-fast-forward push rejection."""
    lowered = message.lower()
    return any(marker in lowered for marker in _NON_FAST_FORWARD_MARKERS)


def _cap_text(text: str, max_chars: int) -> str:
    """Tail-truncate ``text`` to at most ``max_chars`` with a marker line.

    Used for unbounded operational inputs (open issues, gate output) where
    the most recent / most-relevant content lives at the end.

    The marker reports the number of *source* characters removed (the marker
    line itself excluded): the marker consumes part of the budget, so only
    ``keep = max_chars - len(marker)`` original characters survive and the true
    number dropped is ``len(text) - keep``. The returned string still satisfies
    ``len(result) <= max_chars``.
    """
    if len(text) <= max_chars:
        return text
    # Converge the reported count against the marker length it produces: the
    # honest number of original characters dropped is ``len(text) - keep``, but
    # that count's digit width feeds back into ``len(marker)`` (and thus
    # ``keep``). Seed with the raw-budget lower bound and refine — the sequence
    # is non-decreasing and bounded, so a handful of passes reach the fixed
    # point.
    omitted = len(text) - max_chars
    for _ in range(5):
        marker = f"\n[... {omitted} chars truncated ...]\n"
        keep = max_chars - len(marker)
        true_omitted = len(text) - keep
        if true_omitted == omitted:
            break
        omitted = true_omitted
    marker = f"\n[... {omitted} chars truncated ...]\n"
    keep = max_chars - len(marker)
    if keep <= 0:
        return marker[:max_chars]
    return marker + text[-keep:]


#: Placeholder ``proposed_changes`` entry stamped when the LLM response could
#: not be parsed into a structured plan. Shared by ``propose_plan``'s fallback
#: dict and its ``proposed_changes`` ``or``-fallback so the two never drift.
UNPARSEABLE_PLAN_PLACEHOLDER = "(Could not parse structured plan from Claude response)"


def _normalise_str_list(value: object) -> list[str]:
    """Coerce an LLM-returned value into a clean ``list[str]``.

    Mirrors the lenient coercion previously inlined at both the
    ``proposed_changes`` and ``files_affected`` call sites in
    :meth:`EvolutionEngine.propose_plan`: a list is filtered to the
    stringified non-blank entries, a non-blank string wraps into a
    one-item list, and anything else (int, dict, ``None``, blank
    string) yields ``[]``.
    """
    if isinstance(value, list):
        return [str(item) for item in value if item is not None and str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def unread_affected_paths(
    files_affected: Sequence[str],
    receipt: tree_retrieval.TreeReadReceipt,
    root: Path,
) -> list[str]:
    """Return the ``files_affected`` entries the grounded planner left unread.

    Proposal 150: an entry is returned when, normalised to a POSIX path, it ends
    in ``.py``, exists on disk, and is absent from ``receipt.read_paths()`` — the
    admitted ``read_source`` / ``list_cli_surface`` targets only. ``grep_source``
    / ``find_definition`` target a pattern or symbol rather than a file, so their
    hits never mark a path read and never suppress an entry here. Order-preserving
    and de-duplicated; a pure read with no side effects.

    The existence check is rooted at ``root`` — the caller's work-target
    ``repo_workspace`` — rather than the process CWD, matching the rest of this
    module's filesystem access and the ``repo_workspace``-rooted tree view whose
    ``receipt`` this function compares against. A repo-relative ``files_affected``
    entry (e.g. ``maestro/foo.py``) is thus resolved against the work tree even
    under a ``[work]`` redirect.
    """
    read = set(receipt.read_paths())
    unread: list[str] = []
    for entry in files_affected:
        if not isinstance(entry, str):
            continue
        normalised = PurePosixPath(entry.strip()).as_posix()
        if (
            normalised.endswith(".py")
            and normalised not in read
            and normalised not in unread
            and (root / normalised).exists()
        ):
            unread.append(normalised)
    return unread


class ProposalParseError(Exception):
    """Raised when ``propose_plan`` cannot parse a usable plan from the LLM.

    Returning a fallback ``"Evolution proposal"`` plan in this case (the
    pre-fix behaviour) wasted human approval bandwidth on placeholder
    proposals like ``"(Could not parse structured plan from Claude
    response)"``.  Callers in :meth:`MaestroAgent.evolve` catch this and
    journal the failure instead of submitting junk for review.
    """


def _structured_output_as_parse_error(
    exc: SDKError, caller: str,
) -> ProposalParseError | None:
    """Translate a schema-exhausted ``SDKError`` into ``ProposalParseError``.

    The CLI's ``StructuredOutput`` validator rejecting every attempt is the
    same model-output invalidity a ``parse_json`` failure is — it just
    surfaces before a result arrives — so a proposal caller routes it into
    its existing unparseable-reply contract rather than crashing the loop
    (#4535). Any other category returns ``None`` for the caller to re-raise.
    """
    if exc.category is not ErrorCategory.STRUCTURED_OUTPUT_INVALID:
        return None
    return ProposalParseError(
        f"{caller}: structured output rejected by schema validator ({exc})",
    )


#: Cap on the ``git status --porcelain`` excerpt quoted back in a
#: :class:`~maestro.vcs.DirtyWorkingTreeError`. The message names what the
#: operator has to clear, so it must be readable in a log line; a tree with
#: hundreds of stray paths is a host condition, not a listing to reproduce.
_DIRTY_TREE_STATUS_CHARS = 1000


#: Hard cap on a model-supplied plan ``title``. The title is interpolated into
#: the approval notification, the PR title/body, and the decomposition prompt.
_PLAN_TITLE_MAX_CHARS = 200

#: Hard cap on ``expected_impact``, which flows into the approval
#: notification and the self-review prompt.
_PLAN_IMPACT_MAX_CHARS = 500

#: Hard cap on a model-supplied :class:`~maestro.plan_grounding.GroundingProbe`
#: ``locator`` (proposal 114). The locator flows verbatim into the
#: ``[ungrounded:<category>]`` ``obsolete_reason`` that
#: :meth:`EvolutionEngine.submit_for_review`'s grounding guard passes to
#: :meth:`Memory.mark_plan_obsolete`, which raises above
#: ``_OBSOLETE_REASON_MAX_CHARS`` (280); with the ``[planner-*]`` tag the
#: worst case is ~242. Capping at parse keeps every embedding under it, so
#: an over-length model locator can never make ``mark_plan_obsolete`` raise
#: *after* ``evolution_submission._record_obsolete_duplicate`` has already saved the plan — which
#: would leave the ungrounded plan persisted ``pending_review`` and reachable by
#: the reviewer, defeating the guard.
_GROUNDING_LOCATOR_MAX_CHARS = 120

#: Wall-clock ceiling on the ``LINT_PRESENT`` grounding probe's single-rule ruff
#: read (proposal 114). A ``ruff check --select <rule> -- <path>`` over one file
#: is sub-second in practice; the bound just guarantees a hung ruff can never
#: stall :meth:`EvolutionEngine.submit_for_review`. A timeout is caught as the
#: ``None`` fail-open signal (:meth:`EvolutionEngine._run_lint_probe`), so the
#: plan reaches a human rather than being false-refused.
_LINT_PROBE_TIMEOUT_SECONDS = 30.0

#: Shape a model-supplied ``LINT_PRESENT`` probe's rule code must match to be
#: parsed (proposal 114) — a ruff rule is one-to-five uppercase letters followed
#: by one-to-five digits (e.g. ``E501``, ``RUF036``, ``PLW2901``). A missing or
#: malformed rule drops the probe to ``grounding=None`` in
#: :meth:`EvolutionEngine._grounding_from_spec`, so the plan proceeds unguarded
#: rather than issuing a ruff read for a nonsense ``--select`` token.
_RUFF_RULE_RE = re.compile(r"^[A-Z]{1,5}[0-9]{1,5}$")

#: Per-item char cap on the reality-grounding declarations parsed by
#: :meth:`EvolutionEngine._plan_from_spec` (proposal 136).
_REALITY_DECLARATION_MAX_CHARS = 120

#: Hard cap on the number of slices in one multi-PR ``EvolutionProposal`` chain
#: (proposal 118).  ``propose_plan_chain`` asks the LLM for an ordered
#: 2..``_MAX_PROPOSAL_SLICES`` slice array; a wider reply is truncated so a
#: single over-eager decomposition cannot mint an unbounded chain, and a reply
#: with fewer than two valid slices collapses to the single-plan path.
_MAX_PROPOSAL_SLICES = 5

# ---------------------------------------------------------------------------
# Vacuous-criteria validator — three deterministic heuristics that reject
# acceptance criteria which are syntactically valid but contain no testable
# invariant.  Applied at propose_plan() time (zero SDK cost) so vacuous
# criteria never reach self_review or the PR body.
# ---------------------------------------------------------------------------

#: Criteria shorter than this many characters are almost never specific
#: enough to be testable (e.g. "improve things").
_CRITERIA_MIN_CHARS = 25

#: High-frequency filler words whose dominance signals a vacuous criterion.
#: When more than 40% of the tokens (case-insensitive) match this set, the
#: criterion is rejected.
_CRITERIA_FILLER_WORDS: frozenset[str] = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "should", "will", "shall", "would", "could", "may", "might", "must",
    "and", "or", "but", "of", "in", "to", "for", "with", "on", "at",
    "by", "it", "its", "this", "that", "these", "those",
    "properly", "correctly", "appropriately", "well", "good", "better",
    "best", "ensure", "ensures", "make", "makes", "sure",
    "improve", "improves", "improved", "improvement",
    "work", "works", "working", "worked",
    "handle", "handles", "handled", "handling",
})

#: Ratio threshold for filler-word dominance.  Criteria where more than
#: this fraction of tokens are filler words are treated as vacuous.
_CRITERIA_FILLER_RATIO = 0.40

#: Generic nouns that, when they constitute the only "specific" content of
#: a criterion (after stripping filler), indicate vacuousness.  A criterion
#: whose non-filler tokens are exclusively from this set is rejected.
_CRITERIA_GENERIC_NOUNS: frozenset[str] = frozenset({
    "code", "system", "performance", "quality", "implementation",
    "functionality", "feature", "behavior", "behaviour", "output",
    "input", "process", "data", "result", "results", "change",
    "changes", "things", "stuff", "everything", "anything",
    "something", "nothing", "tests", "test", "testing",
    "error", "errors", "issue", "issues", "problem", "problems",
    "function", "functions", "method", "methods", "module", "modules",
})


@dataclass
class MergeCheckResult:
    """Result of checking implemented plans for merged/closed PRs."""

    merged: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    """(plan_id, rejection_reason) pairs for PRs closed without merging."""


# Proposal 131 RESUME arm: bounds on the failing-identity list named in the
# resume preamble that ``implement`` prepends to the implementation prompt. That
# string enters a prompt, so it obeys CLAUDE.md's context-bloat cap — bounded by
# the same rule :func:`maestro.gate_observation.render_identities` applies to its
# one-line join (sorted, capped at a limit of identities, then the join capped at
# a character count), applied here directly since the persisted baseline is
# already the sorted identity list rather than a live ``GateRunObservation``.
_RESUME_PREAMBLE_IDENTITY_LIMIT = 10
_RESUME_PREAMBLE_IDENTITY_CHARS = 400


def _resume_preamble(failing_identities: list[str]) -> str:
    """Return the bounded proposal-131 resume preamble for the implementation prompt.

    Prepended to the implementation prompt on the ``RESUME`` arm so the writer
    continues from the preserved evolution-branch HEAD rather than cold-starting:
    it names the prior attempt's still-failing quality-gate identities — bounded
    exactly as :func:`~maestro.gate_observation.render_identities` bounds its
    one-line join (sorted, capped at :data:`_RESUME_PREAMBLE_IDENTITY_LIMIT`
    identities, the join capped at :data:`_RESUME_PREAMBLE_IDENTITY_CHARS`
    characters) since it enters a prompt — and tells the model to build on the
    existing commits and clear exactly those remaining failures. Pure.
    """
    # The persisted baseline is in the progress vocabulary (#4309), whose repeat
    # ordinals (``key#2``) are a comparison device, not something to show the
    # model; render each distinct key once.
    distinct = {re.sub(r"#\d+$", "", identity) for identity in failing_identities}
    rendered = ", ".join(
        sorted(distinct)[:_RESUME_PREAMBLE_IDENTITY_LIMIT],
    )[:_RESUME_PREAMBLE_IDENTITY_CHARS]
    return (
        "You are RESUMING a previous implementation attempt of this plan, not "
        "starting over. The evolution branch already carries your prior work and "
        "is checked out with origin/main merged in — continue from the existing "
        "branch HEAD, building on those commits rather than reverting or "
        "rewriting them.\n"
        "The previous attempt stalled with these quality gates still failing: "
        f"{rendered}.\n"
        "Focus your changes on clearing exactly those remaining failures.\n\n"
    )


# Proposal 131 re-admission arm: bound on the human answer text named in the
# preamble ``implement`` prepends when a ``blocked`` plan is re-admitted after
# Thomas answers its blocking question. That string enters a prompt, so it obeys
# CLAUDE.md's context-bloat cap — bounded here directly, since the persisted
# ``blocked_answer`` is already a single (message-length-capped) string.
_BLOCKED_ANSWER_PREAMBLE_CHARS = 1000


def _blocked_answer_preamble(answer: str) -> str:
    """Return the bounded proposal-131 re-admission preamble.

    Prepended to the implementation prompt when a ``blocked`` plan is re-admitted
    after Thomas answers its blocking question (see
    :meth:`~maestro.agent.MaestroAgent._readmit_blocked_plan`): it carries the
    human's decision forward so the writer acts on it instead of cold-restarting
    into the same recurring environment/infrastructure wall no code change could
    clear. The answer text enters a prompt, so it obeys CLAUDE.md's context-bloat
    cap — bounded at :data:`_BLOCKED_ANSWER_PREAMBLE_CHARS` characters. Pure.
    """
    rendered = answer.strip()[:_BLOCKED_ANSWER_PREAMBLE_CHARS]
    return (
        "This plan was previously PAUSED on a recurring environment or "
        "infrastructure failure that no code change could clear, and Thomas was "
        "asked how to proceed. He has now answered:\n"
        f"{rendered}\n"
        "Act on this decision as you implement the plan — do not restart blind "
        "into the same failure.\n\n"
    )


def pin_gap_plan(plan: EvolutionPlan, gap: EvolutionFuel) -> EvolutionPlan:
    """Pin *plan* to the gap issue it closes (proposal 155 step 4).

    ``issue_number`` makes the PR body's ``Closes #N`` close the issue on
    merge; the category is ``maintenance``; ``problem_ref`` is ``None``
    because the problem is the issue. Applied to a lone plan and every slice.
    """
    plan.issue_number = gap.issue.number
    plan.category = "maintenance"
    plan.problem_ref = None
    return plan


class EvolutionEngine:
    def __init__(
        self,
        memory: Memory,
        consciousness: Consciousness,
        journal: Journal,
        notifier: Notifier,
        issues: IssueTracker | None = None,
        github_repo: str | None = None,
        *,
        config: Config,
        vcs: VCSClient | None = None,
        code_host: CodeHost | None = None,
        mcp_servers: dict[str, dict[str, Any]] | None = None,
        message_store: MessageStore | None = None,
        pr_ledger_backend: PRLedgerBackend | None = None,
        gate_activation_backend: GateActivationBackend | None = None,
        cost_sample_backend: CostSampleBackend | None = None,
    ) -> None:
        self._memory = memory
        self._consciousness = consciousness
        self._journal = journal
        self._notifier = notifier
        # Repo location comes from the WorkTarget owner (proposal 107): workspace falls back to ``wt.repo_workspace`` and
        # ``github_repo`` to ``wt.github_repo``, mirroring the ``MaestroAgent``
        # fallback arms so a direct construction under a ``[work]`` redirect
        # targets the right repo, not ``None``.
        wt = config.work_target()
        github_repo = github_repo or wt.github_repo
        self._github_repo = github_repo
        self._orig_branch: str = "main"
        self._config = config
        self._mcp_servers = mcp_servers
        self._message_store = message_store

        self._issues = issues or IssueTracker(
            github_repo, timeout=self._config.gh_timeout
        )
        self._pr_ledger = PRLedger(
            config.memory_dir,
            max_attempts_per_head=config.pr_remediation_max_attempts_per_head,
            backend=pr_ledger_backend,
        )
        # Gate-activation trace backend (proposal 114) — the flag-selected ledger
        # the plan-grounding submission guard stamps a ``plan_grounding`` row into.
        # Defaults to the JSONL ledger, mirroring ``pr_ledger_backend`` above.
        self._gate_activations: GateActivationBackend = (
            gate_activation_backend or JsonlGateActivationLedger(config.memory_dir)
        )
        # Cost-sample store (proposal 144) for the measured spend line
        # ``propose_plan`` shows.
        self._cost_samples: CostSampleBackend = (
            cost_sample_backend or JsonlCostSampleStore(config.cost_samples_path)
        )
        self._vcs = vcs or SubprocessVCS(
            wt.repo_workspace,
            timeout=config.git_timeout,
            quick_timeout=config.git_quick_timeout,
            push_timeout=config.implement_timeout,
        )
        self._host = code_host or SubprocessCodeHost(
            github_repo,
            workspace=wt.repo_workspace,
            timeout=config.gh_timeout,
            pr_creation_timeout=config.pr_creation_timeout,
            reviewer=config.github_reviewer,
        )
        # Base-commit gate baselines (proposal 124):
        # ``commit sha -> GateRunObservation of the tree at that sha``, written
        # only by ``_observe_baseline`` and only while the checked-out HEAD
        # already *is* that sha. An absent key means "no baseline was captured
        # for this base", which ``gate_observation.attribute`` reads as
        # ``UNATTRIBUTABLE`` — today's behaviour — so a miss is never a wrong
        # answer, only a less informed one.
        #
        # Deliberately *not* an iteration-cleared ``_*_memo`` like ``agent.py``'s
        # ``_merge_truth_memo`` / ``_pr_readiness_memo``. The claim is narrower
        # than "a commit is immutable", because ``run_quality_gates`` observes a
        # tree and an environment rather than an object: a row is banked only
        # from a *clean* tree standing on that sha, which pins the code half for
        # good, and the environment half is pinned only for the process's life —
        # a repaired container is picked up on the next restart. It is bounded to
        # ``_MAX_GATE_BASELINES`` by dropping the oldest inserted key, so a
        # restart costs at most one re-run per base.
        self._gate_baselines: dict[str, GateRunObservation] = {}

    @property
    def code_host(self) -> CodeHost:
        """Public read-only handle to the evolution engine's CodeHost."""
        return self._host

    @property
    def mcp_servers(self) -> dict[str, dict[str, Any]] | None:
        """Public read-only handle to the configured MCP-server dict."""
        return self._mcp_servers

    def count_eligible_threads_fail_safe(self, pr_number: int) -> int:
        """Delegate to the module-level :func:`count_eligible_threads_fail_safe`."""
        return count_eligible_threads_fail_safe(self._host, pr_number)

    def _run_gate_command(
        self,
        tool: GateTool,
        cmd: list[str],
        workspace: str,
        *,
        timeout_seconds: int | None = None,
    ) -> subprocess.CompletedProcess[str] | GateObservation:
        """See :func:`evolution_quality._run_gate_command`."""
        return evolution_quality._run_gate_command(self, tool, cmd, workspace, timeout_seconds=timeout_seconds)

    def _observe_gate(
        self,
        tool: GateTool,
        cmd: list[str],
        proc: subprocess.CompletedProcess[str],
        workspace: str,
        *,
        timeout_seconds: int | None = None,
    ) -> GateObservation:
        """See :func:`evolution_quality._observe_gate`."""
        return evolution_quality._observe_gate(self, tool, cmd, proc, workspace, timeout_seconds=timeout_seconds)

    def collect_suite(self) -> tuple[int, str, str] | GateNonAnswer:
        """See :func:`evolution_quality.collect_suite`."""
        return evolution_quality.collect_suite(self)

    # Bound by assignment, not a ``def`` that forwards: a forwarding body would be
    # a second ``run_quality_gates(scope=...)`` call the gate-acceptance guard and
    # the one-narrowed-call lease guard both count (see evolution_quality).
    run_quality_gates = evolution_quality.run_quality_gates

    def _declined_gate_result(
        self, verdict: gate_environment.GateRunnabilityVerdict,
    ) -> QualityGateResult:
        """See :func:`evolution_quality._declined_gate_result`."""
        return evolution_quality._declined_gate_result(self, verdict)

    def _observe_baseline(self, sha: str) -> GateRunObservation | None:
        """See :func:`evolution_quality._observe_baseline`."""
        return evolution_quality._observe_baseline(self, sha)

    def _long_context_active(self, session: Session | None) -> bool:
        """Whether the long-context seam should widen a prompt's caps.

        Long-context is active either on the fresh-context fallback (no
        session — the caller attaches model+betas+budget directly) or on a
        session that itself was created on the long-context model
        (``agent.implement_plan``'s impl session flips when
        ``long_context_implementation_enabled`` is set, so the caps should
        expand to match the larger window the session already holds).
        ``query_on_session`` inherits the session's model and betas, so the
        session-reuse path never re-attaches those.

        Sole owner of the seam decision — ``self_review`` and
        ``converge_to_green`` both read it so the auto-fix step widens on
        exactly the same condition self-review does.
        """
        long_context_model = self._config.sdk_model_long_context
        return bool(long_context_model) and (
            session is None
            or self._config.long_context_implementation_enabled
        )

    def action_gate_hooks(self, plan: EvolutionPlan) -> dict[str, Any]:
        """Build the issue #2280 ``Bash`` action-gate for *plan*'s own branch.

        ``implementation`` is declared repo-mutating in
        ``tool_surface.action_gated_phases()``, but that declaration only constrains which
        *provider* may serve the phase — both guard-carrying providers derive
        the actual gate from ``QueryOptions.hooks`` (the Anthropic path
        installs the ``PreToolUse`` callback only when ``hooks is not None``;
        ``cli_paced`` injects no ``--settings`` without it). A ``Bash``-granting
        ``QueryOptions`` with no ``hooks=`` is therefore un-gated on *every*
        provider, so the sessionless arms of ``implement`` / ``self_review`` /
        ``_run_auto_fix_query`` must each install it (issue #2914).

        Sole owner of the branch-scoping decision for the whole implementation
        lineage — public because the fourth consumer is ``agent.py``'s Session
        C impl session, which serves the same ``implementation`` phase over the
        same evolve branch and must not re-derive the scoping independently.
        ``allowed_branches`` is the plan's own evolve branch, never ``None`` —
        the gate fails *closed* on an empty set, which would deny the
        implementation phase's own pushes. ``current_branch`` resolves a
        refspec-less ``git push`` (proposal 099) and ``strip_redirections``
        carries the proposal-096 tokenizer posture, mirroring the executor's
        ``run_task`` wiring. Typed as ``QueryOptions.hooks`` is
        (``dict[str, Any]``) rather than naming the vendor ``HookMatcher``,
        which only ``sdk.py`` / ``tool_gating.py`` / provider glue may import.

        Every consumer pairs these hooks with ``phase="implementation"`` on the
        SDK call, so provider routing binds *directly* to the action-gated
        phase ``validate_action_gate_coverage`` checks. Without the tag the
        sessionless arms resolved through ``get_phase_provider(None)`` to the
        active provider, safe only by way of the unrelated config invariant
        that forbids ``sdk_phase_providers`` while ``enable_session_reuse`` is
        off — exactly the kind of indirect safety issue #2914 was about.
        """
        branch = evolution_branch(plan.plan_id)
        return make_task_tool_hooks(
            frozenset({branch}),
            strip_redirections=self._config.gate_strip_redirections_enabled,
            current_branch=branch,
        )

    @staticmethod
    def _auto_fix_diagnosis(
        review: QualityGateResult,
        attribution: Attribution | None,
        cap: int,
    ) -> str:
        """See :func:`evolution_quality._auto_fix_diagnosis`."""
        return evolution_quality._auto_fix_diagnosis(review, attribution, cap)

    async def _run_auto_fix_query(
        self,
        plan: EvolutionPlan,
        review: QualityGateResult,
        session: Session | None = None,
        *,
        round_n: int,
        max_rounds: int,
        long_context: bool = False,
        bypass_warning_throttle: bool = False,
        attribution: Attribution | None = None,
    ) -> bool:
        """See :func:`evolution_quality._run_auto_fix_query`."""
        return await evolution_quality._run_auto_fix_query(self, plan, review, session, round_n=round_n, max_rounds=max_rounds, long_context=long_context, bypass_warning_throttle=bypass_warning_throttle, attribution=attribution)

    async def auto_fix(
        self,
        plan: EvolutionPlan,
        review: QualityGateResult,
        session: Session | None = None,
        *,
        round_n: int,
        max_rounds: int,
        long_context: bool = False,
        bypass_warning_throttle: bool = False,
        attribution: Attribution | None = None,
    ) -> bool:
        """See :func:`evolution_quality.auto_fix`."""
        return await evolution_quality.auto_fix(self, plan, review, session, round_n=round_n, max_rounds=max_rounds, long_context=long_context, bypass_warning_throttle=bypass_warning_throttle, attribution=attribution)

    async def converge_to_green(
        self,
        plan: EvolutionPlan,
        session: Session | None = None,
        *,
        ceiling: int,
        base_hash: str | None = None,
    ) -> ConvergenceResult:
        """See :func:`evolution_quality.converge_to_green`."""
        return await evolution_quality.converge_to_green(self, plan, session, ceiling=ceiling, base_hash=base_hash)

    def _impacted_scope(self, plan: EvolutionPlan) -> tuple[tuple[str, ...], int]:
        """See :func:`evolution_quality._impacted_scope`."""
        return evolution_quality._impacted_scope(self, plan)

    @staticmethod
    def _restricted_signature(
        review: QualityGateResult,
        impacted: Sequence[str],
        *,
        own_run: bool = False,
    ) -> frozenset[str]:
        """See :func:`evolution_quality._restricted_signature`."""
        return evolution_quality._restricted_signature(review, impacted, own_run=own_run)

    @staticmethod
    def _widen_scope(
        review: QualityGateResult, impacted: Sequence[str], suite_size: int,
    ) -> tuple[str, ...]:
        """See :func:`evolution_quality._widen_scope`."""
        return evolution_quality._widen_scope(review, impacted, suite_size)

    async def _converge_rounds(
        self,
        plan: EvolutionPlan,
        session: Session | None,
        *,
        ceiling: int,
        base_hash: str | None,
        trace: _ScopeTrace,
    ) -> ConvergenceResult:
        """See :func:`evolution_quality._converge_rounds`."""
        return await evolution_quality._converge_rounds(self, plan, session, ceiling=ceiling, base_hash=base_hash, trace=trace)

    @staticmethod
    def _progress_signals(review: QualityGateResult) -> tuple[frozenset[str], bool]:
        """See :func:`evolution_quality._progress_signals`."""
        return evolution_quality._progress_signals(review)

    @staticmethod
    def _progress_counts(review: QualityGateResult) -> tuple[int, bool]:
        """See :func:`evolution_quality._progress_counts`."""
        return evolution_quality._progress_counts(review)

    def _attribute_review(
        self,
        review: QualityGateResult,
        base_key: str | None,
    ) -> tuple[Attribution | None, gate_environment.GateRunnabilityVerdict | None]:
        """See :func:`evolution_quality._attribute_review`."""
        return evolution_quality._attribute_review(self, review, base_key)

    def _convergence_terminal(
        self,
        outcome: ConvergenceDecision,
        review: QualityGateResult,
        *,
        rounds_used: int,
        plan: EvolutionPlan,
        base_hash: str | None,
        reread: bool = False,
    ) -> ConvergenceResult:
        """See :func:`evolution_quality._convergence_terminal`."""
        return evolution_quality._convergence_terminal(self, outcome, review, rounds_used=rounds_used, plan=plan, base_hash=base_hash, reread=reread)

    @staticmethod
    def validate_success_criteria(criteria: list[str]) -> list[str]:
        """Filter out vacuous acceptance criteria using four heuristics.

        1. **Length gate** — criteria shorter than ``_CRITERIA_MIN_CHARS``
           are too terse to encode a testable invariant.
        2. **Filler-word ratio** — when more than ``_CRITERIA_FILLER_RATIO``
           of the tokens are common filler words, the criterion carries no
           specific behavioural claim.
        3. **Generic-noun-only** — when every non-filler token is a
           generic noun (``_CRITERIA_GENERIC_NOUNS``), the criterion reads
           like "the system handles things properly" and is rejected.
        4. **Verifiability-marker check** (set-level) — when no surviving
           criterion contains any word from
           :data:`~maestro.audit_checks._VERIFIABLE_MARKERS` (state-change
           verbs or deliverable nouns), the entire batch is rejected.
           One verifiable word in one criterion lets the set pass.

        Returns only the criteria that survive all four checks.
        """
        kept: list[str] = []
        for criterion in criteria:
            text = criterion.strip()
            if not text:
                continue

            # 1. Length gate
            if len(text) < _CRITERIA_MIN_CHARS:
                continue

            # Tokenise: split on non-alphanumeric boundaries, lowercase
            tokens = re.findall(r"[a-zA-Z]+", text.lower())
            if not tokens:
                continue

            # 2. Filler-word ratio
            filler_count = sum(1 for t in tokens if t in _CRITERIA_FILLER_WORDS)
            if filler_count / len(tokens) > _CRITERIA_FILLER_RATIO:
                continue

            # 3. Generic-noun-only — non-filler tokens all generic
            non_filler = [t for t in tokens if t not in _CRITERIA_FILLER_WORDS]
            if non_filler and all(t in _CRITERIA_GENERIC_NOUNS for t in non_filler):
                continue

            kept.append(criterion)

        # 4. Verifiability-marker check (set-level): at least one
        # surviving criterion must contain a verifiable marker
        # (state-change verb or deliverable noun).  Purely abstract
        # criteria sets (analyze, review, assess) are rejected.
        if kept and not _check_criteria_verifiability(kept):
            kept = []

        # Telemetry: a total filter-out (LLM-quality regression or an
        # over-aggressive heuristic) is otherwise invisible to both call
        # sites (propose_plan, generate_task). Compare against non-empty
        # inputs so whitespace-only entries don't inflate the dropped count.
        considered = [c for c in criteria if c.strip()]
        dropped = len(considered) - len(kept)
        if dropped > 0:
            rejected = [c for c in considered if c not in kept]
            if not kept:
                logger.warning(
                    "validate_success_criteria filtered out all %d "
                    "acceptance criteria as vacuous; none survived: %r",
                    dropped,
                    rejected,
                )
            else:
                logger.info(
                    "validate_success_criteria dropped %d of %d acceptance "
                    "criteria as vacuous; kept=%r rejected=%r",
                    dropped,
                    len(considered),
                    kept,
                    rejected,
                )
        return kept

    async def propose_plan(
        self,
        *,
        gap: EvolutionFuel,
        session: Session | None = None,
        workspace_moment: str = "",
        workspace_moment_id: str | None = None,
        admitted_item_ids: frozenset[str] = frozenset(),
        tree_reads: bool = False,
    ) -> EvolutionPlan:
        """Generate an evolution plan that closes the gap issue *gap*.

        Proposal 155 step 4: the prompt is the deterministic, capped
        :func:`~maestro.evolution_fuel.render_gap_block` of *gap* and the loop
        state that bears on it, and the returned plan is pinned to the gap
        (:func:`pin_gap_plan`): its issue, its ``maintenance`` category and no ``problem_ref``.

        When *session* is provided, ``effort`` has no effect — the
        session-level effort configured at creation applies instead.

        Conscious-access citation (proposal 130): when *workspace_moment* is a
        non-empty rendered broadcast block, it is prepended to the prompt (once,
        before the session / bare-``query()`` branch, mirroring
        :meth:`Planner.decompose` and :meth:`Consciousness.introspect`), the
        prompt and bare-``query()`` schema gain an optional ``workspace_item_ids``
        array, and *workspace_moment_id* + the declared-then-resolved item ids are
        stamped onto the returned plan via :meth:`_plan_from_spec`. The
        *admitted_item_ids* set is the in-producer resolve filter: only ids in it
        survive, so a hallucinated id is never persisted. All three default to
        the no-citation values, so the flag-off path and ``propose_plan_chain``'s
        slices build byte-identical plans. Advisory only — no hash, gate, or
        capability change.

        Grounded planning (proposal 143): when *tree_reads* is true (the agent
        passes ``grounded_planning_enabled`` and not budget-throttled) and the
        single-use arm is on, the call carries the bounded read-only ``tree``
        surface (:meth:`_build_tree_read_options`), appends
        ``_GROUNDED_PLANNING_PROMPT``, streams to the result message (no
        text early-exit, so a JSON-bearing preamble cannot end the tool loop
        before ``structured_output`` arrives), and stamps
        ``grounded_reads`` with the paths the planner read. Otherwise prompt,
        schema and options are byte-identical and ``grounded_reads`` stays
        ``None``.
        """
        # Proposal 139: under the single-use flag this call ignores any passed
        # session and runs its stateless request through ``single_use_call``.
        if self._config.sdk_single_use_evolution_calls_enabled:
            session = None
        _note_session_effort_override("propose_plan", "high", session)

        prompt = f"""Close this capability gap:
{render_gap_block(gap, self._gap_context_inputs(gap))}

Propose a concrete evolution plan — the smallest change to your own code
that closes this gap.

{claude_md_guidance(self._config.require_claude_md_update)}

PLAN-SCOPE REQUIREMENT: Each proposed_changes entry MUST include a
verification verb (test, verify, check, validate, or confirm) for the
file it describes — the plan-scope gate rejects multi-file plans that
lack per-file verification language. For documentation entries, end with
"Verify the updated section accurately reflects the implementation."
For code entries, include how the change is tested or verified.

MINIMALISM: Prefer the smallest change that works. Before adding new
code, check whether the existing codebase, the standard library, or an
already-installed dependency already provides it — reuse over rebuild.
Do not introduce abstractions the task does not need (no interface with
one implementation, no factory for one product). Drop any proposed_changes
entry whose scope exceeds what the rationale requires. A plan may be
net-subtractive: removing a superseded module, consolidating duplicated logic,
or simplifying an over-built subsystem is a first-class evolution, not a lesser
one — deleting more than you add is a valid outcome when it reduces surface area
without losing a capability.

If a kept task branch listed above already produced relevant committed code,
set `source_task_id` to that task's id; the implementation phase will base
the new evolution branch on it instead of starting fresh from main.

`acceptance_criteria` are 1-{ACCEPTANCE_CRITERIA_MAX_ITEMS} binding, testable invariants the implementation
MUST satisfy. Each must be checkable against either a specific code feature or
a specific test assertion. Bad: "improves performance." Good: "task_cost_gate
rule 3 counts both `failure` and `partial` outcomes in the failure-cost
numerator." If the rationale is purely structural and has no behavioral
invariants, list one criterion stating that.

Reply with JSON:
{{
  "title": "Short title",
  "priority": "task_followup" | "capability" | "resilience" | "experiment" | "maintenance",
  "rationale": "Why this change closes the gap",
  "proposed_changes": ["Change 1 description", "Change 2 description", ...],
  "acceptance_criteria": ["Specific testable invariant 1", "Specific testable invariant 2", ...],
  "files_affected": ["maestro/file.py", ...],
  "expected_impact": "The change an observer outside the loop would see",
  "source_task_id": null or "<task_id>" (id of a recent task whose branch should be reused as the implementation base)
}}"""

        # Plan-premise grounding (proposal 114): append the probe instruction only
        # when the flag is on, so the flag-off prompt is byte-identical to today.
        if self._config.plan_grounding_enabled:
            prompt += _PLAN_GROUNDING_PROMPT
        # Generator reality grounding (proposal 136): names only — a gap
        # plan's problem is its issue.
        if self._config.generator_reality_grounding_enabled:
            prompt += _REALITY_NAMES_FIELDS_PROMPT

        tree_read = self._tree_read_if("propose_plan", tree_reads)
        if tree_read is not None:
            prompt += _GROUNDED_PLANNING_PROMPT

        # Conscious-access citation (proposal 130): only when a broadcast moment is
        # delivered — append the citation instruction, then prepend the rendered
        # moment block once (so it sits above the "block above" the instruction
        # references). With no moment the prompt is byte-identical to today.
        if workspace_moment:
            prompt += _WORKSPACE_CITATION_PROMPT
            prompt = f"{workspace_moment}\n\n{prompt}"

        if session is not None:
            result = await query_on_session(
                session, prompt, caller="propose_plan",
                complete_on=json_complete,
            )
        else:
            identity = self._consciousness.get_identity_prompt()
            # Build the schema properties/required as locals so the grounding
            # property is added only on the flag-on branch (proposal 114); the
            # flag-off ``output_format`` stays byte-identical to today.
            schema_properties: dict[str, Any] = {
                "title": {"type": "string"},
                "priority": {"type": "string"},
                "rationale": {"type": "string"},
                "proposed_changes": {"type": "array", "items": {"type": "string"}},
                "acceptance_criteria": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": ACCEPTANCE_CRITERIA_MAX_ITEMS,
                },
                "files_affected": {"type": "array", "items": {"type": "string"}},
                "expected_impact": {"type": "string"},
                "source_task_id": {"type": ["string", "null"]},
            }
            schema_required = ["title", "priority", "rationale",
                               "proposed_changes", "acceptance_criteria",
                               "files_affected", "expected_impact"]
            if self._config.plan_grounding_enabled:
                schema_properties["grounding"] = _GROUNDING_SCHEMA_PROPERTY
            if self._config.generator_reality_grounding_enabled:
                schema_properties.update(_REALITY_NAMES_SCHEMA_PROPERTIES)
            # Optional citation array added only when a moment is broadcast
            # (proposal 130); never required, so the flag-off schema is unchanged.
            if workspace_moment:
                schema_properties["workspace_item_ids"] = (
                    _WORKSPACE_ITEM_IDS_SCHEMA_PROPERTY
                )
            output_format: dict[str, Any] = {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": schema_properties,
                    "required": schema_required,
                },
            }
            try:
                if self._config.sdk_single_use_evolution_calls_enabled:
                    result = await single_use_call(
                        "propose_plan", prompt,
                        system=evolution_slot_system(identity),
                        output_format=output_format,
                        # The tool-bearing call streams to the ResultMessage that
                        # carries ``structured_output``: a text predicate could fire
                        # on a JSON-bearing preamble block before the tool turn.
                        complete_on=json_complete if tree_read is None else None,
                        options=None if tree_read is None else tree_read[0],
                    )
                else:
                    result = await query(
                        prompt=prompt,
                        system=identity,
                        options=QueryOptions(
                            effort="high",
                            caller="propose_plan",
                            output_format=output_format,
                        ),
                        complete_on=json_complete,
                    )
            except SDKError as e:
                parse_error = _structured_output_as_parse_error(e, "propose_plan")
                if parse_error is None:
                    raise
                raise parse_error from e
        # Grounded re-read re-prompt (proposal 150): on the grounded single-use
        # arm only, when the draft cites an existing ``.py`` in ``files_affected``
        # the planner never read and the per-call tree budget still has room,
        # re-send the full prompt plus the draft naming those unread paths and
        # re-parse exactly once — raising ``read_coverage`` so fewer drafts
        # restate shipped code. Skipped when ``tree_read`` is None (flag-off or
        # throttled) and on the ``session is not None`` branch, so those call
        # shapes stay byte-identical. ``grounded_reads`` is stamped below from
        # the final receipt, so a re-prompt's reads are reflected.
        if session is None and tree_read is not None:
            try:
                draft = result.parse_json()
            except json.JSONDecodeError:
                draft = None
            drafted = _normalise_str_list(
                draft.get("files_affected") if isinstance(draft, dict) else None,
            )
            unread = unread_affected_paths(
                drafted, tree_read[1],
                Path(self._config.work_target().repo_workspace),
            )
            budget_left = len(tree_read[1].entries) < tree_retrieval.MAX_TREE_TOOL_CALLS
            if unread and budget_left:
                try:
                    result = await single_use_call(
                        "propose_plan",
                        prompt + reread_affected_prompt(unread, result.response),
                        system=evolution_slot_system(identity),
                        output_format=output_format,
                        complete_on=None,
                        options=tree_read[0],
                    )
                except SDKError as e:
                    parse_error = _structured_output_as_parse_error(e, "propose_plan")
                    if parse_error is None:
                        raise
                    raise parse_error from e
        try:
            data = result.parse_json()
        except json.JSONDecodeError as e:
            logger.warning("Evolution LLM returned invalid JSON — JSONDecodeError: %s", e)
            raise ProposalParseError(
                f"propose_plan: LLM response was not valid JSON ({e})",
            ) from e

        if not isinstance(data, dict):
            logger.warning(
                "Evolution LLM returned a %s, expected a dict — wrapping",
                type(data).__name__,
            )
            data = {
                "proposed_changes": data if isinstance(data, list) else None,
            }

        fallback = {
            "title": "Evolution proposal",
            "priority": "capability",
            "rationale": f"Close gap issue #{gap.issue.number}",
            "proposed_changes": [UNPARSEABLE_PLAN_PLACEHOLDER],
            "acceptance_criteria": [],
            "files_affected": [],
            "expected_impact": "Incremental improvement",
            "source_task_id": None,
        }
        for key, default in fallback.items():
            data.setdefault(key, default)

        # Preserve the explicit-null fallback: ``setdefault`` above only fills a
        # *missing* key, so an LLM reply carrying ``"proposed_changes": null``
        # still lands ``None`` here — fall back to the raw response text as the
        # single change, exactly as before. Then coerce every reviewable field
        # through the shared ``_plan_from_spec`` normaliser (reused verbatim by
        # ``propose_plan_chain``'s per-slice specs).
        if data.get("proposed_changes") is None:
            data["proposed_changes"] = [result.response[:300]]
        plan = self._plan_from_spec(
            data,
            default_rationale=f"Close gap issue #{gap.issue.number}",
            workspace_moment_id=workspace_moment_id,
            admitted_item_ids=admitted_item_ids,
            gap=gap,
        )
        if tree_read is not None:
            plan.grounded_reads = tree_read[1].read_paths()
        return plan

    def tree_reads_for(self, caller: str, *, throttled: bool) -> bool:
        """Whether *caller* gets the ``tree`` surface (proposal 143): flag on,
        not throttled, and listed in ``[loop] grounded_planning_callers``."""
        return (
            self._config.grounded_planning_enabled
            and not throttled
            and caller in self._config.grounded_planning_callers
        )

    def _gap_context_inputs(self, gap: EvolutionFuel) -> GapContextInputs:
        """The loop-state reads :func:`render_gap_block` renders for *gap*.

        Each list is newest last; :data:`GAP_CONTEXT_CAPS` bounds it there.
        """
        plans = self._memory.get_all_plans()
        number = gap.issue.number
        citing = [
            f"{p.plan_id} {p.status}: {reason}"
            for p in plans
            if p.issue_number == number
            and (reason := p.obsolete_reason or p.rejection_reason or p.last_failure_reason)
        ]
        rejections = [
            f"{p.title}: {p.rejection_reason}"
            for p in plans if p.status == "rejected" and p.rejection_reason
        ]
        cited = self._memory.get_cited_source_task_ids()
        candidates = [
            f"{t.task_id} ({t.branch}): {t.goal or ''}"
            for t in recent_planning_tasks(self._memory, self._config)
            if t.outcome in ("success", "partial") and t.branch and t.task_id
            and t.task_id not in cited
        ]
        versions = [v.plan_title for v in self._memory.get_recent_versions(5)]
        live = [p.plan_id for p in select_live_plans(plans)]
        return GapContextInputs(
            citing_reasons=citing,
            recent_rejections=rejections,
            source_task_candidates=candidates,
            version_tail=versions,
            live_targets=[*live, f"issue:{number}"],
        )

    def _tree_read_if(
        self, caller: str, tree_reads: bool,
    ) -> tuple[QueryOptions, tree_retrieval.TreeReadReceipt] | None:
        """The tree grant for *caller* when granted on the single-use arm."""
        if tree_reads and self._config.sdk_single_use_evolution_calls_enabled:
            return self._build_tree_read_options(caller)
        return None

    def _build_tree_read_options(
        self, caller: str,
    ) -> tuple[QueryOptions, tree_retrieval.TreeReadReceipt] | None:
        """Build a bounded ``tree`` session's options for *caller* (proposal 143).

        Returns the per-call options plus the receipt the surface records each
        served read in, materialised through the provider tool adapter (the
        registry→session shape grounded contemplation uses). ``tools=[]``
        disables every built-in, so the session sees only the ``tree`` server.
        An empty tracked set (the VCS read failed) returns ``None``, and the
        call runs tool-free. Each call builds its own registry and receipt, so
        two planner calls never share a budget.
        """
        tracked = self._vcs.tracked_paths()
        if not tracked:
            logger.info("Grounded planning: no tracked paths — %s runs tool-free", caller)
            return None
        view = tree_retrieval.TreeView(
            root=Path(self._config.work_target().repo_workspace), tracked=tracked,
        )
        registry, receipt = tree_retrieval.build_bounded_tree_tool_set(
            view,
            inventory=make_inventory_reader(self._config, self._memory),
            rejections=self._rejection_corpus(),
        )
        allowed_tools, mcp_servers = AnthropicToolAdapter().materialize(registry)
        # ``single_use_call`` stamps the caller and its registry effort.
        options = QueryOptions(
            tools=[],
            allowed_tools=allowed_tools,
            mcp_servers=mcp_servers,
            max_turns=tree_retrieval.TREE_SESSION_MAX_TURNS,
        )
        return options, receipt

    def _rejection_corpus(
        self,
    ) -> list[tree_retrieval.RejectionEntry] | None:
        """The bounded rejected-plan corpus for ``search_rejections``.

        Returns ``None`` — leaving the sixth tool unserved — unless
        ``[loop] grounded_planning_rejection_tool_enabled`` is on; when on, reads
        the plan corpus once from memory and projects each ``rejected`` row into
        a filesystem-free :class:`tree_retrieval.RejectionEntry` the leaf
        searches. The leaf itself reads no filesystem (mirroring the
        ``TreeView`` tracked-set invariant), so the read happens here.
        """
        if not self._config.grounded_planning_rejection_tool_enabled:
            return None
        return [
            tree_retrieval.RejectionEntry(
                title=plan.title,
                status=plan.status,
                reason=plan.rejection_reason or "",
                rationale=plan.rationale,
            )
            for plan in self._memory.get_all_plans()
            if plan.status == "rejected"
        ]

    def _plan_from_spec(
        self,
        spec: dict[str, Any],
        *,
        default_rationale: str,
        status: PlanStatus = "pending_review",
        parent_plan_id: str | None = None,
        phase_index: int = 0,
        workspace_moment_id: str | None = None,
        admitted_item_ids: frozenset[str] = frozenset(),
        gap: EvolutionFuel | None = None,
    ) -> EvolutionPlan:
        """Coerce one reviewable-field spec dict into an :class:`EvolutionPlan`.

        A *gap* (proposal 155 step 4) pins the plan to its issue through
        :func:`pin_gap_plan`.

        The shared field-normalisation core of :meth:`propose_plan` and
        :meth:`propose_plan_chain` (proposal 118): priority/category coercion,
        positive-issue-number validation, acceptance-criteria filter-then-cap,
        ``source_task_id`` validation, and list normalisation — so a single-plan
        response and a chain slice spec coerce their fields identically rather
        than duplicating the block. *default_rationale* is the fallback when the spec omits a rationale
        (the gap issue's title for a lone plan, the proposal goal for a slice).
        *status* / *parent_plan_id* / *phase_index* carry the slice-linkage a
        chain sets; the defaults reproduce the lone ``pending_review`` plan
        ``propose_plan`` has always returned.

        Conscious-access citation (proposal 130): when *workspace_moment_id* is
        non-``None`` (a broadcast moment reached the decision), the declared
        ``spec["workspace_item_ids"]`` are resolved against *admitted_item_ids*
        (keep only ids actually admitted — a cheap in-producer filter that drops
        a hallucinated id — then order-preserving de-dup, so a repeated admitted
        id persists one copy and the list is bounded to ``|admitted_item_ids|``)
        and stamped onto the plan alongside the moment id;
        ``[]`` (saw the moment, cited nothing) stays distinct from ``None`` (no
        moment / legacy). With the defaults — every ``propose_plan_chain`` slice
        and the flag-off ``propose_plan`` path — both fields stay ``None``, so the
        plan is byte-identical to today. These fields are excluded from
        :func:`~maestro.memory.compute_plan_content_hash`, so an approved plan's
        review hash is unchanged.

        Prompt-bound field caps: every *prose* field this method builds is
        bounded, because the raw spec is interpolated into prompts on every
        downstream pass (approval notification, implementation prompt,
        self-review prompt, PR body, decomposition prompt). ``title``,
        ``rationale``, and ``expected_impact`` are truncated to
        ``_PLAN_TITLE_MAX_CHARS`` / ``_PLAN_RATIONALE_MAX_CHARS`` /
        ``_PLAN_IMPACT_MAX_CHARS``, and ``proposed_changes`` is per-item
        truncated to ``_PLAN_CHANGE_ITEM_MAX_CHARS``. Truncation — not
        rejection, as on :meth:`~maestro.memory.Memory.refine_plan` — because
        the first raw model output is not yet a reviewed contract, so discarding
        a whole proposal over one long string would trade bloat for a lost
        evolution cycle.

        No *count* cap is applied to any list a downstream gate reads as its
        input set. ``proposed_changes`` is scanned by
        :func:`_extract_added_cli_surface` and ``files_affected`` by
        :meth:`submit_for_review`'s CLI-grounding guard and
        :func:`~maestro.planner.check_plan_scope`; dropping tail items would
        shrink what those guards see and can *invert* them — a plan whose only
        novel CLI token sits in a dropped clause reads as all-already-shipped
        and is disposed ``obsolete``. ``files_affected`` entries are not
        per-item truncated either: a path is an identifier, not prose, and a
        truncated path names no file on disk while still being read as one.
        This matches the refinement side, which caps neither count.

        The prose truncations do have one measurable gate side-effect, accepted
        deliberately: ``_cited_cli_tokens`` mines ``rationale``, and the
        restatement Jaccard compares rationale bodies, so a phantom
        ``maestro <token>`` citation past the cut escapes
        :func:`_evidence_cli_grounding_verdict` and a near-restatement can score
        marginally lower. Both fail *open* (a plan is admitted, never wrongly
        refused), which is the safe direction for a bloat guard.
        """
        # Untrusted parsed JSON: the model may emit a list/object where an enum
        # string belongs, and an unhashable value raises on the remap lookup and
        # the frozenset membership test alike. Narrow both enums to ``str`` first
        # so a malformed enum lands on the same quiet "capability" fallback an
        # unknown string does — the scalar-or-fallback contract the rest of this
        # normaliser follows.
        raw_priority = spec.get("priority", "capability")
        if isinstance(raw_priority, str):
            remapped = LEGACY_PRIORITY_REMAP.get(raw_priority, raw_priority)
            priority = cast(
                Priority,
                remapped if remapped in VALID_PRIORITIES else "capability",
            )
        else:
            priority = cast(Priority, "capability")

        raw_category = spec.get("category", "capability")
        category = cast(
            Category,
            raw_category
            if isinstance(raw_category, str) and raw_category in REPLY_CATEGORIES
            else "capability",
        )

        raw_issue = spec.get("issue_number")
        try:
            issue_number = int(raw_issue) if raw_issue is not None else None
        except (ValueError, TypeError):
            issue_number = None
        # GitHub issue numbers must be positive integers
        if issue_number is not None and issue_number <= 0:
            issue_number = None

        raw_changes = spec.get("proposed_changes")
        if raw_changes is None:
            raw_changes = [UNPARSEABLE_PLAN_PLACEHOLDER]

        raw_title = spec.get("title") or "Evolution proposal"
        raw_rationale = spec.get("rationale") or default_rationale
        raw_files = spec.get("files_affected") or []

        raw_criteria = spec.get("acceptance_criteria") or []
        if isinstance(raw_criteria, list):
            acceptance_criteria = [
                str(c)[:ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS]
                for c in raw_criteria
                if isinstance(c, str) and c.strip()
            ]
        else:
            acceptance_criteria = []

        # filter then cap — see issue #1955
        acceptance_criteria = self.validate_success_criteria(
            acceptance_criteria,
        )[:ACCEPTANCE_CRITERIA_MAX_ITEMS]

        # Per-item char cap only — no count cap: the whole list is the input
        # ``_extract_added_cli_surface`` scans, so dropping tail clauses would
        # turn a bloat guard into a gate inversion (see the docstring).
        capped_changes = [
            c[:_PLAN_CHANGE_ITEM_MAX_CHARS]
            for c in _normalise_str_list(raw_changes)
        ]

        source_task_id = self._validate_source_task_id(spec.get("source_task_id"))

        # Conscious-access citation (proposal 130). Only when a moment reached the
        # decision (``workspace_moment_id`` set) do we stamp the provenance; the
        # declared ids are resolve-filtered against the admitted set so no
        # hallucinated id is persisted. ``None`` (no moment) stays distinct from
        # ``[]`` (saw the moment, cited nothing).
        cited_moment_id: str | None = None
        cited_item_ids: list[str] | None = None
        if workspace_moment_id is not None:
            cited_moment_id = workspace_moment_id
            declared = spec.get("workspace_item_ids")
            if isinstance(declared, list):
                # Order-preserving de-dup (mirroring the finalized episode's
                # ``cited_plan_ids``), so a reply repeating an admitted id N times
                # persists one copy — never N. Because only admitted ids survive,
                # the de-dup also bounds the list to ``|admitted_item_ids|``.
                # ``dict.fromkeys`` runs over the already-filtered generator of
                # strings, so an unhashable declared member can never raise.
                cited_item_ids = list(
                    dict.fromkeys(
                        c for c in declared
                        if isinstance(c, str) and c in admitted_item_ids
                    ),
                )
            else:
                cited_item_ids = []

        problem_ref, new_capability_names = self._reality_declarations_from_spec(
            spec,
        )

        plan = EvolutionPlan(
            plan_id=uuid.uuid4().hex[:12],
            title=str(raw_title)[:_PLAN_TITLE_MAX_CHARS],
            category=category,
            rationale=str(raw_rationale)[:_PLAN_RATIONALE_MAX_CHARS],
            proposed_changes=capped_changes or [UNPARSEABLE_PLAN_PLACEHOLDER],
            # Deliberately uncapped in both dimensions: these are the paths
            # ``check_plan_scope`` and the CLI-grounding guards read as their
            # input set, and a truncated path names no file on disk. See the
            # docstring.
            files_affected=_normalise_str_list(raw_files),
            expected_impact=str(
                spec.get("expected_impact", ""),
            )[:_PLAN_IMPACT_MAX_CHARS],
            acceptance_criteria=acceptance_criteria,
            priority=priority,
            issue_number=issue_number,
            source_task_id=source_task_id,
            grounding=self._grounding_from_spec(spec),
            status=status,
            parent_plan_id=parent_plan_id,
            phase_index=phase_index,
            created_at=datetime.now(UTC).isoformat(),
            workspace_moment_id=cited_moment_id,
            workspace_item_ids=cited_item_ids,
            problem_ref=problem_ref,
            new_capability_names=new_capability_names,
        )
        return plan if gap is None else pin_gap_plan(plan, gap)

    def _reality_declarations_from_spec(
        self, spec: dict[str, Any],
    ) -> tuple[str | None, list[str] | None]:
        """Parse ``problem_ref`` / ``new_capability_names`` from *spec*.

        Only when ``[loop] generator_reality_grounding_enabled`` is on
        (proposal 136); flag-off both stay ``None``. A non-string ref and a
        non-list name list collapse to ``None`` (the escape hatch); list members
        are filtered to strings and capped at ``MAX_CLEARANCE_NAMES`` (so the
        stored list is exactly the list :func:`clear_mint` tests), and every
        value is capped at ``_REALITY_DECLARATION_MAX_CHARS``.
        """
        if not self._config.generator_reality_grounding_enabled:
            return None, None
        raw_ref = spec.get("problem_ref")
        problem_ref = (
            raw_ref.strip()[:_REALITY_DECLARATION_MAX_CHARS]
            if isinstance(raw_ref, str) and raw_ref.strip()
            else None
        )
        raw_names = spec.get("new_capability_names")
        names = (
            [
                n[:_REALITY_DECLARATION_MAX_CHARS]
                for n in raw_names if isinstance(n, str)
            ][:MAX_CLEARANCE_NAMES]
            if isinstance(raw_names, list)
            else None
        )
        return problem_ref, names

    def _grounding_from_spec(
        self, spec: dict[str, Any],
    ) -> plan_grounding.GroundingProbe | None:
        """Parse a :class:`~maestro.plan_grounding.GroundingProbe` from *spec*.

        Returns a probe only when ``[loop] plan_grounding_enabled`` is on and
        ``spec["grounding"]`` is a dict carrying a valid ``kind`` and a non-empty
        ``locator`` — otherwise ``None`` (the flag-off path, an absent key, or a
        malformed probe). The model-supplied ``locator`` is length-capped at
        ``_GROUNDING_LOCATOR_MAX_CHARS`` so it can never overflow the
        ``[ungrounded:*]`` obsolete-reason ceiling in
        :meth:`submit_for_review`'s grounding guard. A ``LINT_PRESENT`` probe
        additionally requires a well-shaped ruff rule code in ``expectation``
        (:data:`_RUFF_RULE_RE`); a missing or malformed rule drops the probe to
        ``None`` so the plan proceeds unguarded — the safe direction — rather than
        issuing a ruff read for a nonsense ``--select`` token. A
        ``BEHAVIOR_ABSENT`` probe whose witness the plan itself introduces is
        kept: a present witness must still refute it, and only its grounded
        verdict is discounted as self-certifying at submission
        (:func:`plan_grounding.plan_introduces_witness`, #4581). Shared by every
        :meth:`_plan_from_spec` caller;
        :meth:`propose_plan_chain`'s slice specs never request a probe, so they
        parse ``None`` harmlessly (proposal 114).
        """
        if not self._config.plan_grounding_enabled:
            return None
        raw = spec.get("grounding")
        if not isinstance(raw, dict):
            return None
        kind = raw.get("kind")
        locator = raw.get("locator")
        if kind not in get_args(plan_grounding.GroundingKind):
            return None
        if not isinstance(locator, str) or not locator.strip():
            return None
        expectation_raw = raw.get("expectation")
        expectation = (
            expectation_raw.strip() if isinstance(expectation_raw, str) else ""
        )
        # A LINT_PRESENT probe's ``expectation`` is the ruff rule code the reader
        # runs against the file — require a well-shaped code so a malformed or
        # missing rule fails to a null probe (proceed unguarded) rather than
        # ``ruff --select <garbage>``.
        if kind == "LINT_PRESENT" and not _RUFF_RULE_RE.match(expectation):
            return None
        # A BEHAVIOR_ABSENT probe's ``expectation`` is the witness substring whose
        # *presence* refutes the premise, so a witness-less probe can only ever
        # ground — indistinguishable from "no duplicate found". Decline it here
        # (mirroring the LINT_PRESENT rule check) so a malformed probe proceeds
        # unguarded and is never miscounted as a grounding check that ran.
        if kind == "BEHAVIOR_ABSENT" and not expectation:
            return None
        return plan_grounding.GroundingProbe(
            kind=cast(plan_grounding.GroundingKind, kind),
            locator=locator.strip()[:_GROUNDING_LOCATOR_MAX_CHARS],
            expectation=expectation,
        )

    def _run_lint_probe(self, rule: str, path: str) -> str | None:
        """Run a bounded single-rule ruff read for a ``LINT_PRESENT`` probe.

        The injectable reader half of the ``LINT_PRESENT`` grounding check
        (proposal 114): shell out to ``ruff check --select <rule> -- <path>``
        rooted at the work-target ``repo_workspace`` and interpret the result as
        the text :func:`maestro.plan_grounding.confirm` reads —

        * exit ``0`` (ruff ran clean, no finding) → ``""`` (the claimed lint
          error is hallucinated; ``confirm`` returns ``no-such-lint``);
        * exit ``1`` (ruff reported the rule) → the captured findings text (the
          premise holds; ``confirm`` grounds the plan);
        * exit ``>= 2`` (ruff itself errored, e.g. an unknown rule) → ``None``;
        * a launch or timeout failure → ``None``.

        ``None`` is the **fail-open** signal: :meth:`submit_for_review`'s
        grounding guard skips the refusal on ``None`` and lets the plan reach a
        human rather than being false-refused when ruff cannot run. Mirrors
        :func:`maestro.red_root.run_gate_subprocess`'s bounded-subprocess shape
        (``capture_output``, ``check=False``, ``timeout``, specific-tuple
        ``except``) and its ``[sys.executable, "-m", "ruff", ...]`` invocation, so
        unit tests patch ``maestro.evolution.subprocess.run`` and touch no real
        process.
        """
        root = str(Path(self._config.work_target().repo_workspace))
        try:
            completed = subprocess.run(
                [
                    sys.executable, "-m", "ruff", "check",
                    "--select", rule, "--", path,
                ],
                capture_output=True,
                text=True,
                check=False,
                cwd=root,
                timeout=_LINT_PROBE_TIMEOUT_SECONDS,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            # A launch failure, a timeout, or a ``ValueError`` is the fail-open
            # signal — the probe could not run, so the plan proceeds to a human
            # rather than being refused on a check that never happened. The
            # ``ValueError`` covers the ``embedded null byte`` a model-supplied
            # locator can smuggle past the confinement check (a null byte is
            # neither absolute nor a ``..`` component): ``subprocess.run`` rejects
            # it as a plain ``ValueError``, not an ``OSError``, so without this it
            # would escape and crash ``submit_for_review`` — mirroring the
            # ``DEFECT_PRESENT`` read seam, which catches ``ValueError`` for the
            # same reason.
            return None
        if completed.returncode == 0:
            return ""
        if completed.returncode == 1:
            return completed.stdout + completed.stderr
        # Exit >= 2: ruff itself errored (e.g. an unknown rule) — fail open.
        return None

    def _read_grounding_tree_text(
        self, probe: plan_grounding.GroundingProbe,
    ) -> str | None:
        """Read the tree text *probe* is confirmed against — the injectable FS seam.

        :meth:`submit_for_review`'s grounding guard calls this to obtain the text
        :func:`maestro.plan_grounding.confirm` reads, keeping the only filesystem
        touch behind one overridable method so unit tests inject text and touch no
        real FS (proposal 114). For a ``DEFECT_PRESENT`` or a ``BEHAVIOR_ABSENT``
        probe it returns the single file at ``probe.locator``; for a
        ``SYMBOL_ABSENT`` probe it returns the concatenated ``maestro/**/*.py``
        corpus; for a ``LINT_PRESENT`` probe it returns the bounded single-rule
        ruff findings for the rule (``probe.expectation``) against the file
        (``probe.locator``), via :meth:`_run_lint_probe`. All are rooted at the
        work-target ``repo_workspace`` so a ``[work]`` redirect grounds against
        the right tree. Locator confinement and the file/corpus reads belong to
        :mod:`maestro.tree_retrieval`'s door reader.

        The return is ``str | None``. A ``DEFECT_PRESENT`` unreadable, absent, or
        out-of-workspace path yields ``""`` (str): the needle is then absent, so
        ``confirm`` returns ``no-such-defect`` and a plan naming a file that
        cannot be read under the tree is refused — the premise "there is a defect
        in this file" does not hold when the file is missing or unconfinable. A
        ``SYMBOL_ABSENT`` corpus read likewise degrades to ``""``, which means no
        definition is found, so the plan is ``grounded`` and falls through to the
        existing guards (fail open, never wrongly obsoleting a to-build plan).

        ``BEHAVIOR_ABSENT`` and ``LINT_PRESENT`` are the two branches that can
        return ``None``. Both are refuted by *finding* something, so an empty text
        would ground the plan and a failed read would be indistinguishable from a
        genuine pass. ``None`` keeps them apart: an unconfinable or unreadable
        ``BEHAVIOR_ABSENT`` locator, an unconfinable ``LINT_PRESENT`` locator, and
        a ruff read that could not run (:meth:`_run_lint_probe` returning
        ``None``) all fail **open** — the guard skips the refusal on ``None`` and
        stamps an ``unevaluated`` gate row, so a probe that could not be evaluated
        never false-refuses a plan *and* never counts as a check that ran.
        """
        root = Path(self._config.work_target().repo_workspace)
        if probe.kind == "BEHAVIOR_ABSENT":
            # ``None`` (not ``""``): for this kind an empty text would ground the
            # plan, making an unreadable locator indistinguishable from "the
            # witness is absent"; the guard stamps the skip ``unevaluated``.
            return tree_retrieval.read_door_text(root, probe.locator)
        if probe.kind == "DEFECT_PRESENT":
            # A missing or unconfinable file refutes "there is a defect here".
            text = tree_retrieval.read_door_text(root, probe.locator)
            return "" if text is None else text
        if probe.kind == "LINT_PRESENT":
            # Unconfinable ⇒ the lint check could not run ⇒ fail open (``None``).
            if not tree_retrieval.is_confined_locator(probe.locator):
                return None
            return self._run_lint_probe(probe.expectation, probe.locator)
        return tree_retrieval.read_door_corpus(root)

    def _load_generation_inventory(self) -> tuple[RealityInventory, str] | None:
        """Return the reality inventory and door corpus — the injectable FS seam.

        Proposal 136's guards clear declarations against both; the corpus is
        rooted at the work-target ``repo_workspace``. An ``OSError`` returns
        ``None`` so a guard fails open rather than refusing on an unrun check.
        """
        try:
            inventory = load_reality_inventory(self._config, self._memory)
            corpus = tree_retrieval.read_door_corpus(
                Path(self._config.work_target().repo_workspace),
            )
        except OSError as exc:
            logger.info("Reality inventory read failed (%s) — failing open", exc)
            return None
        return inventory, corpus

    def _validate_source_task_id(self, raw: object) -> str | None:
        """Accept ``source_task_id`` only when it points at a kept task branch.

        The LLM may invent IDs or echo a stale one. Three checks must hold:
        (a) it matches a recent ``TaskRecord.task_id``, (b) that record's
        outcome is ``"success"`` or ``"partial"``, (c) ``record.branch`` is
        non-empty. Otherwise drop and log so the implementation phase
        falls back to fresh-from-HEAD.
        """
        if raw is None:
            return None
        if not isinstance(raw, str) or not raw.strip():
            return None
        candidate = raw.strip()
        recent = recent_planning_tasks(self._memory, self._config)
        # Iterate newest-first so duplicate ids resolve to the same record
        # that ``Memory.get_task_record`` would return (latest retry wins).
        for record in reversed(recent):
            if record.task_id != candidate:
                continue
            if record.outcome not in ("success", "partial"):
                logger.warning(
                    "Plan proposed with invalid source_task_id %r "
                    "(outcome=%r) — falling back to fresh-from-HEAD",
                    candidate, record.outcome,
                )
                return None
            if not record.branch:
                logger.warning(
                    "Plan proposed with invalid source_task_id %r "
                    "(no branch on record) — falling back to fresh-from-HEAD",
                    candidate,
                )
                return None
            return candidate
        logger.warning(
            "Plan proposed with invalid source_task_id %r "
            "(no matching recent task) — falling back to fresh-from-HEAD",
            candidate,
        )
        return None

    def _compose_review_message(
        self, plan: EvolutionPlan, heading: str = "New Evolution Plan",
    ) -> str:
        return compose_review_message(plan, heading)

    async def submit_for_review(
        self, plan: EvolutionPlan, *, self_initiated: bool = False,
        exempt_saturation: bool = False,
    ) -> None:
        """Save plan and notify Thomas — see :func:`evolution_submission.submit_for_review`."""
        return await evolution_submission.submit_for_review(
            self, plan, self_initiated=self_initiated,
            exempt_saturation=exempt_saturation,
        )

    async def promote_slice(self, slice_plan: EvolutionPlan) -> None:
        """Promote a held ``"sliced"`` slice to ``pending_review`` for review.

        The one-slice-open sequencing driver (proposal 118) calls this once a
        slice's predecessor PR has settled. The slice was already persisted
        dormant by chain emission, so promotion is a status flip — not a
        re-:meth:`~maestro.memory.Memory.save_evolution_plan` (which would raise
        :class:`DuplicatePlanError`). Re-reads the plan and no-ops unless it is
        still ``"sliced"``, so a double-invocation is idempotent. Dedup is
        intentionally *not* re-run — the slice was authored as part of a vetted
        chain and Thomas still approves it per-slice — so this reuses only
        :meth:`submit_for_review`'s happy-path tail (content-hash snapshot →
        journal note → review notification), never its guard stack.
        """
        current = self._memory.get_plan(slice_plan.plan_id)
        if current is None or current.status != "sliced":
            logger.info(
                "Slice %r is not dormant (status=%r) — skipping promotion",
                slice_plan.plan_id,
                current.status if current is not None else None,
            )
            return
        # Stamp-before-send (proposal 021), only while still sliced (#4473).
        if not self._memory.update_plan_status(
            current.plan_id, "pending_review",
            notified_content_hash=current.plan_content_hash,
            expect_status="sliced",
        ):
            logger.info("Slice %r left 'sliced' mid-promotion", current.plan_id)
            return
        self._journal.record_event(
            content=(
                f"Promoting evolution slice: {current.title}\n"
                f"Phase: {current.phase_index}\n"
                f"Category: {current.category}\n"
                f"Rationale: {current.rationale}\n"
                f"Its predecessor slice has settled — opening it for review."
            ),
            entry_type="evolution_note",
            triggered_by=f"slice_promotion:{current.plan_id}",
        )
        try:
            msg = self._compose_review_message(current)
            await self._notifier.notify_plan_for_review(
                current.plan_id, msg, current.plan_content_hash,
            )
        except Exception:
            logger.warning("Notification failed", exc_info=True)

    async def propose_plan_chain(
        self,
        intent: str,
        session: Session | None = None,
        *,
        tree_reads: bool = False,
        gap_pinned: bool = False,
    ) -> EvolutionPlan | tuple[EvolutionProposal, list[EvolutionPlan]]:
        """Propose a self-improvement as one plan or a multi-PR slice chain.

        The chain-emission arm of proposal 118. Mirrors :meth:`propose_plan`'s
        dual call path (``query_on_session`` when a *session* is provided, else
        ``query`` with a JSON schema, both ``complete_on=json_complete``) and its
        :class:`ProposalParseError` contract. The schema lets the model either
        return a single plan (``multi_pr`` false — the same reviewable fields
        ``propose_plan`` emits) when the intent fits one focused PR, or set
        ``multi_pr`` true and return proposal-level ``proposal_title`` /
        ``proposal_goal`` plus an ordered ``slices`` array (2..
        ``_MAX_PROPOSAL_SLICES``), each slice carrying the same reviewable fields
        as a plan.

        Returns a lone :class:`EvolutionPlan` for a single-PR intent (or a
        ``multi_pr`` reply carrying fewer than two valid slices, which collapses
        to the single-plan path), or an ``(EvolutionProposal, list[EvolutionPlan])``
        tuple: the proposal minted with a fresh id and ``status="rolling_out"``,
        and N slice plans linked by ``parent_plan_id`` / ``phase_index=0..N-1``,
        with phase 0 left ``"pending_review"`` and phases 1..N-1 held ``"sliced"``.
        Field normalisation is reused from :meth:`_plan_from_spec` rather than
        duplicated.

        When *session* is provided, ``effort`` has no effect — the session-level
        effort configured at creation applies instead.

        Grounded planning (proposal 143): when *tree_reads* is true and the
        single-use arm is on, the call carries the bounded ``tree`` surface
        as :meth:`propose_plan` does, stamping ``grounded_reads`` on the lone
        plan or every slice. Otherwise byte-identical.

        *intent* is the caller's framing of what to plan (a gap block, or a
        scope-rejected plan to decompose). *gap_pinned* means the caller
        overwrites every slice's category and issue number (``pin_gap_plan``),
        so neither is asked for.
        """
        # Proposal 139: under the single-use flag this call ignores any passed
        # session and runs its stateless request through ``single_use_call``.
        if self._config.sdk_single_use_evolution_calls_enabled:
            session = None
        _note_session_effort_override("propose_plan_chain", "high", session)

        plan_field_props: dict[str, Any] = {
            "title": {"type": "string"},
            "category": {"type": "string"},
            "priority": {"type": "string"},
            "rationale": {"type": "string"},
            "proposed_changes": {"type": "array", "items": {"type": "string"}},
            "acceptance_criteria": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": ACCEPTANCE_CRITERIA_MAX_ITEMS,
            },
            "files_affected": {"type": "array", "items": {"type": "string"}},
            "expected_impact": {"type": "string"},
            "issue_number": {"type": ["integer", "null"]},
            "source_task_id": {"type": ["string", "null"]},
        }

        prompt = f"""Plan from this intent:
{intent}

Propose your next self-improvement. Most improvements fit one focused PR —
strongly prefer that. Only when the intent genuinely cannot land in a single
reviewable PR — it spans several independent changes that must land in sequence —
decompose it into an ordered chain of thin slices, each a standalone reviewable
PR that builds on its predecessor. A chain has 2 to {_MAX_PROPOSAL_SLICES} slices;
if you cannot justify at least two, it is one plan, not a chain.

{claude_md_guidance(self._config.require_claude_md_update)}

PLAN-SCOPE REQUIREMENT: Each proposed_changes entry MUST include a
verification verb (test, verify, check, validate, or confirm) for the
file it describes — the plan-scope gate rejects multi-file plans that
lack per-file verification language. For documentation entries, end with
"Verify the updated section accurately reflects the implementation."
For code entries, include how the change is tested or verified.

MINIMALISM: Prefer the smallest change that works. Before adding new
code, check whether the existing codebase, the standard library, or an
already-installed dependency already provides it — reuse over rebuild.
Do not introduce abstractions the task does not need. A plan may be
net-subtractive: removing a superseded module or consolidating duplicated
logic is a first-class evolution.

`acceptance_criteria` are 1-{ACCEPTANCE_CRITERIA_MAX_ITEMS} binding, testable invariants the implementation
MUST satisfy. Each must be checkable against either a specific code feature or
a specific test assertion. Bad: "improves performance." Good: "task_cost_gate
rule 3 counts both `failure` and `partial` outcomes in the failure-cost
numerator."

Reply with JSON. For a single-PR change, set "multi_pr" to false and fill the
top-level plan fields:
{{
  "multi_pr": false,
  "title": "Short title",
  "category": "capability" | "infrastructure",
  "priority": "task_followup" | "capability" | "resilience" | "experiment" | "maintenance",
  "rationale": "Why this change matters",
  "proposed_changes": ["Change 1 description", ...],
  "acceptance_criteria": ["Specific testable invariant 1", ...],
  "files_affected": ["maestro/file.py", ...],
  "expected_impact": "The change an observer outside the loop would see",
  "issue_number": null or integer,
  "source_task_id": null or "<task_id>"
}}

For a genuinely multi-PR change, set "multi_pr" to true and return
proposal-level "proposal_title"/"proposal_goal" plus an ordered "slices" array
(2 to {_MAX_PROPOSAL_SLICES} entries, each carrying the same plan fields as
above, ordered so each slice builds on its predecessor):
{{
  "multi_pr": true,
  "proposal_title": "Short title for the whole initiative",
  "proposal_goal": "The shared goal the chain delivers",
  "slices": [
    {{"title": "Slice 1 title", "category": "...", "priority": "...",
      "rationale": "...", "proposed_changes": ["..."],
      "acceptance_criteria": ["..."], "files_affected": ["..."],
      "expected_impact": "...", "issue_number": null,
      "source_task_id": null}},
    ...
  ]
}}"""

        if gap_pinned:
            # The pin overwrites category and issue_number, so the reply
            # need not spend tokens on either.
            del plan_field_props["category"], plan_field_props["issue_number"]
            for field_text in (
                '  "category": "capability" | "infrastructure",\n',
                '  "issue_number": null or integer,\n',
                ' "category": "...",',
                ' "issue_number": null,',
            ):
                prompt = prompt.replace(field_text, "")

        tree_read = self._tree_read_if("propose_plan_chain", tree_reads)
        if tree_read is not None:
            prompt += _GROUNDED_PLANNING_CHAIN_PROMPT

        if session is not None:
            result = await query_on_session(
                session, prompt, caller="propose_plan_chain",
                complete_on=json_complete,
            )
        else:
            identity = self._consciousness.get_identity_prompt()
            output_format: dict[str, Any] = {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "multi_pr": {"type": "boolean"},
                        **plan_field_props,
                        "proposal_title": {"type": "string"},
                        "proposal_goal": {"type": "string"},
                        "slices": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": plan_field_props,
                            },
                        },
                    },
                    "required": ["multi_pr"],
                },
            }
            try:
                if self._config.sdk_single_use_evolution_calls_enabled:
                    result = await single_use_call(
                        "propose_plan_chain", prompt,
                        system=evolution_slot_system(identity),
                        output_format=output_format,
                        # A tool-bearing call streams to ``structured_output``.
                        complete_on=json_complete if tree_read is None else None,
                        options=None if tree_read is None else tree_read[0],
                    )
                else:
                    result = await query(
                        prompt=prompt,
                        system=identity,
                        options=QueryOptions(
                            effort="high",
                            caller="propose_plan_chain",
                            output_format=output_format,
                        ),
                        complete_on=json_complete,
                        phase="evolution",
                    )
            except SDKError as e:
                parse_error = _structured_output_as_parse_error(e, "propose_plan_chain")
                if parse_error is None:
                    raise
                raise parse_error from e
        try:
            data = result.parse_json()
        except json.JSONDecodeError as e:
            logger.warning(
                "Evolution chain LLM returned invalid JSON — JSONDecodeError: %s",
                e,
            )
            raise ProposalParseError(
                f"propose_plan_chain: LLM response was not valid JSON ({e})",
            ) from e

        if not isinstance(data, dict):
            logger.warning(
                "Evolution chain LLM returned a %s, expected a dict — wrapping",
                type(data).__name__,
            )
            data = {
                "proposed_changes": data if isinstance(data, list) else None,
            }

        raw_slices = data.get("slices")
        slice_specs: list[dict[str, Any]] = (
            [s for s in raw_slices if isinstance(s, dict)][:_MAX_PROPOSAL_SLICES]
            if isinstance(raw_slices, list)
            else []
        )
        is_chain = bool(data.get("multi_pr")) and len(slice_specs) >= 2

        if not is_chain:
            # Single-PR intent, or a multi-PR reply carrying fewer than two valid
            # slices — collapse to the lone-plan path. Prefer the sole slice's
            # fields when present, else the top-level fields, then reuse the same
            # explicit-null proposed_changes fallback ``propose_plan`` applies.
            single_spec = slice_specs[0] if slice_specs else data
            if single_spec.get("proposed_changes") is None:
                single_spec["proposed_changes"] = [result.response[:300]]
            plan = self._plan_from_spec(
                single_spec,
                default_rationale=intent[:300],
            )
            if tree_read is not None:
                plan.grounded_reads = tree_read[1].read_paths()
            return plan

        proposal_goal = str(
            data.get("proposal_goal") or data.get("rationale") or intent[:300],
        )[:_PLAN_RATIONALE_MAX_CHARS]
        proposal = EvolutionProposal(
            proposal_id=uuid.uuid4().hex[:12],
            title=str(
                data.get("proposal_title")
                or data.get("title")
                or "Multi-PR evolution proposal",
            ),
            goal=proposal_goal,
            status="rolling_out",
            created_at=datetime.now(UTC).isoformat(),
        )
        slice_plans = [
            self._plan_from_spec(
                spec,
                default_rationale=proposal_goal,
                status="pending_review" if i == 0 else "sliced",
                parent_plan_id=proposal.proposal_id,
                phase_index=i,
            )
            for i, spec in enumerate(slice_specs)
        ]
        if tree_read is not None:
            for slice_plan in slice_plans:
                slice_plan.grounded_reads = tree_read[1].read_paths()
        return proposal, slice_plans

    async def submit_proposal_chain(
        self,
        proposal: EvolutionProposal,
        slices: list[EvolutionPlan],
        exempt_saturation: bool = False,
    ) -> bool:
        """Persist a minted multi-PR chain and submit its head slice (proposal 118).

        Submits the head (``phase_index == 0``) slice through the existing
        :meth:`submit_for_review` *first* (standard notify + content-hash
        binding), then — only once the head is confirmed live — writes the
        owning :class:`EvolutionProposal`, persists every downstream
        (``phase_index > 0``) slice dormant via ``save_evolution_plan`` (they
        carry ``"sliced"`` status), and journals one note that a chain of N
        slices was minted.

        Head acceptance is read back from persisted truth, not inferred from
        ``submit_for_review`` (which returns ``None`` whether it accepted or
        refused the plan): the head is live iff the owner accessor
        ``get_pending_plans`` still lists it. Any of ``submit_for_review``'s
        guards — the pending-plan saturation block, the exact-title / restatement
        dedup, the shipped-CLI guard — can refuse the head, leaving it unsaved or
        ``obsolete`` (absent from that set either way). Persisting the proposal
        and dormant slices before that check would strand a permanently headless
        ``"rolling_out"`` chain (its phase-0 slice missing or obsolete) with no
        cleanup path, so on a refused head this persists nothing and journals an
        honest refusal instead of a false "submitted the head slice" note.
        Idempotent on retry: a duplicate proposal id or slice id is skipped
        rather than double-written, leaning on ``write_evolution_proposal``'s /
        ``save_evolution_plan``'s id guards.

        Returns ``True`` iff the head was accepted and the chain persisted, and
        ``False`` on a refused/absent head (nothing persisted). Callers that
        supersede an originating plan on the strength of the chain (e.g.
        ``MaestroAgent._slice_scope_rejected_plan``) MUST gate that disposal on a
        ``True`` return, so a plan is never dropped for a chain that was never
        minted.
        """
        head = next((p for p in slices if p.phase_index == 0), None)
        if head is not None:
            # Self-initiated: this chain head was minted by the evolve() tail
            # already cleared by the pre-mint generation authority, so it skips
            # the admission gate's saturation arm (proposal 120, step 6); the
            # other submission guards may still refuse the head, which the
            # re-read below handles. Set literally here rather than threading
            # a parameter through ``submit_proposal_chain``'s own signature.
            await self.submit_for_review(
                head, self_initiated=True,
                exempt_saturation=exempt_saturation,
            )
        # Re-read persisted truth rather than trust submit_for_review's return
        # (it returns None on both accept and refuse): the head is live only if
        # no submission guard refused it. Ask the owner accessor "which plans are
        # pending_review?" instead of open-coding a status comparison — a refused
        # head is absent from that set (left unsaved or obsolete by the guard).
        # Only then is it safe to mint the rest of the chain; otherwise we would
        # strand a headless ``"rolling_out"`` proposal.
        pending_ids = {p.plan_id for p in self._memory.get_pending_plans()}
        if head is None or head.plan_id not in pending_ids:
            self._journal.record_event(
                content=(
                    f"Did not mint multi-PR evolution proposal "
                    f"'{proposal.title}' ({proposal.proposal_id}): its head "
                    "slice was not accepted for review (refused by a submission "
                    "guard, or absent), so no proposal or dormant slices were "
                    "persisted — no headless chain left behind."
                ),
                entry_type="evolution_note",
                triggered_by=f"proposal_chain_refused:{proposal.proposal_id}",
            )
            return False
        try:
            self._memory.write_evolution_proposal(proposal)
        except ValueError:
            logger.info(
                "Idempotent retry: evolution proposal %r already written — "
                "skipping", proposal.proposal_id,
            )
        for plan in sorted(slices, key=lambda p: p.phase_index):
            if plan.phase_index == 0:
                continue
            try:
                self._memory.save_evolution_plan(plan)
            except DuplicatePlanError:
                logger.info(
                    "Idempotent retry: chain slice %r already saved — skipping",
                    plan.plan_id,
                )
        self._journal.record_event(
            content=(
                f"Minted multi-PR evolution proposal '{proposal.title}' "
                f"({proposal.proposal_id}) as a chain of {len(slices)} slices "
                f"sharing one goal; submitted the head slice for review and "
                f"held {max(len(slices) - 1, 0)} downstream slices dormant."
            ),
            entry_type="evolution_note",
            triggered_by=f"proposal_chain:{proposal.proposal_id}",
        )
        return True

    async def curate_pending_plans(
        self,
        session: Session | None = None,
        *,
        tree_reads: bool = False,
    ) -> bool:
        """Refine or prune the oldest unrefined ``pending_review`` plan.

        The ``curate_pending_plans`` phase (proposal 060) supplies the first
        production caller of :meth:`Memory.refine_plan`. It picks the oldest
        unrefined pending plan via :func:`select_curation_target`, asks the
        model to either *sharpen* its body, recommend it *obsolete*, or
        *keep* it untouched, then applies the verdict through the already-
        validated write boundary (:meth:`Memory.refine_plan` /
        :meth:`Memory.mark_plan_obsolete`). A successful refine journals an
        ``evolution_note`` and alerts Thomas via
        :meth:`Notifier.notify_plan_refined` (best-effort, mirroring
        :meth:`submit_for_review`); ``obsolete`` and ``keep`` send no
        refined-notification.

        Returns ``True`` when a plan was refined or marked obsolete; ``False``
        on a no-op (no pending plan, ``keep``, or a malformed response — bad
        JSON, non-object, unknown ``action``, or no usable fields — logged and
        skipped unmutated). An over-cap refine field is skipped with a WARNING
        (#3339); an empty or wrong-typed one surfaces loudly via the write
        boundary (see :meth:`_apply_curate_refine`).

        *tree_reads* grants the bounded ``tree`` surface (proposal 143).
        """
        # Proposal 139: under the single-use flag this call ignores any passed
        # session and runs its stateless request through ``single_use_call``.
        if self._config.sdk_single_use_evolution_calls_enabled:
            session = None
        _note_session_effort_override("curate_pending_plans", "high", session)
        target = select_curation_target(
            self._memory.get_all_plans(),
            now=datetime.now(UTC),
            min_age_hours=self._config.loop_curate_min_plan_age_hours,
            prioritize_feedback=self._config.loop_plan_feedback_enabled,
            loop_curate_skip_serviced_enabled=(
                self._config.loop_curate_skip_serviced_enabled
            ),
        )
        if target is None:
            return False

        prompt = render_curate_prompt(target)
        rendered = (
            unaddressed_feedback(target)
            if self._config.loop_plan_feedback_enabled else []
        )
        if rendered:
            prompt += render_feedback_section(target, rendered)

        if session is not None:
            result = await query_on_session(
                session, prompt, caller="curate_pending_plans",
                complete_on=json_complete,
            )
        else:
            identity = self._consciousness.get_identity_prompt()
            tree_read = self._tree_read_if("curate_pending_plans", tree_reads)
            if tree_read is not None:
                result = await single_use_call(
                    "curate_pending_plans", prompt + _GROUNDED_CURATE_PROMPT,
                    system=evolution_slot_system(identity),
                    complete_on=None,
                    options=tree_read[0],
                )
                logger.info(
                    "Grounded planning: curate read %d path(s)",
                    len(tree_read[1].read_paths()),
                )
            elif self._config.sdk_single_use_evolution_calls_enabled:
                result = await single_use_call(
                    "curate_pending_plans", prompt,
                    system=evolution_slot_system(identity),
                    complete_on=json_complete,
                )
            else:
                result = await query(
                    prompt=prompt,
                    system=identity,
                    options=QueryOptions(
                        effort="high", caller="curate_pending_plans",
                    ),
                    complete_on=json_complete,
                )

        if rendered:
            return await curate_with_feedback(self, target, rendered, result)
        try:
            data = result.parse_json()
        except json.JSONDecodeError as e:
            logger.warning(
                "curate_pending_plans: LLM response was not valid JSON for "
                "plan %s — skipping (%s)",
                target.plan_id, e,
            )
            self._memory.touch_plan_curated(target.plan_id)
            return False
        if not isinstance(data, dict):
            logger.warning(
                "curate_pending_plans: expected a JSON object for plan %s, "
                "got %s — skipping",
                target.plan_id, type(data).__name__,
            )
            self._memory.touch_plan_curated(target.plan_id)
            return False

        action = data.get("action")
        if action == "obsolete":
            return self._apply_curate_obsolete(target, data)
        if action == "refine":
            return await self._apply_curate_refine(target, data)
        if action == "keep":
            # Kept plans stay pending_review; advance the round-robin cursor so
            # the next cadence services a different plan instead of re-picking
            # this one forever (#2885).
            self._memory.touch_plan_curated(target.plan_id)
            return False
        logger.warning(
            "curate_pending_plans: unrecognised action %r for plan %s — "
            "skipping",
            action, target.plan_id,
        )
        self._memory.touch_plan_curated(target.plan_id)
        return False

    def _apply_curate_obsolete(
        self,
        target: EvolutionPlan,
        data: dict[str, Any],
    ) -> bool:
        """Apply an ``obsolete`` curate verdict — self-prune, no notification.

        Returns ``True`` when the plan was transitioned to ``obsolete``;
        ``False`` when the verdict lacked a usable reason or the plan had
        already left ``pending_review``.
        """
        reason = data.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            logger.warning(
                "curate_pending_plans: obsolete verdict missing reason for "
                "plan %s — skipping",
                target.plan_id,
            )
            # Deterministic skip that leaves the plan pending_review; advance
            # the round-robin cursor so it does not starve the backlog (#2885).
            self._memory.touch_plan_curated(target.plan_id)
            return False
        if len(reason) > _OBSOLETE_REASON_MAX_CHARS:
            logger.warning(
                "curate_pending_plans: obsolete reason for plan %s is %d chars "
                "(max %d) — skipping, leaving plan pending_review",
                target.plan_id, len(reason), _OBSOLETE_REASON_MAX_CHARS,
            )
            self._memory.touch_plan_curated(target.plan_id)
            return False
        if not self._memory.mark_plan_obsolete(target.plan_id, reason):
            logger.warning(
                "curate_pending_plans: plan %s no longer pending_review — "
                "skipping obsolete",
                target.plan_id,
            )
            return False
        self._journal.record_event(
            content=(
                f"Pruning stale pending plan: {target.title}\n"
                f"Reason: {reason}"
            ),
            entry_type="evolution_note",
            triggered_by=f"curate:{target.plan_id}",
        )
        return True

    async def _apply_curate_refine(
        self,
        target: EvolutionPlan,
        data: dict[str, Any],
        *,
        response_notice: str = "",
    ) -> bool:
        """Apply a ``refine`` curate verdict — rewrite changed fields + notify.

        Only the fields the model actually returned are forwarded to
        :meth:`Memory.refine_plan`, so a partial verdict updates exactly the
        changed fields. A field returned as JSON ``null`` is treated the same
        as an omitted field — models commonly emit unchanged fields as
        ``null`` rather than dropping the key, and the prompt's "omit a field
        to leave it as-is" intent maps cleanly onto that. A verdict carrying
        none of the four writable fields (after dropping ``null`` ones) is
        treated as a no-op. An over-cap rewrite field (rationale over
        ``_PLAN_RATIONALE_MAX_CHARS``, a ``proposed_changes`` item over
        ``_PLAN_CHANGE_ITEM_MAX_CHARS``, an ``acceptance_criteria`` item
        over ``ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS``, a ``files_affected``
        item over ``_PLAN_FILE_PATH_MAX_CHARS``, or an over-count
        ``acceptance_criteria`` / ``files_affected`` list) is
        skipped with a WARNING — mirroring the obsolete sibling — rather than
        crashing the loop (#3339). A field that is empty/whitespace or
        wrong-typed *in isolation* still surfaces loudly via ``refine_plan``'s
        validators; but when such a malformed field co-occurs with an over-cap
        field in the same verdict, the earlier over-cap skip short-circuits
        first and swallows it too — still safe (the plan is left unmutated and
        a WARNING is logged), just not raised. Returns ``True`` when the plan
        was refined, even if a racing decision then suppresses the notify.
        """
        changed: dict[str, Any] = {
            field_name: data[field_name]
            for field_name in (
                "rationale", "proposed_changes", "acceptance_criteria",
                "files_affected",
            )
            if field_name in data and data[field_name] is not None
        }
        if not changed:
            logger.warning(
                "curate_pending_plans: refine verdict carried no writable "
                "fields for plan %s — skipping",
                target.plan_id,
            )
            # Deterministic skip that leaves the plan pending_review; advance
            # the round-robin cursor so it does not starve the backlog (#2885).
            self._memory.touch_plan_curated(target.plan_id)
            return False
        # An over-cap, blank-path or over-count field is model-output
        # jitter, not an invariant violation — skip with a WARNING and advance
        # the round-robin cursor rather than let refine_plan's raise crash the
        # loop (#3339). Wrong-typed or empty fields fall through to
        # refine_plan's own validators unchanged.
        violation = refine_cap_violation(changed)
        if violation is not None:
            logger.warning(
                "curate_pending_plans: refine for plan %s: %s — skipping, "
                "leaving plan pending_review",
                target.plan_id, violation,
            )
            self._memory.touch_plan_curated(target.plan_id)
            return False
        if not self._memory.refine_plan(target.plan_id, **changed):
            logger.warning(
                "curate_pending_plans: plan %s no longer pending_review — "
                "skipping refine",
                target.plan_id,
            )
            return False
        self._journal.record_event(
            content=(
                f"Refining pending plan: {target.title}\n"
                f"Updated fields: {', '.join(sorted(changed))}"
            ),
            entry_type="evolution_note",
            triggered_by=f"curate:{target.plan_id}",
        )
        # Snapshot the post-refine hash as notified (proposal 021).
        refreshed = self._memory.get_plan(target.plan_id)
        if refreshed is None:
            # No snapshot to bind buttons to — send none rather than
            # controls that approve unseen content (#4146).
            return True
        # Never undo a racing human decision (#4473).
        h = refreshed.plan_content_hash
        if not self._memory.update_plan_status(
            target.plan_id, "pending_review", notified_content_hash=h,
            expect_status="pending_review", expect_content_hash=h,
        ):
            logger.info("Plan %s decided mid-refine", target.plan_id)
            return True
        if response_notice:
            await notify_feedback_response(self._notifier, response_notice)
        try:
            # Render the refined body itself so the buttons approve
            # content that was actually shown.
            msg = (
                f"Refined — updated fields: {', '.join(sorted(changed))}\n\n"
                f"{self._compose_review_message(refreshed, 'Refined Evolution Plan')}"
            )
            await self._notifier.notify_plan_refined(
                target.plan_id, msg, refreshed.plan_content_hash,
            )
        except Exception:
            logger.warning("Refined-plan notification failed", exc_info=True)
        return True

    async def implement(
        self,
        plan: EvolutionPlan,
        session: Session | None = None,
    ) -> str:
        """Write code changes on a new branch.

        Called for ``approved`` plans (first attempt) and ``failed`` plans
        with ``failure_count < max_implementation_attempts`` (retry). The
        caller (``_implement_approved_plans``) is responsible for selecting
        eligible plans; this guard is a defence-in-depth check against being
        invoked on a plan that has been rejected, withdrawn, or already
        implemented.

        Returns the base commit hash (before implementation) so the caller
        can pass it to :meth:`self_review` for diff generation.
        """
        if plan.status not in ("approved", "failed"):
            raise RuntimeError(
                f"Cannot implement plan with status={plan.status!r}",
            )

        prompt = f"""You are implementing an approved evolution plan.

Plan: {plan.title}
Rationale: {plan.rationale}
Changes to make:
{chr(10).join(f"- {c}" for c in plan.proposed_changes)}

Files to modify: {plan.files_affected}

Implement these changes carefully. You are modifying your own code —
be precise, test-aware, and thoughtful about side effects.

{claude_md_guidance(self._config.require_claude_md_update)}"""

        branch = evolution_branch(plan.plan_id)

        # Force HEAD onto main before computing the base ref. There is no use
        # case for cutting an evolution branch from a sibling plan's HEAD —
        # historically the existing ``_orig_branch == branch`` fall-back was a
        # tell that the design already mistrusted ambient state. Pair with the
        # ``else: checkout("main")`` below so every code path leading to
        # ``checkout_new_branch`` lands on a known ref first.
        # Read the tree *before* positioning: ``ensure_main_branch``'s non-main
        # arm calls ``discard_changes()``, so probing after it would destroy the
        # very work the refusal exists to protect (#3711).
        self.assert_clean_tree_for_mutation()
        self.ensure_main_branch()
        # Positioning alone is not enough either: the branch fact is asserted
        # after it, so a HEAD that could not be moved onto main refuses before
        # any ``checkout_new_branch`` and before the SDK call below.
        self.assert_on_main_for_mutation()
        self._orig_branch = "main"

        base_ref = "main"
        fallback_reason: str | None = None

        # Proposal 131 RESUME arm: when the flag is on and the plan carries valid
        # resume context — a prior strictly-progressing stall preserved this
        # branch at ``resume_branch_head`` (see ``implement_plan``) — continue
        # from that preserved branch with memory instead of the destructive
        # recreate below. Merge origin/main into it so the gates start from a
        # current base, and fall back to the fresh-branch build on any conflict /
        # VCS error, exactly as the ``source_task_id`` base path does. Guarded on
        # the branch still resolving to the recorded HEAD, so a pruned or moved
        # branch is never silently resumed onto stale state.
        resume_active = False
        if (
            self._config.attempt_disposition_enabled
            and plan.resume_count > 0
            and plan.resume_failing_identities
            and plan.resume_branch_head is not None
            and self._vcs.rev_parse(f"refs/heads/{branch}")
            == plan.resume_branch_head
        ):
            try:
                self._vcs.checkout(branch)
                self._vcs.fetch("origin")
                merge = self._vcs.merge_from(
                    "origin/main",
                    message=(
                        "maestro: merge main into evolution branch before resume"
                    ),
                )
                if merge.success:
                    resume_active = True
                else:
                    with contextlib.suppress(VCSError):
                        self._vcs.abort_merge()
                    with contextlib.suppress(VCSError):
                        self._vcs.checkout(self._orig_branch)
                    logger.warning(
                        "Evolution plan %s: RESUME merge of origin/main into %s "
                        "conflicted; rebuilding from a fresh branch",
                        plan.plan_id, branch,
                    )
            except VCSError as exc:
                with contextlib.suppress(VCSError):
                    self._vcs.abort_merge()
                with contextlib.suppress(VCSError):
                    self._vcs.checkout(self._orig_branch)
                logger.warning(
                    "Evolution plan %s: RESUME rebase failed (%s); rebuilding "
                    "from a fresh branch", plan.plan_id, exc,
                )

        if resume_active:
            # Continue from the preserved branch: name the prior attempt's still-
            # failing identities and tell the writer to build on the existing
            # commits rather than restart.
            prompt = _resume_preamble(plan.resume_failing_identities) + prompt
        else:
            # Resolve the base for the new evolution branch. When the plan
            # nominates a kept task branch via source_task_id, base on that
            # branch (after merging origin/main into it so quality gates start
            # from a clean tree). Demote to fresh-from-orig_branch if the task
            # branch is missing or the merge conflicts.
            if self._config.task_branch_reuse_enabled and plan.source_task_id:
                record = self._memory.get_task_record(plan.source_task_id)
                task_branch_name = planning_base_branch(record, self._config)
                if (
                    task_branch_name
                    and self._vcs.rev_parse(f"refs/heads/{task_branch_name}")
                ):
                    try:
                        self._vcs.checkout(task_branch_name)
                        self._vcs.fetch("origin")
                        merge = self._vcs.merge_from(
                            "origin/main",
                            message=(
                                "maestro: merge main into task branch before "
                                "evolution rebase"
                            ),
                        )
                        if merge.success:
                            base_ref = task_branch_name
                        else:
                            with contextlib.suppress(VCSError):
                                self._vcs.abort_merge()
                            with contextlib.suppress(VCSError):
                                self._vcs.checkout(self._orig_branch)
                            fallback_reason = "merge conflict with origin/main"
                    except VCSError as exc:
                        with contextlib.suppress(VCSError):
                            self._vcs.abort_merge()
                        with contextlib.suppress(VCSError):
                            self._vcs.checkout(self._orig_branch)
                        fallback_reason = (
                            f"vcs error during task branch rebase: {exc}"
                        )
                else:
                    fallback_reason = "task branch missing"

            # Position HEAD on base_ref before creating the evolution branch.
            # The else branch is defence-in-depth alongside ``ensure_main_branch``
            # at entry: a future change that drops the entry guard cannot silently
            # cut the new branch from a sibling plan's HEAD.
            if base_ref != "main":
                self._vcs.checkout(base_ref)
            else:
                self._vcs.checkout("main")

            # Create branch (delete stale branch from prior failed attempt if needed)
            try:
                self._vcs.checkout_new_branch(branch)
            except VCSError:
                # Switch away from stale branch before deleting it
                with contextlib.suppress(VCSError):
                    self._vcs.checkout(self._orig_branch)
                with contextlib.suppress(VCSError):
                    self._vcs.delete_branch(branch)
                if base_ref != "main":
                    with contextlib.suppress(VCSError):
                        self._vcs.checkout(base_ref)
                self._vcs.checkout_new_branch(branch)

            if fallback_reason:
                logger.warning(
                    "Evolution plan %s: source_task_id rebase failed (%s); "
                    "using fresh branch from %s",
                    plan.plan_id, fallback_reason, self._orig_branch,
                )

        # Proposal 131 re-admission: a plan re-admitted after Thomas answered its
        # blocked question carries his decision in ``blocked_answer``. Prepend it
        # so the writer acts on that decision instead of restarting blind. The
        # ASK_HUMAN arm disposed of the evolution branch when it blocked, so the
        # resume guard above never matches a re-admitted plan — this preamble is
        # the only carried context. Flag-off, or with no ``blocked_answer``, the
        # prompt is byte-identical to today.
        if self._config.attempt_disposition_enabled and plan.blocked_answer:
            prompt = _blocked_answer_preamble(plan.blocked_answer) + prompt
            # One-shot consumption (proposal 131): clear the stamped answer once
            # it has been injected, so a later *retry* of this same plan does not
            # re-prepend Thomas's original decision onto an attempt unrelated to
            # the block it cleared — the answer bridges the block-and-answer round
            # trip once, not every future attempt. Persisted (and cleared on the
            # in-hand snapshot); ``blocked_answer`` is excluded from
            # ``compute_plan_content_hash``, so the approval binding is untouched,
            # and re-passing ``plan.status`` mutates only that side-channel field.
            self._memory.update_plan_status(
                plan.plan_id, plan.status, blocked_answer=None,
            )
            plan.blocked_answer = None

        try:
            # Capture the base commit for self-review's diff and (proposal 124)
            # attribution. The right base differs by path:
            #
            # * Fresh / source-task path: HEAD stands on the branch's own base —
            #   the just-cut branch carries no commits yet — so ``base_hash`` is
            #   that base and :meth:`_observe_baseline` can bank its gate result
            #   as the attribution baseline while HEAD still matches ``base_hash``
            #   by construction. A fresh-from-``main`` branch normally finds the
            #   tend lap's entry already cached; a ``source_task_id`` base is the
            #   case that actually pays for a run — off the event loop, since
            #   paying for it means a full gate sweep.
            # * Proposal-131 resume path: HEAD instead stands on the *preserved
            #   branch tip* (the prior attempt's commits, now with origin/main
            #   merged in). The tip is NOT the branch's base, so basing on it
            #   would (a) show self_review only the incremental resume commit
            #   rather than the whole accumulated plan diff, and (b) let
            #   :func:`~maestro.gate_observation.attribute` misread the prior
            #   attempt's own still-failing identities as ``INHERITED`` — they
            #   are a subset of the tip's red — routing a genuinely-progressing
            #   resume into the destructive INHERITED hold, the opposite of the
            #   resume intent. Base both on ``origin/main`` instead — the
            #   branch's true fork point after the merge above — so the diff
            #   spans the whole plan work and attribution only calls a red
            #   inherited when it truly predates the branch. ``_observe_baseline``
            #   only banks a baseline whose sha equals HEAD, so it is never run
            #   against the polluted tip here; attribution serves the cached
            #   ``origin/main`` observation the first (fresh) attempt warmed, or
            #   falls back to the safe ``UNATTRIBUTABLE`` when none exists.
            if resume_active:
                base_hash = self._vcs.rev_parse("origin/main") or ""
            else:
                base_hash = self._vcs.get_head_hash() or ""
                await asyncio.to_thread(self._observe_baseline, base_hash)

            # Surface the binding acceptance criteria to the writer so it aims
            # at the contract from the first commit rather than first meeting
            # it at self-review. Built after base_ref is resolved: when the
            # plan rebased onto a source-task branch, the pre-existing commits
            # were written before this plan's criteria existed, so a
            # reconciliation note is appended only on that path.
            if (
                self._config.criteria_alignment_enabled
                and plan.acceptance_criteria
            ):
                prompt += format_acceptance_criteria_block(
                    plan.acceptance_criteria, style="writing",
                )
                if base_ref != "main":
                    prompt += (
                        "\nThe existing commits on this base predate these"
                        " criteria and must be reconciled to them.\n"
                    )

            # Let Claude write the code
            if session is not None:
                await query_on_session(
                    session, prompt, caller="implement_plan",
                    stream_idle_timeout_seconds=(
                        self._config.sdk_tool_stream_idle_timeout_seconds
                    ),
                )
            else:
                identity = self._consciousness.get_identity_prompt()
                await query(
                    prompt=prompt,
                    system=identity,
                    options=QueryOptions(
                        allowed_tools=[
                            "Read", "Write", "Edit", "Bash", "Glob", "Grep",
                        ],
                        mcp_servers=self._mcp_servers,
                        disallowed_tools=(
                            self._config.external_mcp_disallowed_tools()
                        ),
                        caller="implement_plan",
                        cwd=str(self._config.workspace),
                        hooks=self.action_gate_hooks(plan),
                    ),
                    stream_idle_timeout_seconds=(
                        self._config.sdk_tool_stream_idle_timeout_seconds
                    ),
                    phase="implementation",
                )

            # Stage and commit. On the source-task path — and the proposal-131
            # resume path — refinement may add nothing, and we accept the
            # existing commits (a kept task branch's, or the prior resume
            # attempt's) as the change. The no-changes guard only fires on the
            # fresh-from-main path, where producing zero changes is a real
            # failure.
            self._vcs.stage_all()
            if self._vcs.has_changes():
                self._vcs.commit(f"maestro: {plan.title}\n\n{plan.rationale}")
            elif base_ref == "main" and not resume_active:
                raise RuntimeError("Query produced no file changes")

            return base_hash
        except Exception as exc:
            error_detail = exc.detail if isinstance(exc, SDKError) else str(exc)
            logger.error(
                "Failed to implement plan %s: %s", plan.plan_id, error_detail,
                exc_info=True,
            )
            with contextlib.suppress(VCSError):
                self._vcs.discard_changes()
            try:
                self._vcs.checkout(self._orig_branch)
            except VCSError:
                logger.warning("Failed to checkout %s during cleanup", self._orig_branch)
            try:
                self._vcs.delete_branch(branch)
            except VCSError:
                logger.warning("Failed to delete branch %s during cleanup", branch)
            raise

    def _review_terminal(
        self,
        *,
        approved: bool,
        made_changes: bool,
        rounds_used: int,
        ceiling: int,
        verify_grace_remaining: int,
    ) -> tuple[ConvergenceDecision, int]:
        """See :func:`evolution_quality._review_terminal`."""
        return evolution_quality._review_terminal(self, approved=approved, made_changes=made_changes, rounds_used=rounds_used, ceiling=ceiling, verify_grace_remaining=verify_grace_remaining)

    async def self_review(
        self,
        plan: EvolutionPlan,
        base_hash: str,
        session: Session | None = None,
    ) -> bool:
        """See :func:`evolution_quality.self_review`."""
        return await evolution_quality.self_review(self, plan, base_hash, session)
            # ``"continue"`` — the reviewer made progress (a committed fix, or a
            # grace-bought verify round against a green branch); run another.

    async def create_pr(
        self, plan: EvolutionPlan,
    ) -> str:
        """Push branch and create GitHub PR."""
        branch = evolution_branch(plan.plan_id)

        # Refuse to push a dirty tree: an uncommitted edit here is the plan's
        # own output and would silently miss the PR (#4067). Deliberately a
        # plain ``VCSError``, not ``DirtyWorkingTreeError`` — that type is the
        # pre-branch environment refusal, whereas this branch exists and must
        # take the ordinary plan-failure path.
        status = self._vcs.porcelain_status()
        if status.strip():
            logger.error(
                "Refusing to push %s — uncommitted changes:\n%s",
                branch, status[:_DIRTY_TREE_STATUS_CHARS],
            )
            raise VCSError(
                f"Refusing to push {branch}: the working tree has uncommitted "
                f"changes the PR would miss:\n{status[:_DIRTY_TREE_STATUS_CHARS]}"
            )

        try:
            self._vcs.push(branch, set_upstream=True)
        except VCSError as exc:
            # A non-fast-forward rejection means origin already carries a
            # branch at this ref. ``plan_id`` is a ``uuid4`` (``uuid.uuid4().
            # hex[:12]`` at plan construction), so the branch name is globally
            # unique to this one plan — the orphan can only be a PR-less
            # remnant of a *prior implementation attempt of this same plan*
            # that pushed the branch but never opened a PR (push succeeded,
            # ``create_pr`` did not); the retry recreated the branch fresh from
            # ``main``, so its history no longer descends from origin's
            # previously-pushed head and every push re-rejects (#2813).
            # Reconcile it once: delete the orphan (reversible disposal, scoped
            # to ``maestro/evolve/*``, analogous to the permitted ``gh pr
            # close`` — never a force-push) and re-push. Only when NO open PR
            # references the branch; a branch carrying a live PR is never
            # touched and the loud failure path is unchanged.
            if not _is_non_fast_forward_rejection(str(exc)):
                logger.exception("Failed to push branch %s", branch)
                raise
            # Positively confirm the branch is PR-less before any delete.
            # ``get_open_evolution_pr_states`` is best-effort by default and
            # returns ``{}`` on any ``gh`` failure (auth, rate-limit 403,
            # network, malformed JSON); treating that empty dict as proof of
            # 'no PR' would fail open — a transient host failure would then
            # delete a branch that actually carries a live PR, the exact
            # destructive outcome the guard must prevent (#2813). ``strict=True``
            # raises instead, so an unconfirmed state surfaces the original
            # non-fast-forward rejection rather than deleting blind.
            try:
                open_prs = self._host.get_open_evolution_pr_states(
                    "maestro/evolve/", strict=True,
                )
            except VCSError:
                logger.exception("Failed to push branch %s", branch)
                raise exc from None
            # Refresh remote-tracking refs from origin before the presence
            # check so it reads live state, not a stale last-fetch cache: a
            # workspace that never fetched the orphan would otherwise read it
            # as absent and skip the reconcile (#2813). If the refresh itself
            # fails we cannot trust the presence read, so surface the original
            # non-fast-forward rejection rather than delete blind.
            try:
                self._vcs.fetch()
            except VCSError:
                logger.exception("Failed to push branch %s", branch)
                raise exc from None
            remote_branches = self._vcs.list_remote_branches()
            if branch in open_prs or branch not in remote_branches:
                logger.exception("Failed to push branch %s", branch)
                raise
            logger.warning(
                "Push to %s rejected non-fast-forward; deleting PR-less orphan "
                "on origin and retrying push once (#2813)", branch,
            )
            self._vcs.delete_remote_branch(branch)
            self._vcs.push(branch, set_upstream=True)

        try:
            closes_line = (
                f"\n\n{self._issue_link_verb(plan)} #{plan.issue_number}"
                if plan.issue_number else ""
            )
            lineage_line = (
                f"\n\nBuilds on task {plan.source_task_id} "
                "(branch reused from prior self-directed work)."
                if self._config.task_branch_reuse_enabled and plan.source_task_id
                else ""
            )
            criteria_section = format_acceptance_criteria_block(
                plan.acceptance_criteria, style="plain",
            )
            body = (
                f"## Evolution Plan: {plan.title}\n\n"
                f"**Category:** {plan.category}\n"
                f"**Priority:** {plan.priority}\n\n"
                f"### Rationale\n{plan.rationale}\n\n"
                f"### Expected Impact\n{plan.expected_impact}\n\n"
                f"### Changes\n" +
                "\n".join(f"- {c}" for c in plan.proposed_changes) +
                criteria_section +
                closes_line +
                lineage_line
            )

            pr_url = self._host.create_pr(
                f"[Maestro] {plan.title}", body,
            )
        except VCSError:
            logger.exception(
                "Failed to create PR for plan %s; the plan will be demoted "
                "although branch %s may already be live on the host",
                plan.plan_id, evolution_branch(plan.plan_id),
            )
            raise

        self._memory.update_plan_status(
            plan.plan_id, "implemented", pr_url=pr_url, rejection_reason=None,
        )

        try:
            await self._notifier.notify(
                f"PR created for: {plan.title}\n{pr_url}"
            )
        except asyncio.CancelledError:
            logger.warning("Notification failed for PR creation", exc_info=True)

        # Switch back to the branch we were on before implementation. Do not
        # re-raise: the PR is already open and rolling back would mask success.
        # Stamp the failure so the next branch-bleed incident is visible in
        # telemetry instead of being silently absorbed (the original
        # ``contextlib.suppress(VCSError)`` here hid the bug behind #1867).
        try:
            self._vcs.checkout(self._orig_branch)
        except VCSError:
            try:
                current = self._vcs.get_current_branch()
            except VCSError:
                current = "<unknown>"
            logger.warning(
                "create_pr cleanup checkout to %s failed; HEAD now on %s",
                self._orig_branch, current, exc_info=True,
            )

        return pr_url

    def cleanup_branch(
        self, plan_id: str, exc: BaseException | None = None
    ) -> None:
        """Switch back to the original branch and delete the implementation branch.

        *exc*, when given, is the failure the caller is unwinding. A
        :class:`~maestro.vcs.DirtyWorkingTreeError` is the mutating-phase
        refusal raised by :meth:`assert_clean_tree_for_mutation` *before* any
        branch is cut, so there is nothing to clean up — and the
        ``discard_changes`` below would wipe the operator's uncommitted edit,
        the very thing that refusal exists to preserve (#3711). Return
        untouched in that case.
        """
        if isinstance(exc, DirtyWorkingTreeError):
            return
        self.release_branch(plan_id)
        with contextlib.suppress(VCSError):
            self._vcs.delete_branch(evolution_branch(plan_id))

    def release_branch(self, plan_id: str) -> None:
        """Move HEAD off the implementation branch without deleting it.

        Discards uncommitted edits and checks out the original branch (or
        ``main`` when the original *was* the evolution branch). The RESUME
        terminal keeps the branch for the next lap but must not leave the
        rest of the iteration reading its half-fixed tree (#4370); the
        committed head it resumes from survives the discard. A checkout that
        leaves HEAD on the evolution branch is logged at ERROR so a stranded
        HEAD is loud — the next iteration's ``ensure_main_branch`` remains
        the backstop.
        """
        branch = evolution_branch(plan_id)
        target = self._orig_branch if self._orig_branch != branch else "main"
        with contextlib.suppress(VCSError):
            self._vcs.discard_changes()
        with contextlib.suppress(VCSError):
            self._vcs.checkout(target)
        with contextlib.suppress(VCSError):
            if self._vcs.get_current_branch() == branch:
                logger.error(
                    "Plan %s: failed to release HEAD from '%s' to '%s'",
                    plan_id, branch, target,
                )

    def assert_clean_tree_for_mutation(self) -> None:
        """Refuse a mutating phase unless the working tree is clean.

        The unconditional ``stage_all`` in the implementation tail folds
        whatever the tree carries into the plan's commit, so a diff left by a
        crashed run or an operator's in-flight edit would ride onto the freshly
        cut evolution branch (#3711). It discards nothing — the edit is theirs,
        so the refusal is the fix — and because ``ensure_main_branch``'s
        non-main arm *does* discard, this must run before any positioning.

        A ``VCSError`` from the probe re-raises as the same refusal: an
        unreadable tree is indistinguishable from a dirty one here.
        """
        try:
            status = self._vcs.porcelain_status()
        except VCSError as exc:
            raise DirtyWorkingTreeError(
                f"Could not read the working tree: {exc}"
            ) from exc
        if status.strip():
            # Name the host condition at ERROR: the refusal is re-raised as an
            # environment fault, so without this the operator sees only a
            # traceback and has to guess what to clear (#3711).
            logger.error(
                "Refusing to cut an evolution branch — uncommitted changes:\n%s",
                status[:_DIRTY_TREE_STATUS_CHARS],
            )
            raise DirtyWorkingTreeError(
                "Refusing to cut an evolution branch: the working tree has "
                "uncommitted changes that would be folded into the plan's "
                f"commit:\n{status[:_DIRTY_TREE_STATUS_CHARS]}"
            )

    def assert_on_main_for_mutation(self) -> None:
        """Refuse a mutating phase unless HEAD is on ``main``.

        ``ensure_main_branch`` is best-effort *positioning* — it logs and
        returns on failure — so this asserts the fact it only attempts.
        Cutting an evolution branch from a sibling plan's HEAD is the
        isolation break the pairing exists for (#3711). As above, an
        unreadable branch is treated exactly like a wrong one.
        """
        try:
            current = self._vcs.get_current_branch()
        except VCSError as exc:
            raise DirtyWorkingTreeError(
                f"Could not establish a clean main working tree: {exc}"
            ) from exc
        if current != "main":
            logger.error(
                "Refusing to cut an evolution branch — HEAD is on '%s', "
                "not main", current,
            )
            raise DirtyWorkingTreeError(
                f"Refusing to cut an evolution branch from '{current}' — "
                "HEAD must be on main"
            )

    def ensure_main_branch(self) -> None:
        """Ensure the working tree is on the main branch.

        Called at the start of each loop iteration so that normal operations
        (contemplation, task generation, evolution proposals) never run on a
        stale evolution branch left over from a crash or suppressed error.
        """
        try:
            current = self._vcs.get_current_branch()
        except VCSError:
            logger.warning("Could not determine current branch", exc_info=True)
            return
        if current != "main":
            logger.warning(
                "Expected main branch but on '%s' — switching to main",
                current,
            )
            with contextlib.suppress(VCSError):
                self._vcs.discard_changes()
            try:
                self._vcs.checkout("main")
            except VCSError:
                logger.error(
                    "Failed to switch to main from '%s'", current,
                    exc_info=True,
                )

    def create_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> int | None:
        """Create a GitHub issue. Returns issue number, or None on failure."""
        return self._issues.create(title, body, labels=labels)

    def _issue_link_verb(self, plan: EvolutionPlan) -> str:
        """``Closes`` unless *plan* is a non-final slice of a chain.

        Every slice of a gap chain carries the gap's ``issue_number``; only
        the last slice may close it, so merging an early slice does not
        close the issue while its successors are still dormant or in review.
        """
        if plan.parent_plan_id is None:
            return "Closes"
        last = max(
            (p.phase_index for p in self._memory.get_all_plans()
             if p.parent_plan_id == plan.parent_plan_id),
            default=plan.phase_index,
        )
        return "Closes" if plan.phase_index >= last else "Refs"

    def is_open_gap_issue(self, number: int) -> bool:
        """Whether issue *number* is an open, trusted, door-shaped gap issue now.

        The door-shaped title (:func:`owner_id_of_title`) tells a gap pin
        apart from an ``issue_number`` a planner reply chose itself, and
        :func:`classify_gap_trust` (an operator label or a row the door
        wrote) keeps a door-titled issue anyone else opened from being
        pinned; closed, unreadable or untrusted fails closed.
        """
        meta = self._issues.meta(number)
        if (
            meta is None
            or meta.state != "open"
            or owner_id_of_title(meta.title) is None
        ):
            return False
        issue = Issue(
            number=number, title=meta.title, labels=list(meta.labels),
            body="", created_at="",
        )
        logins = effective_operator_logins(
            self._config, self.code_host.resolve_owner(),
        )
        store = build_gap_store(self._config)
        try:
            trust = classify_gap_trust(
                issue, tracker=self._issues, store=store, operator_logins=logins,
            )
        finally:
            store.close()
        return trust in ("operator", "rederived")

    def gap_issue_bypasses_gates(self, number: int) -> bool:
        """Whether open gap issue *number* is the red ``maestro:gate`` gap.

        Mirrors :attr:`EvolutionFuel.bypasses_gates` from the issue's
        door-shaped title, so a re-sliced red-gate plan keeps its
        saturation exemption; closed or unreadable fails closed.
        """
        meta = self._issues.meta(number)
        return (
            meta is not None
            and meta.state == "open"
            and owner_id_of_title(meta.title) == GATE_OWNER_ID
        )

    def close_linked_issue(self, plan: EvolutionPlan) -> None:
        """Close the GitHub issue linked to this plan, if any."""
        if plan.issue_number is not None:
            self._issues.close(plan.issue_number)

    async def learn_from_rejection(
        self,
        plan: EvolutionPlan,
        reason: str,
        session: Session | None = None,
        self_model_session: Session | None = None,
    ) -> None:
        """Record rejection as a journal event and update self-model.

        Idempotent: if a realization event for this exact ``plan.plan_id``
        already exists in the journal events, the call short-circuits.  Without
        this guard, a transient failure in :meth:`Consciousness.update_self_model`
        leaves the realization journalled but the ``rejection_learned``
        flag unset, and the next loop iteration writes a duplicate
        realization for the same rejection.

        When *session* is provided, the rejection-classifier SDK call is
        dispatched on the session so the identity prompt is cached and a
        single-cycle batch of rejections shares one transcript. The
        ``query()`` fallback is retained for tests and the
        ``enable_session_reuse=False`` configuration path.

        *self_model_session* is deliberately **separate** from *session*:
        the classifier session is opened at ``effort="low"`` while the
        self-model update's own one-shot arm requests ``effort="high"``, so
        reusing the classifier's session here would silently demote the
        update. When it is ``None`` the updater takes its documented bare
        ``query()`` arm, unchanged.

        The ``rejection_category`` is classified **once**, on this first
        (non-short-circuited) call. If the classifier raises a *transient*
        :class:`SDKError` (a category in
        :data:`~maestro.sdk.TRANSIENT_SDK_CATEGORIES`), the record is
        persisted with category ``"other"`` and — because the journal guard
        is written below, before the record — the category is **never
        re-classified** and cannot self-heal. This is intentional
        best-effort behavior: the category is an advisory signal consumed by
        the reality inventory's rejection categories, not a
        correctness-critical field.

        Any other classifier failure — a non-transient ``SDKError`` or a bare
        ``TimeoutError`` — propagates instead, before anything is journalled
        or persisted, so the whole call is retried rather than laundering an
        outage or a defect into a durable ``"other"`` record (#3795).
        """
        triggered = f"rejection:{plan.plan_id}"
        for existing in self._journal.read_events_by_type("realization"):
            if existing.triggered_by == triggered:
                logger.info(
                    "Rejection learning already journalled for plan %s — skipping",
                    plan.plan_id,
                )
                return

        # Classify the rejection — best-effort for a *transient* SDK failure
        # only (rate limit, stream stall, CLI crash, stale session): that is
        # swallowed so the structured record still persists with category
        # "other".  Every other failure propagates (see the handler below).
        rejection_category: RejectionCategory = "other"
        classifier_prompt = (
            f"Classify this rejection reason into one category.\n"
            f"Reason: {reason}\n\n"
            f"Categories: scope_too_large, wrong_approach, not_needed, "
            f"quality_issue, duplicate, other\n\n"
            f"Reply with just the category name."
        )
        classification: QueryResult | None = None
        try:
            if session is not None:
                _note_session_effort_override(
                    "rejection_classifier", "low", session,
                )
                classification = await query_on_session(
                    session,
                    f"You classify evolution plan rejection reasons.\n\n{classifier_prompt}",
                    caller="rejection_classifier",
                )
            else:
                classification = await query(
                    prompt=classifier_prompt,
                    system="You classify evolution plan rejection reasons.",
                    options=QueryOptions(
                        effort="low", caller="rejection_classifier",
                    ),
                )
        except SDKError as exc:
            # Swallow *only* a transient classifier call so the structured
            # record still persists with category "other".  Transience is
            # keyed on the error *category*, not the exception class: an
            # AUTH/UNKNOWN/NETWORK/TIMEOUT ``SDKError`` is unrecoverable
            # everywhere else in the loop, and softening it here would
            # convert an outage or a defect into durable domain data
            # (rejection learning runs once per plan and is never
            # re-derived).  A bare ``TimeoutError`` is likewise not caught
            # — it reaches a caller only from the SDK transport, which
            # ``maestro/sdk.py`` documents must propagate with its original
            # semantics.  Parsing happens below, outside this handler, so a
            # parser regression surfaces loudly instead of being masked as
            # "classifier failed" (Fail-loud doctrine, CLAUDE.md / #1404).
            if exc.category not in TRANSIENT_SDK_CATEGORIES:
                raise
            logger.warning(
                "Rejection classifier failed for plan %s; defaulting to 'other'",
                plan.plan_id, exc_info=True,
            )

        if classification is not None:
            tokens = (classification.response or "").strip().lower().split()
            for token in tokens:
                # Strip trailing punctuation (e.g. "scope_too_large.") so
                # a decorated answer still matches.
                stripped = token.strip(".,;:!?\"'`()[]{}")
                if stripped in VALID_REJECTION_CATEGORIES:
                    rejection_category = cast(RejectionCategory, stripped)
                    break

        # The realization is journalled only now, *after* the classifier has
        # either answered or been swallowed as transient.  It is the
        # idempotency guard for this whole method, so writing it earlier
        # would make a re-raised non-transient classifier error permanent:
        # the retry would short-circuit on the guard and the rejection would
        # end with no record and no self-model update at all — strictly more
        # loss than the "other" record the narrowed handler exists to
        # prevent (#3795).  It still precedes ``update_self_model``, whose
        # transient failure the guard was introduced to absorb.
        self._journal.record_event(
            content=(
                f"Evolution plan rejected: {plan.title}\n"
                f"Reason: {reason}\n"
                f"I need to learn from this. What was too aggressive, "
                f"misguided, or premature about this proposal?"
            ),
            entry_type="realization",
            triggered_by=triggered,
        )

        self._memory.record_rejection(RejectionRecord(
            plan_id=plan.plan_id,
            title=plan.title,
            category=plan.category,
            priority=plan.priority,
            rejection_reason=reason,
            rejection_category=rejection_category,
            timestamp=datetime.now(UTC).isoformat(),
        ))

        # Update self-model with this limitation
        reflection = (
            f"My evolution proposal '{plan.title}' was rejected: {reason}. "
            f"This tells me something about my limitations or misjudgments."
        )
        # ``self_model_session`` — not ``session``. The two carry different
        # effort policies (high for the self-model update, low for the
        # classifier), so collapsing them into one argument would silently
        # demote this call; passing ``None`` keeps the documented bare arm.
        await self._consciousness.update_self_model(
            reflection,
            session=self_model_session,
            restatement_concept_key_enabled=(
                self._config.loop_restatement_concept_key_enabled
            ),
            self_model_external_anchor_enabled=(
                self._config.self_model_external_anchor_enabled
            ),
        )

    # ------------------------------------------------------------------
    # PR helpers
    # ------------------------------------------------------------------

    def find_existing_pr(self, plan: EvolutionPlan) -> PrMergeState | None:
        """Return an open or merged PR already on ``plan``'s evolve branch.

        Raises :class:`VCSError` when the host lookup fails (#4553).
        """
        return self._host.find_evolution_pr_for_branch(
            evolution_branch(plan.plan_id),
        )

    @staticmethod
    def _pr_number_from_url(pr_url: str) -> int:
        """Extract PR number from a GitHub PR URL.

        Raises :class:`ValueError` if the URL does not contain a valid
        positive PR number.
        """
        if not isinstance(pr_url, str) or not pr_url.strip():
            raise ValueError(f"Invalid PR URL: {pr_url!r}")
        # Strip query strings and fragments before extracting the number.
        clean = pr_url.split("?")[0].split("#")[0]
        number = int(clean.rstrip("/").rsplit("/", 1)[-1])
        if number <= 0:
            raise ValueError(f"Invalid PR number: {number}")
        return number

    def check_merged_plans(
        self, *, settled_oracle: Callable[[int], MergeTruth],
    ) -> MergeCheckResult:
        """Detect implemented plans whose PRs have been merged or closed.

        For each plan with status ``"implemented"`` and a stored ``pr_url``,
        observes the PR's merge state **once** through the injected
        *settled_oracle* — the agent's provenance-stamped, fetch-first,
        per-iteration-memoized :meth:`~maestro.agent.MaestroAgent.is_pr_settled`
        merge-truth chokepoint (proposal 087) — and updates the plan status:

        - ``"merged"`` when :attr:`~maestro.pr_facts.MergeTruth.branch_landed`
          (the branch's commits are in ``main``)
        - ``"rejected"`` when :attr:`~maestro.pr_facts.MergeTruth.settled` and
          not ``branch_landed`` (the PR was closed without merging)
        - left ``"implemented"`` for a still-open PR

        The single observation collapses the two host round-trips this method
        used to make (``is_pr_merged`` then ``is_pr_closed``) into one memoized
        read that shares its verdict with every other PR-servicing face this
        iteration, so this decision no longer bypasses the merge-truth
        chokepoint.  The ``"merged"`` flip's side-effects (``VersionRecord``,
        ``evolution_count`` bump, milestone journal) are still owned by
        ``agent.py`` and driven off the returned :class:`MergeCheckResult`.

        Returns a :class:`MergeCheckResult` with merged and rejected plan IDs.
        """
        result = MergeCheckResult()
        for plan in self._memory.get_all_plans():
            if plan.status != "implemented" or not plan.pr_url:
                continue

            try:
                pr_number = self._pr_number_from_url(plan.pr_url)
            except ValueError:
                logger.warning(
                    "Could not parse PR number from %r for plan %s",
                    plan.pr_url, plan.plan_id,
                )
                continue
            try:
                truth = settled_oracle(pr_number)
                if truth.branch_landed:
                    self._memory.update_plan_status(plan.plan_id, "merged")
                    result.merged.append(plan.plan_id)
                    logger.info(
                        "Plan %s (%s) merged via PR %s",
                        plan.plan_id, plan.title, plan.pr_url,
                    )
                elif truth.settled:
                    reason = "PR closed without merging"
                    self._memory.update_plan_status(
                        plan.plan_id, "rejected",
                        rejection_reason=reason,
                    )
                    result.rejected.append((plan.plan_id, reason))
                    logger.info(
                        "Plan %s (%s) rejected — PR %s was closed without merging",
                        plan.plan_id, plan.title, plan.pr_url,
                    )
            except Exception:
                # Isolate the failure to this plan.  *settled_oracle* is the
                # fail-loud chokepoint (its ``git fetch`` re-raises so no
                # verdict is authored on unfreshened refs); absorbing here
                # preserves exactly that property — this arm writes nothing and
                # leaves the plan ``implemented`` for the next iteration — while
                # keeping one unreachable PR from stranding every other plan's
                # flip.  The crash signal survives elsewhere: the faces that
                # *act* on merge truth call ``is_pr_settled`` unwrapped.
                logger.warning(
                    "Failed to check merge status for plan %s",
                    plan.plan_id, exc_info=True,
                )
        return result

    # ------------------------------------------------------------------
    # Pending-PR tending
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_iso(ts: str | None) -> datetime | None:
        """Parse an ISO-8601 timestamp into a UTC ``datetime`` or ``None``."""
        if not ts:
            return None
        try:
            value = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value

    @staticmethod
    def _signature_component_differs(
        current: str | None, stamped: str | None,
    ) -> bool:
        return evolution_tend._signature_component_differs(current, stamped)

    @staticmethod
    def _signature_comparable(
        current: tuple[str | None, ...],
        stamped: tuple[str | None, ...],
    ) -> bool:
        return evolution_tend._signature_comparable(current, stamped)

    @staticmethod
    def _lap_signature_measurable(
        *,
        origin_head: str | None,
        check_conclusion: str,
    ) -> bool:
        return evolution_tend._lap_signature_measurable(origin_head=origin_head, check_conclusion=check_conclusion)

    @staticmethod
    def _reminder_threshold_hours(reminders_sent: int) -> float:
        return evolution_tend._reminder_threshold_hours(reminders_sent)

    async def tend_evolution_prs(
        self, *, settled_oracle: Callable[[int], MergeTruth],
    ) -> TendPRsResult:
        return await evolution_tend.tend_evolution_prs(self, settled_oracle=settled_oracle)

    @staticmethod
    def _emit_tend_errors(result: TendPRsResult) -> TendPRsResult:
        return evolution_tend._emit_tend_errors(result)

    def _observe_pr_staleness(self, branch: str) -> PrStaleness:
        return evolution_tend._observe_pr_staleness(self, branch)

    @staticmethod
    def _summarize_failed_gates(review: QualityGateResult) -> str:
        return evolution_tend._summarize_failed_gates(review)

    # ------------------------------------------------------------------
    # Static analysis helpers
    # ------------------------------------------------------------------


    def _find_uncalled_symbols(self, diff: str) -> list[str]:
        """See :func:`evolution_quality._find_uncalled_symbols`."""
        return evolution_quality._find_uncalled_symbols(self, diff)
