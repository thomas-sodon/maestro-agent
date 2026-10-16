"""Tests for startup capability validation (proposal 058, Phase 2 Step 7).

:func:`maestro.providers.capability_gate.validate_provider_capabilities` runs at
``cli.main`` time and fails loud when a phase is routed to a provider that lacks
a capability it needs — before any query. These tests pin:

* the inert default (``anthropic`` advertises every capability → always passes),
* the two fail-loud paths from the Step-7 acceptance (a ``remote_mcp``-requiring
  phase on a ``remote_mcp=False`` provider, and a long-context phase on a
  ``long_context=False`` provider),
* that an introspection-tool phase on an ``in_process_tools=True`` provider
  passes, and
* the config-aware ``required_capabilities_for_phase`` mapping.

The registry holds process-global state; the autouse fixture snapshots and
restores it so registering fake providers stays order-independent.
"""

from __future__ import annotations

import ast
import pathlib
import sys
from collections.abc import Callable, Iterator
from dataclasses import replace
from types import MappingProxyType

import pytest

from maestro import call_policy
from maestro.call_policy import CALL_POLICIES
from maestro.config import SDK_PHASE_NAMES, Config
from maestro.providers import capability_gate, registry
from maestro.providers.base import Capabilities, Provider
from maestro.providers.capability_gate import (
    required_capabilities_for_phase,
    validate_action_gate_coverage,
    validate_provider_capabilities,
)
from maestro.tool_surface import REPO_MUTATING, action_gated_phases
from tests.structural_guards import _installs_action_gate
from tests.support.structural_guard import _MAESTRO_ROOT


@pytest.fixture(autouse=True)
def _reset_registry_state() -> Iterator[None]:
    """Snapshot and restore the registry's in-place-mutated tables per test.

    ``_REGISTRY`` / ``_INSTANCES`` are mutated in place, so they stay here; the
    registered runtime globals are restored by the autouse ``_own_sdk_runtime``
    owner in ``tests/conftest.py``.
    """
    saved_registry = dict(registry._REGISTRY)
    saved_instances = dict(registry._INSTANCES)
    try:
        yield
    finally:
        registry._REGISTRY.clear()
        registry._REGISTRY.update(saved_registry)
        registry._INSTANCES.clear()
        registry._INSTANCES.update(saved_instances)


def _provider_factory(**overrides: bool) -> Callable[[], Provider]:
    """Build a factory for a fully-capable provider with *overrides* applied.

    Defaults to every capability ``True`` (like ``AnthropicProvider``); pass
    e.g. ``remote_mcp=False`` to model a capability-limited provider.
    """
    caps = {
        "sessions": True,
        "in_process_tools": True,
        "remote_mcp": True,
        "extended_thinking": True,
        "long_context": True,
        "structured_output": True,
        "usage_metering": True,
        "streaming": True,
    }
    caps.update(overrides)

    class _Fake:
        capabilities = Capabilities(**caps)

        async def query(self, *args: object, **kwargs: object) -> object:
            return None

        async def create_session(self, *args: object, **kwargs: object) -> object:
            return None

        async def query_on_session(
            self, *args: object, **kwargs: object,
        ) -> object:
            return None

        async def disconnect_session(
            self, *args: object, **kwargs: object,
        ) -> None:
            return None

    return _Fake


class TestDefaultPasses:
    """The inert default (anthropic everywhere) satisfies every phase."""

    def test_default_config_passes(self) -> None:
        # Returns None (no raise) — anthropic advertises every capability.
        assert validate_provider_capabilities(Config()) is None

    def test_default_config_with_external_mcp_and_long_context_passes(self) -> None:
        config = Config(
            context7_mcp_enabled=True,
            sdk_model_long_context="claude-long",
            long_context_implementation_enabled=True,
        )
        assert validate_provider_capabilities(config) is None


