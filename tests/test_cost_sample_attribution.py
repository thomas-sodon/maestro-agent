"""Proposal 140: every cost sample records the effort and model that served it.

``effort`` is the effort that actually reached the provider — the session's
creation-time effort on a session turn, the call's own on a one-shot call,
``"default"`` when none was sent — and ``model`` is the resolved model already
on :class:`~maestro.sdk.QueryUsage`. Legacy rows read back ``None``/``None``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk.types import ResultMessage

from maestro import sdk
from maestro.cost_tracking import CostSample, _cost_sample_from_dict
from maestro.providers.openai import OpenAIProvider
from maestro.sdk import QueryOptions, QueryUsage
from maestro.state_store import StateStore, StateStoreSchemaError


def _result_msg(text: str = "ok") -> ResultMessage:
    return ResultMessage(
        subtype="result",
        duration_ms=100,
        duration_api_ms=80,
        is_error=False,
        num_turns=1,
        session_id="s",
        total_cost_usd=0.01,
        usage={"input_tokens": 10, "output_tokens": 5},
        result=text,
    )


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Capture cost samples ``_log_sdk_call`` appends; no usage sink."""
    captured = MagicMock()
    monkeypatch.setattr(sdk, "_cost_sample_backend", captured)
    monkeypatch.setattr(sdk, "_usage_sink", None)
    return captured


def _sample(backend: MagicMock) -> CostSample:
    backend.append.assert_called_once()
    sample = backend.append.call_args.args[0]
    assert isinstance(sample, CostSample)
    return sample


class _FakeSession:
    """A session double carrying only what ``_create_session_impl`` stamps."""

    def __init__(self, **sidecars: Any) -> None:
        for name, value in sidecars.items():
            setattr(self, name, value)
        self.query = AsyncMock()

    async def receive_messages(self) -> Any:
        yield _result_msg()


# ---------------------------------------------------------------------------
# Anthropic path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_stamps_effort_sidecar() -> None:
    client = MagicMock()
    client.connect = AsyncMock()
    with patch("maestro.sdk.ClaudeSDKClient", side_effect=lambda **_: client):
        await sdk.create_session("hi", options=QueryOptions(effort="high"))
    assert client._maestro_effort == "high"


@pytest.mark.asyncio
async def test_session_turn_records_session_effort_not_turn_intent(
    backend: MagicMock,
) -> None:
    """The key assertion: a turn on a session opened at ``high`` records
    ``high`` — the effort the model received — whatever its call site declared
    (a turn has no effort channel; ``query_on_session`` takes none)."""
    session = _FakeSession(
        _maestro_effort="high", _maestro_model="claude-opus-4-8",
        _maestro_betas=None,
    )
    await sdk.query_on_session(session, "p", caller="introspect")  # type: ignore[arg-type]
    sample = _sample(backend)
    assert sample.effort == "high"
    assert sample.model == "claude-opus-4-8"


@pytest.mark.asyncio
async def test_magicmock_session_records_default_not_a_mock(
    backend: MagicMock,
) -> None:
    session = MagicMock()
    session.query = AsyncMock()

    async def receive() -> Any:
        yield _result_msg()

    session.receive_messages = receive
    await sdk.query_on_session(session, "p", caller="c")
    sample = _sample(backend)
    assert sample.effort == "default"
    assert sample.model is None


@pytest.mark.asyncio
async def test_non_str_stamped_effort_records_default(backend: MagicMock) -> None:
    session = _FakeSession(_maestro_effort=5)
    await sdk.query_on_session(session, "p", caller="c")  # type: ignore[arg-type]
    assert _sample(backend).effort == "default"


@pytest.mark.asyncio
async def test_one_shot_records_call_effort(backend: MagicMock) -> None:
    async def gen(**_: Any) -> Any:
        yield _result_msg()

    with patch("maestro.sdk._query", side_effect=gen):
        await sdk.query("p", options=QueryOptions(effort="medium"))
    assert _sample(backend).effort == "medium"


@pytest.mark.asyncio
async def test_one_shot_without_effort_records_default(backend: MagicMock) -> None:
    async def gen(**_: Any) -> Any:
        yield _result_msg()

    with patch("maestro.sdk._query", side_effect=gen):
        await sdk.query("p", options=QueryOptions(caller="execute_step"))
    assert _sample(backend).effort == "default"


def test_log_sdk_call_persists_model(backend: MagicMock) -> None:
    sdk._log_sdk_call(QueryUsage(caller="c", model="claude-opus-4-8[1m]"))
    sample = _sample(backend)
    assert sample.model == "claude-opus-4-8[1m]"
    assert sample.effort == "default"


# ---------------------------------------------------------------------------
# Other providers
# ---------------------------------------------------------------------------


def _openai_client(*responses: object) -> object:
    return SimpleNamespace(
        responses=SimpleNamespace(create=AsyncMock(side_effect=list(responses))),
    )


def _openai_response() -> SimpleNamespace:
    return SimpleNamespace(id="r", output_text="x", model="gpt-5", usage=None)


@pytest.mark.asyncio
async def test_openai_one_shot_carries_options_effort(backend: MagicMock) -> None:
    provider = OpenAIProvider(client=_openai_client(_openai_response()))
    result = await provider.query("p", options=QueryOptions(effort="low"))
    assert result.usage.effort == "low"
    assert _sample(backend).effort == "low"


