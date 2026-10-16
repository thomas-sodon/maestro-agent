"""Provider selection registry for Maestro's reasoning surface (proposal 058).

Maestro reaches the model through four functions in :mod:`maestro.sdk`
(``query`` / ``create_session`` / ``query_on_session`` / ``disconnect_session``).
Phase 2 Step 5 turns the LLM vendor into a *selectable* implementation: those
four public functions delegate through :func:`get_active_provider`, so flipping
the active provider changes which :class:`~maestro.providers.base.Provider`
backs every call.

The seam is a process-global name set once at startup, validated against a
registry, fail-loud on an unknown name. Step 5 landed it deliberately *inert* —
the active name defaulted to ``"anthropic"`` with nothing calling
:func:`configure_provider`. Step 6 since wired ``cli.main`` to read the
``[sdk] provider`` TOML field; absent that field the default still resolves to
:class:`~maestro.providers.anthropic.AnthropicProvider`, which runs today's exact
code path.

``cli_paced`` joined the registry when 056's standalone ``[sdk] backend`` seam was
retired (056 Step 11 / 058 Phase 4): the human-paced ``claude``-CLI logic now lives
wholly in :class:`~maestro.providers.cli_paced.CliPacedProvider`, registered here
behind a lazy factory and selectable through ``[sdk] provider = "cli_paced"``. The
old ``maestro.sdk_backends`` package and the ``[sdk] backend`` field no longer
exist.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping

from maestro.providers.anthropic import AnthropicProvider
from maestro.providers.base import Provider


def _cli_paced_factory() -> Provider:
    """Lazily import and build the cli_paced provider (proposal 056 Step 11).

    The import is deferred to call time so :mod:`maestro.providers.cli_paced`
    — and the :mod:`maestro_sdk` package it pulls in transitively — load only
    when an operator selects or per-phase-routes ``cli_paced``. The default
    ``anthropic`` path never imports either.
    """
    from maestro.providers.cli_paced import CliPacedProvider

    return CliPacedProvider()


#: Name → factory map of known providers. A factory is a zero-arg callable
#: returning a fresh :class:`Provider`; the providers are stateless, so
#: :func:`get_active_provider` caches one instance per name (see ``_INSTANCES``).
#: ``cli_paced`` is registered behind a lazy factory so its ``maestro_sdk`` import
#: stays off the default path (see :func:`_cli_paced_factory`).
_REGISTRY: dict[str, Callable[[], Provider]] = {
    "anthropic": AnthropicProvider,
    "cli_paced": _cli_paced_factory,
}

#: Process-global name of the active provider. ``"anthropic"`` is the inert
#: default; Step 6 sets this from ``[sdk] provider`` via :func:`configure_provider`.
_DEFAULT_PROVIDER = "anthropic"
_active_provider_name: str = _DEFAULT_PROVIDER

#: Per-phase provider override map (phase name → registered provider name), set
#: once at startup from ``[sdk] <phase>_provider`` via
#: :func:`configure_phase_providers`. Empty by default, so every phase resolves to
#: the active provider and the whole per-phase seam stays inert under the
#: ``anthropic`` default. This is the *runtime* counterpart to the startup-only
#: :meth:`Config.provider_for_phase`: :func:`get_phase_provider` reads it so the
#: ``sdk.py`` seam actually routes a phase's session/query to its override
#: (completing proposal 058 Step 6's "routes only the named phases" acceptance;
#: prerequisite for proposal 067's per-phase canary).
_phase_provider_names: dict[str, str] = {}

#: Cache of provider instances keyed by registry name. Factories are stateless,
#: so one instance per name is reused across calls; populated lazily by
#: :func:`get_active_provider`.
_INSTANCES: dict[str, Provider] = {}


def register(name: str, factory: Callable[[], Provider]) -> None:
    """Register *factory* under *name* so :func:`configure_provider` can select it.

    The hook later providers (OpenAI, a re-homed ``cli_paced``) use to join the
    registry. Re-registering a name replaces its factory and drops any cached
    instance so the next :func:`get_active_provider` rebuilds from the new factory.
    """
    _REGISTRY[name] = factory
    _INSTANCES.pop(name, None)


def configure_provider(name: str) -> None:
    """Select the active provider for subsequent seam calls (startup-time).

    Raises :class:`ValueError` for an unknown name so a misconfiguration fails
    loudly here rather than at the first query.
    """
    global _active_provider_name
    if name not in _REGISTRY:
        raise ValueError(
            f"unknown provider {name!r}; expected one of {sorted(_REGISTRY)}"
        )
    _active_provider_name = name


def current_provider_name() -> str:
    """Return the name of the currently selected provider."""
    return _active_provider_name


def configure_phase_providers(overrides: Mapping[str, str]) -> None:
    """Install the per-phase provider override map (startup-time).

    *overrides* maps a phase name to a provider name (the
    ``[sdk] <phase>_provider`` keys, already schema-validated by
    :meth:`Config.validate`). Each provider name is re-checked against the
    *registry* here — not just the schema's ``VALID_SDK_PROVIDERS`` — so a
    per-phase override pointing at a schema-valid-but-unregistered provider
    (e.g. ``openai`` today) fails loud at startup, exactly as
    :func:`configure_provider` does for the global selection. Replaces any prior
    map, so a re-invocation (or a CLI subcommand that re-runs ``cli.main``) takes
    the latest config. Inert when *overrides* is empty.
    """
    global _phase_provider_names
    validated: dict[str, str] = {}
    for phase, name in overrides.items():
        if name not in _REGISTRY:
            raise ValueError(
                f"unknown provider {name!r} for phase {phase!r}; expected one "
                f"of {sorted(_REGISTRY)}"
            )
        validated[phase] = name
    _phase_provider_names = validated


def get_phase_provider(phase: str | None) -> Provider:
    """Return the :class:`Provider` that should serve *phase*.

    The runtime resolver the ``sdk.py`` seam consults so a per-phase
    ``[sdk] <phase>_provider`` override actually routes that phase's
    session/query at query time. Falls back to :func:`get_active_provider` when
    *phase* is ``None`` or carries no override, so the default path is
    byte-identical to today's global-provider dispatch. The returned instance is
    the cached per-name singleton (see :func:`get_provider`).
    """
    if phase is not None:
        name = _phase_provider_names.get(phase)
        if name is not None:
            return get_provider(name)
    return get_active_provider()


def get_provider(name: str) -> Provider:
    """Return the registered provider instance for *name* (cached per name).

    Like :func:`get_active_provider` but for an *arbitrary* registered name —
    used by capability validation (:mod:`maestro.providers.capability_gate`) to
    inspect a per-phase provider's :class:`~maestro.providers.base.Capabilities`
    without making it the active selection. Raises :class:`ValueError` for an
    unknown name so a per-phase ``[sdk] <phase>_provider`` override pointing at
    an unregistered provider fails loud at startup, mirroring
    :func:`configure_provider`.
    """
    if name not in _REGISTRY:
        raise ValueError(
            f"unknown provider {name!r}; expected one of {sorted(_REGISTRY)}"
        )
    instance = _INSTANCES.get(name)
    if instance is None:
        instance = _REGISTRY[name]()
        _INSTANCES[name] = instance
    return instance


def get_active_provider() -> Provider:
    """Return the active :class:`Provider`, a cached singleton per name.

    The four public functions in :mod:`maestro.sdk` delegate here. The factory is
    stateless, so the instance is cached by name and reused on every call.
    """
    return get_provider(_active_provider_name)


# Every module global assigned under a ``global`` statement; snapshotted and
# restored around each test by the suite's autouse owner, and AST-checked
# against the module's ``global`` statements in ``tests/test_sdk_runtime_owner.py``.
_RUNTIME_GLOBALS: tuple[str, ...] = ("_active_provider_name", "_phase_provider_names")


def _copy_container(value: object) -> object:
    """Shallow-copy mutable containers; keep every other object by identity.

    Copying a backend, sink or ``asyncio.Semaphore`` would restore a clone
    (sharing the original's internals) rather than the original object.
    """
    if isinstance(value, (set, dict, list)):
        return copy.copy(value)
    return value


def snapshot_provider_runtime() -> dict[str, object]:
    """Capture the provider selection, shallow-copying the per-phase map."""
    module_globals = globals()
    return {
        name: _copy_container(module_globals[name]) for name in _RUNTIME_GLOBALS
    }


def restore_provider_runtime(snapshot: Mapping[str, object]) -> None:
    """Write a :func:`snapshot_provider_runtime` capture back into the module."""
    module_globals = globals()
    for name in _RUNTIME_GLOBALS:
        module_globals[name] = snapshot[name]


__all__ = [
    "configure_phase_providers",
    "configure_provider",
    "current_provider_name",
    "get_active_provider",
    "get_phase_provider",
    "get_provider",
    "register",
    "restore_provider_runtime",
    "snapshot_provider_runtime",
]