class TestFailLoud:
    """A phase routed to a provider missing a needed capability raises."""

    def test_remote_mcp_phase_on_remote_mcp_false_provider_raises(self) -> None:
        registry.register("limited", _provider_factory(remote_mcp=False))
        config = Config(
            context7_mcp_enabled=True,
            sdk_phase_providers={"implementation": "limited"},
        )
        with pytest.raises(ValueError, match="remote_mcp") as exc:
            validate_provider_capabilities(config)
        message = str(exc.value)
        assert "implementation" in message
        assert "limited" in message

    def test_serena_mcp_phase_on_remote_mcp_false_provider_raises(self) -> None:
        """Enabling serena on a ``remote_mcp=False`` provider aborts at startup."""
        registry.register("limited_serena", _provider_factory(remote_mcp=False))
        config = Config(
            serena_mcp_enabled=True,
            sdk_phase_providers={"run_task": "limited_serena"},
        )
        with pytest.raises(ValueError, match="remote_mcp") as exc:
            validate_provider_capabilities(config)
        assert "run_task" in str(exc.value)

    def test_long_context_phase_on_long_context_false_provider_raises(self) -> None:
        registry.register("nolc", _provider_factory(long_context=False))
        config = Config(
            long_context_implementation_enabled=True,
            sdk_model_long_context="claude-long",
            sdk_phase_providers={"implementation": "nolc"},
        )
        with pytest.raises(ValueError, match="long_context") as exc:
            validate_provider_capabilities(config)
        assert "implementation" in str(exc.value)

    def test_run_task_long_context_required_when_executor_long_context_enabled(
        self,
    ) -> None:
        # cli.py constructs the Executor with a non-None long_context_model
        # whenever both flags are set, and Executor.start_session pins the
        # run_task session to that model — so a long_context=False provider
        # routed to run_task must fail loud at startup, not at first query.
        registry.register("nolc", _provider_factory(long_context=False))
        config = Config(
            long_context_implementation_enabled=True,
            sdk_model_long_context="claude-long",
            sdk_phase_providers={"run_task": "nolc"},
        )
        with pytest.raises(ValueError, match="long_context") as exc:
            validate_provider_capabilities(config)
        assert "run_task" in str(exc.value)

    def test_in_process_tools_false_provider_for_tool_phase_raises(self) -> None:
        registry.register("notools", _provider_factory(in_process_tools=False))
        config = Config(sdk_phase_providers={"run_task": "notools"})
        with pytest.raises(ValueError, match="in_process_tools"):
            validate_provider_capabilities(config)

    def test_contemplation_on_tool_less_provider_raises_only_flag_on(self) -> None:
        """Proposal 132: an ``in_process_tools=False`` provider routed to
        contemplation passes flag-off (contemplation needs nothing) but fails loud
        once grounded reflection is enabled."""
        registry.register(
            "notools_contemplate", _provider_factory(in_process_tools=False),
        )
        off = Config(sdk_phase_providers={"contemplation": "notools_contemplate"})
        assert validate_provider_capabilities(off) is None
        on = Config(
            grounded_reflection_enabled=True,
            sdk_phase_providers={"contemplation": "notools_contemplate"},
        )
        with pytest.raises(ValueError, match="in_process_tools") as exc:
            validate_provider_capabilities(on)
        assert "contemplation" in str(exc.value)

    def test_unregistered_per_phase_provider_raises(self) -> None:
        # "openai" is schema-valid but not registered (Phase 3); routing a phase
        # to it must fail loud at startup, not at first query.
        config = Config(sdk_phase_providers={"evolution": "openai"})
        with pytest.raises(ValueError, match="unknown provider 'openai'"):
            validate_provider_capabilities(config)


