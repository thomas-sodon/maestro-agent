"""Prompt for the ``curate_pending_plans`` loop phase (proposal 060).

Consumed by the ``curate_pending_plans`` phase
(:meth:`maestro.evolution.EvolutionEngine.curate_pending_plans`) through
:func:`render_curate_prompt`. The phase selects the oldest unrefined
``pending_review`` plan and asks the
model to either *sharpen* its body (``rationale`` / ``proposed_changes`` /
``acceptance_criteria`` / ``files_affected``) so it is easier for the human
reviewer to judge, or *recommend dropping* a plan that has gone stale or is no longer worth doing.
A ``keep`` verdict leaves the plan untouched.

The verdict is applied through the already-validated write boundary
(:meth:`Memory.refine_plan` / :meth:`Memory.mark_plan_obsolete`), which
re-enforces the field caps below — but the prompt states the caps up front so
a confused model does not waste a turn producing an over-long body that the
write boundary would only reject.

The prompt is rendered via :meth:`str.format`. Literal braces in the JSON
examples are doubled (``{{`` / ``}}``) so only the named placeholders are
substituted: ``plan_id``, ``title``, ``created_at``, ``rationale``,
``proposed_changes``, ``acceptance_criteria``, ``files_affected``, and the
cap values ``rationale_max_chars`` / ``change_item_max_chars`` /
``file_path_max_chars`` / ``files_affected_max_items`` /
``obsolete_reason_max_chars`` (rendered from the ``maestro.memory`` constants)
and ``acceptance_criteria_max_items`` / ``acceptance_criteria_item_max_chars``
(rendered from ``ACCEPTANCE_CRITERIA_MAX_ITEMS`` /
``ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS``) so the prompt never drifts from the
enforced limits.

Those caps bound the model's *output*; the plan fields substituted into the
prompt are bounded separately by an input budget (#4476).
:func:`curate_input_fits` admits a plan only when every substituted field is
within its write-boundary cap (catching legacy rows saved before the caps),
``proposed_changes`` holds at most ``CURATE_PROPOSED_CHANGES_MAX_ITEMS`` items,
and the summed field length is at most ``CURATE_PLAN_INPUT_MAX_CHARS``.
Truncating instead would be unsafe — a ``refine`` replaces
``proposed_changes`` and an ``obsolete`` drops the plan, so the model must
never judge content it did not see. An over-budget plan is therefore never
curated: :func:`maestro.audit_checks.select_curation_target` skips it and
:func:`render_curate_prompt` refuses it, leaving it to the human reviewer.
The stored list itself is never capped by count, because the CLI-grounding
and plan-scope guards read it whole.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from maestro.memory import (
    _OBSOLETE_REASON_MAX_CHARS,
    _PLAN_CHANGE_ITEM_MAX_CHARS,
    _PLAN_FILE_PATH_MAX_CHARS,
    _PLAN_FILES_AFFECTED_MAX_ITEMS,
    _PLAN_RATIONALE_MAX_CHARS,
)
from maestro.types import (
    ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS,
    ACCEPTANCE_CRITERIA_MAX_ITEMS,
)

if TYPE_CHECKING:
    from maestro.memory import EvolutionPlan

#: Most ``proposed_changes`` items a plan may carry and still be curated.
CURATE_PROPOSED_CHANGES_MAX_ITEMS = 20

#: Allowance for the uncapped ``title`` within the input budget.
_CURATE_TITLE_ALLOWANCE_CHARS = 500

#: Allowance for the ``"- "`` prefixes and newlines joining list items.
_CURATE_LINE_PREFIX_ALLOWANCE_CHARS = 3 * (
    CURATE_PROPOSED_CHANGES_MAX_ITEMS
    + ACCEPTANCE_CRITERIA_MAX_ITEMS
    + _PLAN_FILES_AFFECTED_MAX_ITEMS
)

#: Ceiling on the summed length of the plan fields substituted into the
#: prompt, derived from the write-boundary caps so it never drifts from them.
CURATE_PLAN_INPUT_MAX_CHARS = (
    _CURATE_TITLE_ALLOWANCE_CHARS
    + _PLAN_RATIONALE_MAX_CHARS
    + CURATE_PROPOSED_CHANGES_MAX_ITEMS * _PLAN_CHANGE_ITEM_MAX_CHARS
    + ACCEPTANCE_CRITERIA_MAX_ITEMS * ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS
    + _PLAN_FILES_AFFECTED_MAX_ITEMS * _PLAN_FILE_PATH_MAX_CHARS
    + _CURATE_LINE_PREFIX_ALLOWANCE_CHARS
)

CURATE_PLAN_PROMPT = """\
You are curating a single pending evolution plan that is awaiting human
review. Your job is to make the human reviewer's decision easier: either
SHARPEN the plan so its intent and acceptance criteria are clear and
testable, or recommend DROPPING it if it has gone stale, was overtaken by
other work, or is no longer worth doing. If the plan is already clear and
still worth doing, leave it untouched.

