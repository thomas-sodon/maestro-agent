"""Tests for the provider selection registry (proposal 058, Phase 2 Step 5).

The registry (:mod:`maestro.providers.registry`) is the seam through which
``sdk.py``'s four public functions reach a :class:`~maestro.providers.base.Provider`.
These tests pin its inert default (``"anthropic"``), the cached-singleton
resolution, the fail-loud rejection of unknown names, and the ``register`` hook
later providers use to join.

The registry holds process-global state (``_active_provider_name`` / ``_INSTANCES``
/ ``_REGISTRY`` / ``_phase_provider_names``); the autouse fixture snapshots and
restores all four so tests stay order-independent.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from maestro import sdk
from maestro.providers import registry
from maestro.providers.anthropic import AnthropicProvider
from maestro.providers.base import Capabilities, Provider
from maestro.sdk import QueryResult


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


class _FakeProvider:
    """Minimal :class:`Provider`-satisfying stand-in for selection tests."""

    capabilities = Capabilities(
        sessions=False,
        in_process_tools=False,
        remote_mcp=False,
        extended_thinking=False,
        long_context=False,
        structured_output=False,
        usage_metering=False,
        streaming=False,
    )

    async def query(self, *args: object, **kwargs: object) -> object:
        return None

    async def create_session(self, *args: object, **kwargs: object) -> object:
        return None

    async def query_on_session(self, *args: object, **kwargs: object) -> object:
        return None

    async def disconnect_session(self, *args: object, **kwargs: object) -> None:
        return None


class TestDefault:
    """The inert default resolves to the Anthropic provider."""

    def test_default_provider_name_is_anthropic(self) -> None:
        assert registry.current_provider_name() == "anthropic"

    def test_anthropic_resolves_to_anthropic_provider(self) -> None:
        assert isinstance(registry.get_active_provider(), AnthropicProvider)

    def test_active_provider_satisfies_protocol(self) -> None:
        """``get_active_provider`` returns a structurally-valid Provider."""
        assert isinstance(registry.get_active_provider(), Provider)


class TestSingletonCache:
    """The factory is stateless, so one instance is reused per name."""

    def test_get_active_provider_caches_singleton(self) -> None:
        first = registry.get_active_provider()
        second = registry.get_active_provider()
        assert first is second


class TestConfigure:
    """Selection validates against the registry and fails loud on unknowns."""

    def test_configure_provider_rejects_unknown_name(self) -> None:
        with pytest.raises(ValueError, match="unknown provider 'nope'") as exc:
            registry.configure_provider("nope")
        # The remediation message lists the known names so the operator can fix it.
        assert "anthropic" in str(exc.value)

    def test_unknown_name_does_not_mutate_active(self) -> None:
        with pytest.raises(ValueError):
            registry.configure_provider("nope")
        assert registry.current_provider_name() == "anthropic"

    def test_configure_provider_selects_registered(self) -> None:
        registry.register("fake", _FakeProvider)
        registry.configure_provider("fake")
        assert registry.current_provider_name() == "fake"
        assert isinstance(registry.get_active_provider(), _FakeProvider)

    @pytest.mark.parametrize("name", ["openai"])
    def test_schema_valid_names_fail_loud_until_registered(self, name: str) -> None:
        """`openai` validates at config-schema level but is not yet registered
        (Phase 3 leaves it unwired until the tool adapter is threaded into
        ``query``), so selection fails loud here — the transitional posture
        pinned by proposal 058 Step 6. ``cli_paced`` was registered by Step 12,
        so it is no longer in this set (see :class:`TestCliPaced`)."""
        assert name not in registry._REGISTRY
        with pytest.raises(ValueError, match=f"unknown provider {name!r}"):
            registry.configure_provider(name)

    def test_registering_then_selecting_openai_resolves(self) -> None:
        registry.register("openai", _FakeProvider)
        registry.configure_provider("openai")
        assert isinstance(registry.get_active_provider(), _FakeProvider)


class TestCliPaced:
    """``cli_paced`` is registered (proposal 058 Step 12) behind a lazy factory.

    Unlike ``openai`` (still unregistered), selecting ``cli_paced`` resolves to
    the re-homed :class:`~maestro.providers.cli_paced.CliPacedProvider`. The
    lazy factory keeps the ``maestro_sdk`` import off the default path until the
    provider is actually built here.
    """

    def test_cli_paced_is_registered(self) -> None:
        assert "cli_paced" in registry._REGISTRY

    def test_configure_then_resolve_cli_paced(self) -> None:
        from maestro.providers.cli_paced import CliPacedProvider

        registry.configure_provider("cli_paced")
        assert registry.current_provider_name() == "cli_paced"
        assert isinstance(registry.get_active_provider(), CliPacedProvider)

    def test_cli_paced_provider_satisfies_protocol(self) -> None:
        assert isinstance(registry.get_provider("cli_paced"), Provider)


class TestRegister:
    """The ``register`` hook adds a factory and drops any stale cache."""

    def test_register_adds_factory(self) -> None:
        registry.register("fake", _FakeProvider)
        # A freshly-registered name is now selectable.
        registry.configure_provider("fake")
        assert isinstance(registry.get_active_provider(), _FakeProvider)

    def test_register_replaces_factory_and_drops_cached_instance(self) -> None:
        registry.register("fake", _FakeProvider)
        registry.configure_provider("fake")
        original = registry.get_active_provider()

        class _OtherFake(_FakeProvider):
            pass

        registry.register("fake", _OtherFake)
        replacement = registry.get_active_provider()
        # Re-registering dropped the cached instance, so the next resolve
        # rebuilds from the new factory.
        assert replacement is not original
        assert isinstance(replacement, _OtherFake)


class TestGetProvider:
    """``get_provider`` fetches an arbitrary registered name, fail-loud on unknowns.

    Capability validation (Step 7) uses it to inspect a *per-phase* provider's
    capabilities without making it the active selection.
    """

    def test_get_provider_returns_registered_instance(self) -> None:
        registry.register("fake", _FakeProvider)
        assert isinstance(registry.get_provider("fake"), _FakeProvider)

    def test_get_provider_caches_singleton(self) -> None:
        registry.register("fake", _FakeProvider)
        first = registry.get_provider("fake")
        second = registry.get_provider("fake")
        assert first is second

    def test_get_provider_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown provider 'nope'") as exc:
            registry.get_provider("nope")
        assert "anthropic" in str(exc.value)

    def test_get_active_provider_delegates_to_get_provider(self) -> None:
        # The active default resolves through the same path as get_provider.
        assert registry.get_provider("anthropic") is registry.get_active_provider()


class TestSdkDelegation:
    """``sdk.py``'s four public functions route through ``get_active_provider``.

    This is the Step 5 acceptance pin: with the active provider patched to a
    mock, each ``sdk.*`` call forwards verbatim to the matching provider method.
    The lazy ``from maestro.providers.registry import get_active_provider`` inside
    each ``sdk`` body resolves the patched attribute at call time.
    """

    @staticmethod
    def _mock_provider() -> Mock:
        provider = Mock()
        provider.query = AsyncMock(return_value=QueryResult(response="q-result"))
        # ``create_session`` returns a *taggable* object: the seam stamps the
        # opening provider on it (``_SESSION_PROVIDER_ATTR``), which a bare ``str``
        # would reject (no ``__dict__``). A real session always accepts the tag.
        provider.create_session = AsyncMock(return_value=SimpleNamespace())
        provider.query_on_session = AsyncMock(return_value=QueryResult(response="qos-result"))
        provider.disconnect_session = AsyncMock(return_value=None)
        return provider

    @pytest.mark.asyncio
    async def test_query_routes_to_active_provider(self) -> None:
        provider = self._mock_provider()

        def _complete(_client: object, _text: str) -> bool:
            return True

        with patch.object(registry, "get_active_provider", return_value=provider):
            result = await sdk.query(
                "hello",
                system="sys",
                complete_on=_complete,
                stream_idle_timeout_seconds=4.0,
            )

        assert result.response == "q-result"
        provider.query.assert_awaited_once_with(
            "hello",
            options=None,
            system="sys",
            complete_on=_complete,
            stream_idle_timeout_seconds=4.0,
        )

    @pytest.mark.asyncio
    async def test_create_session_routes_to_active_provider(self) -> None:
        provider = self._mock_provider()

        with patch.object(registry, "get_active_provider", return_value=provider):
            result = await sdk.create_session(
                "init",
                system="sys",
                resume_session_id="sess-1",
            )

        # No ``phase`` → the seam resolves through ``get_phase_provider(None)``,
        # which falls back to the (patched) active provider.
        assert result is provider.create_session.return_value
        provider.create_session.assert_awaited_once_with(
            "init",
            options=None,
            system="sys",
            resume_session_id="sess-1",
        )
        # The session is tagged with the provider that opened it so a later
        # ``query_on_session`` / ``disconnect_session`` routes back to it.
        assert getattr(result, sdk._SESSION_PROVIDER_ATTR) is provider

    @pytest.mark.asyncio
    async def test_query_on_session_routes_to_active_provider(self) -> None:
        provider = self._mock_provider()
        session = object()

        with patch.object(registry, "get_active_provider", return_value=provider):
            result = await sdk.query_on_session(
                session,
                "follow-up",
                caller="phase",
                stream_idle_timeout_seconds=2.0,
            )

        assert result.response == "qos-result"
        provider.query_on_session.assert_awaited_once_with(
            session,
            "follow-up",
            caller="phase",
            complete_on=None,
            stream_idle_timeout_seconds=2.0,
            bypass_warning_throttle=False,
        )

    @pytest.mark.asyncio
    async def test_disconnect_session_routes_to_active_provider(self) -> None:
        provider = self._mock_provider()
        session = object()

        with patch.object(registry, "get_active_provider", return_value=provider):
            result = await sdk.disconnect_session(session)

        assert result is None
        provider.disconnect_session.assert_awaited_once_with(session)


class TestDefaultProviderNoRecursion:
    """The default Anthropic provider reaches the ``_*_impl`` bodies, not the seam.

    ``TestSdkDelegation`` patches ``get_active_provider`` with a ``Mock`` (the real
    provider never runs); ``test_provider_anthropic`` patches the impls and calls
    ``provider.query`` directly (the registry seam never runs). Neither exercises
    the full ``sdk.query → AnthropicProvider.query → sdk._query_impl`` path with the
    *real* default provider active — which is exactly where a regression would
    recurse (``sdk.query → provider.query → sdk.query → …``) if the provider ever
    targeted the public functions instead of the impls. These tests pin that the
    composition terminates at the impl, awaited exactly once.
    """

    @pytest.mark.asyncio
    async def test_query_reaches_impl_once(self) -> None:
        # No patch on get_active_provider: the inert default AnthropicProvider runs.
        assert isinstance(registry.get_active_provider(), AnthropicProvider)
        with patch.object(sdk, "_query_impl", AsyncMock(return_value=QueryResult(response="r"))) as impl:
            assert (await sdk.query("hi")).response == "r"
        impl.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_session_reaches_impl_once(self) -> None:
        # A taggable sentinel: the seam stamps the opening provider on the
        # returned session, which a bare ``str`` would reject.
        sentinel = SimpleNamespace()
        with patch.object(
            sdk, "_create_session_impl", AsyncMock(return_value=sentinel),
        ) as impl:
            assert await sdk.create_session("init") is sentinel
        impl.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_query_on_session_reaches_impl_once(self) -> None:
        session = object()
        with patch.object(
            sdk, "_query_on_session_impl", AsyncMock(return_value=QueryResult(response="qos")),
        ) as impl:
            assert (await sdk.query_on_session(session, "follow-up")).response == "qos"
        impl.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disconnect_session_reaches_impl_once(self) -> None:
        session = object()
        with patch.object(
            sdk, "_disconnect_session_impl", AsyncMock(return_value=None),
        ) as impl:
            assert await sdk.disconnect_session(session) is None
        impl.assert_awaited_once()


class TestConfigurePhaseProviders:
    """``configure_phase_providers`` installs the per-phase override map (058 Step 6 runtime).

    The map is the runtime counterpart to startup-only ``Config.provider_for_phase``:
    it is what ``get_phase_provider`` reads so the ``sdk.py`` seam routes a phase's
    session to its ``[sdk] <phase>_provider`` override. Names are re-checked against
    the registry here, fail-loud on an unregistered provider.
    """

    def test_empty_map_is_inert(self) -> None:
        registry.configure_phase_providers({})
        assert registry._phase_provider_names == {}

    def test_installs_validated_map(self) -> None:
        registry.configure_phase_providers({"contemplation": "cli_paced"})
        assert registry._phase_provider_names == {"contemplation": "cli_paced"}

    def test_rejects_unregistered_provider(self) -> None:
        # ``openai`` is schema-valid but unregistered → fail loud, mirroring
        # ``configure_provider`` for the global selection.
        assert "openai" not in registry._REGISTRY
        with pytest.raises(
            ValueError,
            match="unknown provider 'openai' for phase 'evolution'",
        ):
            registry.configure_phase_providers({"evolution": "openai"})

    def test_unknown_does_not_mutate_existing_map(self) -> None:
        registry.configure_phase_providers({"contemplation": "cli_paced"})
        with pytest.raises(ValueError):
            registry.configure_phase_providers({"evolution": "nope"})
        # Validation builds a fresh dict and only swaps it in after every name
        # passes, so a rejected reconfigure leaves the prior map intact.
        assert registry._phase_provider_names == {"contemplation": "cli_paced"}

    def test_replaces_prior_map(self) -> None:
        registry.configure_phase_providers({"contemplation": "cli_paced"})
        registry.configure_phase_providers({"evolution": "cli_paced"})
        assert registry._phase_provider_names == {"evolution": "cli_paced"}


class TestGetPhaseProvider:
    """``get_phase_provider`` routes an overridden phase, else falls back to active."""

    def test_none_phase_returns_active(self) -> None:
        assert registry.get_phase_provider(None) is registry.get_active_provider()

    def test_unoverridden_phase_returns_active(self) -> None:
        registry.configure_phase_providers({"contemplation": "cli_paced"})
        assert registry.get_phase_provider("evolution") is registry.get_active_provider()

    def test_overridden_phase_returns_that_provider(self) -> None:
        registry.register("fake", _FakeProvider)
        registry.configure_phase_providers({"contemplation": "fake"})
        provider = registry.get_phase_provider("contemplation")
        assert isinstance(provider, _FakeProvider)
        # Genuinely distinct from the active (default anthropic) provider.
        assert provider is not registry.get_active_provider()

    def test_overridden_phase_is_cached_singleton(self) -> None:
        registry.register("fake", _FakeProvider)
        registry.configure_phase_providers({"run_task": "fake"})
        first = registry.get_phase_provider("run_task")
        second = registry.get_phase_provider("run_task")
        assert first is second


class TestSdkPhaseRouting:
    """The seam routes per-phase via the override map and the session tag (058 Step 6 runtime).

    ``create_session`` / ``query`` resolve the provider through
    ``get_phase_provider(phase)``; the opened session is tagged so
    ``query_on_session`` / ``disconnect_session`` route every later operation back
    to the same provider, independent of the active selection or the current phase.
    """

    @staticmethod
    def _taggable_provider() -> Mock:
        provider = Mock()
        provider.query = AsyncMock(return_value=QueryResult(response="q"))
        provider.create_session = AsyncMock(return_value=SimpleNamespace())
        provider.query_on_session = AsyncMock(return_value=QueryResult(response="qos"))
        provider.disconnect_session = AsyncMock(return_value=None)
        return provider

    @pytest.mark.asyncio
    async def test_create_session_routes_via_phase_override(self) -> None:
        phase_provider = self._taggable_provider()
        registry.register("fake", lambda: phase_provider)
        registry.configure_phase_providers({"contemplation": "fake"})

        session = await sdk.create_session("init", phase="contemplation")

        phase_provider.create_session.assert_awaited_once()
        # The returned session carries the phase provider's tag.
        assert getattr(session, sdk._SESSION_PROVIDER_ATTR) is phase_provider

    @pytest.mark.asyncio
    async def test_create_session_unoverridden_phase_uses_active(self) -> None:
        phase_provider = self._taggable_provider()
        active = self._taggable_provider()
        registry.register("fake", lambda: phase_provider)
        registry.configure_phase_providers({"contemplation": "fake"})

        with patch.object(registry, "get_active_provider", return_value=active):
            session = await sdk.create_session("init", phase="evolution")

        active.create_session.assert_awaited_once()
        phase_provider.create_session.assert_not_called()
        assert getattr(session, sdk._SESSION_PROVIDER_ATTR) is active

    @pytest.mark.asyncio
    async def test_query_routes_via_phase_override(self) -> None:
        phase_provider = self._taggable_provider()
        registry.register("fake", lambda: phase_provider)
        registry.configure_phase_providers({"evolution": "fake"})

        result = await sdk.query("hi", phase="evolution")

        assert result.response == "q"
        phase_provider.query.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_query_on_session_routes_via_session_tag(self) -> None:
        # A session tagged with one provider routes its follow-up there even when
        # the active/global provider is a different one (cross-provider safety).
        tagged = self._taggable_provider()
        active = self._taggable_provider()
        session = SimpleNamespace(**{sdk._SESSION_PROVIDER_ATTR: tagged})

        with patch.object(registry, "get_active_provider", return_value=active):
            result = await sdk.query_on_session(
                session, "follow-up", caller="contemplation",
            )

        assert result.response == "qos"
        tagged.query_on_session.assert_awaited_once()
        active.query_on_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_disconnect_session_routes_via_session_tag(self) -> None:
        tagged = self._taggable_provider()
        active = self._taggable_provider()
        session = SimpleNamespace(**{sdk._SESSION_PROVIDER_ATTR: tagged})

        with patch.object(registry, "get_active_provider", return_value=active):
            await sdk.disconnect_session(session)

        tagged.disconnect_session.assert_awaited_once_with(session)
        active.disconnect_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_untagged_session_falls_back_to_active(self) -> None:
        # ``object()`` has no tag (and no ``__dict__``) → the active provider runs.
        active = self._taggable_provider()
        session = object()

        with patch.object(registry, "get_active_provider", return_value=active):
            await sdk.query_on_session(session, "follow-up")

        active.query_on_session.assert_awaited_once()