class TestCliPacedFullyQualified:
    """``cli_paced`` is fully qualified (proposal 061 Step 11): with all eight
    capability flags ``True``, the gate routes it to any phase — including the
    tool phases with external MCP and the long-context executor enabled."""

    def test_cli_paced_as_global_provider_satisfies_every_phase(self) -> None:
        config = Config(
            sdk_provider="cli_paced",
            context7_mcp_enabled=True,
            sdk_model_long_context="claude-long",
            long_context_implementation_enabled=True,
        )
        assert validate_provider_capabilities(config) is None

    def test_cli_paced_routed_to_tool_phases_passes(self) -> None:
        config = Config(
            context7_mcp_enabled=True,
            sdk_phase_providers={
                "implementation": "cli_paced",
                "run_task": "cli_paced",
            },
        )
        assert validate_provider_capabilities(config) is None

    def test_cli_paced_like_provider_missing_one_flag_still_raises(self) -> None:
        # Guard the gate itself: a provider otherwise fully capable but missing the
        # one flag a tool phase needs still fails loud — so the all-True pass above
        # is the capability flags being satisfied, not the gate ignoring them.
        registry.register(
            "cli_paced_minus_one", _provider_factory(in_process_tools=False)
        )
        config = Config(sdk_phase_providers={"run_task": "cli_paced_minus_one"})
        with pytest.raises(ValueError, match="in_process_tools"):
            validate_provider_capabilities(config)


class TestCapablePartialProviderPasses:
    """A provider missing only unneeded capabilities still passes."""

    def test_in_process_only_provider_passes_without_external_mcp(self) -> None:
        # No external MCP configured → remote_mcp not required; long context off
        # → long_context not required. in_process_tools=True suffices.
        registry.register(
            "intronly",
            _provider_factory(remote_mcp=False, long_context=False),
        )
        config = Config(sdk_phase_providers={"implementation": "intronly"})
        assert validate_provider_capabilities(config) is None


class TestRequiredCapabilities:
    """``required_capabilities_for_phase`` is config-aware and grounded."""

    def test_reasoning_only_phases_require_nothing(self) -> None:
        config = Config()
        for phase in (
            "contemplation",
            "task_generation",
            "artifact_review",
            "rejection",
        ):
            assert required_capabilities_for_phase(phase, config) == frozenset()

    def test_tool_phase_requires_in_process_tools(self) -> None:
        config = Config()  # no external MCP, no long context
        assert required_capabilities_for_phase("implementation", config) == frozenset(
            {"in_process_tools"}
        )

    def test_contemplation_requires_in_process_tools_only_with_grounded_reflection(
        self,
    ) -> None:
        """Proposal 132: contemplation is tool-free by default, needs in_process_tools
        with the grounded-reflection flag on."""
        off = Config()
        assert required_capabilities_for_phase("contemplation", off) == frozenset()
        on = Config(grounded_reflection_enabled=True)
        assert required_capabilities_for_phase("contemplation", on) == frozenset(
            {"in_process_tools"}
        )

    def test_evolution_requires_in_process_tools_only_with_grounded_planning(
        self,
    ) -> None:
        """Proposal 143: evolution needs in_process_tools only with the flag on."""
        assert required_capabilities_for_phase("evolution", Config()) == frozenset()
        on = Config(grounded_planning_enabled=True)
        assert required_capabilities_for_phase("evolution", on) == frozenset(
            {"in_process_tools"}
        )

    @pytest.mark.parametrize(
        "field",
        [
            "context7_mcp_enabled",
            "serena_mcp_enabled",
        ],
    )
    def test_external_mcp_adds_remote_mcp(self, field: str) -> None:
        config = Config(**{field: True})
        assert required_capabilities_for_phase("run_task", config) == frozenset(
            {"in_process_tools", "remote_mcp"}
        )

    def test_implementation_long_context_requires_long_context(self) -> None:
        config = Config(
            long_context_implementation_enabled=True,
            sdk_model_long_context="claude-long",
        )
        assert required_capabilities_for_phase("implementation", config) == frozenset(
            {"in_process_tools", "long_context"}
        )

    def test_implementation_long_context_off_without_model(self) -> None:
        # Flag on but no model configured → long_context not required.
        config = Config(long_context_implementation_enabled=True)
        assert "long_context" not in required_capabilities_for_phase(
            "implementation", config,
        )

    def test_evolution_requires_no_capabilities_default(self) -> None:
        """The evolve phase session is ``tools=[]`` — no capabilities required."""
        config = Config()
        assert required_capabilities_for_phase("evolution", config) == frozenset()

    def test_evolution_requires_no_capabilities_with_external_mcp(self) -> None:
        """The evolve session never attaches mcp_servers, so remote_mcp irrelevant."""
        config = Config(
            context7_mcp_enabled=True,
        )
        assert required_capabilities_for_phase("evolution", config) == frozenset()

    def test_evolution_requires_no_capabilities_with_long_context_model(self) -> None:
        """The evolve session never pins the long-context model — ``self_review``
        runs on the implementation phase's ``impl_session``, not the evolve session.
        """
        config = Config(
            sdk_model_long_context="claude-long",
            long_context_implementation_enabled=True,
        )
        assert required_capabilities_for_phase("evolution", config) == frozenset()

    def test_run_task_long_context_required_when_executor_long_context_enabled(
        self,
    ) -> None:
        # cli.py builds the Executor with a non-None long_context_model under
        # exactly this pair, and Executor.start_session pins the run_task
        # session to that model with LONG_CONTEXT_BETA — so the gate must
        # require ``long_context`` for run_task too, not just implementation.
        config = Config(
            long_context_implementation_enabled=True,
            sdk_model_long_context="claude-long",
        )
        assert required_capabilities_for_phase("run_task", config) == frozenset(
            {"in_process_tools", "long_context"}
        )

    def test_run_task_long_context_off_without_model(self) -> None:
        # Flag on but no model configured → long_context not required (matches
        # cli.py's executor_long_context_model gating).
        config = Config(long_context_implementation_enabled=True)
        assert "long_context" not in required_capabilities_for_phase(
            "run_task", config,
        )

    def test_run_task_long_context_off_without_flag(self) -> None:
        # Model set but flag off → cli.py leaves executor_long_context_model
        # None, so run_task's session uses the default model.
        config = Config(sdk_model_long_context="claude-long")
        assert "long_context" not in required_capabilities_for_phase(
            "run_task", config,
        )