Do NOT approve, reject, or implement the plan — a human still gives the
final Approve/Reject after any refinement. You only improve the wording or
recommend removal.

Plan under review
=================

  plan_id: {plan_id}
  title: {title}
  created_at: {created_at}

  rationale:
  {rationale}

  proposed_changes:
  {proposed_changes}

  files_affected:
  {files_affected}

  acceptance_criteria:
  {acceptance_criteria}

How to decide
=============

  - "refine": the plan is worth keeping but its body can be made clearer,
    tighter, or more testable. Return ONLY the fields you actually changed
    — omit a field to leave it exactly as-is. A refine MUST change at least
    one field; if nothing needs changing, use "keep" instead. Refinement is
    a REPLACE, not an append, and never a wipe: any field you return must be
    non-empty and must preserve the plan's original intent. Do not broaden
    the scope. Keep ``files_affected`` consistent with the paths
    ``proposed_changes`` names: if a refine adds or drops a file, return the
    corrected ``files_affected`` too.
  - "obsolete": the plan should be dropped — it is stale, redundant, already
    done, or no longer worth the effort. Provide a one-line ``reason``.
  - "keep": the plan is already clear and still worth doing. Change nothing.
    Prefer this when in doubt — needless rewrites add review noise.

Hard caps and constraints
=========================

  - ``rationale`` MUST be at most {rationale_max_chars} characters.
  - Each ``proposed_changes`` item MUST be at most {change_item_max_chars}
    characters.
  - Each ``acceptance_criteria`` item MUST be at most
    {acceptance_criteria_item_max_chars} characters.
  - Each ``files_affected`` item MUST be a repo-relative path of at most
    {file_path_max_chars} characters, and the list MUST contain at most
    {files_affected_max_items} items.
  - ``reason`` (for "obsolete") MUST be at most {obsolete_reason_max_chars}
    characters.
  - ``proposed_changes``, ``acceptance_criteria`` and ``files_affected``, if
    returned, must be non-empty lists of non-blank strings.
  - ``acceptance_criteria``, if returned, MUST contain at most
    {acceptance_criteria_max_items} items — the schema binds 1-{acceptance_criteria_max_items} testable
    invariants, and a longer list is rejected by the write boundary.
  - Do NOT include raw secrets, credentials, or absolute filesystem paths.

Output schema
=============

Return a single JSON object. The object MUST include:

  - ``"action"``: exactly one of "refine", "obsolete", "keep".

If ``"action"`` is "refine", include ONLY the changed fields among:

  - ``"rationale"``: the rewritten rationale (string).
  - ``"proposed_changes"``: the rewritten changes (list of strings).
  - ``"acceptance_criteria"``: the rewritten criteria (list of strings).
  - ``"files_affected"``: the corrected repo-relative paths (list of strings).

If ``"action"`` is "obsolete", also include:

  - ``"reason"``: a one-line explanation of why to drop the plan.

If ``"action"`` is "keep", include no other fields.

Worked example (refine — sharpen the criteria only)
===================================================

Output: {{"action": "refine", "acceptance_criteria": ["A unit test asserts foo() returns the cached value on the second call.", "ruff, mypy, and pytest all pass with coverage at 100%."]}}