@pytest.mark.asyncio
async def test_openai_one_shot_without_options_records_default(
    backend: MagicMock,
) -> None:
    provider = OpenAIProvider(client=_openai_client(_openai_response()))
    result = await provider.query("p")
    assert result.usage.effort is None
    assert _sample(backend).effort == "default"


@pytest.mark.asyncio
async def test_openai_session_turn_carries_session_effort(
    backend: MagicMock,
) -> None:
    provider = OpenAIProvider(client=_openai_client(_openai_response()))
    session = await provider.create_session(options=QueryOptions(effort="high"))
    result = await provider.query_on_session(session, "p", caller="c")
    assert result.usage.effort == "high"
    assert _sample(backend).effort == "high"


@pytest.mark.asyncio
async def test_openai_session_without_options_records_none() -> None:
    provider = OpenAIProvider(client=_openai_client(_openai_response()))
    session = await provider.create_session()
    session.options = None
    with patch.object(sdk, "_log_sdk_call"):
        result = await provider.query_on_session(session, "p", caller="c")
    assert result.usage.effort is None


# ---------------------------------------------------------------------------
# JSONL decode
# ---------------------------------------------------------------------------


_BASE_ROW: dict[str, object] = {
    "timestamp": "2026-09-27T00:00:00+00:00",
    "cost_usd": 0.1,
    "input_tokens": 1,
    "output_tokens": 1,
    "cache_read_tokens": 0,
    "cache_creation_tokens": 0,
    "caller": "c",
    "duration_ms": 1,
}


def test_legacy_row_decodes_to_none() -> None:
    sample = _cost_sample_from_dict(dict(_BASE_ROW), Path("p"))
    assert sample is not None
    assert sample.effort is None
    assert sample.model is None


def test_non_str_fields_decode_to_none() -> None:
    sample = _cost_sample_from_dict(
        {**_BASE_ROW, "effort": 3, "model": []}, Path("p"),
    )
    assert sample is not None
    assert sample.effort is None
    assert sample.model is None


def test_attributed_row_decodes() -> None:
    sample = _cost_sample_from_dict(
        {**_BASE_ROW, "effort": "high", "model": "claude-opus-4-8"}, Path("p"),
    )
    assert sample is not None
    assert sample.effort == "high"
    assert sample.model == "claude-opus-4-8"


# ---------------------------------------------------------------------------
# SQLite store
# ---------------------------------------------------------------------------


def _cost_sample(**overrides: Any) -> CostSample:
    fields: dict[str, Any] = {**_BASE_ROW, **overrides}
    return CostSample(**fields)


def test_sqlite_round_trips_effort_and_model() -> None:
    store = StateStore(":memory:")
    store.append_cost_sample(_cost_sample(effort="high", model="claude-opus-4-8"))
    loaded = store.recent_cost_samples(5)
    assert loaded[0].effort == "high"
    assert loaded[0].model == "claude-opus-4-8"
    windowed = store.load_cost_samples_since(0.0)
    assert windowed[0].effort == "high"
    assert windowed[0].model == "claude-opus-4-8"


_LEGACY_COST_DDL = """
CREATE TABLE cost_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    ts_epoch REAL NOT NULL,
    cost_usd REAL NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cache_creation_tokens INTEGER NOT NULL,
    caller TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    estimated INTEGER NOT NULL,
    unmetered INTEGER NOT NULL,
    provider TEXT
)
"""


def test_legacy_cost_samples_table_is_repaired_on_open() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(_LEGACY_COST_DDL)
    conn.execute(
        "INSERT INTO cost_samples (timestamp, ts_epoch, cost_usd, input_tokens, "
        "output_tokens, cache_read_tokens, cache_creation_tokens, caller, "
        "duration_ms, estimated, unmetered, provider) "
        "VALUES ('2026-09-27T00:00:00+00:00', 1.0, 0.1, 1, 1, 0, 0, 'old', 1, 0, 0, NULL)",
    )
    conn.commit()
    store = StateStore(connection=conn)
    store.append_cost_sample(_cost_sample(effort="low", model="m"))
    loaded = store.recent_cost_samples(5)
    assert loaded[0].caller == "old"
    assert loaded[0].effort is None
    assert loaded[0].model is None
    assert loaded[1].effort == "low"
    assert loaded[1].model == "m"


class _AlterDroppingConnection:
    """Delegates to a real connection but swallows the cost_samples ALTER, so
    the post-repair column check sees the gap."""

    def __init__(self, inner: sqlite3.Connection) -> None:
        object.__setattr__(self, "_inner", inner)

    def execute(self, sql: str, *args: Any) -> Any:
        if sql.startswith("ALTER TABLE cost_samples"):
            return None
        return self._inner.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._inner, name, value)


def test_unrepairable_cost_samples_table_raises() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(_LEGACY_COST_DDL)
    conn.commit()
    with pytest.raises(
        StateStoreSchemaError, match="cost_samples table is missing",
    ) as excinfo:
        StateStore(connection=_AlterDroppingConnection(conn))  # type: ignore[arg-type]
    assert "effort, model" in str(excinfo.value)
