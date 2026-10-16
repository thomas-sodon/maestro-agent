"""Declarative per-caller SDK call policy (proposal 139).

``CALL_POLICIES`` maps every live ``caller`` tag that reaches
``QueryOptions`` / ``query`` / ``query_on_session`` to the policy that call
declares for itself: effort, named tool surface (a
:class:`~maestro.tool_surface.ToolSurface` — required, no "default toolbelt"),
provider phase and session scope.
Effort is seeded from each call site's stateless-arm ``QueryOptions`` literals — the call's own declared policy —
rather than from whatever a persistent session's opener chose. For
``SINGLE_USE`` callers the tool surface and provider phase are seeded from the
evolution slot (slot B) they run on today, so moving a caller onto
:func:`single_use_call` keeps its tools and provider routing.

:func:`single_use_call` is the registry's first reader: it runs one
``SINGLE_USE`` caller on a session opened for that call alone. The
evolution-lineage callers route through it when
``[sdk] single_use_evolution_calls_enabled`` is on (default-off);
``tests/test_call_policy.py`` keeps the registry complete against the tree.
It refuses an action-gated phase (``tool_surface.action_gated_phases()``) unless the caller
supplies per-call ``options`` carrying the plan's #2280 action-gate hooks, so a
hook-less session there can never bypass the Bash gate.

Two declarations sit beside the registry as data no seam reads yet (proposal
140): ``EFFORT_TIERS`` — the named effort tiers (review ``high``, planning and
refinement ``medium``, everything else ``low``) the seam will resolve to — and
``SESSION_LINEAGES`` — each shared-session opener and the callers whose turns
run on it, so the tests can assert every lineage agrees on one tier effort
and one :func:`~maestro.tool_surface.effective_surface` (proposal 142).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from maestro.prompts.defaults import select_objective_prompt
from maestro.sdk import (
    QueryOptions,
    QueryResult,
    create_session,
    disconnect_session,
    query_on_session,
)
from maestro.tool_surface import (
    REFLECTION_READ_ONLY,
    REPO_MUTATING,
    TOOL_FREE,
    TREE_READ_ONLY,
    ToolSurface,
    action_gated_phases,
    effective_surface,
)


class SessionScope(StrEnum):
    """Whether a caller owns a fresh session or rides a shared one."""

    SINGLE_USE = "single_use"
    SHARED = "shared"


@dataclass(frozen=True)
class CallPolicy:
    """One caller's declared execution policy.

    ``effort=None`` means "provider default" (the call passes no effort), not
    "unknown". ``surface`` is the caller's declared named tool surface;
    :func:`single_use_call` enforces a tool-free one, and the SDK seam derives
    every call's ``tools`` from it under ``[sdk] enforced_tool_surface_enabled``
    (proposal 142).
    """

    effort: str | None
    surface: ToolSurface
    phase: str | None
    scope: SessionScope


_SINGLE = SessionScope.SINGLE_USE
_SHARED = SessionScope.SHARED

CALL_POLICIES: Mapping[str, CallPolicy] = MappingProxyType({
    # Evolution lineage — single-use scope.
    "propose_plan": CallPolicy("high", TREE_READ_ONLY, "evolution", _SINGLE),
    "propose_plan_chain": CallPolicy("high", TREE_READ_ONLY, "evolution", _SINGLE),
    "curate_pending_plans": CallPolicy("high", TREE_READ_ONLY, "evolution", _SINGLE),
    # Shared-session calls.
    "introspect": CallPolicy("medium", REFLECTION_READ_ONLY, None, _SHARED),
    "update_self_model": CallPolicy("high", REFLECTION_READ_ONLY, None, _SHARED),
    "respond_to_messages": CallPolicy("low", TOOL_FREE, "contemplation", _SHARED),
    "planner": CallPolicy("medium", TOOL_FREE, None, _SHARED),
    "replan": CallPolicy("medium", TOOL_FREE, None, _SHARED),
    "replan_for_scope": CallPolicy("low", TOOL_FREE, None, _SHARED),
    "distill_findings": CallPolicy("low", TOOL_FREE, None, _SHARED),
    "assess_criteria_outcome": CallPolicy("low", TOOL_FREE, None, _SHARED),
    "execute_step": CallPolicy(None, REPO_MUTATING, "run_task", _SHARED),
    "implement_plan": CallPolicy(None, REPO_MUTATING, "implementation", _SHARED),
    "self_review": CallPolicy("high", REPO_MUTATING, "implementation", _SINGLE),
    "auto_fix": CallPolicy("high", REPO_MUTATING, "implementation", _SHARED),
    "rejection_classifier": CallPolicy("low", TOOL_FREE, None, _SHARED),
    "generate_task": CallPolicy("low", TOOL_FREE, None, _SHARED),
    "artifact_review": CallPolicy("medium", TOOL_FREE, None, _SHARED),
    "workspace_counterfactual": CallPolicy("low", TOOL_FREE, None, _SHARED),
    "startup_probe": CallPolicy("low", TOOL_FREE, "startup_probe", _SHARED),
    "rate_limit_idle_probe": CallPolicy("low", TOOL_FREE, None, _SHARED),
    "warning_hold_probe": CallPolicy("low", TOOL_FREE, None, _SHARED),
    # Session openers.
    "evolution_session": CallPolicy("high", TOOL_FREE, "evolution", _SHARED),
    "curate_session": CallPolicy("high", TOOL_FREE, "evolution", _SHARED),
    "contemplation_session": CallPolicy(
        "high", REFLECTION_READ_ONLY, "contemplation", _SHARED,
    ),
    "implementation_session": CallPolicy(
        "high", REPO_MUTATING, "implementation", _SHARED,
    ),
    "run_session": CallPolicy("medium", TOOL_FREE, "run_task", _SHARED),
    "task_generation_session": CallPolicy("low", TOOL_FREE, "task_generation", _SHARED),
    "artifact_review_session": CallPolicy(
        "medium", TOOL_FREE, "artifact_review", _SHARED,
    ),
    "rejection_session": CallPolicy("low", TOOL_FREE, "rejection", _SHARED),
    "rejection_self_model_session": CallPolicy("high", TOOL_FREE, "rejection", _SHARED),
})


class EffortTier(StrEnum):
    """A named effort tier; :data:`TIER_EFFORT` maps each to its effort."""

    REVIEW = "review"
    PLANNING = "planning"
    REFINEMENT = "refinement"
    DEFAULT = "default"


TIER_EFFORT: Mapping[EffortTier, str] = MappingProxyType({
    EffortTier.REVIEW: "high",
    EffortTier.PLANNING: "medium",
    EffortTier.REFINEMENT: "medium",
    EffortTier.DEFAULT: "low",
})

#: Members of the non-default tiers; every other registered caller is DEFAULT.
EFFORT_TIERS: Mapping[EffortTier, frozenset[str]] = MappingProxyType({
    EffortTier.REVIEW: frozenset({"self_review"}),
    EffortTier.PLANNING: frozenset({
        "propose_plan", "propose_plan_chain",
    }),
    EffortTier.REFINEMENT: frozenset({
        "curate_pending_plans", "replan_for_scope",
    }),
})

#: Shared-session opener tag → callers whose ``query_on_session`` turns run on
#: that session in production. Invariant: every member runs at its opener's
#: effort (a turn cannot change the effort its session was opened with), so
#: each member's tier effort must equal the opener's. ``evolution_session`` and
#: ``curate_session`` carry no lineage — every caller riding them is
#: ``SINGLE_USE``.
SESSION_LINEAGES: Mapping[str, frozenset[str]] = MappingProxyType({
    "contemplation_session": frozenset({"introspect", "update_self_model"}),
    "implementation_session": frozenset({"implement_plan", "auto_fix"}),
    "run_session": frozenset({
        "planner", "replan", "distill_findings", "assess_criteria_outcome",
    }),
    "rejection_session": frozenset({"rejection_classifier"}),
    "rejection_self_model_session": frozenset({"update_self_model"}),
    "task_generation_session": frozenset({"generate_task"}),
    "artifact_review_session": frozenset({"artifact_review"}),
    "execute_step": frozenset({"execute_step"}),
    "workspace_counterfactual": frozenset({"workspace_counterfactual"}),
})


def tier_of(caller: str) -> EffortTier:
    """Return *caller*'s effort tier; ``KeyError`` for an unregistered tag."""
    if caller not in CALL_POLICIES:
        raise KeyError(caller)
    for tier, members in EFFORT_TIERS.items():
        if caller in members:
            return tier
    return EffortTier.DEFAULT


