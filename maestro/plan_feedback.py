"""Reviewer feedback ledger on pending evolution plans (proposal 145).

A third review decision beside approve and reject: *request changes*. Each
entry is persisted on ``EvolutionPlan.reviewer_feedback`` by
``Memory.add_plan_feedback`` (the single write door, ``pending_review`` only)
and closed by ``Memory.mark_feedback_addressed`` once the curator answers it.
The ledger is bookkeeping, not reviewable content, so it is excluded from
``compute_plan_content_hash``.

Imports only the stdlib and ``maestro.notifications.base`` so
``maestro.memory`` can import this module without a cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, get_args

from maestro.notifications.base import CALLBACK_HASH_PREFIX_LEN

if TYPE_CHECKING:
    from collections.abc import Iterable

    from maestro.memory import EvolutionPlan

FeedbackSource = Literal["dashboard", "telegram"]
FEEDBACK_SOURCES: frozenset[str] = frozenset(get_args(FeedbackSource))

#: Cap on one reviewer feedback entry's text (it later enters the curate prompt).
FEEDBACK_TEXT_MAX_CHARS = 1000
#: Cap on the curator's response stamped onto an addressed entry.
FEEDBACK_RESPONSE_MAX_CHARS = 500
#: Unaddressed entries a plan may hold before the write door refuses more.
MAX_UNADDRESSED_FEEDBACK = 3
#: Addressed entries retained per plan; older ones are pruned on stamp.
MAX_ADDRESSED_FEEDBACK_KEPT = 3


@dataclass
class PlanFeedback:
    """One reviewer feedback entry on a ``pending_review`` plan."""

    feedback_id: str
    text: str
    submitted_at: str = ""
    source: str = "dashboard"
    # Prefix of the plan content hash the reviewer saw, truncated to
    # ``CALLBACK_HASH_PREFIX_LEN`` so both surfaces compare the same way.
    content_hash_seen: str = ""
    curate_attempts: int = 0
    addressed_at: str | None = None
    response: str = ""


def truncate_hash_prefix(content_hash: str) -> str:
    """Return *content_hash* truncated to ``CALLBACK_HASH_PREFIX_LEN``."""
    return content_hash[:CALLBACK_HASH_PREFIX_LEN]


# A review body leads with ``[plan <plan_id>@<hash prefix>]`` so a Telegram
# reply threads back to its plan statelessly (Telegram echoes the replied-to
# text). Only the first two non-blank lines are scanned: the curator's refine
# notice puts one line above the composed body, and ``_truncate`` cuts from the
# end.
_PLAN_REF_RE = re.compile(
    rf"\[plan ([A-Za-z0-9_-]+)@([0-9a-f]{{{CALLBACK_HASH_PREFIX_LEN}}})\]",
)
_PLAN_REF_SCAN_LINES = 2


@dataclass(frozen=True)
class PlanRef:
    """The plan a replied-to review body names, and the hash prefix it showed."""

    plan_id: str
    hash_prefix: str


def format_plan_ref_marker(plan_id: str, content_hash: str) -> str:
    """Render the ``[plan <id>@<prefix>]`` marker for *plan_id* at *content_hash*.

    The sole owner of the marker format :data:`_PLAN_REF_RE` parses, so the
    Telegram notifier can repeat it on continuation chunks without a plan row.
    """
    return f"[plan {plan_id}@{truncate_hash_prefix(content_hash)}]"


def format_plan_ref(plan: EvolutionPlan) -> str:
    """Render the review-body marker binding a reply to *plan*'s content hash."""
    return format_plan_ref_marker(plan.plan_id, plan.plan_content_hash)


def parse_plan_ref(text: object) -> PlanRef | None:
    """Return the :class:`PlanRef` a replied-to text carries, else ``None``."""
    if not isinstance(text, str):
        return None
    lines = [line for line in text.splitlines() if line.strip()]
    for line in lines[:_PLAN_REF_SCAN_LINES]:
        match = _PLAN_REF_RE.search(line)
        if match:
            return PlanRef(plan_id=match.group(1), hash_prefix=match.group(2))
    return None


