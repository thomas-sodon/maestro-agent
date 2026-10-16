"""Tool-surface drift sensor (proposal 142, step 2).

Every SDK call runs under ``bypassPermissions``, where ``allowed_tools`` is an
auto-approve list that restricts nothing. So a caller's *declared* built-ins can
diverge from what the CLI *advertised* and what the model *executed*. This
module measures that gap per caller over transcript sidecars. It is pure over
:class:`~maestro.transcript.TranscriptRecord` values: no I/O and no SDK.
It is the baseline later enforcement work flips against. The module also owns
the registry-derived phase sets (:func:`action_gated_phases`,
:func:`mcp_attaching_phases`, :func:`external_mcp_phases`) the startup
capability checks read, and the action-gate hook matcher
(:func:`gate_hook_matcher`) both providers install at runtime.

Definitions:

- **declared built-ins** — :data:`TASK_TOOL_BUILTINS` for the repo-mutating
  callers, else the built-ins of the registry caller's ungrounded
  :func:`effective_surface` (``frozenset()`` for a tool-free caller), and
  ``None`` (undeclared → the CLI's default toolbelt) for an unregistered tag.
- **advertised-minus-declared** — built-in names in ``tools_advertised`` that are
  not declared. A sidecar with ``tools_advertised is None`` is counted
  advertised-unknown instead.
- **off-surface executed** — ``tool_call`` turns naming a built-in outside the
  declared set (every built-in, for an undeclared caller), minus the calls the
  CLI refused (``No such tool available: <Name>``), which are counted separately.
- **declared MCP servers** — the servers of the registry caller's *grounded*
  :func:`effective_surface` (a grounded ``introspect`` legitimately carries the
  reflection servers), ``None`` for an unregistered tag.
- **advertised-undeclared servers** — names in ``mcp_servers_advertised`` that
  are not declared (every name, for an unregistered caller). An
  ``external_mcp`` surface admits any server: the external set is
  config-dependent and this pure sensor takes no ``Config``, so the permissive
  reading is chosen. ``mcp_servers_advertised is None`` counts as
  advertised-unknown instead.

``mcp__*`` names are excluded from the built-in figures (per-server attachment
is the MCP grant, measured by the server figures above), and so
is the vendor-injected ``StructuredOutput``. Sidecars stamped
``caller == "unknown"`` are skipped.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING

from maestro.transcript import TranscriptRecord, TranscriptTurn
from maestro.tree_retrieval import TREE_SERVER_NAME

if TYPE_CHECKING:
    from maestro.config import Config
    from maestro.sdk import QueryOptions

#: The six built-ins the repo-mutating call sites list in ``allowed_tools``.
TASK_TOOL_BUILTINS: frozenset[str] = frozenset({
    "Read", "Write", "Edit", "Bash", "Glob", "Grep",
})

#: Callers whose call sites declare :data:`TASK_TOOL_BUILTINS`.
DECLARED_TASK_TOOL_CALLERS: frozenset[str] = frozenset({
    "implement_plan", "implementation_session", "self_review", "auto_fix",
    "execute_step",
})

_EXCLUDED_TOOLS: frozenset[str] = frozenset({"StructuredOutput"})
_MCP_PREFIX = "mcp__"
_UNKNOWN_CALLER = "unknown"

# ``sdk.py`` writes a ``tool_call`` turn as ``"<Name> input=<repr>"``.
_TOOL_CALL_RE = re.compile(r"^([A-Za-z_][\w-]*) input=")
# ``sdk.py`` prefixes an ``is_error`` result with ``"ERROR "``; a non-error
# result merely quoting the refusal string (a Read of this proposal) is not one.
_ERROR_PREFIX = "ERROR "
_REFUSAL_RE = re.compile(r"No such tool available: ([A-Za-z_][\w-]*)")


# ── Named surface vocabulary (proposal 142, step 3) ─────────────────────────
# Pure data plus pure resolvers; the SDK seam reads ``resolve_call_options``
# under ``[sdk] enforced_tool_surface_enabled``. ``call_policy`` imports
# this vocabulary, so ``CALL_POLICIES`` is imported inside the functions that
# read it (``effective_surface``, ``declared_builtins``), never at module level.


@dataclass(frozen=True)
class ToolSurface:
    """A named tool surface: base built-ins plus the MCP servers it attaches."""

    builtins: frozenset[str]
    mcp_servers: frozenset[str]
    external_mcp: bool


_INTROSPECTION_SERVERS: frozenset[str] = frozenset({
    "journal", "self_awareness", "memory", "issues", "diagnostics",
})

TOOL_FREE = ToolSurface(frozenset(), frozenset(), external_mcp=False)
REFLECTION_READ_ONLY = ToolSurface(
    frozenset(), _INTROSPECTION_SERVERS, external_mcp=False,
)
#: The planner's bounded read-only tree surface (proposal 143): no built-ins,
#: only the in-process ``tree`` server.
TREE_READ_ONLY = ToolSurface(
    frozenset(), frozenset({TREE_SERVER_NAME}), external_mcp=False,
)
REPO_MUTATING = ToolSurface(
    TASK_TOOL_BUILTINS | {"TaskOutput", "TaskStop"},
    _INTROSPECTION_SERVERS | {"messaging", "maestro_email", "maestro_telegram"},
    external_mcp=True,
)

#: The closed set of named surfaces.
NAMED_SURFACES: Mapping[str, ToolSurface] = MappingProxyType({
    "tool_free": TOOL_FREE,
    "reflection_read_only": REFLECTION_READ_ONLY,
    "tree_read_only": TREE_READ_ONLY,
    "repo_mutating": REPO_MUTATING,
})

#: Every built-in that can start a process.
SHELL_CAPABLE_BUILTINS: frozenset[str] = frozenset({"Bash", "Monitor"})
#: What both providers' action-gate hook matchers inspect (:func:`gate_hook_matcher`).
GATE_MATCHED_TOOLS: frozenset[str] = frozenset({"Bash"})

#: Declared surfaces whose grant is a runtime fact: ungrounded, they are tool-free.
RUNTIME_GROUNDED_SURFACES: frozenset[ToolSurface] = frozenset({
    REFLECTION_READ_ONLY, TREE_READ_ONLY,
})


def effective_surface(caller: str, *, grounded: bool) -> ToolSurface:
    """Return *caller*'s surface in effect; ``KeyError`` for an unregistered tag.

    A declared surface in :data:`RUNTIME_GROUNDED_SURFACES` resolves to
    :data:`TOOL_FREE` unless *grounded*; every other surface is itself.
    """
    from maestro.call_policy import CALL_POLICIES

    declared = CALL_POLICIES[caller].surface
    if declared in RUNTIME_GROUNDED_SURFACES and not grounded:
        return TOOL_FREE
    return declared


def phase_effective_surfaces(*, grounded: bool) -> Mapping[str, frozenset[ToolSurface]]:
    """Map each provider phase to the effective surfaces of the callers routed to it."""
    from maestro.call_policy import CALL_POLICIES

    phases: dict[str, set[ToolSurface]] = {}
    for caller, policy in CALL_POLICIES.items():
        if policy.phase is not None:
            phases.setdefault(policy.phase, set()).add(
                effective_surface(caller, grounded=grounded),
            )
    return MappingProxyType({p: frozenset(s) for p, s in phases.items()})


def action_gated_phases() -> frozenset[str]:
    """Phases where any caller's surface carries a shell-capable built-in.

    Read at the grounded view; grounding never adds built-ins, so the result
    does not depend on config.
    """
    return frozenset(
        phase
        for phase, surfaces in phase_effective_surfaces(grounded=True).items()
        if any(s.builtins & SHELL_CAPABLE_BUILTINS for s in surfaces)
    )


def mcp_attaching_phases(*, grounded: bool) -> frozenset[str]:
    """Phases where any caller's effective surface attaches MCP servers."""
    return frozenset(
        phase
        for phase, surfaces in phase_effective_surfaces(grounded=grounded).items()
        if any(s.mcp_servers for s in surfaces)
    )


