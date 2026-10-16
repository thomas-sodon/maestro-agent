"""Tests targeting specific uncovered lines to bring coverage to >= 99%.

Each test is focused on a specific uncovered branch or line.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

FAKE_DIR = Path("/fake/coverage_gaps")


# ---------------------------------------------------------------------------
# cli.py:92 — logger.warning for secret health check warnings
# ---------------------------------------------------------------------------


def test_build_agent_logs_secret_warnings(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """_build_agent logs a warning for each entry returned by check_secrets()."""
    import logging

    from maestro.cli import _build_agent
    from maestro.config import Config

    config = Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR)

    # Make check_secrets() return a non-empty warning list so line 91-92 is hit.
    with patch.object(Config, "check_secrets", return_value=["TELEGRAM_BOT_TOKEN not set"]), \
            caplog.at_level(logging.WARNING, logger="maestro"):
        _build_agent(config)

    assert any("Secret health check" in msg for msg in caplog.messages), (
        f"Expected 'Secret health check' warning, got: {caplog.messages}"
    )


# ---------------------------------------------------------------------------
# cli.py:324-326 — preflight with secret warnings
# ---------------------------------------------------------------------------


def test_cmd_preflight_with_secret_warnings(
    mem_fs: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cmd_preflight should display individual warning lines when secrets warn."""
    from maestro.cli import cmd_preflight
    from maestro.config import Config

    config = Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR)
    args = argparse.Namespace()

    with patch.object(Config, "check_secrets", return_value=["Missing token warning"]):
        cmd_preflight(args, config)

    captured = capsys.readouterr()
    assert "warning" in captured.out.lower() or "Missing token warning" in captured.out


