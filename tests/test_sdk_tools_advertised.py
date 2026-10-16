"""``_consume_messages`` captures the CLI's advertised tool surface (proposal 142).

The CLI's init ``SystemMessage`` carries the tool list the session actually has;
the consumer stamps it into the transcript sidecar via
``SessionLogger.set_tools_advertised``. Kept out of ``tests/test_sdk.py`` for the
source-file size ceiling.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from claude_agent_sdk.types import SystemMessage, TaskStartedMessage

from maestro.sdk import _consume_messages


async def _consume(message: Any, session_log: Any) -> None:
    async def gen() -> Any:
        yield message

    # ``stream_idle_timeout_seconds=0`` disables the idle wait_for so no real
    # asyncio timer is armed.
    await _consume_messages(
        gen(), "contemplate", session_log, stream_idle_timeout_seconds=0,
    )


@pytest.mark.asyncio
async def test_init_message_records_advertised_tools() -> None:
    session_log = MagicMock()
    message = SystemMessage(
        subtype="init",
        data={"tools": ["Read", 7, "Monitor"], "model": "m"},
    )
    await _consume(message, session_log)
    session_log.set_tools_advertised.assert_called_once_with(["Read", "Monitor"])


@pytest.mark.asyncio
async def test_non_init_system_message_is_ignored() -> None:
    session_log = MagicMock()
    message = SystemMessage(subtype="status", data={"tools": ["Read"]})
    await _consume(message, session_log)
    session_log.set_tools_advertised.assert_not_called()


@pytest.mark.asyncio
async def test_non_list_tools_payload_is_ignored() -> None:
    session_log = MagicMock()
    message = SystemMessage(subtype="init", data={"tools": "Read"})
    await _consume(message, session_log)
    session_log.set_tools_advertised.assert_not_called()


@pytest.mark.asyncio
async def test_init_message_without_session_log_does_not_raise() -> None:
    message = SystemMessage(subtype="init", data={"tools": ["Read"]})
    await _consume(message, None)


@pytest.mark.asyncio
async def test_task_started_keeps_its_own_branch() -> None:
    session_log = MagicMock()
    message = TaskStartedMessage(
        subtype="init",
        data={"tools": ["Read"]},
        task_id="t",
        description="sub-task",
        uuid="u",
        session_id="s",
    )
    await _consume(message, session_log)
    session_log.set_tools_advertised.assert_not_called()
    session_log.log.assert_any_call("Task started: sub-task")


@pytest.mark.asyncio
async def test_init_message_records_advertised_mcp_server_names() -> None:
    session_log = MagicMock()
    message = SystemMessage(
        subtype="init",
        data={"mcp_servers": [
            {"name": "journal", "status": "connected"}, {"name": 3}, "x",
        ]},
    )
    await _consume(message, session_log)
    session_log.set_mcp_servers_advertised.assert_called_once_with(["journal"])
    session_log.set_tools_advertised.assert_not_called()


@pytest.mark.asyncio
async def test_non_list_mcp_servers_payload_is_ignored() -> None:
    session_log = MagicMock()
    message = SystemMessage(subtype="init", data={"mcp_servers": "journal"})
    await _consume(message, session_log)
    session_log.set_mcp_servers_advertised.assert_not_called()


@pytest.mark.asyncio
async def test_non_init_system_message_stamps_no_servers() -> None:
    session_log = MagicMock()
    message = SystemMessage(
        subtype="status", data={"mcp_servers": [{"name": "journal"}]},
    )
    await _consume(message, session_log)
    session_log.set_mcp_servers_advertised.assert_not_called()
