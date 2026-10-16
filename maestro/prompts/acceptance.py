"""Shared renderer for an ``EvolutionPlan``'s acceptance-criteria block.

An ``EvolutionPlan``'s ``acceptance_criteria`` are its binding success
contract — the up-to-``ACCEPTANCE_CRITERIA_MAX_ITEMS`` testable invariants ``EvolutionEngine.propose_plan``
extracts (and ``validate_success_criteria`` prunes for non-vacuity) before
any code is written. That contract is surfaced at several points across the
branch/PR lifecycle, and each rendering used to be hand-rolled at its call
site with a slightly different shape, free to drift. This module owns the
one rendering so the contract reads identically everywhere it appears.

:func:`format_acceptance_criteria_block` reproduces four shapes, selected by
*style*:

- ``"binding"`` — the self-review block (``Evolution.self_review``): a
  ``## Acceptance Criteria (binding)`` header, the review-only "cite the
  hunk + cite the test … blocking defect — fix it before LGTM" contract,
  then a 1-based numbered list.
- ``"writing"`` — the code-writing phases (``Evolution.implement`` and
  ``Evolution._run_auto_fix_query``): the same header and 1-based numbered
  list, but a writing-phase contract ("implement each criterion and add a
  failing test") in place of the review-only language.
- ``"plain"`` — the PR body (``Evolution.create_pr``): a
  ``### Acceptance Criteria`` header (with its leading blank line) followed
  by a flat ``- `` list, no trailing newline.
- ``"remediation"`` — the PR-remediation prompt
  (``maestro.loop.pr_remediation._compose_pr_remediation_prompt``): a
  header-less ``- Acceptance criteria:`` label followed by a
  2-space-indented ``  - `` sub-list, joined into one string.

The function returns ``""`` for an empty *criteria* list, so callers may
append the result unconditionally. It consumes
``EvolutionPlan.acceptance_criteria`` and produces only text — the
extraction, validation, and outcome gating of the criteria live elsewhere
and are untouched. ``maestro/prompts/`` modules are import leaves, so both
``evolution.py`` and ``loop/pr_remediation.py`` import this with no cycle.
"""

from __future__ import annotations

from typing import Literal

from maestro.types import validate_acceptance_criteria_count

AcceptanceCriteriaStyle = Literal["binding", "writing", "plain", "remediation"]


def format_acceptance_criteria_block(
    criteria: list[str],
    *,
    style: AcceptanceCriteriaStyle,
) -> str:
    """Render *criteria* as an acceptance-criteria block in the given *style*.

    Returns ``""`` when *criteria* is empty so callers can append the result
    without a guard. See the module docstring for the four shapes.

    Raises :class:`ValueError` when *criteria* exceeds
    ``ACCEPTANCE_CRITERIA_MAX_ITEMS``. This is the single choke that makes
    "no over-cap criteria list reaches a prompt" provable: a legacy row the
    loader admitted intact fails here, at the moment it would spend context,
    rather than quietly bloating four prompt renderings.
    """
    if not criteria:
        return ""
    validate_acceptance_criteria_count(criteria)

    if style in ("binding", "writing"):
        numbered = "\n".join(
            f"{i}. {c}" for i, c in enumerate(criteria, start=1)
        )
        if style == "binding":
            contract = (
                "For each item below, you MUST cite (a) the diff hunk"
                " implementing it and (b) the test asserting it. Any"
                " criterion missing either citation is a blocking defect"
                " — fix it before responding LGTM."
            )
        else:
            contract = (
                "Implement each criterion below and add a test that would"
                " fail if it were violated; these are the binding success"
                " contract this change will be judged against."
            )
        return (
            "\n## Acceptance Criteria (binding)\n"
            f"{contract}\n"
            f"{numbered}\n"
        )

    if style == "plain":
        return (
            "\n\n### Acceptance Criteria\n"
            + "\n".join(f"- {c}" for c in criteria)
        )

    # style == "remediation"
    lines = ["- Acceptance criteria:"]
    lines.extend(f"  - {c}" for c in criteria)
    return "\n".join(lines)