def external_mcp_phases(*, grounded: bool) -> frozenset[str]:
    """Phases where any caller's effective surface attaches the external MCP servers."""
    return frozenset(
        phase
        for phase, surfaces in phase_effective_surfaces(grounded=grounded).items()
        if any(s.external_mcp for s in surfaces)
    )


def gate_hook_matcher() -> str:
    """The ``PreToolUse`` matcher alternation over :data:`GATE_MATCHED_TOOLS`."""
    return "|".join(sorted(GATE_MATCHED_TOOLS))


def is_gate_matched(tool_name: str) -> bool:
    """Whether the action-gate callback inspects *tool_name*."""
    return tool_name in GATE_MATCHED_TOOLS


def ungated_shell_builtins() -> frozenset[str]:
    """Shell-capable built-ins the action gate does not inspect (issue #4381)."""
    return SHELL_CAPABLE_BUILTINS - GATE_MATCHED_TOOLS


def deny_ungated_shell(disallowed: list[str] | None) -> list[str]:
    """Union *disallowed* (``None`` → ``["Agent"]``) with :func:`ungated_shell_builtins`.

    Both provider seams route their deny list through here regardless of
    ``[sdk] enforced_tool_surface_enabled``, so no session can reach a shell
    the installed gate never sees. Order is stable and duplicates are dropped.
    """
    names = list(disallowed) if disallowed is not None else ["Agent"]
    return names + sorted(ungated_shell_builtins() - set(names))