def _feedback_from_dict(item: dict[str, object]) -> PlanFeedback:
    submitted_at = item.get("submitted_at", "")
    source = item.get("source", "dashboard")
    seen = item.get("content_hash_seen", "")
    attempts = item.get("curate_attempts", 0)
    if isinstance(attempts, float) and attempts.is_integer():
        attempts = int(attempts)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        attempts = 0
    addressed_at = item.get("addressed_at")
    response = item.get("response", "")
    return PlanFeedback(
        feedback_id=str(item["feedback_id"]),
        text=str(item["text"]),
        submitted_at=submitted_at if isinstance(submitted_at, str) else "",
        source=(
            source
            if isinstance(source, str) and source in FEEDBACK_SOURCES
            else "dashboard"
        ),
        content_hash_seen=(
            truncate_hash_prefix(seen) if isinstance(seen, str) else ""
        ),
        curate_attempts=attempts,
        addressed_at=addressed_at if isinstance(addressed_at, str) and addressed_at else None,
        response=response if isinstance(response, str) else "",
    )


def coerce_feedback(raw: object) -> list[PlanFeedback]:
    """Coerce a stored ``reviewer_feedback`` value into ``list[PlanFeedback]``.

    A non-list yields ``[]``; a non-dict item, or one without a string
    ``feedback_id`` and ``text``, is dropped; wrong-typed scalar fields fall
    back to their defaults and an integral float ``curate_attempts`` is
    coerced to ``int``.
    """
    if not isinstance(raw, list):
        return []
    return [
        _feedback_from_dict(item)
        for item in raw
        if isinstance(item, dict)
        and isinstance(item.get("feedback_id"), str)
        and isinstance(item.get("text"), str)
    ]


def unaddressed_feedback(plan: EvolutionPlan) -> list[PlanFeedback]:
    """Return *plan*'s entries the curator has not yet addressed."""
    return [f for f in plan.reviewer_feedback if f.addressed_at is None]


def has_unaddressed_feedback(plan: EvolutionPlan) -> bool:
    """True when *plan* holds at least one unaddressed feedback entry."""
    return any(f.addressed_at is None for f in plan.reviewer_feedback)


def any_pending_feedback(pending_plans: Iterable[EvolutionPlan]) -> bool:
    """True when some ``pending_review`` plan holds unaddressed feedback.

    Feedback left on a plan since approved, rejected or obsoleted is inert and
    never arms the off-cadence curate trigger, even if a caller passes such a
    plan in.
    """
    return any(
        p.status == "pending_review" and has_unaddressed_feedback(p)
        for p in pending_plans
    )


#: Curate passes an unaddressed entry may survive before it is released.
MAX_FEEDBACK_CURATE_ATTEMPTS = 3
#: Response stamped on feedback the curator could not act on after the budget.
FEEDBACK_UNSERVICEABLE_RESPONSE = (
    "Maestro could not apply this feedback — approve, reject, or rephrase it"
)
#: Upper bound on one rendered entry's header (bullet, timestamp, stale marker,
#: indent, join newline) beyond its text, so the block cap never clips a text.
_FEEDBACK_ENTRY_OVERHEAD_CHARS = 160
#: Hard cap on the rendered entries block of the curate feedback section.
FEEDBACK_ENTRIES_MAX_CHARS = (
    FEEDBACK_TEXT_MAX_CHARS + _FEEDBACK_ENTRY_OVERHEAD_CHARS
) * MAX_UNADDRESSED_FEEDBACK


def _render_entry(plan: EvolutionPlan, entry: PlanFeedback) -> str:
    stale = bool(entry.content_hash_seen) and not plan.plan_content_hash.startswith(
        entry.content_hash_seen,
    )
    marker = " (written against an earlier version of this plan)" if stale else ""
    return (
        f"  - submitted_at: {entry.submitted_at[:64] or '(unknown)'}{marker}\n"
        f"    {entry.text[:FEEDBACK_TEXT_MAX_CHARS]}"
    )


def render_feedback_section(
    plan: EvolutionPlan, entries: list[PlanFeedback],
) -> str:
    """Render the curate prompt's reviewer-feedback section for *entries*.

    A stale entry (its ``content_hash_seen`` no longer prefixes the plan's
    hash) is flagged, never dropped. The entries block is hard-capped at
    ``FEEDBACK_ENTRIES_MAX_CHARS``. The template is imported lazily because
    the prompts module imports ``maestro.memory``, which imports this one.
    """
    from maestro.prompts.curate_plan import CURATE_FEEDBACK_SECTION

    block = "\n".join(
        _render_entry(plan, e) for e in entries[:MAX_UNADDRESSED_FEEDBACK]
    )[:FEEDBACK_ENTRIES_MAX_CHARS]
    return CURATE_FEEDBACK_SECTION.format(
        entries=block, response_max_chars=FEEDBACK_RESPONSE_MAX_CHARS,
    )