Worked example (obsolete)
=========================

Output: {{"action": "obsolete",
         "reason": "Superseded by the merged cache layer; no longer needed."}}

Worked example (keep)
=====================

Output: {{"action": "keep"}}

Return a single JSON object. No trailing commentary.
"""


CURATE_FEEDBACK_SECTION = """

Reviewer feedback — address it
==============================

The human reviewer asked for changes to this plan. Their feedback below
OVERRIDES the "prefer keep" and "do not broaden the scope" guidance above,
but only as far as the feedback asks. "obsolete" is NOT allowed for this
plan — refine it to address the feedback, or keep it and explain why.

{entries}

Every verdict for this plan MUST also include ``"response"``: a non-empty
string of at most {response_max_chars} characters answering the reviewer
(what you changed, or why you kept the plan as-is).

Worked example (refine answering feedback)
==========================================

Output: {{"action": "refine", "acceptance_criteria": ["A unit test asserts the retry stops after three attempts."], "response": "Narrowed the criteria to the retry bound you asked about."}}
"""


def curate_input_fits(plan: EvolutionPlan) -> bool:
    """Whether *plan* fits the curation input budget (#4476).

    Pure and zero-cost. ``False`` when any substituted field exceeds its
    write-boundary cap, ``proposed_changes`` exceeds
    ``CURATE_PROPOSED_CHANGES_MAX_ITEMS``, or the summed field length
    exceeds ``CURATE_PLAN_INPUT_MAX_CHARS``. ``files_affected`` carries no
    per-field gate — creation leaves it uncapped, and excluding broad-scope
    plans would strand the scope-narrowing feedback they attract — so it is
    bounded by the summed length alone.
    """
    if len(plan.rationale) > _PLAN_RATIONALE_MAX_CHARS:
        return False
    if len(plan.proposed_changes) > CURATE_PROPOSED_CHANGES_MAX_ITEMS:
        return False
    if any(len(c) > _PLAN_CHANGE_ITEM_MAX_CHARS for c in plan.proposed_changes):
        return False
    if any(
        len(c) > ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS
        for c in plan.acceptance_criteria
    ):
        return False
    total = (
        len(plan.title)
        + len(plan.rationale)
        + sum(len(c) + 3 for c in plan.proposed_changes)
        + sum(len(c) + 3 for c in plan.acceptance_criteria)
        + sum(len(f) + 3 for f in plan.files_affected)
    )
    return total <= CURATE_PLAN_INPUT_MAX_CHARS


def render_curate_prompt(plan: EvolutionPlan) -> str:
    """Render ``CURATE_PLAN_PROMPT`` for *plan* with the enforced caps.

    Raises :class:`ValueError` when *plan* does not fit the curation input
    budget (:func:`curate_input_fits`), so no caller can send it unbounded.
    """
    if not curate_input_fits(plan):
        raise ValueError(
            f"plan {plan.plan_id} exceeds the curation input budget",
        )
    return CURATE_PLAN_PROMPT.format(
        plan_id=plan.plan_id,
        title=plan.title,
        created_at=plan.created_at,
        rationale=plan.rationale or "(none)",
        proposed_changes=(
            "\n".join(f"- {c}" for c in plan.proposed_changes) or "(none)"
        ),
        acceptance_criteria=(
            "\n".join(f"- {c}" for c in plan.acceptance_criteria)
            or "(none)"
        ),
        files_affected=(
            "\n".join(f"- {f}" for f in plan.files_affected) or "(none)"
        ),
        file_path_max_chars=_PLAN_FILE_PATH_MAX_CHARS,
        files_affected_max_items=_PLAN_FILES_AFFECTED_MAX_ITEMS,
        rationale_max_chars=_PLAN_RATIONALE_MAX_CHARS,
        change_item_max_chars=_PLAN_CHANGE_ITEM_MAX_CHARS,
        acceptance_criteria_item_max_chars=ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS,
        obsolete_reason_max_chars=_OBSOLETE_REASON_MAX_CHARS,
        acceptance_criteria_max_items=ACCEPTANCE_CRITERIA_MAX_ITEMS,
    )