@dataclass(frozen=True)
class ResolvedToolOptions:
    """The enforcing option values a :class:`ToolSurface` resolves to."""

    tools: list[str]
    mcp_server_names: frozenset[str]
    disallowed_tools: list[str] | None


def resolve_tool_options(
    surface: ToolSurface, config: Config,
) -> ResolvedToolOptions:
    """Resolve *surface* into enforcing ``tools`` / server / deny-list values."""
    servers = surface.mcp_servers
    disallowed: list[str] | None = None
    if surface.external_mcp:
        servers = servers | frozenset(config.mcp_servers_config() or {})
        disallowed = config.external_mcp_disallowed_tools()
    return ResolvedToolOptions(
        tools=sorted(surface.builtins),
        mcp_server_names=servers,
        disallowed_tools=disallowed,
    )


def admits_tool_name(surface: ToolSurface, name: str, config: Config) -> bool:
    """Whether *surface* grants the tool *name* (built-in or ``mcp__`` name)."""
    if name in _EXCLUDED_TOOLS:
        return True
    if not name.startswith(_MCP_PREFIX):
        return name in surface.builtins
    resolved = resolve_tool_options(surface, config)
    server = name[len(_MCP_PREFIX):].split("__", 1)[0]
    return (
        server in resolved.mcp_server_names
        and name not in (resolved.disallowed_tools or [])
    )


def call_effective_surface(caller: str, options: QueryOptions) -> ToolSurface:
    """Return *caller*'s effective surface for one call's *options*.

    Grounding is the runtime fact the call site's MCP attachment carries: a
    runtime-grounded declared surface is grounded iff *options* attaches any
    server. ``KeyError`` for an unregistered caller.
    """
    from maestro.call_policy import CALL_POLICIES

    declared = CALL_POLICIES[caller].surface
    grounded = declared in RUNTIME_GROUNDED_SURFACES and bool(options.mcp_servers)
    return effective_surface(caller, grounded=grounded)


def resolve_call_options(
    options: QueryOptions | None, config: Config,
) -> QueryOptions:
    """Return *options* with ``tools`` derived from its caller's surface.

    Raises ``ValueError`` — before any subprocess starts — for missing options,
    a missing or unregistered caller, a tool name or MCP server the effective
    surface does not admit, or a shell-capable surface without action-gate
    hooks (a ``hooks`` mapping with no ``PreToolUse`` entry installs no gate).
    The call-site ``disallowed_tools`` is unioned with the surface's deny
    list, so a caller's own denials can never drop a surface denial.
    """
    from maestro.call_policy import CALL_POLICIES

    prefix = "enforced_tool_surface_enabled"
    if options is None:
        raise ValueError(f"{prefix}: SDK call carries no QueryOptions")
    caller = options.caller
    if caller is None:
        raise ValueError(f"{prefix}: SDK call carries no caller tag")
    if caller not in CALL_POLICIES:
        raise ValueError(
            f"{prefix}: caller {caller!r} is not registered in CALL_POLICIES"
        )
    surface = call_effective_surface(caller, options)
    resolved = resolve_tool_options(surface, config)
    for name in [*(options.allowed_tools or []), *(options.tools or [])]:
        if not admits_tool_name(surface, name, config):
            raise ValueError(
                f"{prefix}: caller {caller!r} surface does not admit tool {name!r}"
            )
    for server in options.mcp_servers or {}:
        if server not in resolved.mcp_server_names:
            raise ValueError(
                f"{prefix}: caller {caller!r} surface does not admit MCP "
                f"server {server!r}"
            )
    if surface.builtins & SHELL_CAPABLE_BUILTINS and not (options.hooks or {}).get(
        "PreToolUse"
    ):
        raise ValueError(
            f"{prefix}: caller {caller!r} has a shell-capable surface but no "
            "action-gate hooks"
        )
    disallowed = list(options.disallowed_tools or [])
    disallowed += [
        name for name in resolved.disallowed_tools or [] if name not in disallowed
    ]
    return replace(
        options, tools=resolved.tools, disallowed_tools=disallowed or None,
    )


def declared_builtins(caller: str) -> frozenset[str] | None:
    """Return *caller*'s declared built-in set; ``None`` when undeclared."""
    if caller in DECLARED_TASK_TOOL_CALLERS:
        return TASK_TOOL_BUILTINS
    from maestro.call_policy import CALL_POLICIES

    if caller not in CALL_POLICIES:
        return None
    return effective_surface(caller, grounded=False).builtins


