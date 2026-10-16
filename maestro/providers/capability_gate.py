"""Startup capability validation for the provider selection seam (058 Step 7).

Phases declare the provider capabilities their loop sessions depend on; the
selected provider declares what it can do (:class:`~maestro.providers.base.Capabilities`).
:func:`validate_provider_capabilities` cross-checks the two at ``cli.main`` time
and **fails loud** when a phase is routed to a provider that lacks a capability
it needs — before the first query, never mid-loop. This is the same fail-loud
doctrine the rest of the loop follows.

The check is inert under today's default (``[sdk] provider = "anthropic"``):
:class:`~maestro.providers.anthropic.AnthropicProvider` advertises every
capability, so validation always passes until an operator routes a phase to a
capability-limited provider (e.g. the OpenAI provider; ``cli_paced`` reached
full parity in proposal 061).

Requirements are grounded in how :mod:`maestro.agent` builds each phase's
session:

* The tool phases are derived from the ``CALL_POLICIES`` surfaces (proposal
  142), never hand-listed: a phase whose callers' *effective* surfaces attach
  MCP servers (:func:`~maestro.tool_surface.mcp_attaching_phases`, so
  ``contemplation`` only while ``grounded_reflection_enabled`` is on) needs
  ``in_process_tools``, plus ``remote_mcp`` when a surface there attaches the
  external servers (:func:`~maestro.tool_surface.external_mcp_phases`) and any
  is configured.
* The action-gated phases are those whose surfaces carry a shell-capable
  built-in (:func:`~maestro.tool_surface.action_gated_phases`).
* ``long_context`` is opt-in: ``implementation`` and ``run_task`` both flip to
  the long-context model when ``long_context_implementation_enabled`` and
  ``sdk_model_long_context`` are set — ``cli.py`` constructs the ``Executor``
  with a non-None ``long_context_model`` under exactly that pair, and the
  executor session backs the ``run_task`` phase. The ``evolution`` phase's
  session never pins ``sdk_model_long_context`` (no ``model=`` override at
  session creation), so it has no long-context capability requirement either.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from maestro.config import SDK_PHASE_NAMES
from maestro.providers.cli_paced_gate import gate_hook_argv, gate_hook_command
from maestro.providers.registry import get_provider
from maestro.tool_surface import (
    action_gated_phases,
    external_mcp_phases,
    mcp_attaching_phases,
)

if TYPE_CHECKING:
    from maestro.config import Config

#: Providers known to supply the #2280 Bash mutation guard, mapped to the
#: enforcement path that carries it. This is a *trust* declaration,
#: deliberately separate from the :class:`Capabilities` flags (proposal 061
#: drew exactly that boundary: "the eight flags describe capability, not
#: trust"). ``anthropic`` forwards ``QueryOptions.hooks`` into the in-process
#: ``PreToolUse`` callback; ``cli_paced`` replicates the same
#: ``evaluate_bash_command`` core into the spawned ``claude`` via an injected
#: ``--settings`` hook (proposal 066). A provider absent here must never be
#: routed to an action-gated phase — extend this map only after wiring (and
#: parity-testing) a gate path for it.
_MUTATION_GUARDS: dict[str, str] = {
    "anthropic": "in-process PreToolUse hook (maestro.tool_gating)",
    "cli_paced": (
        "CLI-injected --settings PreToolUse hook "
        "(maestro.providers.cli_paced_gate)"
    ),
}


def required_capabilities_for_phase(phase: str, config: Config) -> frozenset[str]:
    """Return the :class:`Capabilities` field names *phase* requires under *config*.

    The set is config-aware: ``remote_mcp`` is only required when an external
    MCP server is enabled, and ``long_context`` only when the long-context model
    is configured for a phase that uses it. Phases whose effective surfaces
    attach nothing return an empty set.
    """
    required: set[str] = set()
    # Grounded planning (proposal 143) grounds the evolution phase's ``tree``
    # surface; grounded reflection (proposal 132) grounds every other phase.
    grounded = (
        config.grounded_planning_enabled
        if phase == "evolution"
        else config.grounded_reflection_enabled
    )
    if phase in mcp_attaching_phases(grounded=grounded):
        required.add("in_process_tools")
    if phase in external_mcp_phases(grounded=grounded) and (
        config.context7_mcp_enabled
        or config.serena_mcp_enabled
    ):
        required.add("remote_mcp")
    if (
        phase in {"implementation", "run_task"}
        and config.long_context_implementation_enabled
        and config.sdk_model_long_context
    ):
        required.add("long_context")
    return frozenset(required)


def validate_provider_capabilities(config: Config) -> None:
    """Assert every phase's resolved provider supports the capabilities it needs.

    Iterates the phases in :data:`~maestro.config.SDK_PHASE_NAMES`, resolves each
    one's provider via :meth:`Config.provider_for_phase`, looks up its declared
    :class:`~maestro.providers.base.Capabilities`, and raises :class:`ValueError`
    with a remediation message naming the phase, provider, and missing
    capabilities. An unregistered per-phase provider also raises (via
    :func:`~maestro.providers.registry.get_provider`), keeping the fail-loud
    posture for both gaps. Returns ``None`` when every phase is satisfied.
    """
    for phase in sorted(SDK_PHASE_NAMES):
        provider_name = config.provider_for_phase(phase)
        provider = get_provider(provider_name)
        capabilities = provider.capabilities
        missing = sorted(
            cap
            for cap in required_capabilities_for_phase(phase, config)
            if not getattr(capabilities, cap)
        )
        if missing:
            raise ValueError(
                f"provider {provider_name!r} routed to phase {phase!r} lacks "
                f"required capabilities {missing}; route the phase to a capable "
                f"provider via [sdk] provider or [sdk] {phase}_provider, or "
                f"disable the feature that needs it"
            )

def validate_action_gate_coverage(config: Config) -> None:
    """Assert every action-gated phase routes to a mutation-guard-carrying provider.

    The sibling of :func:`validate_provider_capabilities`, run at ``cli.main``
    time: for each phase in :func:`~maestro.tool_surface.action_gated_phases`, the
    resolved provider must appear in :data:`_MUTATION_GUARDS` — i.e. carry an enforcement path for
    the issue #2280 Bash action-gate. Raises :class:`ValueError` naming the
    phase and the missing guard otherwise, so a hand-rolled (or future) provider
    cannot be routed to a repo-mutating phase un-gated. When a phase routes to
    ``cli_paced`` it additionally checks that the injected hook's executable
    (:func:`~maestro.providers.cli_paced_gate.gate_hook_argv`) resolves: an
    unresolvable hook exits non-zero, which the ``claude`` CLI treats as a
    non-blocking error — fail-open (issue #4160). Inert under the default
    config: ``anthropic`` carries an in-process guard and probes nothing.
    """
    for phase in sorted(action_gated_phases()):
        provider_name = config.provider_for_phase(phase)
        if provider_name not in _MUTATION_GUARDS:
            raise ValueError(
                f"provider {provider_name!r} routed to action-gated phase "
                f"{phase!r} supplies no Bash mutation guard (the issue #2280 "
                f"PreToolUse action-gate); route the phase to a guard-carrying "
                f"provider ({sorted(_MUTATION_GUARDS)}) or wire a gate "
                f"enforcement path for {provider_name!r} before routing it to "
                f"mutating phases"
            )
        if provider_name == "cli_paced":
            argv = gate_hook_argv()
            if shutil.which(argv[0]) is None:
                raise ValueError(
                    f"provider 'cli_paced' routed to action-gated phase "
                    f"{phase!r} cannot launch its Bash action-gate hook "
                    f"{gate_hook_command()!r}: {argv[0]!r} is not an "
                    f"executable; a hook that fails to start fails open"
                )


__all__ = [
    "required_capabilities_for_phase",
    "validate_action_gate_coverage",
    "validate_provider_capabilities",
]
