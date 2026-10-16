"""Plan-field prompt fragments and their ``output_format`` schema properties.

Appended by :class:`maestro.evolution.EvolutionEngine` only on the branch whose
flag or input asks for the field, so every other prompt and schema stays
byte-identical; kept here so ``evolution.py`` stays under its shrink-only size
pin.
"""

from __future__ import annotations

from typing import Any, get_args

from maestro import plan_grounding

#: Prompt fragment appended to :meth:`EvolutionEngine.propose_plan` when
#: ``[loop] plan_grounding_enabled`` is on (proposal 114). Instructs the model to
#: attach a closed ``grounding`` probe for a plan premised on a concrete defect
#: (``DEFECT_PRESENT``) or a behavior that does not yet exist at a named site
#: (``BEHAVIOR_ABSENT``) so
#: :meth:`EvolutionEngine.submit_for_review` can confirm the premise against a
#: fresh reading of the tree before a human approval is spent. *Appended* (not
#: interpolated) so the flag-off prompt stays byte-identical to today; begins
#: with a blank-line gap of its own.
_PLAN_GROUNDING_PROMPT = """

PLAN-PREMISE GROUNDING: If this plan's premise is a checkable fact about the
tree, attach a "grounding" probe so it can be confirmed against a fresh reading
of the code before it reaches review. Use it in exactly four cases:
- The plan proposes to BUILD a named new mechanism (a gate, sensor, actuator,
  function, or class) — set {"kind": "BEHAVIOR_ABSENT", "locator": "<the path,
  relative to the repo root, of an EXISTING file the behavior would live in>",
  "expectation": "<a short literal substring that would already be present in that
  file if the behavior were implemented>"}. The premise is that nothing at this
  site already does this; if the witness is already there, the mechanism already
  ships (under whatever name) and the plan is a duplicate. Both fields are
  required, and the locator must be a file that exists today — a probe naming a
  file that cannot be read is discarded unevaluated, not treated as a pass.
  Use this for every BUILD plan: the name you intend to create is absent by
  construction, so checking the name proves nothing.
- Use SYMBOL_ABSENT only when the plan names an EXISTING symbol it intends to
  create-or-redefine (you believe it is gone, or you want it to fail if it is
  not) — set {"kind": "SYMBOL_ABSENT", "locator": "<the exact symbol name>",
  "expectation": ""}. The premise is that the symbol does not yet exist; if it is
  already defined, the plan is a duplicate.
- The plan proposes to FIX a concrete defect in one named file — set {"kind":
  "DEFECT_PRESENT", "locator": "<the file path>", "expectation": "<a short
  literal substring that must be present in that file for the defect to exist>"}.
- The plan proposes to FIX a ruff lint error in one named file — set {"kind":
  "LINT_PRESENT", "locator": "<the file path>", "expectation": "<the ruff rule
  code, e.g. RUF036>"}. The premise is that ruff reports that rule on that file;
  if ruff runs clean, the lint error is hallucinated.
Omit "grounding" (or set it null) for open-ended feature work whose premise is
not a single checkable symbol/needle/rule. Add it as a top-level "grounding" key
in the JSON reply."""

#: The optional ``grounding`` JSON-schema property added to
#: :meth:`EvolutionEngine.propose_plan`'s ``output_format`` only when grounding
#: is active (proposal 114), so the flag-off schema is byte-identical. A closed
#: ``kind`` enum plus a required ``locator``; ``expectation`` is optional (unused
#: by ``SYMBOL_ABSENT``). Nullable so the model can decline a probe for
#: open-ended work.
_GROUNDING_SCHEMA_PROPERTY: dict[str, Any] = {
    "type": ["object", "null"],
    "properties": {
        "kind": {
            "type": "string",
            "enum": list(get_args(plan_grounding.GroundingKind)),
        },
        "locator": {"type": "string"},
        "expectation": {"type": "string"},
    },
    "required": ["kind", "locator"],
}

#: Prompt fragment appended to :meth:`EvolutionEngine.propose_plan` when
#: ``[loop] generator_reality_grounding_enabled`` is on (proposal 136). Asks the
#: model only for the names it will newly define, so
#: :meth:`EvolutionEngine.submit_for_review` can clear them against the reality
#: inventory; a gap plan's problem is its issue, so no ``problem_ref`` is asked
#: for (proposal 155 step 4). Begins with a blank-line gap of its own.
_REALITY_NAMES_FIELDS_PROMPT = """

REALITY GROUNDING: Declare one more top-level key in the JSON reply.
- "new_capability_names": the exact module-level public names this plan will
  newly DEFINE — top-level functions, classes, constants, CLI subcommands,
  config flags, or gate names (never methods, attributes, or dunders) — or []
  when it defines none. Declare a new CLI subcommand as "cli:<subcommand>"
  (e.g. "cli:my-report"). A name that already ships is refused as a duplicate,
  so list only names that do not exist yet."""

#: The optional ``new_capability_names`` JSON-schema property added to
#: :meth:`EvolutionEngine.propose_plan`'s ``output_format`` only when reality
#: grounding is active (proposal 136). Nullable and never required.
_REALITY_NAMES_SCHEMA_PROPERTIES: dict[str, Any] = {
    "new_capability_names": {
        "type": ["array", "null"], "items": {"type": "string"},
    },
}

#: Advisory conscious-access citation instruction (proposal 130) appended to
#: :meth:`EvolutionEngine.propose_plan`'s prompt only when a broadcast moment is
#: delivered (``workspace_moment`` non-empty), so the flag-off prompt stays
#: byte-identical. It asks the model to declare which admitted item ids (rendered
#: as ``wi-...`` handles in the moment block) actually informed the plan; a
#: cheap in-producer resolve filter later drops any id not in the admitted set,
#: so a hallucinated id is never persisted. The citation grants no capability and
#: gates nothing.
_WORKSPACE_CITATION_PROMPT = """

CONSCIOUS-ACCESS CITATION: The conscious-moment block above lists admitted items,
each line prefixed with its `wi-...` id. If any of those items informed this
plan, add a "workspace_item_ids" array to your JSON naming ONLY the ids you
actually used, copied exactly. Cite nothing you did not use; use an empty array
(or omit the field) if no admitted item informed the plan. Never invent an id."""

#: The optional ``workspace_item_ids`` JSON-schema property added to
#: :meth:`EvolutionEngine.propose_plan`'s ``output_format`` only when a moment is
#: broadcast (proposal 130), so the flag-off schema is byte-identical. Advisory
#: provenance, never required.
_WORKSPACE_ITEM_IDS_SCHEMA_PROPERTY: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string"},
}
