"""The plan review-notification body (proposal 145 extraction).

Sole composer of the review notice ``EvolutionEngine`` sends for a freshly
minted plan, a promoted chain slice, and the curator's refine notice. The
heading carries :func:`maestro.plan_feedback.format_plan_ref` so a Telegram
reply threads back to the plan it reviews.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from maestro.plan_feedback import format_plan_ref

if TYPE_CHECKING:
    from maestro.memory import EvolutionPlan

# Review-notification caps for the rendered files / acceptance-criteria lists.
REVIEW_MSG_MAX_ITEMS = 10
REVIEW_MSG_ITEM_CHARS = 200


def compose_review_message(
    plan: EvolutionPlan, heading: str = "New Evolution Plan",
) -> str:
    """Compose the plan review-notification body.

    Files and acceptance criteria are rendered because
    ``compute_plan_content_hash`` covers them: the buttons approve that hash,
    so every hashed field must be visible (#4146). The body has no overall
    length cap: visibility is enforced at the transport, where the Telegram
    notifier chunks rather than truncates and binds its buttons only to a
    body delivered whole (#4589). Both lists are capped
    (``REVIEW_MSG_MAX_ITEMS`` by ``REVIEW_MSG_ITEM_CHARS``). The
    plan-ref marker sits before the title so a long title cannot push it past
    the lines ``parse_plan_ref`` scans.
    """
    issue_line = (
        f"\n\nAddresses issue #{plan.issue_number}"
        if plan.issue_number else ""
    )

    def _section(label: str, items: list[str]) -> str:
        if not items:
            return ""
        shown = items[:REVIEW_MSG_MAX_ITEMS]
        lines = [f"  - {i[:REVIEW_MSG_ITEM_CHARS]}" for i in shown]
        if len(items) > len(shown):
            lines.append(f"  (+{len(items) - len(shown)} more)")
        return f"\n\n{label}:\n" + "\n".join(lines)

    return (
        f"{heading} {format_plan_ref(plan)}: {plan.title}\n"
        f"Category: {plan.category}"
        f" | Priority: {plan.priority}\n"
        f"Rationale: {plan.rationale}\n"
        f"Impact: {plan.expected_impact}\n\n"
        f"Changes:\n"
        + "\n".join(f"  - {c}" for c in plan.proposed_changes)
        + _section("Files", plan.files_affected)
        + _section("Acceptance criteria", plan.acceptance_criteria)
        + issue_line
        + "\n\nReview on dashboard or use the buttons below."
    )