def tier_effort(caller: str) -> str:
    """Return the effort *caller*'s tier stands for."""
    return TIER_EFFORT[tier_of(caller)]


def evolution_slot_system(system: str) -> str:
    """Prefix *system* with the objective framing slot B's system carries.

    The shared evolution session is created with the objective prompt
    (``select_objective_prompt``) ahead of the identity slab; an
    evolution-lineage caller moved onto its own single-use session keeps that
    framing so the flag changes only the session scope and effort, not the
    prompt.
    """
    return f"{select_objective_prompt()}\n\n{system}"


async def single_use_call(
    caller: str,
    prompt: str,
    *,
    system: str | None,
    output_format: dict[str, Any] | None = None,
    complete_on: Callable[[Any, str], bool] | None = None,
    stream_idle_timeout_seconds: float | None = None,
    options: QueryOptions | None = None,
) -> QueryResult:
    """Run *caller* on a session opened for this one call, then tear it down.

    The session is created with *caller*'s registered policy (effort, tools,
    phase), receives exactly one ``query_on_session`` turn, and
    is disconnected in ``finally``. ``output_format`` is per call because it is
    fixed at session creation and some callers vary it. *options* carries the
    per-call fields a static registry cannot declare (tools, MCP servers,
    ``cwd``, model, betas, hooks); effort, caller and phase stay the
    registry's, and a tool-free policy surface (``tools=[]``) and the
    *output_format* argument silently replace ``options.tools`` /
    ``options.output_format``. Raises ``KeyError`` for an unregistered caller and
    ``ValueError`` for a ``SHARED`` one, for *options* whose ``effort`` or
    ``caller`` disagrees with the policy, or for an action-gated phase without
    ``options.hooks`` — all before any session opens. A disconnect failure
    propagates (fail loud).
    """
    policy = CALL_POLICIES[caller]
    if policy.scope is not SessionScope.SINGLE_USE:
        raise ValueError(f"caller {caller!r} is not single-use")
    if options is not None:
        if options.effort is not None and options.effort != policy.effort:
            raise ValueError(
                f"caller {caller!r} options effort {options.effort!r} "
                f"disagrees with policy effort {policy.effort!r}"
            )
        if options.caller is not None and options.caller != caller:
            raise ValueError(
                f"options caller {options.caller!r} disagrees with {caller!r}"
            )
    if policy.phase in action_gated_phases() and (
        options is None or options.hooks is None
    ):
        raise ValueError(
            f"caller {caller!r} runs in action-gated phase {policy.phase!r} "
            "without action-gate hooks"
        )
    # Only a tool-free surface is enforced here; a built-in-bearing one (the
    # REPO_MUTATING ``self_review``) keeps its caller's ``options.tools``, which
    # the seam derives from the surface under
    # ``[sdk] enforced_tool_surface_enabled`` (proposal 142).
    surface = effective_surface(caller, grounded=False)
    tools: list[str] | None = None if surface.builtins else []
    if options is None:
        session_options = QueryOptions(
            effort=policy.effort,
            tools=tools,
            output_format=output_format,
            caller=caller,
        )
    else:
        session_options = dataclasses.replace(
            options,
            effort=policy.effort,
            output_format=output_format,
            caller=caller,
        )
        if tools is not None:
            session_options.tools = tools
    session = await create_session(
        system=system, options=session_options, phase=policy.phase,
    )
    try:
        return await query_on_session(
            session,
            prompt,
            caller=caller,
            complete_on=complete_on,
            stream_idle_timeout_seconds=stream_idle_timeout_seconds,
        )
    finally:
        await disconnect_session(session)