class TestActionGateCoverage:
    """``validate_action_gate_coverage`` (proposal 066): an action-gated phase
    may only route to a provider carrying the #2280 Bash mutation guard. This
    is a trust assertion, deliberately separate from the Capabilities flags —
    a fully-capable provider with no gate path must still fail loud."""

    def test_default_config_passes(self) -> None:
        # anthropic everywhere → in-process hook guard → clean load.
        assert validate_action_gate_coverage(Config()) is None

    def test_cli_paced_global_provider_passes(self) -> None:
        # Step 5 acceptance: cli_paced on the mutating phases with the gate
        # present (the provider injects it by construction) loads clean.
        assert validate_action_gate_coverage(Config(sdk_provider="cli_paced")) is None

    def test_cli_paced_routed_to_action_gated_phases_passes(self) -> None:
        config = Config(
            sdk_phase_providers={
                "implementation": "cli_paced",
                "run_task": "cli_paced",
            },
        )
        assert validate_action_gate_coverage(config) is None

    @pytest.mark.parametrize("phase", ["run_task", "implementation"])
    def test_guardless_provider_on_action_gated_phase_raises(
        self, phase: str,
    ) -> None:
        # Step 5 acceptance: a provider with no gate path ("openai" stands in
        # for any hand-rolled provider — schema-valid, fully capable, but
        # carrying no mutation guard) fails loud at config-load, naming the
        # phase and the missing guard.
        config = Config(sdk_phase_providers={phase: "openai"})
        with pytest.raises(ValueError, match="mutation guard") as exc:
            validate_action_gate_coverage(config)
        message = str(exc.value)
        assert phase in message
        assert "openai" in message

    def test_guardless_global_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="action-gated phase"):
            validate_action_gate_coverage(Config(sdk_provider="openai"))

    def test_guardless_provider_on_non_mutating_phase_passes(self) -> None:
        # Non-action-gated phases are unaffected: contemplation on a guardless
        # provider is fine — it executes no repo-mutating Bash.
        config = Config(sdk_phase_providers={"contemplation": "openai"})
        assert validate_action_gate_coverage(config) is None