def declared_mcp_servers(caller: str) -> frozenset[str] | None:
    """Return *caller*'s declared MCP server set; ``None`` when unregistered."""
    from maestro.call_policy import CALL_POLICIES

    if caller not in CALL_POLICIES:
        return None
    return effective_surface(caller, grounded=True).mcp_servers


def _undeclared_servers(
    caller: str,
    advertised: list[str],
    external_servers: frozenset[str] | None,
) -> set[str]:
    """The advertised server names *caller*'s surface does not declare.

    An ``external_mcp`` surface also admits *external_servers*; when that
    set is ``None`` (unknown) it admits any server.
    """
    declared = declared_mcp_servers(caller)
    if declared is None:
        return set(advertised)
    if effective_surface(caller, grounded=True).external_mcp:
        if external_servers is None:
            return set()
        declared = declared | external_servers
    return {name for name in advertised if name not in declared}


def _is_builtin(name: str) -> bool:
    return not name.startswith(_MCP_PREFIX) and name not in _EXCLUDED_TOOLS


def _tool_call_name(turn: TranscriptTurn) -> str | None:
    """The tool name of a ``tool_call`` turn, or ``None`` when unparseable."""
    if turn.kind != "tool_call":
        return None
    match = _TOOL_CALL_RE.match(turn.content)
    return match.group(1) if match else None


def _refused_name(turn: TranscriptTurn) -> str | None:
    """The tool name a ``tool_result`` turn reports the CLI refused, if any."""
    if turn.kind != "tool_result" or not turn.content.startswith(_ERROR_PREFIX):
        return None
    match = _REFUSAL_RE.search(turn.content)
    return match.group(1) if match else None


def _off_surface(name: str, declared: frozenset[str] | None) -> bool:
    return _is_builtin(name) and (declared is None or name not in declared)


@dataclass(frozen=True)
class CallerDrift:
    """One caller's measured tool-surface drift over the window."""

    caller: str
    declared: frozenset[str] | None
    sidecars: int = 0
    advertised_unknown: int = 0
    sidecars_with_off_surface: int = 0
    off_surface_executed: dict[str, int] = field(default_factory=dict)
    refusals: dict[str, int] = field(default_factory=dict)
    advertised_minus_declared: frozenset[str] = frozenset()
    mcp_servers_advertised_unknown: int = 0
    mcp_servers_advertised_undeclared: frozenset[str] = frozenset()

    @property
    def has_drift(self) -> bool:
        return bool(
            self.off_surface_executed
            or self.refusals
            or self.advertised_minus_declared
            or self.mcp_servers_advertised_undeclared
        )


@dataclass(frozen=True)
class ToolSurfaceDriftReport:
    """Per-caller drift plus whole-window totals."""

    callers: tuple[CallerDrift, ...]
    sidecars: int
    skipped_unknown: int

    @property
    def off_surface_total(self) -> int:
        return sum(sum(c.off_surface_executed.values()) for c in self.callers)

    @property
    def refusals_total(self) -> int:
        return sum(sum(c.refusals.values()) for c in self.callers)

    @property
    def has_drift(self) -> bool:
        return any(c.has_drift for c in self.callers)

    def report_payload(self) -> dict[str, object]:
        """JSON-ready view of the report (``--json``)."""
        return {
            "sidecars": self.sidecars,
            "skipped_unknown": self.skipped_unknown,
            "off_surface_total": self.off_surface_total,
            "refusals_total": self.refusals_total,
            "callers": [
                {
                    "caller": c.caller,
                    "declared": (
                        None if c.declared is None else sorted(c.declared)
                    ),
                    "sidecars": c.sidecars,
                    "advertised_unknown": c.advertised_unknown,
                    "sidecars_with_off_surface": c.sidecars_with_off_surface,
                    "off_surface_executed": dict(
                        sorted(c.off_surface_executed.items()),
                    ),
                    "refusals": dict(sorted(c.refusals.items())),
                    "advertised_minus_declared": sorted(
                        c.advertised_minus_declared,
                    ),
                    "mcp_servers_advertised_unknown": (
                        c.mcp_servers_advertised_unknown
                    ),
                    "mcp_servers_advertised_undeclared": sorted(
                        c.mcp_servers_advertised_undeclared,
                    ),
                }
                for c in self.callers
            ],
        }