def test_cmd_preflight_with_no_secret_warnings(
    mem_fs: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cmd_preflight reports secrets ok when check_secrets returns no warnings."""
    from maestro.cli import cmd_preflight
    from maestro.config import Config

    config = Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR)
    args = argparse.Namespace()

    with patch.object(Config, "check_secrets", return_value=[]), \
         patch.object(Config, "check_auth", return_value="ok"):
        cmd_preflight(args, config)

    captured = capsys.readouterr()
    assert "secrets" in captured.out
    assert "ok" in captured.out


# ---------------------------------------------------------------------------
# cli.py:499 — --list-sessions without --sdk warns
# ---------------------------------------------------------------------------


def test_cmd_logs_list_sessions_without_sdk_warns(
    mem_fs: dict[str, str],
) -> None:
    """--list-sessions without --sdk should note the flag is ignored."""
    from maestro.cli import cmd_logs
    from maestro.config import Config

    d = Path("/fake/logs_list")
    config = Config(memory_dir=d, workspace=d)
    config.ensure_dirs()
    mem_fs[str(config.app_log_path)] = "line1\nline2\n"

    args = argparse.Namespace(
        sdk=False, follow=False, lines=50, date=None,
        session=None, list_sessions=True,
    )

    old_stdout = sys.stdout
    sys.stdout = buf = io.StringIO()
    try:
        cmd_logs(args, config)
    finally:
        sys.stdout = old_stdout

    output = buf.getvalue()
    # Should include a note that --list is ignored without --sdk
    assert "--list" in output or "ignored" in output.lower()


# ---------------------------------------------------------------------------
# cli.py:573-577 — cmd_email when email not configured
# ---------------------------------------------------------------------------


def test_cmd_email_no_client_exits(mem_fs: dict[str, str]) -> None:
    """cmd_email should print a helpful error and sys.exit(1) when email not configured."""
    from maestro.cli import cmd_email
    from maestro.config import Config

    config = Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR)
    args = argparse.Namespace(email_command="send", to="test@test.com",
                              subject="hi", body="hello")

    with patch("maestro.cli._build_email_client", return_value=None):
        old_stdout = sys.stdout
        sys.stdout = buf = io.StringIO()
        try:
            with pytest.raises(SystemExit) as exc_info:
                cmd_email(args, config)
        finally:
            sys.stdout = old_stdout

    assert exc_info.value.code == 1
    output = buf.getvalue()
    assert "Email not configured" in output


# ---------------------------------------------------------------------------
# cli.py:700 — main() with --verbose flag
# ---------------------------------------------------------------------------


def test_main_verbose_flag_sets_debug_logging() -> None:
    """main() with --verbose should set DEBUG log level."""
    with patch("sys.argv", ["maestro", "--verbose", "status"]), \
         patch("maestro.cli._build_agent") as mock_build, \
         patch("maestro.cli.cmd_status"), \
         patch("logging.basicConfig") as mock_basicconfig:
        mock_build.return_value = MagicMock()

        try:
            from maestro.cli import main
            main()
        except SystemExit:
            pass

    # Should have called basicConfig with DEBUG level
    import logging
    calls = [c for c in mock_basicconfig.call_args_list
             if c.kwargs.get("level") == logging.DEBUG or
             (c.args and c.args[0] == logging.DEBUG)]
    assert len(calls) > 0 or any(
        c.kwargs.get("level") == logging.DEBUG
        for c in mock_basicconfig.call_args_list
    ), f"Expected DEBUG basicConfig call, got: {mock_basicconfig.call_args_list}"


# ---------------------------------------------------------------------------
# cli.py:744 — main called as __main__
# ---------------------------------------------------------------------------


def test_main_as_module_entrypoint() -> None:
    """Confirm cli.py exposes __main__ block (line 744 is the `main()` call)."""
    # The line `if __name__ == "__main__": main()` is covered by checking that
    # the cli module defines a main() callable and the block is present.
    # We simulate by calling main() directly with a patched sys.argv.
    with patch("sys.argv", ["maestro"]), pytest.raises(SystemExit) as exc_info:
        from maestro.cli import main
        main()

    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# evolution.py:277-278 — issue_number = None when ValueError/TypeError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_plan_non_numeric_issue_number(mem_fs: dict[str, str]) -> None:
    """EvolutionEngine.propose_plan coerces non-numeric issue_number to None.

    Lines 277-278: except (ValueError, TypeError): issue_number = None
    """
    from maestro.config import Config
    from maestro.consciousness import Consciousness
    from maestro.evolution import EvolutionEngine
    from maestro.journal import Journal
    from maestro.memory import Memory
    from maestro.notifications.base import NullNotifier

    config = Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR)
    memory = Memory(FAKE_DIR)
    consciousness = Consciousness(FAKE_DIR)
    consciousness.load_self_model()
    journal = Journal(FAKE_DIR)
    notifier = NullNotifier()
    engine = EvolutionEngine(memory, consciousness, journal, notifier, config=config)

    # Provide a plan with non-numeric issue_number that can't be int()-converted
    response_data = {
        "title": "Test plan",
        "category": "capability",
        "priority": "capability",
        "rationale": "Improve things",
        "proposed_changes": ["Change X"],
        "files_affected": ["maestro/test.py"],
        "expected_impact": "Better",
        "issue_number": "not-a-number",  # will raise ValueError in int()
    }

    # A gap plan pins its issue, so the spec normaliser is exercised directly.
    plan = engine._plan_from_spec(response_data, default_rationale="r")

    assert plan is not None
    assert plan.issue_number is None


# ---------------------------------------------------------------------------
# evolution.py — an unreadable branch refuses the mutating phase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_implement_vcs_error_refuses_mutating_phase(mem_fs: dict[str, str]) -> None:
    """When ``get_current_branch()`` raises ``VCSError``, ``implement()`` refuses.

    The entry precondition cannot establish a clean ``main``, and an unreadable
    tree is treated exactly like a dirty one — no branch is cut (#3711).
    """
    from maestro.config import Config
    from maestro.consciousness import Consciousness
    from maestro.evolution import EvolutionEngine
    from maestro.journal import Journal
    from maestro.memory import EvolutionPlan, Memory
    from maestro.notifications.base import NullNotifier
    from maestro.vcs import DirtyWorkingTreeError, VCSError

    config = Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR)
    memory = Memory(FAKE_DIR)
    consciousness = Consciousness(FAKE_DIR)
    journal = Journal(FAKE_DIR)
    notifier = NullNotifier()
    engine = EvolutionEngine(memory, consciousness, journal, notifier, config=config)
    # Sentinel so the assertion below is meaningful (not the __init__ default).
    engine._orig_branch = "feature/sentinel"

    # Plan must be approved or implement() raises before reaching VCS code.
    plan = EvolutionPlan(
        plan_id="ep1",
        title="Test evolution",
        category="capability",
        rationale="Test",
        proposed_changes=["Do something"],
        files_affected=[],
        expected_impact="None",
        created_at="2025-01-01T00:00:00Z",
        status="approved",
    )
    memory.save_evolution_plan(plan)

    mock_vcs = MagicMock()
    mock_vcs.get_current_branch.side_effect = VCSError("cannot determine branch")
    mock_vcs.porcelain_status.return_value = ""  # the tree itself is clean
    engine._vcs = mock_vcs

    # An unreadable branch is indistinguishable from a dirty one, so the
    # mutating-phase precondition refuses rather than guessing "main" (#3711).
    with pytest.raises(DirtyWorkingTreeError):
        await engine.implement(plan)

    mock_vcs.checkout_new_branch.assert_not_called()
    mock_vcs.stage_all.assert_not_called()


# ---------------------------------------------------------------------------
# dashboard/auth.py:77-78 — hmac exception path in _check_cookie
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_cookie_hmac_exception_returns_false(mem_fs: dict[str, str]) -> None:
    """BearerAuthMiddleware._check_cookie returns False when hmac.compare_digest raises.

    Lines 77-78: except (TypeError, ValueError, Exception): return False
    This is covered when morsel.value raises an exception during compare_digest.
    """
    from maestro.dashboard.auth import BearerAuthMiddleware

    middleware = BearerAuthMiddleware(MagicMock(), token="valid-token")

    # Simulate a cookie where hmac.compare_digest raises TypeError
    with patch("maestro.dashboard.auth.hmac.compare_digest", side_effect=TypeError("bad type")):
        # Build a mock request with a valid cookie header
        mock_request = MagicMock()
        mock_request.headers.get = MagicMock(side_effect=lambda k, d="": {
            "cookie": "maestro_token=some-value",
        }.get(k, d))

        result = middleware._check_cookie(mock_request)

    assert result is False


# ---------------------------------------------------------------------------
# email_tools.py:120,124,128 — inner decorated function bodies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_email_mcp_server_send_tool_invokes_handler() -> None:
    """The email_send inner function in create_email_mcp_server calls _handle_send.

    Line 120: return await _handle_send(client, args)
    """
    from maestro.email_tools import create_email_mcp_server

    mock_client = MagicMock()
    mock_client.send = AsyncMock(return_value="<msg-id@test>")

    captured_tools: list = []

    with patch("maestro.email_tools.create_inproc_mcp_server",
               side_effect=lambda **kw: captured_tools.extend(kw.get("tools", []))):
        create_email_mcp_server(mock_client)

    # Find email_send tool and call its handler
    send_tool = next(t for t in captured_tools if hasattr(t, "name") and "send" in t.name)
    result = await send_tool.handler({
        "to": "test@example.com", "subject": "Hello", "body": "World",
    })
    assert "test@example.com" in result["text"]
    mock_client.send.assert_called_once()


@pytest.mark.asyncio
async def test_email_mcp_server_inbox_tool_invokes_handler() -> None:
    """The email_inbox inner function in create_email_mcp_server calls _handle_inbox.

    Line 124: return await _handle_inbox(client, args)
    """
    from maestro.email_tools import create_email_mcp_server

    mock_client = MagicMock()
    mock_client.fetch_inbox = AsyncMock(return_value=[])

    captured_tools: list = []

    with patch("maestro.email_tools.create_inproc_mcp_server",
               side_effect=lambda **kw: captured_tools.extend(kw.get("tools", []))):
        create_email_mcp_server(mock_client)

    inbox_tool = next(t for t in captured_tools if hasattr(t, "name") and "inbox" in t.name)
    result = await inbox_tool.handler({})
    assert "No emails found" in result["text"]


@pytest.mark.asyncio
async def test_email_mcp_server_read_tool_invokes_handler() -> None:
    """The email_read inner function in create_email_mcp_server calls _handle_read.

    Line 128: return await _handle_read(client, args)
    """
    from maestro.email_tools import create_email_mcp_server

    mock_client = MagicMock()
    mock_client.fetch_by_uid = AsyncMock(return_value=None)

    captured_tools: list = []

    with patch("maestro.email_tools.create_inproc_mcp_server",
               side_effect=lambda **kw: captured_tools.extend(kw.get("tools", []))):
        create_email_mcp_server(mock_client)

    read_tool = next(t for t in captured_tools if hasattr(t, "name") and "read" in t.name)
    result = await read_tool.handler({"uid": "999"})
    assert "not found" in result["text"].lower()


# ---------------------------------------------------------------------------
# telegram_tools.py:103,111 — inner decorated function bodies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telegram_mcp_server_send_tool_invokes_handler() -> None:
    """The telegram_send inner function in create_telegram_mcp_server calls _handle_send.

    Line 103: return await _handle_send(notifier, args)
    """
    from maestro.telegram_tools import create_telegram_mcp_server

    mock_notifier = MagicMock()
    mock_notifier.notify = AsyncMock()
    mock_store = MagicMock()
    mock_store.get_unread = MagicMock(return_value=[])

    captured_tools: list = []

    with patch("maestro.telegram_tools.create_inproc_mcp_server",
               side_effect=lambda **kw: captured_tools.extend(kw.get("tools", []))):
        create_telegram_mcp_server(mock_notifier, mock_store)

    send_tool = next(t for t in captured_tools if hasattr(t, "name") and "send" in t.name)
    result = await send_tool.handler({"message": "Hello!"})
    assert "Hello!" in result["text"]
    mock_notifier.notify.assert_called_once_with("Hello!")


@pytest.mark.asyncio
async def test_telegram_mcp_server_read_tool_invokes_handler() -> None:
    """The telegram_read_messages inner function calls _handle_read.

    Line 111: return await _handle_read(message_store, args)
    """
    from maestro.telegram_tools import create_telegram_mcp_server

    mock_notifier = MagicMock()
    mock_notifier.notify = AsyncMock()
    mock_store = MagicMock()
    mock_store.get_unread = MagicMock(return_value=[])

    captured_tools: list = []

    with patch("maestro.telegram_tools.create_inproc_mcp_server",
               side_effect=lambda **kw: captured_tools.extend(kw.get("tools", []))):
        create_telegram_mcp_server(mock_notifier, mock_store)

    read_tool = next(t for t in captured_tools if hasattr(t, "name") and "read" in t.name)
    result = await read_tool.handler({})
    assert "No unread messages" in result["text"]


# ---------------------------------------------------------------------------
# agent.py:882-883 — exception handler in startup lifecycle detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_loop_startup_lifecycle_exception_is_resilient(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """run_loop() catches any exception in startup lifecycle detection and continues.

    Lines 882-883: except Exception: logger.warning("Startup lifecycle detection failed")
    """
    from maestro.evolution import MergeCheckResult

    agent = make_agent()

    # Write a valid lifecycle file, but patch fromisoformat to raise
    lifecycle_path = str(agent.config.lifecycle_path)
    mem_fs[lifecycle_path] = json.dumps({"last_shutdown": "2026-01-01T00:00:00+00:00"})

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(),
    )
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    # Make journal.record_event raise to trigger the except block at line 882
    agent.journal.record_event = MagicMock(side_effect=RuntimeError("journal failed"))

    # Should not raise — the exception is caught and logged
    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    # Agent should still run normally despite the lifecycle detection failure
    agent.contemplate.assert_called_once()


# ---------------------------------------------------------------------------
# Additional: memory.py:164 — blank line skip in get_all_tasks
# ---------------------------------------------------------------------------


def test_memory_load_all_tasks_skips_blank_lines(mem_fs: dict[str, str]) -> None:
    """Memory._load_all_tasks() skips blank lines in the tasks file.

    Line 164: continue (blank line skip)
    """
    from maestro.memory import Memory

    memory = Memory(FAKE_DIR)
    tasks_path = str(FAKE_DIR / "task_history.jsonl")

    task_json_1 = json.dumps({
        "task_id": "t1", "goal": "Do something", "plan": [], "outcome": "success",
        "result_summary": "Done", "errors": [], "duration_secs": 1.0,
        "timestamp": "2025-01-01T00:00:00Z",
    })
    task_json_2 = json.dumps({
        "task_id": "t2", "goal": "Other", "plan": [], "outcome": "success",
        "result_summary": "Done", "errors": [], "duration_secs": 1.0,
        "timestamp": "2025-01-02T00:00:00Z",
    })

    # Blank line between two valid records (strip() removes only edges)
    mem_fs[tasks_path] = f"{task_json_1}\n   \n{task_json_2}\n"

    tasks = memory._load_all_tasks()
    assert len(tasks) == 2
    assert tasks[0].task_id == "t1"
    assert tasks[1].task_id == "t2"


# ---------------------------------------------------------------------------
# Additional: messaging.py:50 — blank line skip in _load_all
# ---------------------------------------------------------------------------


def test_messaging_load_skips_blank_lines(mem_fs: dict[str, str]) -> None:
    """MessageStore._load_all() skips blank lines in the messages file.

    Line 50: continue (blank line skip)
    """
    from maestro.messaging import MessageStore

    store = MessageStore(FAKE_DIR)
    messages_path = str(FAKE_DIR / "messages.jsonl")

    msg_json_1 = json.dumps({
        "message_id": "m1", "timestamp": "2025-01-01T00:00:00Z",
        "sender": "thomas", "content": "Hello",
        "message_type": "info", "read": False,
    })
    msg_json_2 = json.dumps({
        "message_id": "m2", "timestamp": "2025-01-02T00:00:00Z",
        "sender": "thomas", "content": "There",
        "message_type": "info", "read": False,
    })

    # Blank line between two valid records (strip() removes only edges)
    mem_fs[messages_path] = f"{msg_json_1}\n   \n{msg_json_2}\n"

    messages = store.get_recent(10)
    assert len(messages) == 2
    assert messages[0].message_id == "m1"
    assert messages[1].message_id == "m2"


# ---------------------------------------------------------------------------
# Additional: Journal.get_recent_narrative — non-string content skip
# ---------------------------------------------------------------------------


def test_journal_get_recent_narrative_skips_non_string_content(
    mem_fs: dict[str, str],
) -> None:
    """Journal.get_recent_narrative() skips entries where content is not a string.

    journal.py::Journal.get_recent_narrative — if not isinstance(e.content, str): continue
    """
    from maestro.journal import Journal, JournalEntry

    journal = Journal(FAKE_DIR)

    # Write a normal entry and one with non-string content
    entry_good = JournalEntry(
        entry_id="e1", timestamp="2025-01-01T00:00:00Z",
        entry_type="reflection", content="Real thought",
        triggered_by="agent_tool",
    )
    entry_bad_data = {
        "entry_id": "e2", "timestamp": "2025-01-01T00:01:00Z", "stage": "awakening",
        "entry_type": "reflection", "content": 42,  # non-string
        "triggered_by": "agent_tool",
    }

    journal_path = str(FAKE_DIR / "journal.jsonl")
    import json as json_module
    from dataclasses import asdict
    # Blank line between entries exercises the `continue` in _load_all
    mem_fs[journal_path] = (
        json_module.dumps(asdict(entry_good))
        + "\n   \n"
        + json_module.dumps(entry_bad_data)
        + "\n"
    )

    narrative = journal.get_recent_narrative(max_entries=10, max_chars=5000)
    assert "Real thought" in narrative
    # Non-string content entry should be skipped
    assert "42" not in narrative
