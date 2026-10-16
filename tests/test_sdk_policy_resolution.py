"""Proposal 140 step 4 — the SDK seam resolves effort from the call-policy registry.

Under ``[sdk] call_policy_resolution_enabled`` the public ``query`` /
``create_session`` / ``query_on_session`` delegators replace the call site's
effort with ``call_policy.tier_effort(caller)``, refuse a missing or
unregistered caller, tag each session with its resolved effort, and refuse a
turn whose tier effort differs from its session's. The provider is a mock
returned by a patched ``get_phase_provider``; no network or subprocess.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro import sdk
from maestro.sdk import QueryOptions, QueryResult


class _Session:
    """A plain session double whose instance ``__dict__`` holds real tags."""


@pytest.fixture
def provider() -> Iterator[MagicMock]:
    """Patch the phase and active provider lookups with one mock provider."""
    mock = MagicMock()
    mock.query = AsyncMock(return_value=QueryResult(response="result"))
    mock.create_session = AsyncMock(side_effect=lambda *a, **k: _Session())
    mock.query_on_session = AsyncMock(return_value=QueryResult(response="turn"))
    with (
        patch("maestro.providers.registry.get_phase_provider", return_value=mock),
        patch("maestro.providers.registry.get_active_provider", return_value=mock),
    ):
        yield mock


@pytest.fixture
def enabled() -> None:
    """Turn the seam resolution on (the conftest owner restores it)."""
    sdk.configure_call_policy_resolution(True)


def _sent_options(mock: AsyncMock) -> Any:
    return mock.call_args.kwargs["options"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize(
    ("caller", "effort"),
    [("execute_step", "low"), ("self_review", "high"), ("propose_plan", "medium")],
)
async def test_query_resolves_tier_effort(
    provider: MagicMock, caller: str, effort: str,
) -> None:
    options = QueryOptions(caller=caller)
    await sdk.query("p", options=options)
    sent = _sent_options(provider.query)
    assert sent.effort == effort
    assert sent is not options
    assert options.effort is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize(
    ("caller", "effort"),
    [("execute_step", "low"), ("self_review", "high"), ("propose_plan", "medium")],
)
async def test_create_session_resolves_and_tags(
    provider: MagicMock, caller: str, effort: str,
) -> None:
    options = QueryOptions(caller=caller, effort="max")
    session = await sdk.create_session(options=options)
    assert _sent_options(provider.create_session).effort == effort
    assert session.__dict__[sdk._SESSION_EFFORT_ATTR] == effort
    assert options.effort == "max"


@pytest.mark.asyncio
@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize(
    "options", [None, QueryOptions(), QueryOptions(caller="not_a_registered_caller")],
)
async def test_unresolvable_call_is_refused(
    provider: MagicMock, options: QueryOptions | None,
) -> None:
    with pytest.raises(ValueError, match="call_policy_resolution_enabled"):
        await sdk.query("p", options=options)
    with pytest.raises(ValueError, match="call_policy_resolution_enabled"):
        await sdk.create_session(options=options)
    provider.query.assert_not_called()
    provider.create_session.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enabled")
async def test_matching_turn_dispatches(provider: MagicMock) -> None:
    session = await sdk.create_session(
        options=QueryOptions(caller="implement_plan"),
    )
    result = await sdk.query_on_session(session, "p", caller="auto_fix")
    assert result.response == "turn"
    provider.query_on_session.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enabled")
async def test_mismatched_turn_raises(provider: MagicMock) -> None:
    session = await sdk.create_session(
        options=QueryOptions(caller="implement_plan"),
    )
    with pytest.raises(ValueError, match="self_review"):
        await sdk.query_on_session(session, "p", caller="self_review")
    provider.query_on_session.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enabled")
async def test_none_caller_turn_raises(provider: MagicMock) -> None:
    session = await sdk.create_session(
        options=QueryOptions(caller="implement_plan"),
    )
    with pytest.raises(ValueError, match="no caller tag"):
        await sdk.query_on_session(session, "p")
    provider.query_on_session.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize("session", [_Session(), MagicMock(), object()])
async def test_untagged_session_raises(provider: MagicMock, session: Any) -> None:
    with pytest.raises(ValueError, match="opened at None"):
        await sdk.query_on_session(session, "p", caller="auto_fix")
    provider.query_on_session.assert_not_called()


@pytest.mark.asyncio
async def test_flag_off_passes_options_through(provider: MagicMock) -> None:
    options = QueryOptions(caller="execute_step", effort="max")
    await sdk.query("p", options=options)
    assert _sent_options(provider.query) is options
    session = await sdk.create_session(options=options)
    assert _sent_options(provider.create_session) is options
    assert sdk._SESSION_EFFORT_ATTR not in session.__dict__
    await sdk.query_on_session(session, "p", caller="self_review")
    provider.query_on_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_flag_off_none_options_session_untagged(provider: MagicMock) -> None:
    session = await sdk.create_session()
    assert sdk._SESSION_EFFORT_ATTR not in session.__dict__
    await sdk.query("p")
    assert _sent_options(provider.query) is None


def test_configure_toggles_global() -> None:
    sdk.configure_call_policy_resolution(True)
    assert sdk._call_policy_resolution_enabled is True
    sdk.configure_call_policy_resolution(False)
    assert sdk._call_policy_resolution_enabled is False



@pytest.mark.asyncio
@pytest.mark.usefixtures("enabled")
async def test_single_use_call_resolves_to_tier_and_dispatches(
    provider: MagicMock,
) -> None:
    from maestro.call_policy import CALL_POLICIES, single_use_call

    provider.disconnect_session = AsyncMock()
    assert CALL_POLICIES["propose_plan"].effort == "high"
    result = await single_use_call("propose_plan", "p", system=None)
    assert result.response == "turn"
    assert _sent_options(provider.create_session).effort == "medium"
    provider.query_on_session.assert_awaited_once()
    provider.disconnect_session.assert_awaited_once()
