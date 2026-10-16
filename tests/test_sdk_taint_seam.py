"""Proposal 150 step 4 — the SDK seam stamps ``QueryResult.untrusted_input``.

The provider is a mock returned by a patched ``get_phase_provider``; no network
or subprocess.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro import sdk
from maestro.sdk import QueryResult
from maestro.taint import mark_untrusted

FENCED = f"quote this: {mark_untrusted('gap_issue', 'abc', 'repo')}"


class _Session:
    """A plain session double whose instance ``__dict__`` holds real tags."""


@pytest.fixture
def provider() -> Iterator[MagicMock]:
    mock = MagicMock()
    mock.query = AsyncMock(side_effect=lambda *a, **k: QueryResult(response="r"))
    mock.create_session = AsyncMock(side_effect=lambda *a, **k: _Session())
    mock.query_on_session = AsyncMock(
        side_effect=lambda *a, **k: QueryResult(response="t"),
    )
    with (
        patch("maestro.providers.registry.get_phase_provider", return_value=mock),
        patch("maestro.providers.registry.get_active_provider", return_value=mock),
    ):
        yield mock


def test_constructor_cannot_set_taint() -> None:
    assert QueryResult(response="x").untrusted_input is False
    with pytest.raises(TypeError):
        QueryResult(response="x", untrusted_input=True)  # type: ignore[call-arg]


@pytest.mark.asyncio
@pytest.mark.usefixtures("provider")
async def test_query_clean_is_false() -> None:
    result = await sdk.query("clean", system="sys")
    assert result.untrusted_input is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("provider")
async def test_query_fenced_prompt_is_true() -> None:
    result = await sdk.query(FENCED)
    assert result.untrusted_input is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("provider")
async def test_query_fenced_system_is_true() -> None:
    result = await sdk.query("clean", system=FENCED)
    assert result.untrusted_input is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("provider")
async def test_clean_session_turn_is_false() -> None:
    session = await sdk.create_session("clean")
    result = await sdk.query_on_session(session, "turn")
    assert result.untrusted_input is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("provider")
async def test_fenced_initial_prompt_taints_later_turns() -> None:
    session = await sdk.create_session(FENCED)
    result = await sdk.query_on_session(session, "turn")
    assert result.untrusted_input is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("provider")
async def test_fenced_session_system_taints_later_turns() -> None:
    session = await sdk.create_session(None, system=FENCED)
    result = await sdk.query_on_session(session, "turn")
    assert result.untrusted_input is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("provider")
async def test_taint_is_sticky_after_one_fenced_turn() -> None:
    session = await sdk.create_session("clean")
    first = await sdk.query_on_session(session, FENCED)
    later = await sdk.query_on_session(session, "clean again")
    assert first.untrusted_input is True
    assert later.untrusted_input is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("provider")
async def test_untagged_mock_session_reads_false() -> None:
    session = MagicMock()
    result = await sdk.query_on_session(session, "turn")
    assert result.untrusted_input is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("provider")
async def test_slotted_session_without_dict_reads_false() -> None:
    class _Slotted:
        __slots__ = ("_maestro_provider", "_maestro_untrusted_input")

    result = await sdk.query_on_session(_Slotted(), "turn")  # type: ignore[arg-type]
    assert result.untrusted_input is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("provider")
async def test_slotted_session_keeps_creation_taint() -> None:
    class _Slotted:
        __slots__ = ("_maestro_provider", "_maestro_untrusted_input")

    session = _Slotted()
    session._maestro_untrusted_input = True
    result = await sdk.query_on_session(session, "clean turn")  # type: ignore[arg-type]
    assert result.untrusted_input is True


@pytest.fixture
def clean_ids() -> Iterator[dict[str, None]]:
    with patch.dict(sdk._CLEAN_SESSION_IDS, clear=True):
        yield sdk._CLEAN_SESSION_IDS


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_ids")
async def test_raising_fenced_turn_still_taints(provider: MagicMock) -> None:
    session = await sdk.create_session("clean")
    provider.query_on_session.side_effect = RuntimeError("stall")
    with pytest.raises(RuntimeError, match="stall"):
        await sdk.query_on_session(session, FENCED)
    provider.query_on_session.side_effect = lambda *a, **k: QueryResult(response="t")
    result = await sdk.query_on_session(session, "resume in place")
    assert result.untrusted_input is True


async def _session_with_id(sid: str, prompt: str) -> _Session:
    session = await sdk.create_session("clean")
    session._maestro_last_session_id = sid  # type: ignore[attr-defined]
    await sdk.query_on_session(session, prompt)
    return session


@pytest.mark.asyncio
@pytest.mark.usefixtures("provider")
async def test_resumed_clean_session_stays_clean(clean_ids: dict[str, None]) -> None:
    await _session_with_id("sid-clean", "turn")
    assert "sid-clean" in clean_ids
    resumed = await sdk.create_session(None, resume_session_id="sid-clean")
    result = await sdk.query_on_session(resumed, "turn")
    assert result.untrusted_input is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("provider")
async def test_resumed_fenced_session_carries_taint(clean_ids: dict[str, None]) -> None:
    session = await _session_with_id("sid-f", "turn")
    await sdk.query_on_session(session, FENCED)
    assert "sid-f" not in clean_ids
    resumed = await sdk.create_session(None, resume_session_id="sid-f")
    result = await sdk.query_on_session(resumed, "turn")
    assert result.untrusted_input is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("provider", "clean_ids")
async def test_resumed_unknown_session_is_tainted() -> None:
    resumed = await sdk.create_session(None, resume_session_id="from-before-restart")
    result = await sdk.query_on_session(resumed, "turn")
    assert result.untrusted_input is True


def test_clean_session_ids_are_capped(clean_ids: dict[str, None]) -> None:
    with patch.object(sdk, "_CLEAN_SESSION_IDS_CAP", 2):
        for sid in ("a", "b", "c"):
            session = _Session()
            session._maestro_last_session_id = sid  # type: ignore[attr-defined]
            sdk._record_session_taint(session, False)
    assert list(clean_ids) == ["b", "c"]


def test_clean_session_cap_evicts_least_recently_used(
    clean_ids: dict[str, None],
) -> None:
    with patch.object(sdk, "_CLEAN_SESSION_IDS_CAP", 2):
        for sid in ("a", "b", "a", "c"):
            session = _Session()
            session._maestro_last_session_id = sid  # type: ignore[attr-defined]
            sdk._record_session_taint(session, False)
    assert list(clean_ids) == ["a", "c"]


@pytest.mark.asyncio
async def test_resumed_session_raising_fenced_turn_taints_its_id(
    provider: MagicMock, clean_ids: dict[str, None],
) -> None:
    await _session_with_id("sid-r", "turn")
    resumed = await sdk.create_session(None, resume_session_id="sid-r")
    provider.query_on_session.side_effect = RuntimeError("stall")
    with pytest.raises(RuntimeError, match="stall"):
        await sdk.query_on_session(resumed, FENCED)
    assert "sid-r" not in clean_ids
    again = await sdk.create_session(None, resume_session_id="sid-r")
    provider.query_on_session.side_effect = lambda *a, **k: QueryResult(response="t")
    result = await sdk.query_on_session(again, "turn")
    assert result.untrusted_input is True