class TestGateHookResolvable:
    """Issue #4160: a cli_paced action-gated phase fails loud at config-load
    when the injected hook's executable cannot be resolved (it would exit
    non-zero, which the claude CLI treats as fail-open)."""

    @pytest.mark.parametrize(
        "config",
        [
            Config(sdk_provider="cli_paced"),
            Config(sdk_phase_providers={"run_task": "cli_paced"}),
            Config(sdk_phase_providers={"implementation": "cli_paced"}),
        ],
    )
    def test_unresolvable_hook_raises(
        self, config: Config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(capability_gate.shutil, "which", lambda _p: None)
        with pytest.raises(ValueError, match="gate-bash-hook") as exc:
            validate_action_gate_coverage(config)
        assert "cli_paced" in str(exc.value)

    def test_resolvable_hook_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        probed: list[str] = []

        def _which(path: str) -> str:
            probed.append(path)
            return path

        monkeypatch.setattr(capability_gate.shutil, "which", _which)
        assert validate_action_gate_coverage(Config(sdk_provider="cli_paced")) is None
        assert probed == [sys.executable] * len(action_gated_phases())

    def test_default_routing_never_probes_hook(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        probed: list[str] = []

        def _which(path: str) -> None:
            probed.append(path)

        monkeypatch.setattr(capability_gate.shutil, "which", _which)
        assert validate_action_gate_coverage(Config()) is None
        assert probed == []


# --- Declaration-to-installation cross-check (issue #2914) -------------------


def _gated_phase_modules() -> list[pathlib.Path]:
    """Every ``*.py`` under ``maestro/``, deterministically ordered.

    The scan is package-wide rather than the three modules that build such
    sessions today: a phase-tagged construction site added in a *new* module
    would otherwise be silently un-checked, and the sweep is cheap — only calls
    carrying a literal ``phase=`` in :func:`~maestro.tool_surface.action_gated_phases` are considered
    at all.
    """
    return sorted(_MAESTRO_ROOT.rglob("*.py"))


def _string_kwarg(call: ast.Call, name: str) -> str | None:
    """Return *call*'s *name* keyword when it is a plain string literal."""
    for kw in call.keywords:
        if (
            kw.arg == name
            and isinstance(kw.value, ast.Constant)
            and isinstance(kw.value.value, str)
        ):
            return kw.value.value
    return None


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    """Return *call*'s *name* keyword value, or ``None`` when absent."""
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _same_module_functions(tree: ast.AST) -> dict[str, ast.AST]:
    """Map every function defined in *tree* to its node, by bare name."""
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _called_function_name(expr: ast.expr) -> str | None:
    """Return the bare function name *expr* calls (``f()`` / ``self.f()``)."""
    if not isinstance(expr, ast.Call):
        return None
    if isinstance(expr.func, ast.Name):
        return expr.func.id
    if isinstance(expr.func, ast.Attribute):
        return expr.func.attr
    return None


def _query_options_literals(node: ast.AST) -> list[ast.Call]:
    """Return every ``QueryOptions(...)`` construction inside *node*."""
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == "QueryOptions"
    ]


def _resolve_options(
    options: ast.expr, functions: dict[str, ast.AST],
) -> list[ast.Call]:
    """Resolve an ``options=`` expression to the ``QueryOptions`` it builds.

    Two shapes resolve: a literal ``QueryOptions(...)``, and one hop of
    indirection into a same-module helper (``options=self._step_query_options(
    ...)`` in ``executor.py``), whose body is scanned for its own literal.
    Anything else returns ``[]`` so the caller fails loud naming the site — a
    new indirection pattern must be handled deliberately rather than slipping
    through un-checked.
    """
    if (
        isinstance(options, ast.Call)
        and isinstance(options.func, ast.Name)
        and options.func.id == "QueryOptions"
    ):
        return [options]
    name = _called_function_name(options)
    if name is not None and name in functions:
        return _query_options_literals(functions[name])
    return []


def _is_tool_less(options: ast.Call) -> bool:
    """True when *options* sets ``tools=[]`` — an empty *base* tool set.

    Per the ``QueryOptions.tools`` docstring an explicit empty base set means
    the session has no tools at all (``--tools ""``), materially unlike
    ``allowed_tools=[]``. Such a session cannot execute ``Bash``, so there is
    nothing for the action gate to deny and it is exempt. Grounded in the
    documented field semantics, not an ad-hoc allowlist of call sites.
    """
    tools = _keyword(options, "tools")
    return isinstance(tools, (ast.List, ast.Tuple)) and not tools.elts


class TestActionGatedPhaseSessionsInstallTheGate:
    """Every action-gated phase's session is actually *built* with the gate.

    ``validate_action_gate_coverage`` above asserts only that an action-gated
    phase routes to a provider *named* in ``_MUTATION_GUARDS`` — a trust
    declaration. It never asserts the phase's session carries the gate, and
    both guard-carrying providers derive the real gate from
    ``QueryOptions.hooks``: the Anthropic path installs the ``PreToolUse``
    callback only under ``if options.hooks is not None``, and ``cli_paced``
    returns no ``--settings`` injection without it. So the two halves drifted:
    ``implementation`` was declared repo-mutating while its session was built
    un-gated on every provider (issue #2914). This binds the declaration to
    the installation, driven by ``action_gated_phases()`` itself, so adding a
    phase to that set without wiring its session fails here.
    """

    def _gated_sites(self) -> list[tuple[str, int, ast.Call, dict[str, ast.AST]]]:
        """Every phase-tagged session/query call in an action-gated phase."""
        sites: list[tuple[str, int, ast.Call, dict[str, ast.AST]]] = []
        for path in _gated_phase_modules():
            rel = path.relative_to(_MAESTRO_ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            functions = _same_module_functions(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if _called_function_name(node) not in {"create_session", "query"}:
                    continue
                phase = _string_kwarg(node, "phase")
                if phase is None or phase not in action_gated_phases():
                    continue
                sites.append((rel, node.lineno, node, functions))
        return sites

    def test_every_action_gated_phase_site_is_found(self) -> None:
        # The scan must actually reach both declared phases; a rename that
        # silently emptied it would make the assertions below vacuous.
        phases = {
            _string_kwarg(node, "phase") for _rel, _line, node, _fns in
            self._gated_sites()
        }
        assert phases == set(action_gated_phases())

    def test_every_action_gated_session_passes_hooks(self) -> None:
        ungated: list[str] = []
        unresolved: list[str] = []
        for rel, line, node, functions in self._gated_sites():
            options = _keyword(node, "options")
            if options is None:
                unresolved.append(f"{rel}:{line} — no options= keyword")
                continue
            resolved = _resolve_options(options, functions)
            if not resolved:
                unresolved.append(
                    f"{rel}:{line} — options= resolves to neither a "
                    f"QueryOptions literal nor a same-module helper",
                )
                continue
            for opts in resolved:
                if _is_tool_less(opts):
                    continue  # tools=[] ⇒ no Bash to gate.
                # Presence of a ``hooks=`` keyword is not enough: ``hooks=None``
                # is the exact expression ``sdk.py``'s ``if options.hooks is not
                # None`` skips, so it would satisfy a presence check while
                # installing nothing. Shares the structural guard's predicate so
                # "a real gate" has one definition across both enforcers.
                if not _installs_action_gate(_keyword(opts, "hooks")):
                    ungated.append(f"{rel}:{opts.lineno}")
        # Fail loud on an unhandled indirection rather than passing silently.
        assert unresolved == []
        assert ungated == []


def _single_use_gated_violations(
    tree: ast.AST, rel: str,
) -> tuple[list[str], list[str]]:
    """Return ``(sites, ungated)`` for gated ``single_use_call`` calls in *tree*.

    A site is a ``single_use_call("<caller>", ...)`` whose literal caller is a
    ``CALL_POLICIES`` entry in an action-gated phase; it is ungated unless its
    ``options=`` resolves to ``QueryOptions`` literals that all install the gate.
    """
    functions = _same_module_functions(tree)
    sites: list[str] = []
    ungated: list[str] = []
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Call)
            or _called_function_name(node) != "single_use_call"
            or not node.args
            or not isinstance(node.args[0], ast.Constant)
        ):
            continue
        policy = CALL_POLICIES.get(node.args[0].value)
        if policy is None or policy.phase not in action_gated_phases():
            continue
        site = f"{rel}:{node.lineno}"
        sites.append(site)
        options = _keyword(node, "options")
        resolved = _resolve_options(options, functions) if options else []
        if not resolved or not all(
            _installs_action_gate(_keyword(opts, "hooks")) for opts in resolved
        ):
            ungated.append(site)
    return sites, ungated


class TestSingleUseGatedCallsInstallTheGate:
    """A ``single_use_call`` in an action-gated phase passes gated options.

    ``single_use_call`` refuses a hook-less gated session at runtime; this
    binds the same rule statically, so every such call site builds its
    ``options=`` with a real action gate.
    """

    def test_every_gated_single_use_call_passes_hooks(self) -> None:
        sites: list[str] = []
        ungated: list[str] = []
        for path in _gated_phase_modules():
            rel = path.relative_to(_MAESTRO_ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            found, bad = _single_use_gated_violations(tree, rel)
            sites += found
            ungated += bad
        # Non-vacuity: the flag-on ``self_review`` arm must be reached.
        assert any(site.startswith("evolution_quality.py:") for site in sites)
        assert ungated == []

    def test_seeded_hookless_single_use_call_is_flagged(self) -> None:
        source = (
            "async def f(p):\n"
            "    await single_use_call(\n"
            '        "self_review", p, system=None,\n'
            '        options=QueryOptions(allowed_tools=["Bash"]),\n'
            "    )\n"
            "    await single_use_call(\"self_review\", p, system=None)\n"
            "    await single_use_call(\"reflect\", p, system=None)\n"
        )
        sites, ungated = _single_use_gated_violations(ast.parse(source), "m.py")
        assert sites == ["m.py:2", "m.py:6"]
        assert ungated == ["m.py:2", "m.py:6"]


_NO_CAPS: frozenset[str] = frozenset()
_TOOLS = frozenset({"in_process_tools"})
_TOOLS_REMOTE = frozenset({"in_process_tools", "remote_mcp"})
_TOOLS_LONG = frozenset({"in_process_tools", "long_context"})


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (Config(), {"implementation": _TOOLS, "run_task": _TOOLS}),
        (
            Config(context7_mcp_enabled=True),
            {"implementation": _TOOLS_REMOTE, "run_task": _TOOLS_REMOTE},
        ),
        (
            Config(grounded_reflection_enabled=True),
            {"implementation": _TOOLS, "run_task": _TOOLS, "contemplation": _TOOLS},
        ),
        (
            Config(
                long_context_implementation_enabled=True,
                sdk_model_long_context="claude-opus-5-5[1m]",
            ),
            {"implementation": _TOOLS_LONG, "run_task": _TOOLS_LONG},
        ),
    ],
)
def test_derived_capability_golden_table(
    config: Config, expected: dict[str, frozenset[str]],
) -> None:
    """Proposal 142 step 6: the surface-derived requirements equal today's."""
    for phase in SDK_PHASE_NAMES:
        assert required_capabilities_for_phase(phase, config) == expected.get(
            phase, _NO_CAPS,
        )


def test_seeded_shell_surface_moves_action_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shell-capable surface in a new phase makes that phase action-gated."""
    patched = dict(CALL_POLICIES)
    patched["contemplation_session"] = replace(
        patched["contemplation_session"], surface=REPO_MUTATING,
    )
    monkeypatch.setattr(call_policy, "CALL_POLICIES", MappingProxyType(patched))
    config = Config(sdk_phase_providers={"contemplation": "openai"})
    with pytest.raises(ValueError, match="contemplation"):
        validate_action_gate_coverage(config)
