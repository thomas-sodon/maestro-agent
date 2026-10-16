"""Plan submission door — the ``submit_for_review`` cluster split out of :mod:`maestro.evolution` (proposal 147).

Decides whether a minted plan is admitted to human review or recorded
``obsolete``: the rejection cooldown, the restatement / source-task / backlog
guards, and the CLI / evidence-CLI grounding verdicts. The moved
``EvolutionEngine`` methods live here as ``fn(engine, ...)`` functions; the
engine keeps a one-line ``submit_for_review`` delegator so callers are unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from maestro import (
    plan_grounding,
)
from maestro.audit_checks import (
    DEFERRED_COOLDOWN_TAG,
    classify_door_refusal_reason,
    log_gate_activation,
)
from maestro.generation_gate import GENERATION_STANDDOWN_DIRECTIVE
from maestro.memory import (
    _OBSOLETE_REASON_MAX_CHARS,
    DuplicatePlanError,
    EvolutionPlan,
    find_pending_plan_cluster,
    is_plan_restatement,
    is_source_task_recited,
)
from maestro.pr_facts import (
    LIVE_STATUSES,
    select_live_plans,
)
from maestro.reality_inventory import (
    clear_mint,
    declares_cli_capability,
    flag_graduation_grounding_verdict,
)
from maestro.reality_inventory import (
    read_shipped_cli_surface as _shipped_cli_surface,
)

if TYPE_CHECKING:
    from maestro.evolution import EvolutionEngine


logger = logging.getLogger("maestro")

#: One-shot prune/land directive the admission gate (proposal 116, step 7)
#: writes into an empty ``next_task_directive`` slot when it refuses a net-new
#: mint. Relocated to :data:`maestro.generation_gate.GENERATION_STANDDOWN_DIRECTIVE`
#: (proposal 120) so the pre-mint generation gate and this submission door draw
#: the byte-identical directive from one owner; re-pointed here for the existing
#: call sites and ``tests/test_evolution_submission_grounding.py``. Names the disposal lever
#: (``mark_plan_obsolete``) so the next self-directed pass clears the graveyard
#: instead of growing it. ≤200 chars (``save_self_model`` re-truncates to
#: ``Consciousness.MAX_DIRECTIVE_CHARS``).
_GENERATION_ADMISSION_DIRECTIVE = GENERATION_STANDDOWN_DIRECTIVE

#: Cooldown window after a plan with a given title is rejected, during which
#: a new plan with the same normalized title is suppressed.  Counts from the
#: rejected plan's ``reviewed_at`` timestamp, which ``update_plan_status``
#: stamps on every transition into ``rejected`` (human decision or PR
#: close).  After the window elapses, the
#: same idea can be re-proposed (because context, codebase, or Thomas's
#: priorities may have changed enough to make a previously bad idea good).
_REJECTION_COOLDOWN_DAYS = 7


# Add/register intent language — a plan clause lacking one of these words (a
# modify/fix reference to an existing command) contributes no tokens, so a
# legitimate CLI-modification plan is never obsoleted. Within an intent clause
# a ``maestro <sub>`` counts as proposed only as the OBJECT of the intent verb
# (``_CLI_ADDED_SUBCMD_RES``); a mere reference ("add a table to the maestro
# cost-report output") is context for flag binding only. The intent word must
# stand alone: one inside a hyphenated token or ``--flag`` (``measure-new-x``,
# ``--new-format``) does not count, since a hyphen is a ``\b`` boundary.
_CLI_ADD_INTENT_VERBS = (
    r"add|adds|adding|added|register|registers|registering|registered|"
    r"new|introduce|introduces|introducing|introduced|expose|exposes|"
    r"exposing|exposed|create|creates|creating|created|implement|implements|"
    r"implementing|implemented"
)
_CLI_ADD_INTENT_RE = re.compile(
    rf"\b({_CLI_ADD_INTENT_VERBS})\b", re.IGNORECASE,
)
# A long ``--flag`` token (short single-dash flags are too ambiguous to mine).
_CLI_FLAG_RE = re.compile(r"--[a-zA-Z][\w-]*")
# A hyphen-joined CLI token (``measure-new-x``, ``--new-format``, ``add-foo``),
# blanked out before the add-intent check.
_CLI_HYPHEN_TOKEN_RE = re.compile(r"-*[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+")
# A ``maestro <subcommand>`` name (the word following a bare ``maestro``).
_CLI_SUBCMD_RE = re.compile(r"\bmaestro\s+([a-z][a-z0-9-]*)")
# A ``maestro <subcommand>`` introduced as the added thing: directly after an
# intent verb, with optional articles/``new`` and an optional opening backtick
# (``register the new `maestro x```), or named as a ``(sub)command``. ``new``
# is no verb here, so "the new maestro x output" stays a reference. Only the
# verb prefix is case-insensitive: ``maestro`` and its token stay lowercase
# like ``_CLI_SUBCMD_RE``, so "Add Maestro status reporting" proposes nothing.
_CLI_ADD_VERBS_NO_NEW = _CLI_ADD_INTENT_VERBS.replace("new|", "")
_CLI_ADDED_SUBCMD_RES = (
    re.compile(
        rf"(?i:\b(?:{_CLI_ADD_VERBS_NO_NEW})\s+(?:(?:a|an|the|new)\s+)*)`?"
        r"maestro\s+([a-z][a-z0-9-]*)",
    ),
    re.compile(r"\bmaestro\s+([a-z][a-z0-9-]*)`?\s+(?:sub)?command\b"),
)
# Subcommand-context patterns — used only to BIND a proposed flag to the
# subcommand a clause targets. Over-matching here is safe by construction: a
# wrong or spurious binding can only fail the guard OPEN (a flag whose target is
# not confirmed already-registered is treated as novel and never obsoleted).
_CLI_SUBCMD_CONTEXT_RES = (
    _CLI_SUBCMD_RE,
    re.compile(r"\b([a-z][a-z0-9-]*)\s+(?:sub)?command\b"),
    re.compile(
        r"\b(?:to|on|for|onto|under|in)\s+(?:the\s+)?"
        r"([a-z0-9]+(?:-[a-z0-9]+)+)\b"
    ),
)


def _clause_has_add_intent(clause: str) -> bool:
    """True when *clause* carries add-intent outside its CLI tokens."""
    stripped = _CLI_FLAG_RE.sub(" ", _CLI_HYPHEN_TOKEN_RE.sub(" ", clause))
    return bool(_CLI_ADD_INTENT_RE.search(stripped))


def _extract_added_cli_surface(
    plan: EvolutionPlan,
) -> tuple[set[str], set[tuple[str, str]], set[str]]:
    """Extract the CLI surface *plan* proposes to ADD, flags bound to subcommands.

    Scans the plan title and ``proposed_changes``, splitting each into clauses
    on sentence/line boundaries. Only add/register/new-intent clauses contribute
    (a modify/fix reference yields nothing); only an intent word outside the
    clause's hyphenated and ``--flag`` tokens counts (``measure-new-x`` does
    not supply its own intent). Within such a clause a ``maestro <name>``
    counts as proposed only as the object of the intent verb; a mere reference
    to a shipped command only supplies flag-binding context. Returns
    ``(proposed_subcommands, bound_flags, unbound_flags)``:

    * ``proposed_subcommands`` — ``maestro <name>`` subcommand tokens that are
      the object of the add intent (a global namespace).
    * ``bound_flags`` — ``(subcommand, --flag)`` pairs, where the clause naming
      the flag also names a target subcommand (``maestro <sub>``, ``<sub>
      subcommand``, or a hyphenated ``to/on <sub>``).
    * ``unbound_flags`` — ``--flag`` tokens whose clause names no subcommand;
      these can only match a shipped *global* flag, else they are novel surface.

    Zero-cost; used only to ground a new-CLI plan against the shipped surface.
    """
    proposed_subs: set[str] = set()
    bound_flags: set[tuple[str, str]] = set()
    unbound_flags: set[str] = set()
    for part in (plan.title, *plan.proposed_changes):
        for clause in re.split(r"[.\n;]+", part):
            if not _clause_has_add_intent(clause):
                continue
            for pattern in _CLI_ADDED_SUBCMD_RES:
                proposed_subs.update(m.group(1) for m in pattern.finditer(clause))
            context: set[str] = set()
            for pattern in _CLI_SUBCMD_CONTEXT_RES:
                context.update(m.group(1) for m in pattern.finditer(clause))
            flags = {m.group(0) for m in _CLI_FLAG_RE.finditer(clause)}
            for flag in flags:
                if context:
                    for sub in context:
                        bound_flags.add((sub, flag))
                else:
                    unbound_flags.add(flag)
    return proposed_subs, bound_flags, unbound_flags


def _intent_clause_subcommand_mentions(plan: EvolutionPlan) -> set[str]:
    """Every ``maestro <name>`` named in an add-intent clause of *plan*."""
    return {
        m.group(1)
        for part in (plan.title, *plan.proposed_changes)
        for clause in re.split(r"[.\n;]+", part)
        if _clause_has_add_intent(clause)
        for m in _CLI_SUBCMD_RE.finditer(clause)
    }


def _cli_grounding_verdict(plan: EvolutionPlan) -> set[str] | None:
    """Return the already-shipped CLI tokens *plan* proposes, or ``None``.

    ``None`` ⇒ do not refuse: the plan proposes no add-intent CLI token, or it
    names at least one token that is NOT confirmed already-registered on the
    shipped surface — a novel subcommand, a subcommand-bound flag absent from
    that subcommand, or an unbound flag that is not a shipped global flag. A
    non-empty set ⇒ every CLI token the plan proposes to add is already shipped;
    the caller lands it ``obsolete`` and lists the returned tokens.

    Flags are grounded against their OWNING subcommand, never a flat global bag:
    proposing ``--json`` for a subcommand that lacks it is novel surface even
    when another subcommand already registers ``--json``.
    """
    subcommands, flags_by_sub, global_flags = _shipped_cli_surface()
    proposed_subs, bound_flags, unbound_flags = _extract_added_cli_surface(plan)
    if not (proposed_subs or bound_flags or unbound_flags):
        return None
    # Any un-shipped ``maestro <name>`` in an add-intent clause vetoes, even
    # when it is not the verb's object ("add maestro a and maestro b"): the
    # object-only extraction must never under-count a novel subcommand.
    if not _intent_clause_subcommand_mentions(plan) <= subcommands:
        return None

    # ``proposed_subs`` is a subset of the mentions just vetted, so shipped.
    shipped: set[str] = set(proposed_subs)
    for flag in unbound_flags:
        if flag not in global_flags:
            return None
        shipped.add(flag)
    flag_targets: dict[str, set[str]] = {}
    for sub, flag in bound_flags:
        flag_targets.setdefault(flag, set()).add(sub)
    for flag, targets in flag_targets.items():
        if not all(flag in flags_by_sub.get(target, set()) for target in targets):
            return None
        shipped.add(flag)
    # ``shipped`` is non-empty here: the top guard returned ``None`` when the
    # plan proposed no CLI token, and every surviving token was added above.
    return shipped


# The verb prefixes of maestro's *read-surface* subcommands — the only CLI
# tokens a rationale plausibly cites as measured *evidence*: a ``measure-*``
# sensor, a ``cost-*`` / ``pr-*`` / ``list-*`` / ``show-*`` read, or a
# ``deed-`` / ``owned-`` / ``audit-`` / ``gate-`` ledger view. A cited token
# must begin with one of these verbs to count as a CLI invocation. This
# deliberately excludes action verbs (``burn-``, ``notify-``, ``resume-``)
# whose tokens collide with the codebase's hyphenated *concept* nouns
# (``burn-rate``) and which are never cited as evidence anyway.
_CLI_EVIDENCE_VERB_PREFIXES = (
    "measure", "cost", "pr", "list", "show", "deed", "owned", "audit", "gate",
)

# A cited ``maestro`` CLI subcommand: a ``maestro <token>`` invocation where
# ``<token>`` is *subcommand-shaped* — one of the read-surface verb prefixes
# above followed by at least one ``-noun`` segment (``measure-premise-accuracy``,
# ``cost-report``). The literal ``maestro`` prefix is *required* — this is the
# precise signal that the rationale presents the token as a command it ran, not
# merely as prose. It is deliberately narrow: the pattern also matches inside
# backticks (```maestro measure-foo```, since ``\bmaestro`` matches after a
# backtick), so a backtick-wrapped invocation is still captured, but a bare
# backtick-wrapped hyphenated token WITHOUT the ``maestro`` prefix is NOT — that
# would sweep up the many hyphenated invariant/concept names this codebase
# backticks in prose (``merge-truth``, ``red-root``, ``wip-primacy``,
# ``human-in-the-loop`` …) and wrongly obsolete legitimate plans. Anchoring to
# the read-surface verbs also excludes the *prefixed* concept phrase
# ``maestro <hyphenated-concept>`` — ``maestro self-model``, ``maestro
# merge-truth``, ``maestro red-root``, ``maestro burn-rate`` — whose first
# segment is not a read-surface verb (PR #3584 false positive). A bare English
# word after ``maestro`` (``maestro autonomously ...``) and underscore-bearing
# identifiers (``source_task_id``) are excluded too, so the guard fails open on
# ambiguous prose rather than refusing a legitimate plan.
_CITED_CLI_TOKEN_RE = re.compile(
    r"\bmaestro\s+((?:"
    + "|".join(_CLI_EVIDENCE_VERB_PREFIXES)
    + r")-[a-z0-9]+(?:-[a-z0-9]+)*)"
)


def _cited_cli_tokens(rationale: str) -> set[str]:
    """Extract subcommand-shaped ``maestro <token>`` CLI tokens cited in *rationale*.

    Mines a plan's rationale for a ``maestro <token>`` invocation (in prose or
    backtick-wrapped) where ``<token>`` has the hyphenated ``verb-noun``
    subcommand shape. The literal ``maestro`` prefix is required, so a bare
    hyphenated concept name in backticks (```merge-truth```), a bare English
    word after ``maestro``, or an underscore-bearing identifier contributes
    nothing — the guard consuming these fails open, never refusing a plan on an
    ambiguous match. Zero-cost, no I/O.
    """
    return {match.group(1) for match in _CITED_CLI_TOKEN_RE.finditer(rationale)}


def _plan_names_cli_token(plan: EvolutionPlan, token: str) -> bool:
    """Return True when an add-intent clause of *plan* names *token* as a whole token.

    Title and ``proposed_changes`` are split into clauses as
    :func:`_extract_added_cli_surface` does, and only a clause carrying
    ``_CLI_ADD_INTENT_RE`` outside the token itself counts (``measure-new-x``
    must not supply its own ``new``), so a modify/fix mention of a phantom
    subcommand is not an exemption. The match is hyphen-bounded (``measure-x``
    does not match inside ``measure-x-extra``) and needs no ``maestro`` prefix
    or backticks, so "add a `measure-x` subcommand to maestro/cli.py" names it.
    Zero-cost.
    """
    pattern = re.compile(rf"(?<![a-z0-9-]){re.escape(token)}(?![a-z0-9-])")
    return any(
        pattern.search(clause)
        and _CLI_ADD_INTENT_RE.search(pattern.sub(" ", clause))
        for part in (plan.title, *plan.proposed_changes)
        for clause in re.split(r"[.\n;]+", part)
    )


def _evidence_cli_grounding_verdict(plan: EvolutionPlan) -> set[str] | None:
    """Return the phantom ``maestro`` subcommands *plan* cites as evidence, or None.

    The inverse of :func:`_cli_grounding_verdict`: that guard grounds a plan's
    *proposed new* CLI surface against the shipped tree; this one grounds a
    plan's *cited evidence*. Returns the set of subcommand-shaped
    ``maestro <token>`` names the rationale cites that are (a) subcommand-shaped
    (guaranteed by :func:`_cited_cli_tokens`), (b) absent from
    :func:`_shipped_cli_surface`'s registered subcommand namespace, and (c) not
    among the tokens the plan itself introduces via a ``maestro/cli.py`` change
    — a whole-token match over the add-intent clauses of the title and
    ``proposed_changes`` (:func:`_plan_names_cli_token`), deliberately independent of
    :func:`_extract_added_cli_surface`, whose ``maestro``-prefixed extraction
    fails open for :func:`_cli_grounding_verdict` but would fail *closed* here.
    ``None`` ⇒ do not refuse: the rationale cites no CLI token, or every cited
    token is real (shipped) or forward-referenced by the plan's own diff.

    By construction this cannot refuse a plan whose cited surface exists or
    whose diff adds it — it disposes only genuinely phantom-grounded mints
    (a sensor cited as landed while it lives only on an unmerged branch).
    Zero SDK cost.
    """
    cited = _cited_cli_tokens(plan.rationale)
    if not cited:
        return None
    subcommands, _flags_by_sub, _global_flags = _shipped_cli_surface()
    # Tokens the plan itself introduces via a cli.py change are legitimate
    # forward references, not phantom evidence — exempt any an add-intent
    # clause of the title or changes names, with or without a ``maestro`` prefix.
    touches_cli = any("maestro/cli.py" in f for f in plan.files_affected)
    phantom = {
        token
        for token in cited
        if token not in subcommands
        and not (touches_cli and _plan_names_cli_token(plan, token))
    }
    return phantom or None


# Budget for the parenthesised id/token list inside a submission-door
# ``obsolete_reason``. The longest fixed template wrapped around it (the
# unshipped-CLI-evidence reason, ~95 chars) plus this budget plus the
# ``" +K more"`` suffix stays well under ``_OBSOLETE_REASON_MAX_CHARS``
# (280), so a refusal reason is bounded by construction and the
# ``mark_plan_obsolete`` precondition can never trip on a long list (#4569).
_REASON_LIST_BUDGET = 120


def _bounded_reason_list(items: Sequence[str], budget: int) -> str:
    """Join *items* with ``", "``, truncating to *budget* chars plus a ``+K more`` tail.

    The full list stays in the journal note; only the persisted reason is
    bounded. A lone first item longer than *budget* is clipped with a
    trailing ellipsis so the clip stays visible.
    """
    parts: list[str] = []
    length = 0
    for index, item in enumerate(items):
        added = len(item) + (2 if parts else 0)
        if length + added > budget:
            remaining = len(items) - index
            if not parts:
                parts.append(item[: budget - 1] + "\u2026")
                remaining -= 1
            joined = ", ".join(parts)
            return f"{joined} +{remaining} more" if remaining else joined
        parts.append(item)
        length += added
    return ", ".join(parts)


def _record_obsolete_duplicate(
    engine: EvolutionEngine,
    plan: EvolutionPlan,
    *,
    obsolete_reason: str,
    journal_content: str,
    triggered_by: str,
) -> bool:
    """Persist *plan* as ``obsolete`` with a journal note; no review burden.

    Shared by the pairwise duplicate-pending guard and the cluster-level
    themed-restatement guard: both refuse to mint a fresh
    ``pending_review`` row, instead leaving an obsolete paper trail and
    journaling why. Returns ``True`` when the plan was newly recorded and
    ``False`` on an idempotent retry (the row already existed), so callers
    can suppress duplicate refusal logging. It is the single
    submission-door refusal writer, and so the sole caller that stamps
    ``refused_at_submission=True`` — a refused mint rule 18 must never
    credit as an owned prune (#4157).

    *obsolete_reason* must satisfy :meth:`Memory.mark_plan_obsolete`'s
    precondition (non-blank, at most ``_OBSOLETE_REASON_MAX_CHARS``); it is
    checked *before* the persist and raises ``ValueError`` so a bad reason
    can never strand the refused plan as an approvable ``pending_review``
    row (#4569).
    """
    if not obsolete_reason.strip():
        raise ValueError("reason must be non-empty when marking obsolete")
    if len(obsolete_reason) > _OBSOLETE_REASON_MAX_CHARS:
        raise ValueError(f"reason exceeds {_OBSOLETE_REASON_MAX_CHARS} chars")
    try:
        engine._memory.save_evolution_plan(plan)
    except DuplicatePlanError:
        logger.info(
            "Idempotent retry: duplicate-pending plan %r already "
            "saved — skipping", plan.plan_id,
        )
        return False
    engine._memory.mark_plan_obsolete(
        plan.plan_id, obsolete_reason, refused_at_submission=True,
    )
    engine._journal.record_event(
        content=journal_content,
        entry_type="evolution_note",
        triggered_by=triggered_by,
    )
    return True

def _defer_gap_for_cooldown(
    engine: EvolutionEngine, plan: EvolutionPlan, prev: EvolutionPlan, how: str,
) -> None:
    """Record an issue-bearing *plan* the rejection cooldown refused (proposal 155).

    The ``[deferred:cooldown:<prev_id>]`` reason is the deferral the fuel
    selector reads back (:func:`gap_cooldown_until`), so the gap is not
    re-selected until *prev*'s cooldown elapses; it never counts as an
    attempt. An issue-less plan keeps the silent return.
    """
    if plan.issue_number is None:
        return
    reason = (
        f"{DEFERRED_COOLDOWN_TAG}{prev.plan_id}] {how} of a plan rejected "
        f"within {_REJECTION_COOLDOWN_DAYS} days: {prev.title}"
    )[:_OBSOLETE_REASON_MAX_CHARS]
    _record_obsolete_duplicate(
        engine, plan, obsolete_reason=reason,
        journal_content=(
            f"Deferred gap plan '{plan.title}' for issue #{plan.issue_number}: "
            f"rejection cooldown on {prev.plan_id}."
        ),
        triggered_by="rejection_cooldown",
    )


# ── Gap-plan attempt accounting (proposal 155 step 4) ────────────────────────

GapPlanDisposition = Literal["live", "attempt", "deferred"]
#: Live on its gap: the live-work statuses plus a dormant chain slice and a
#: held (``blocked``) plan, which is still in flight.
_GAP_LIVE_STATUSES = frozenset({*LIVE_STATUSES, "sliced", "blocked"})
_DEFERRED_FAMILIES = frozenset({"saturation", "cooldown"})
# ``_slice_scope_rejected_plan`` retires the approved original with this
# prefix once its multi-PR chain persists; the chain carries the attempt.
SUPERSEDED_BY_PROPOSAL_PREFIX = "Superseded by multi-PR proposal "


def gap_plan_disposition(
    plan: EvolutionPlan, retry_budget: int = 0,
) -> GapPlanDisposition:
    """Whether *plan* is live on its gap, spent an attempt, or was deferred.

    A ``failed`` plan whose ``failure_count`` is still under *retry_budget*
    (``max_implementation_attempts``) is retried by implementation, so it is
    live; it spends its attempt only once that budget is spent.

    A merged plan whose issue is still open (it reopened) spent its attempt;
    an ``obsolete`` row is deferred only when the door refused it for backlog
    saturation or the rejection cooldown — every other obsolete reason (a
    content refusal, a curate prune) is an attempt. An original superseded
    by its multi-PR chain is deferred too: the chain's slices carry that
    one attempt, so it is not counted twice.
    """
    if plan.status in _GAP_LIVE_STATUSES:
        return "live"
    if plan.status == "failed" and plan.failure_count < retry_budget:
        return "live"
    if plan.status == "obsolete":
        reason = plan.obsolete_reason or ""
        if reason.startswith(SUPERSEDED_BY_PROPOSAL_PREFIX) or (
            classify_door_refusal_reason(reason) in _DEFERRED_FAMILIES
        ):
            return "deferred"
    return "attempt"


def _gap_plans(plans: Sequence[EvolutionPlan], issue_number: int) -> list[EvolutionPlan]:
    return [p for p in plans if p.issue_number == issue_number]


def _created_after(plan: EvolutionPlan, since: datetime) -> bool:
    """Whether *plan* was created after *since*; an unparseable stamp counts."""
    try:
        stamp = datetime.fromisoformat((plan.created_at or "").replace("Z", "+00:00"))
    except ValueError:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp > since


def count_gap_attempts(
    plans: Sequence[EvolutionPlan], issue_number: int, retry_budget: int = 0,
    *, since: datetime | None = None,
) -> int:
    """How many plans citing *issue_number* spent an attempt on it.

    The slices of one multi-PR chain (sharing a ``parent_plan_id``) are one
    attempt, spent only once no sibling slice is still live: a merged early
    slice of a chain still in flight is progress, not a spent attempt.
    *since* (the gap's ``needs-decision`` reset, proposal 155 step 5) counts
    only plans created after it; a plan with an unparseable stamp still
    counts (fail closed).
    """
    gap = _gap_plans(plans, issue_number)
    if since is not None:
        gap = [p for p in gap if _created_after(p, since)]
    chains: dict[str, list[GapPlanDisposition]] = {}
    attempts = 0
    for p in gap:
        disposition = gap_plan_disposition(p, retry_budget)
        if p.parent_plan_id is not None:
            chains.setdefault(p.parent_plan_id, []).append(disposition)
        elif disposition == "attempt":
            attempts += 1
    return attempts + sum(
        "attempt" in d and "live" not in d for d in chains.values()
    )


def gap_has_live_plan(
    plans: Sequence[EvolutionPlan], issue_number: int, retry_budget: int = 0,
) -> bool:
    """True when a live (or still-retried) plan already cites *issue_number*."""
    return any(
        gap_plan_disposition(p, retry_budget) == "live"
        for p in _gap_plans(plans, issue_number)
    )


def gap_cooldown_until(
    plans: Sequence[EvolutionPlan], issue_number: int,
) -> datetime | None:
    """When the newest cooldown deferral on *issue_number* stops deferring it.

    The cited rejection's ``reviewed_at`` (else ``created_at``) plus
    ``_REJECTION_COOLDOWN_DAYS``; ``None`` when no deferral cites a known
    rejection or its stamp is unparseable (fail open: the gap is fuel).
    """
    by_id = {p.plan_id: p for p in plans}
    deferred = [
        p for p in _gap_plans(plans, issue_number)
        if p.status == "obsolete"
        and (p.obsolete_reason or "").startswith(DEFERRED_COOLDOWN_TAG)
    ]
    if not deferred:
        return None
    newest = max(deferred, key=lambda p: p.created_at or "")
    cited_id = (newest.obsolete_reason or "")[len(DEFERRED_COOLDOWN_TAG):].split("]", 1)[0]
    cited = by_id.get(cited_id)
    raw = (cited.reviewed_at or cited.created_at) if cited is not None else None
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp + timedelta(days=_REJECTION_COOLDOWN_DAYS)


def _sibling_slice_ids(engine: EvolutionEngine, plan: EvolutionPlan) -> set[str]:
    """Return the plan ids of *plan*'s sibling slices, else an empty set.

    A slice is a sibling of *plan* when both share the same
    ``parent_plan_id`` naming a real :class:`EvolutionProposal`. Multi-PR
    self-improvement (proposal 118) lands one goal across an ordered chain
    of intentionally-related slices, so a sibling must not be mistaken for a
    duplicate by the cross-plan dedup guards. The returned set never
    includes *plan* itself, so a guard only ever sees a plan's *siblings*.

    The empty set is the negative control: when
    ``[loop] multi_pr_proposals_enabled`` is off, ``plan.parent_plan_id`` is
    ``None``, or it names no persisted proposal, no guard is exempted and
    every one fires exactly as today. Zero SDK cost.
    """
    if not engine._config.multi_pr_proposals_enabled:
        return set()
    parent = plan.parent_plan_id
    if not parent:
        return set()
    if not any(
        p.proposal_id == parent
        for p in engine._memory.get_evolution_proposals()
    ):
        return set()
    return {
        s.plan_id
        for s in engine._memory.get_slices_for_proposal(parent)
        if s.plan_id != plan.plan_id
    }


async def submit_for_review(
    engine: EvolutionEngine, plan: EvolutionPlan, *, self_initiated: bool = False,
    exempt_saturation: bool = False,
) -> None:
    """Save plan and notify Thomas.

    ``self_initiated`` marks a submission arriving from a self-initiated
    plan-mint channel already cleared by the pre-mint generation authority
    (``MaestroAgent._pre_mint_generation_verdict`` →
    ``generation_gate.may_mint_plan``). When the pre-mint gate is active
    (``[loop] pre_mint_generation_gate_enabled``), such a submission skips only
    the admission gate below, whose only arm is backlog saturation — the
    pre-mint owner already cleared it; any non-pre-gated path (external / user
    resubmission) still hits that arm. The default ``False`` preserves today's
    behaviour, matching the house style of opt-in gate parameters such as
    ``Memory.save_evolution_plan(..., enforce_restatement_gate=False)``.
    ``exempt_saturation`` skips both backlog-saturation arms (admission and
    submit-block) for a
    plan minted from gate-bypassing fuel (a red ``maestro:gate``), whose
    priced ``propose_plan`` would otherwise be refused and re-paid every tick.
    """
    # Generator reality grounding (proposal 136) — the first authority,
    # ahead of the 114 probe below. When the flag is on and the plan declares
    # a ``problem_ref`` or ``new_capability_names``, clear them against the
    # reality inventory: a name that already ships or an unmeasured problem
    # lands the plan ``obsolete`` with an ``[ungrounded-generation:<reason>]``
    # reason and returns without notifying. A plan declaring neither passes
    # untouched; an unreadable inventory fails open with an ``unevaluated``
    # gate row. Flag-off, no inventory read occurs. Zero SDK cost. A cleared
    # declaration that includes a ``cli:`` capability is authoritative on
    # "adds CLI surface that already ships", so it demotes the shipped-CLI
    # guard below to a backstop for every other plan.
    cli_declaration_cleared = False
    if engine._config.generator_reality_grounding_enabled and (
        plan.problem_ref is not None or plan.new_capability_names
    ):
        loaded = await asyncio.to_thread(engine._load_generation_inventory)
        if loaded is None:
            log_gate_activation(
                engine._gate_activations,
                gate="generator_reality_grounding",
                action="unevaluated",
                detail=f"[unevaluated] plan {plan.plan_id}",
            )
        else:
            inventory, corpus = loaded
            clearance = clear_mint(
                inventory,
                problem_ref=plan.problem_ref,
                new_capability_names=plan.new_capability_names,
                corpus_text=corpus,
            )
            if not clearance.cleared:
                tag = f"[ungrounded-generation:{clearance.reason}]"
                recorded = _record_obsolete_duplicate(
                    engine,
                    plan,
                    obsolete_reason=(
                        f"{tag} {clearance.offending} is refuted by the "
                        "reality inventory"
                    ),
                    journal_content=(
                        f"Refused to mint evolution plan '{plan.title}': its "
                        f"declaration {clearance.offending!r} is refuted by "
                        f"the reality inventory {tag}. Recorded obsolete; not "
                        "submitted, never notified."
                    ),
                    triggered_by=(
                        f"generator_reality_grounding:{clearance.reason}"
                    ),
                )
                if recorded:
                    log_gate_activation(
                        engine._gate_activations,
                        gate="generator_reality_grounding",
                        action="obsolete",
                        detail=f"{tag} {clearance.offending}",
                    )
                    logger.info(
                        "Refusing plan %r: %s %s — recorded obsolete, not "
                        "submitted", plan.plan_id, tag, clearance.offending,
                    )
                return
            cli_declaration_cleared = declares_cli_capability(
                plan.new_capability_names,
            )

    # Plan-premise grounding guard (proposal 114) — the first submission
    # guard after proposal 136's reality grounding above, ahead of every dedup/cooldown/CLI-grounding/backlog check and any
    # human notification. When ``[loop] plan_grounding_enabled`` is on and the
    # plan carries a grounding probe, read the located tree state and let the
    # pure ``plan_grounding.confirm`` oracle decide: a probe the tree refutes
    # (a ``SYMBOL_ABSENT`` whose locator is already defined, a
    # ``DEFECT_PRESENT`` whose needle is absent, a ``LINT_PRESENT`` whose ruff
    # rule runs clean on the file) lands the plan ``obsolete`` via the shared
    # refusal path with an ``[ungrounded:<category>]`` reason, stamps a
    # ``plan_grounding`` gate-activation row, and returns without notifying —
    # exactly the rejection the human reviewer writes today, produced before an
    # approval is spent. A ``grounded`` verdict, a plan with no probe, or the
    # flag-off path falls through unchanged. The tree read lives behind
    # ``_read_grounding_tree_text`` so unit tests inject text and touch no real
    # filesystem. A ``None`` read (a ``LINT_PRESENT`` ruff read that could not
    # run, or an unconfinable/unreadable lint or ``BEHAVIOR_ABSENT`` locator)
    # fails **open**: skip the refusal, stamp an ``unevaluated`` gate row so
    # the no-op stays legible, and let the plan reach the existing guards
    # rather than false-refusing on a check that never happened. Zero SDK
    # cost.
    if engine._config.plan_grounding_enabled and plan.grounding is not None:
        # Offload the grounding read to a worker thread: the ``LINT_PRESENT``
        # branch shells out to ruff (blocking, up to
        # ``_LINT_PROBE_TIMEOUT_SECONDS``) and the ``SYMBOL_ABSENT`` branch
        # reads the whole ``maestro`` corpus, so running the sync seam inline
        # would stall this async caller's event loop. ``to_thread`` keeps the
        # loop responsive; the result is unchanged.
        read_text = await asyncio.to_thread(
            engine._read_grounding_tree_text, plan.grounding,
        )
        if read_text is None:
            # The probe could not be evaluated (a ruff read that could not
            # run, or a BEHAVIOR_ABSENT/LINT_PRESENT locator that is
            # unconfinable or unreadable). Fail open — but stamp the no-op so
            # an empty ``plan_grounding`` ledger is legible as "no probe was
            # ever refuted" rather than silently conflating that with "no
            # probe could ever be read".
            log_gate_activation(
                engine._gate_activations,
                gate="plan_grounding",
                action="unevaluated",
                detail=(
                    f"[unevaluated] {plan.grounding.kind} "
                    f"{plan.grounding.locator}"
                ),
            )
            logger.info(
                "Plan %r grounding probe %s on %r could not be evaluated — "
                "failing open, plan proceeds unguarded", plan.plan_id,
                plan.grounding.kind, plan.grounding.locator,
            )
        else:
            verdict = plan_grounding.confirm(plan.grounding, read_text)
            if (
                verdict.grounded
                and plan.grounding.kind == "BEHAVIOR_ABSENT"
                and plan_grounding.plan_introduces_witness(
                    plan.grounding.expectation,
                    [*plan.proposed_changes, *plan.acceptance_criteria],
                )
            ):
                # Self-certifying (#4581): the witness is absent only because
                # this plan proposes to add it, so the grounded verdict proves
                # nothing. Count it unevaluated and fail open to the guards.
                log_gate_activation(
                    engine._gate_activations,
                    gate="plan_grounding",
                    action="unevaluated",
                    detail=(
                        f"[self-certifying] {plan.grounding.kind} "
                        f"{plan.grounding.locator}"
                    ),
                )
            if not verdict.grounded:
                category = verdict.category or "ungrounded"
                recorded = _record_obsolete_duplicate(
                    engine,
                    plan,
                    obsolete_reason=(
                        f"[ungrounded:{category}] grounding probe "
                        f"{plan.grounding.kind} on {plan.grounding.locator} is "
                        "refuted by a fresh reading of the tree"
                        + plan_grounding.planner_read_tag(plan.grounding, plan.grounded_reads)
                    ),
                    journal_content=(
                        f"Refused to mint evolution plan '{plan.title}': its "
                        f"grounding probe ({plan.grounding.kind} on "
                        f"{plan.grounding.locator}) is refuted by the tree "
                        f"[{category}]. Recorded obsolete; not submitted, never "
                        "notified. Grounding a plan's premise against disk stops "
                        "a false-premise plan from spending a human approval."
                    ),
                    triggered_by=f"plan_grounding:{category}",
                )
                if recorded:
                    log_gate_activation(
                        engine._gate_activations,
                        gate="plan_grounding",
                        action="obsolete",
                        detail=(
                            f"[ungrounded:{category}] {plan.grounding.kind} "
                            f"{plan.grounding.locator}"
                        ),
                    )
                    logger.info(
                        "Refusing ungrounded plan %r: grounding probe %s on %r "
                        "refuted by the tree [%s] — recorded obsolete, not "
                        "submitted", plan.plan_id, plan.grounding.kind,
                        plan.grounding.locator, category,
                    )
                return

    # Deduplication has two layers:
    # 1. Active dedup excludes ``rejected`` / ``failed`` / ``obsolete``
    #    (per-status rationale lives in ``PlanStatus`` + CLAUDE.md).
    # 2. Rejection cooldown (M1): a *rejected* same-title plan
    #    blocks re-proposal for ``_REJECTION_COOLDOWN_DAYS``. The
    #    gap prompt shows the LLM recent rejections, but the LLM
    #    alone proved unreliable, so this
    #    hard safety net stops the dashboard flood within the
    #    window. After the cooldown elapses the idea is re-proposable.
    #
    # No cooldown is enforced for ``obsolete`` titles — preventing a
    # ``propose → mark_obsolete → propose`` loop is the future
    # ``curate_pending_plans`` phase's responsibility, not dedup's.
    all_plans_full = engine._memory.get_all_plans()
    active_plans = [
        p for p in all_plans_full
        if p.status not in ("rejected", "failed", "obsolete")
    ]
    rejected_plans = [p for p in all_plans_full if p.status == "rejected"]
    # Sibling-slice dedup exemption (proposal 118): a guard's refusal is
    # suppressed only when *every* plan it collides with is a sibling slice
    # of the same ``EvolutionProposal`` — a vetted sequence, not a
    # duplicate. An empty ``sibling_ids`` (flag off, no parent, or an
    # unknown parent) exempts nothing, so a collision touching any
    # non-sibling plan still refuses exactly as today.
    sibling_ids = _sibling_slice_ids(engine, plan)

    def _collision_all_siblings(colliding_ids: set[str]) -> bool:
        return bool(colliding_ids) and colliding_ids <= sibling_ids

    normalized_title = " ".join(plan.title.strip().lower().split())
    if not normalized_title:
        # Empty/whitespace-only titles are not meaningful for dedup —
        # skip deduplication so distinct plans aren't silently dropped.
        pass
    else:
        for recent in active_plans:
            if " ".join((recent.title or "").strip().lower().split()) == normalized_title:
                if _collision_all_siblings({recent.plan_id}):
                    # A sibling slice may legitimately share a title —
                    # never dedup it away (proposal 118).
                    continue
                logger.info(
                    "Skipping duplicate plan: '%s' (matches '%s')",
                    plan.title, recent.title,
                )
                if plan.issue_number is not None:
                    # A gap plan's refusal is an attempt on its issue
                    # (proposal 155 step 4): leave the restatement paper trail.
                    _record_obsolete_duplicate(
                        engine, plan,
                        obsolete_reason=(
                            f"Near-duplicate of {recent.plan_id} (exact title)"
                        ),
                        journal_content=(
                            f"Refused to mint evolution plan '{plan.title}': "
                            f"exact title of existing plan {recent.plan_id}. "
                            "Recorded obsolete; not submitted."
                        ),
                        triggered_by="duplicate_exact_title",
                    )
                return

        now = datetime.now(UTC)
        for prev in rejected_plans:
            # Use reviewed_at if available (the rejection moment, stamped
            # on every entry into ``rejected`` — human decision or PR
            # close), else fall back to created_at.  Unparseable timestamps fail open
            # (we re-allow the proposal) — better than silently
            # blocking forever on a stale data corruption.
            ts_raw = prev.reviewed_at or prev.created_at
            if not ts_raw:
                continue
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                logger.warning(
                    "Could not parse rejection timestamp %r for plan %s",
                    ts_raw, prev.plan_id,
                )
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            age_days = (now - ts).total_seconds() / 86_400
            if age_days >= _REJECTION_COOLDOWN_DAYS:
                # Cooldown elapsed — this rejection no longer blocks
                # re-proposal by exact title *or* content restatement.
                continue
            if _collision_all_siblings({prev.plan_id}):
                # A sibling slice of the same proposal is intentionally
                # related; the cross-plan cooldown must not refuse it
                # (proposal 118) — skip both its exact-title and content
                # restatement checks for this rejected sibling.
                continue
            if " ".join((prev.title or "").strip().lower().split()) == normalized_title:
                logger.info(
                    "Rejection cooldown active: '%s' was rejected"
                    " %.1f days ago (< %d) — skipping resubmission",
                    plan.title, age_days, _REJECTION_COOLDOWN_DAYS,
                )
                _defer_gap_for_cooldown(engine, plan, prev, "exact title")
                return
            # Width fix: the exact-title check above misses a re-proposal
            # that reworks the title while keeping the *content* of a
            # recently-rejected loser. Extend the same refusal path to a
            # content-word Jaccard restatement of a within-cooldown
            # rejected plan, reusing ``is_plan_restatement``'s existing
            # threshold (no new constant). The fuzzy pending/live-corpus
            # guards further below never see rejected plans, so without
            # this a title-reworked duplicate of a recent loser slips
            # straight through to Thomas's queue.
            if is_plan_restatement(plan.title, plan.rationale, [prev]) is not None:
                logger.info(
                    "Rejection cooldown active: '%s' restates rejected plan"
                    " '%s' (rejected %.1f days ago, < %d) — skipping"
                    " resubmission",
                    plan.title, prev.title, age_days, _REJECTION_COOLDOWN_DAYS,
                )
                _defer_gap_for_cooldown(engine, plan, prev, "restatement")
                return

    # Re-cited-source-branch guard — refuse a plan that re-bases on a
    # ``source_task_id`` a *live* plan already claims. The content-word
    # Jaccard restatement/cluster guards below key on *body* overlap, so a
    # re-wrap of the same stranded task branch under fresh framing slips
    # past them (the ``score_delivery_prediction`` re-proposal that reached
    # a rejection after a sibling branch had already consumed that work).
    # This deterministic, zero-SDK signal — the incoming plan cites a
    # branch a live (``pending_review``/``approved``/``implemented``) plan
    # already cites — is distinct from both. On a match, land the
    # candidate ``obsolete`` (a
    # paper trail, never a second review burden) via the shared refusal
    # path and journal the collision. A null/uncited ``source_task_id``
    # (a plan based fresh from ``main``) never triggers this.
    if is_source_task_recited(plan.source_task_id, all_plans_full) and (
        # Sibling exemption (proposal 118): a chain's slices legitimately
        # reuse the same base branch. Re-check recitation against the corpus
        # with siblings removed — reusing the sole recitation owner rather
        # than re-deriving its live-status filter — and refuse only when a
        # *non-sibling* live plan still cites the branch.
        is_source_task_recited(
            plan.source_task_id,
            [p for p in all_plans_full if p.plan_id not in sibling_ids],
        )
    ):
        recorded = _record_obsolete_duplicate(
            engine,
            plan,
            obsolete_reason=(
                f"Re-cites source branch {plan.source_task_id} already "
                "claimed by a live plan — land or dispose that plan first"
            ),
            journal_content=(
                f"Refused to mint evolution plan '{plan.title}': it "
                f"re-bases on source branch {plan.source_task_id}, which a "
                "live (pending_review/approved/implemented) plan already "
                "cites. "
                "Recorded obsolete; not submitted. Re-wrapping a stranded "
                "branch a sibling already consumed only earns a rejection."
            ),
            triggered_by=f"recited_source_task:{plan.source_task_id}",
        )
        if recorded:
            logger.info(
                "Refusing re-cited-source plan %r: source_task_id %r "
                "already claimed by a live plan — recorded obsolete, not "
                "submitted", plan.plan_id, plan.source_task_id,
            )
        return

    # Flag-graduation grounding guard (alongside the CLI grounding arms) —
    # refuse a self-initiated plan proposing to flip/graduate an
    # EXPERIMENTAL_FLAGS flag before its review_by horizon closes: a
    # human-owned cutover. The verdict (flag_lifecycle) returns the offending
    # key only when the plan touches a cutover path, names the flag with flip
    # intent, and the horizon is still future; on a hit, land obsolete.
    premature_flag = (
        flag_graduation_grounding_verdict(plan, datetime.now(UTC).date())
        if self_initiated
        else None
    )
    if premature_flag is not None:
        tag = f"[flag-graduation-premature:{premature_flag}]"
        recorded = _record_obsolete_duplicate(
            engine,
            plan,
            obsolete_reason=(
                f"{tag} graduates experimental flag {premature_flag} before "
                "its review_by window closes — a human-owned cutover"
            ),
            journal_content=(
                f"Refused to mint plan '{plan.title}': it proposes to "
                f"graduate experimental flag {premature_flag} before its "
                f"review_by horizon {tag}. Recorded obsolete; not submitted."
            ),
            triggered_by=f"flag_graduation_premature:{premature_flag}",
        )
        if recorded:
            log_gate_activation(
                engine._gate_activations,
                gate="flag_graduation_grounding",
                action="obsolete",
                detail=tag,
            )
            logger.info(
                "Refusing premature flag-graduation plan %r: flag %s still "
                "within its review_by window — recorded obsolete",
                plan.plan_id, premature_flag,
            )
        return

    # Shipped-CLI-surface guard — refuse a plan whose only CLI novelty is a
    # subcommand/flag already registered on ``main``. The restatement guards
    # (title-exact, rejection cooldown, content-word Jaccard, source-branch
    # recitation) all read the *plan* corpus (pending/live/rejected), never
    # the shipped tree, so a plan proposing an already-implemented CLI
    # surface — the dominant 'already implemented on main' duplicate
    # sub-cluster — reaches Thomas as wasted review. When ``maestro/cli.py``
    # is in ``files_affected`` and every add-intent CLI token the plan
    # proposes is already registered on ``cli.py``'s AST-derived surface —
    # a proposed subcommand already in the global namespace, or a flag
    # already registered on the *specific subcommand* the plan targets —
    # land it obsolete via the shared refusal path. Flags are grounded per
    # owning subcommand, so proposing ``--json`` for a subcommand that lacks
    # it is novel surface even when another subcommand registers ``--json``.
    # A plan that names at least one un-shipped token, an unbindable flag,
    # references tokens only to modify/fix an existing command (no add-intent
    # language), or merely cites a shipped command rather than adding it,
    # passes through untouched. Zero SDK cost; grounds the
    # proposal against disk (grounding-beats-inheritance on my own path).
    # Backstop since proposal 136: skipped when the reality-inventory
    # clearance above ran and cleared a declared ``cli:`` capability (the
    # declaration wins); it still guards every plan that declared no
    # ``cli:`` capability, or whose clearance did not run.
    touches_cli = any("maestro/cli.py" in f for f in plan.files_affected)
    if touches_cli and cli_declaration_cleared:
        logger.info(
            "Skipping shipped-CLI guard for plan %r: the reality-inventory "
            "clearance of its cli: declaration is authoritative",
            plan.plan_id,
        )
    elif touches_cli:
        shipped_tokens = _cli_grounding_verdict(plan)
        if shipped_tokens:
            token_list = ", ".join(sorted(shipped_tokens))
            reason_list = _bounded_reason_list(
                sorted(shipped_tokens), _REASON_LIST_BUDGET,
            )
            recorded = _record_obsolete_duplicate(
                engine,
                plan,
                obsolete_reason=(
                    f"Proposes only already-shipped CLI surface "
                    f"({reason_list}) already registered in cli.py"
                ),
                journal_content=(
                    f"Refused to mint evolution plan '{plan.title}': every "
                    f"CLI token it proposes to add ({token_list}) is already "
                    "registered in maestro/cli.py's shipped surface. Recorded "
                    "obsolete; not submitted. Grounding a new-CLI plan against "
                    "the tree stops the 'already-implemented' duplicate "
                    "sub-cluster from reaching review."
                ),
                triggered_by=f"shipped_cli_surface:{token_list}",
            )
            if recorded:
                logger.info(
                    "Refusing already-shipped-CLI plan %r: every proposed "
                    "CLI token (%s) already registered in cli.py — recorded "
                    "obsolete, not submitted", plan.plan_id, token_list,
                )
            return

    # Cited-CLI-evidence grounding guard — the inverse of the shipped-CLI
    # guard above. That guard grounds a plan's *proposed new* CLI surface
    # against the tree; this one grounds a plan's *cited evidence*. A
    # recurring, deterministically-detectable DOA rejection class is the
    # 'nonexistent-evidence premise': a rationale that leans on a
    # ``maestro <subcommand>`` sensor — typically a ``measure-*`` read
    # surface — that does not exist on main because it was itself only
    # proposed on an unmerged branch. When the rationale cites a
    # subcommand-shaped ``maestro <token>`` (in prose or backtick-wrapped)
    # that is neither registered on ``cli.py``'s shipped
    # surface nor introduced by this plan's own ``maestro/cli.py`` change,
    # the cited evidence is phantom: land the plan obsolete via the shared
    # refusal path. By construction it cannot touch a plan whose cited
    # surface is real or whose own diff adds it — it disposes only
    # genuinely phantom-grounded mints. Zero SDK cost; grounds cited
    # evidence against disk (grounding-beats-inheritance on my own path).
    phantom_tokens = _evidence_cli_grounding_verdict(plan)
    if phantom_tokens:
        token_list = ", ".join(sorted(phantom_tokens))
        reason_list = _bounded_reason_list(
            sorted(phantom_tokens), _REASON_LIST_BUDGET,
        )
        recorded = _record_obsolete_duplicate(
            engine,
            plan,
            obsolete_reason=(
                f"Cites unshipped CLI evidence ({reason_list}) — no such "
                "maestro subcommand on main or in this plan's own diff"
            ),
            journal_content=(
                f"Refused to mint evolution plan '{plan.title}': its "
                f"rationale cites `maestro` subcommand(s) ({token_list}) as "
                "established evidence, but none is registered in "
                "maestro/cli.py's shipped surface nor added by this plan's "
                "own diff. Recorded obsolete; not submitted. Citing a "
                "sensor I only proposed on an unmerged branch as if it were "
                "landed evidence is the nonexistent-evidence premise."
            ),
            triggered_by=f"unshipped_cli_evidence:{token_list}",
        )
        if recorded:
            logger.info(
                "Refusing phantom-CLI-evidence plan %r: rationale cites "
                "unshipped maestro subcommand(s) (%s) — recorded obsolete, "
                "not submitted", plan.plan_id, token_list,
            )
        return

    # Duplicate-pending-plan guard — refuse to mint a copy of a plan
    # Thomas has not yet ruled on. Before persisting a fresh
    # ``pending_review`` row, check whether this candidate near-restates
    # an open pending plan (different title, overlapping body — the
    # title-exact dedup above only catches identical titles), reusing the
    # zero-LLM-cost content-word Jaccard machinery at the tunable
    # ``[loop] duplicate_pending_plan_threshold``. On a match, record the
    # candidate as ``obsolete`` (a paper trail, never a second
    # ``pending_review`` burden) and journal the refusal citing the open
    # plan. This constrains my own proposal behavior at the source — the
    # proposal-layer twin of the duplicated-remediation-stamp signature —
    # rather than adding another sensor pointed at the disease.
    # Every submission-time guard below compares the candidate against the
    # whole live-work corpus (``pending_review``/``approved``/``implemented``,
    # via ``Memory.get_live_plan_records`` / ``select_live_plans``) rather than
    # ``pending_review`` only, so a restatement of an approved or
    # open-PR-backed plan is refused too (proposal 100).
    duplicate_id = engine._memory.find_duplicate_pending_plan(
        plan.title, plan.rationale,
        engine._config.loop_duplicate_pending_plan_threshold,
    )
    # Sibling exemption (proposal 118): a near-restatement of a sibling
    # slice is a vetted sequence, not a duplicate. Unlike the full-set
    # cluster guards below, ``find_duplicate_pending_plan`` returns only the
    # *first* match, so a plan colliding with both a sibling and a
    # non-sibling could surface the sibling and wrongly slip through. When
    # the first match is a sibling, re-run the matcher over the
    # sibling-excluded corpus — mirroring the ``is_source_task_recited``
    # exemption above — so a co-colliding *non-sibling* live plan still
    # refuses. An empty ``sibling_ids`` (flag off / no parent) never enters
    # this branch, so the guard is byte-identical to today.
    if duplicate_id is not None and duplicate_id in sibling_ids:
        duplicate_id = engine._memory.find_duplicate_pending_plan(
            plan.title, plan.rationale,
            engine._config.loop_duplicate_pending_plan_threshold,
            exclude_ids=sibling_ids,
        )
    if duplicate_id is not None:
        # Under the live-work corpus the match may be an
        # ``approved``/``implemented`` plan (behind an open PR), not a
        # ``pending_review`` one, so the journal names the corpus actually
        # consulted (proposal 100).
        plan_word = "live plan"
        await_word = "already in flight"
        unruled = "still in flight"
        recorded = _record_obsolete_duplicate(
            engine,
            plan,
            obsolete_reason=(
                f"Near-duplicate of {plan_word} {duplicate_id} {await_word}"
            ),
            journal_content=(
                f"Refused to mint duplicate evolution plan '{plan.title}': "
                f"it near-restates {plan_word} {duplicate_id}, {unruled}. "
                "Recorded obsolete; not submitted for review."
            ),
            triggered_by=f"duplicate_pending:{duplicate_id}",
        )
        if recorded:
            logger.info(
                "Refusing duplicate-pending plan %r: near-restates pending "
                "%r (content-word Jaccard ≥ %.2f) — recorded obsolete, "
                "not submitted", plan.plan_id, duplicate_id,
                engine._config.loop_duplicate_pending_plan_threshold,
            )
        return

    # Cluster-level themed-restatement guard — the pairwise check above
    # sees only plan pairs, so a backlog of near-restatements (each below
    # the Jaccard cutoff, all meaning the same thing) can accumulate one
    # plan at a time: the literal mechanism of the stand-down treadmill.
    # When at least ``[loop] pending_plan_cluster_threshold`` open pending
    # plans share this candidate's dominant-token theme, land it obsolete
    # rather than minting a fresh themed restatement.
    cluster = engine._memory.find_theme_cluster_pending_plans(
        plan.title, plan.rationale,
        engine._config.loop_pending_plan_cluster_threshold,
    )
    # Sibling exemption (proposal 118): a themed cluster consisting only of
    # sibling slices is a vetted sequence — refuse only when a non-sibling
    # plan is part of the cluster.
    if cluster and not _collision_all_siblings(
        {p.plan_id for p in cluster}
    ):
        cluster_ids = ", ".join(p.plan_id for p in cluster)
        reason_ids = _bounded_reason_list(
            [p.plan_id for p in cluster], _REASON_LIST_BUDGET,
        )
        # Honest corpus wording under the live-work corpus (see the duplicate
        # guard above; proposal 100).
        plans_word = "live plans"
        await_word = "already in flight"
        unruled = "still in flight"
        recorded = _record_obsolete_duplicate(
            engine,
            plan,
            obsolete_reason=(
                f"Themed restatement of {len(cluster)} {plans_word} "
                f"({reason_ids}) {await_word}"
            ),
            journal_content=(
                f"Refused to mint themed evolution plan '{plan.title}': "
                f"it joins a cluster of {len(cluster)} {plans_word} "
                f"({cluster_ids}) that share its dominant theme and "
                f"{unruled}. Recorded obsolete; not submitted for review."
            ),
            triggered_by=f"theme_cluster:{cluster_ids}",
        )
        if recorded:
            logger.info(
                "Refusing themed-cluster plan %r: shares theme with %d "
                "pending plans (%s) — recorded obsolete, not submitted",
                plan.plan_id, len(cluster), cluster_ids,
            )
        return

    # Cluster-level pending-plan stop — refuse to mint a plan when a
    # *theme-cluster* of pending plans already chokes the review queue.
    # The duplicate guard above only catches a near-restatement of a
    # single plan at the strict ``duplicate_pending_plan_threshold``; it
    # misses the proliferation failure mode where each variant sits just
    # *below* that cutoff yet collectively forms one theme.
    # ``find_pending_plan_cluster`` counts pending plans whose content-word
    # Jaccard meets the looser ``[loop] pending_cluster_threshold``; once
    # that count reaches ``[loop] pending_cluster_max`` the incoming plan
    # is landed ``obsolete`` (a paper trail, never another pending burden)
    # and the refusal is journaled with the cluster size.
    # ``pending_cluster_max <= 0`` disables the stop.
    cluster_max = engine._config.loop_pending_cluster_max
    if cluster_max > 0:
        cluster = find_pending_plan_cluster(
            engine._memory,
            f"{plan.title}\n{plan.rationale}",
            engine._config.loop_pending_cluster_threshold,
        )
        # Sibling exemption (proposal 118): a pending-plan cluster made up
        # only of sibling slices is a vetted sequence, not proliferation —
        # refuse only when a non-sibling plan is part of the cluster.
        if len(cluster) >= cluster_max and not _collision_all_siblings(
            {p.plan_id for p in cluster}
        ):
            cluster_ids = ", ".join(p.plan_id for p in cluster)
            # Honest corpus wording under the live-work corpus (see the
            # duplicate guard above; proposal 100).
            plans_word = "live plans"
            reason_state = "already in flight"
            thought_state = "already in flight"
            recorded = _record_obsolete_duplicate(
                engine,
                plan,
                obsolete_reason=(
                    f"Theme-clusters with {len(cluster)} {plans_word} "
                    f"{reason_state}"
                ),
                journal_content=(
                    f"Refused to mint evolution plan '{plan.title}': it "
                    f"theme-clusters with {len(cluster)} {plans_word} "
                    f"({cluster_ids}) {thought_state} "
                    f"(≥ {cluster_max} cap). Recorded obsolete; not "
                    "submitted for review."
                ),
                triggered_by=f"pending_cluster:{len(cluster)}",
            )
            if not recorded:
                return
            logger.info(
                "Refusing clustered pending plan %r: theme-clusters with "
                "%d pending plans (≥ %d cap, Jaccard ≥ %.2f) — recorded "
                "obsolete, not submitted", plan.plan_id, len(cluster),
                cluster_max, engine._config.loop_pending_cluster_threshold,
            )
            return

    # Generation-admission gate (proposal 116, step 7) — a default-on cap on
    # net-new production that fires on backlog depth. Unlike the submit-block
    # below it writes one bounded prune/land directive into an empty
    # ``next_task_directive`` slot (first-writer-wins). Fully behind
    # ``[loop] generation_admission_enabled``. Zero SDK cost.
    #
    # Saturation arm demoted to a backstop (proposal 120, step 6): once the
    # pre-mint generation authority is active (``[loop]
    # pre_mint_generation_gate_enabled``), it already cleared backlog
    # saturation for the self-initiated channels, so a pre-gated submission
    # (``self_initiated=True``) skips it. The legacy theme-concentration arm is
    # retired (proposal 155 Step 3). The deeper, content-blind
    # ``pending_plan_submit_block_threshold`` submit-block below stays the
    # deepest backstop for every path except a red ``maestro:gate`` mint
    # (``exempt_saturation``), bounded to one live plan per gap.
    if engine._config.generation_admission_enabled:
        # A gate-bypassing (red ``maestro:gate``) mint is exempt here as at
        # the submit-block below (proposal 155).
        pre_gated = exempt_saturation or (
            engine._config.pre_mint_generation_gate_enabled and self_initiated
        )
        admission_block = engine._config.loop_pending_plan_submit_block_threshold
        admission_pending = len(select_live_plans(all_plans_full))
        backlog_saturated = (
            not pre_gated
            and admission_block > 0
            and admission_pending >= admission_block
        )
        if backlog_saturated:
            trigger = "saturation"
            detail = (
                f"backlog saturated ({admission_pending} >= "
                f"{admission_block})"
            )
            recorded = _record_obsolete_duplicate(
                engine,
                plan,
                obsolete_reason=(
                    f"Admission gate refused net-new mint: {detail} — land "
                    "or dispose an existing plan before proposing more"
                ),
                journal_content=(
                    f"Refused to mint evolution plan '{plan.title}': the "
                    f"admission gate fired ({detail}). Recorded obsolete; "
                    "not submitted. The forward path is to land or dispose "
                    "an existing plan, not manufacture more intentions."
                ),
                triggered_by=f"generation_admission:{trigger}",
            )
            # First-writer-wins: only steer the next self-directed pass when
            # no directive is already pending (a prior gate/bridge owns it).
            model = engine._consciousness.load_self_model()
            if not model.next_task_directive:
                model.next_task_directive = _GENERATION_ADMISSION_DIRECTIVE
                engine._consciousness.save_self_model(model)
            if recorded:
                logger.info(
                    "Admission gate refused plan %r (%s) — recorded "
                    "obsolete, not submitted", plan.plan_id, trigger,
                )
            return

    # Backlog-saturation submit-block — the inverse of every guard above.
    # The duplicate/cluster guards cap *redundant* production (copies of
    # what already sits pending); this caps *total* production regardless
    # of content. Once the open ``pending_review`` backlog reaches
    # ``[loop] pending_plan_submit_block_threshold`` plans, refuse to mint
    # another: the only forward path becomes landing or disposing an
    # existing plan first (``mark_plan_obsolete`` is never blocked — the
    # disposal lever stays open while production is capped). Record the
    # refused candidate ``obsolete`` (a paper trail, never a fresh review
    # burden) and journal the refusal. ``threshold <= 0`` disables the cap.
    submit_block = engine._config.loop_pending_plan_submit_block_threshold
    if submit_block > 0 and not exempt_saturation:
        # Saturation counts every live plan
        # (``pending_review``/``approved``/``implemented``) over the
        # already-loaded list, so in-flight work Thomas approved or that sits
        # behind an open PR also counts toward the cap (proposal 100).
        pending_count = len(select_live_plans(all_plans_full))
        if pending_count >= submit_block:
            # ``pending_count`` tallies live plans (``approved``/
            # ``implemented`` included), so name that corpus honestly rather
            # than asserting a pure ``pending_review`` backlog (proposal 100).
            backlog_label = "Live-work"
            backlog_body = "live plans in flight"
            backlog_name = "live-work"
            recorded = _record_obsolete_duplicate(
                engine,
                plan,
                obsolete_reason=(
                    f"{backlog_label} backlog saturated: {pending_count} "
                    f"{backlog_body} (>= {submit_block} cap) — land or "
                    "dispose existing plans before proposing more"
                ),
                journal_content=(
                    f"Refused to mint evolution plan '{plan.title}': the "
                    f"{backlog_name} backlog is saturated at {pending_count} "
                    f"plans (>= {submit_block} submit-block cap). Recorded "
                    "obsolete; not submitted. The forward path is to land or "
                    "dispose existing plans, not manufacture more intentions."
                ),
                triggered_by=f"submit_block:{pending_count}",
            )
            if recorded:
                logger.info(
                    "Refusing saturated-backlog plan %r: %d pending plans "
                    ">= %d cap — recorded obsolete, not submitted",
                    plan.plan_id, pending_count, submit_block,
                )
            return

    try:
        engine._memory.save_evolution_plan(plan)
    except DuplicatePlanError:
        logger.info(
            "Idempotent retry: plan %r already saved — skipping duplicate save",
            plan.plan_id,
        )
        return
    issue_line = f"\nIssue: #{plan.issue_number}" if plan.issue_number else ""
    engine._journal.record_event(
        content=(
            f"Proposing evolution: {plan.title}\n"
            f"Category: {plan.category}\n"
            f"Priority: {plan.priority}\n"
            f"Rationale: {plan.rationale}\n"
            f"This should: {plan.expected_impact}"
            f"{issue_line}"
        ),
        entry_type="evolution_note",
        triggered_by=f"evolution:{plan.plan_id}",
    )

    # Snapshot the hash the reviewer will see, before the send, so a
    # drifted approval is refused (proposal 021); conditional so a
    # racing human decision is never undone (#4473).
    h = plan.plan_content_hash
    if not engine._memory.update_plan_status(
        plan.plan_id, "pending_review", notified_content_hash=h,
        expect_status="pending_review", expect_content_hash=h,
    ):
        logger.info("Plan %s decided before notify", plan.plan_id)
        return
    try:
        msg = engine._compose_review_message(plan)
        await engine._notifier.notify_plan_for_review(
            plan.plan_id, msg, plan.plan_content_hash,
        )
    except Exception:
        logger.warning("Notification failed", exc_info=True)