def _record_counts(
    record: TranscriptRecord, declared: frozenset[str] | None,
) -> tuple[Counter[str], Counter[str]]:
    """Return ``(off-surface executed, refusals)`` counts for one sidecar."""
    calls: Counter[str] = Counter()
    refusals: Counter[str] = Counter()
    for turn in record.turns:
        called = _tool_call_name(turn)
        if called is not None and _off_surface(called, declared):
            calls[called] += 1
        refused = _refused_name(turn)
        if refused is not None and _is_builtin(refused):
            refusals[refused] += 1
    executed = Counter({
        name: count - refusals[name]
        for name, count in calls.items()
        if count > refusals[name]
    })
    return executed, refusals


def measure_tool_surface_drift(
    records: Iterable[TranscriptRecord],
    external_servers: frozenset[str] | None = None,
) -> ToolSurfaceDriftReport:
    """Roll *records* up into a per-caller :class:`ToolSurfaceDriftReport`.

    *external_servers* names the configured external MCP servers an
    ``external_mcp`` surface admits; ``None`` admits any server there.
    """
    sidecars: Counter[str] = Counter()
    unknown: Counter[str] = Counter()
    with_off: Counter[str] = Counter()
    executed: dict[str, Counter[str]] = {}
    refused: dict[str, Counter[str]] = {}
    extra: dict[str, set[str]] = {}
    servers_unknown: Counter[str] = Counter()
    servers_extra: dict[str, set[str]] = {}
    skipped = 0
    total = 0

    for record in records:
        caller = record.caller
        if caller == _UNKNOWN_CALLER:
            skipped += 1
            continue
        total += 1
        declared = declared_builtins(caller)
        sidecars[caller] += 1
        caller_extra = extra.setdefault(caller, set())
        if record.tools_advertised is None:
            unknown[caller] += 1
        else:
            caller_extra.update(
                name for name in record.tools_advertised
                if _off_surface(name, declared)
            )
        caller_servers = servers_extra.setdefault(caller, set())
        if record.mcp_servers_advertised is None:
            servers_unknown[caller] += 1
        else:
            caller_servers.update(
                _undeclared_servers(
                    caller, record.mcp_servers_advertised, external_servers,
                ),
            )
        off, ref = _record_counts(record, declared)
        if off:
            with_off[caller] += 1
        executed.setdefault(caller, Counter()).update(off)
        refused.setdefault(caller, Counter()).update(ref)

    callers = tuple(
        CallerDrift(
            caller=caller,
            declared=declared_builtins(caller),
            sidecars=sidecars[caller],
            advertised_unknown=unknown[caller],
            sidecars_with_off_surface=with_off[caller],
            off_surface_executed=dict(executed[caller]),
            refusals=dict(refused[caller]),
            advertised_minus_declared=frozenset(extra[caller]),
            mcp_servers_advertised_unknown=servers_unknown[caller],
            mcp_servers_advertised_undeclared=frozenset(servers_extra[caller]),
        )
        for caller in sorted(sidecars)
    )
    return ToolSurfaceDriftReport(
        callers=callers, sidecars=total, skipped_unknown=skipped,
    )


def _counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{name}={n}" for name, n in sorted(counts.items())) or "-"


def render_report(report: ToolSurfaceDriftReport) -> str:
    """Text view of *report*; a one-line summary when nothing drifted."""
    if not report.has_drift:
        return (
            f"No tool-surface drift ({report.sidecars} sidecars across "
            f"{len(report.callers)} callers)."
        )
    lines = [
        "Tool-Surface Drift",
        "=" * 40,
        f"Sidecars:            {report.sidecars}",
        f"Skipped (unknown):   {report.skipped_unknown}",
        f"Off-surface calls:   {report.off_surface_total}",
        f"Refused calls:       {report.refusals_total}",
    ]
    for c in report.callers:
        if not c.has_drift:
            continue
        declared = (
            "default toolbelt (undeclared)" if c.declared is None
            else ", ".join(sorted(c.declared)) or "none (tool-free)"
        )
        lines += [
            "",
            f"{c.caller}  ({c.sidecars} sidecars, "
            f"{c.advertised_unknown} advertised-unknown)",
            f"  declared:             {declared}",
            f"  off-surface executed: {_counts(c.off_surface_executed)} "
            f"(in {c.sidecars_with_off_surface} sidecars)",
            f"  refused:              {_counts(c.refusals)}",
            "  advertised-undeclared: "
            + (", ".join(sorted(c.advertised_minus_declared)) or "-"),
            "  advertised-undeclared servers: "
            + (", ".join(sorted(c.mcp_servers_advertised_undeclared)) or "-")
            + f" ({c.mcp_servers_advertised_unknown} advertised-unknown)",
        ]
    return "\n".join(lines)
