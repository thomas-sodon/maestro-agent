"""Tests for Planner and Executor with mocked Claude SDK."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from maestro.executor import Executor
from maestro.memory import PlanStep
from maestro.planner import Planner
from maestro.sdk import QueryResult
from tests.conftest import MockQueryResult

# --- Planner tests ---

@pytest.mark.asyncio
async def test_planner_decompose():
    planner = Planner()
    response = json.dumps({"steps": [
        {"description": "Read the file"},
        {"description": "Modify the function"},
        {"description": "Run tests"},
    ]})

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=response)
        steps = await planner.decompose("Refactor code", "identity context")

    assert len(steps) == 3
    assert steps[0].description == "Read the file"
    assert steps[2].description == "Run tests"
    for s in steps:
        assert s.step_id != ""
        assert s.status == "pending"


@pytest.mark.asyncio
async def test_planner_decompose_bad_json():
    planner = Planner()

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="Not JSON")
        steps = await planner.decompose("Simple goal", "ctx")

    assert len(steps) == 1
    assert steps[0].description == "Simple goal"


@pytest.mark.asyncio
async def test_planner_decompose_empty_steps():
    planner = Planner()
    response = json.dumps([])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=response)
        steps = await planner.decompose("Goal", "ctx")

    # Empty array should fallback to single step
    assert len(steps) == 1
    assert steps[0].description == "Goal"


@pytest.mark.asyncio
async def test_planner_decompose_dict_with_steps_key():
    """When LLM returns {"steps": [...]} instead of [...], extract the list."""
    planner = Planner()
    response = json.dumps({
        "steps": [
            {"description": "Read the file"},
            {"description": "Write the code"},
        ]
    })

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=response)
        steps = await planner.decompose("Build feature", "ctx")

    assert len(steps) == 2
    assert steps[0].description == "Read the file"
    assert steps[1].description == "Write the code"


@pytest.mark.asyncio
async def test_planner_decompose_dict_with_arbitrary_list_value():
    """When LLM returns a dict with a single list value, extract it."""
    planner = Planner()
    response = json.dumps({
        "plan": [
            {"description": "Step A"},
            {"description": "Step B"},
            {"description": "Step C"},
        ]
    })

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=response)
        steps = await planner.decompose("Do stuff", "ctx")

    assert len(steps) == 3
    assert steps[0].description == "Step A"


@pytest.mark.asyncio
async def test_planner_decompose_dict_no_list_value_falls_back():
    """When LLM returns a dict with no list values, fall back to goal."""
    planner = Planner()
    response = json.dumps({"description": "Some text", "count": 3})

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=response)
        steps = await planner.decompose("My goal", "ctx")

    assert len(steps) == 1
    assert steps[0].description == "My goal"


@pytest.mark.asyncio
async def test_planner_decompose_non_dict_items():
    planner = Planner()
    response = json.dumps(["Step 1", "Step 2"])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=response)
        steps = await planner.decompose("Goal", "ctx")

    assert len(steps) == 2
    assert steps[0].description == "Step 1"


@pytest.mark.asyncio
async def test_planner_decompose_none_items_skipped():
    """Bug 40: None items in LLM response should be skipped, not turned
    into steps with description 'None'."""
    planner = Planner()
    mock_result = QueryResult(response=json.dumps([
        None,
        {"description": "valid step"},
    ]))
    with patch("maestro.planner.query", new_callable=AsyncMock, return_value=mock_result):
        steps = await planner.decompose("goal", "identity")

    descriptions = [s.description for s in steps]
    assert "None" not in descriptions
    assert "valid step" in descriptions


@pytest.mark.asyncio
async def test_planner_decompose_empty_goal_raises():
    """Empty or whitespace-only goal must raise ValueError."""
    planner = Planner()
    with pytest.raises(ValueError, match="non-empty string"):
        await planner.decompose("", "ctx")
    with pytest.raises(ValueError, match="non-empty string"):
        await planner.decompose("   ", "ctx")


@pytest.mark.asyncio
async def test_planner_decompose_non_list_json_falls_back():
    """When LLM returns a non-list, non-dict JSON value, fall back to goal."""
    planner = Planner()
    response = json.dumps(42)

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=response)
        steps = await planner.decompose("My goal", "ctx")

    assert len(steps) == 1
    assert steps[0].description == "My goal"


@pytest.mark.asyncio
async def test_planner_decompose_empty_description_skipped():
    """Steps with empty or whitespace-only descriptions are skipped."""
    planner = Planner()
    response = json.dumps([
        {"description": ""},
        {"description": "   "},
        {"description": "valid step"},
    ])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=response)
        steps = await planner.decompose("Goal", "ctx")

    assert len(steps) == 1
    assert steps[0].description == "valid step"


@pytest.mark.asyncio
async def test_planner_decompose_long_description_truncated():
    """Bug 7: Very long step descriptions from LLM output should be
    truncated to prevent bloating memory and journal entries."""
    planner = Planner()
    long_desc = "A" * 10000
    response = json.dumps([{"description": long_desc}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(response=response)
        steps = await planner.decompose("test", "identity")

    # Step descriptions should be capped at 2000 chars
    assert len(steps[0].description) <= 2000


@pytest.mark.asyncio
async def test_planner_decompose_passes_json_complete() -> None:
    """Stateless decompose threads json_complete into query()."""
    from unittest.mock import MagicMock

    from maestro.sdk import json_complete

    planner = Planner()
    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response='{"steps": []}')
        await planner.decompose("goal", "ctx")

    assert mock_q.call_args.kwargs.get("complete_on") is json_complete

    with patch(
        "maestro.planner.query_on_session", new_callable=AsyncMock,
    ) as mock_qos:
        mock_qos.return_value = MockQueryResult(response='{"steps": []}')
        await planner.decompose("goal", "ctx", session=MagicMock())

    assert mock_qos.call_args.kwargs.get("complete_on") is json_complete


@pytest.mark.asyncio
async def test_planner_decompose_prompt_folds_doc_step_into_step_cap() -> None:
    """The step-count cap and the documentation caveat are one instruction.

    Regression for #241: framing the documentation update as a separate
    paragraph *on top of* the 2-to-7 cap lets a maxed-out decomposition
    obey both only by emitting an eighth step. The two must read as one
    instruction so the documentation update is the final step *within*
    the budget, not an extra one. Proposal 108: the caveat itself is now
    gated on ``require_claude_md_update`` — the fold only applies when it
    is present, so this asserts the folded shape with the flag on.
    """
    planner = Planner()
    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response='{"steps": []}')
        await planner.decompose(
            "Refactor code", "ctx", require_claude_md_update=True,
        )

    prompt = mock_q.call_args.kwargs["prompt"]

    # Both the cap and the documentation caveat survive in the prompt.
    assert "2 to 7 steps" in prompt
    assert "documentation" in prompt

    # No sentence break separates the cap from the documentation caveat:
    # they live in the same instruction, not two adjacent paragraphs.
    cap = prompt.index("2 to 7 steps")
    doc = prompt.index("documentation", cap)
    assert "." not in prompt[cap:doc]


@pytest.mark.asyncio
async def test_planner_decompose_prompt_drops_doc_step_when_flag_off() -> None:
    """Proposal 108: with ``require_claude_md_update`` False (the default), the
    doc-update caveat is dropped so a decomposition is not pushed to spend a
    step on a CLAUDE.md edit the loop never reads. The step-count cap stays.
    """
    planner = Planner()
    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response='{"steps": []}')
        await planner.decompose("Refactor code", "ctx")

    prompt = mock_q.call_args.kwargs["prompt"]

    assert "2 to 7 steps" in prompt
    assert "documentation" not in prompt
    assert "CLAUDE.md" not in prompt


@pytest.mark.asyncio
async def test_planner_decompose_prepends_workspace_moment_both_paths() -> None:
    """Proposal 130: a non-empty ``workspace_moment`` is prepended once to the
    front of the assembled prompt on both the bare-``query()`` and
    ``query_on_session`` delivery paths; an empty moment (the default, flag-off
    path) leaves the prompt byte-identical.
    """
    from unittest.mock import MagicMock

    moment = (
        "## Conscious moment (broadcast to task_execution)\n"
        "Focus: the focus"
    )
    planner = Planner()

    # --- Bare query() path ---
    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response='{"steps": []}')
        await planner.decompose("goal", "ctx")
    prompt_off_bare = mock_q.call_args.kwargs["prompt"]

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response='{"steps": []}')
        await planner.decompose("goal", "ctx", workspace_moment=moment)
    prompt_on_bare = mock_q.call_args.kwargs["prompt"]

    assert prompt_on_bare.startswith(moment + "\n\n")
    # Byte-identity: the only difference is the prepended block.
    assert prompt_on_bare == moment + "\n\n" + prompt_off_bare

    # --- query_on_session path (prompt is the second positional arg) ---
    with patch(
        "maestro.planner.query_on_session", new_callable=AsyncMock,
    ) as mock_qos:
        mock_qos.return_value = MockQueryResult(response='{"steps": []}')
        await planner.decompose("goal", "ctx", session=MagicMock())
    prompt_off_sess = mock_qos.call_args.args[1]

    with patch(
        "maestro.planner.query_on_session", new_callable=AsyncMock,
    ) as mock_qos:
        mock_qos.return_value = MockQueryResult(response='{"steps": []}')
        await planner.decompose(
            "goal", "ctx", session=MagicMock(), workspace_moment=moment,
        )
    prompt_on_sess = mock_qos.call_args.args[1]

    assert prompt_on_sess.startswith(moment + "\n\n")
    assert prompt_on_sess == moment + "\n\n" + prompt_off_sess


@pytest.mark.asyncio
async def test_planner_replan_passes_json_complete() -> None:
    """replan threads json_complete through both session and stateless paths."""
    from unittest.mock import MagicMock

    from maestro.sdk import json_complete

    planner = Planner()
    failed = PlanStep(step_id="s1", description="bad", status="failed")
    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response='{"steps": []}')
        await planner.replan("goal", "ctx", [], failed)

    assert mock_q.call_args.kwargs.get("complete_on") is json_complete

    with patch(
        "maestro.planner.query_on_session", new_callable=AsyncMock,
    ) as mock_qos:
        mock_qos.return_value = MockQueryResult(response='{"steps": []}')
        await planner.replan("goal", "ctx", [], failed, session=MagicMock())

    assert mock_qos.call_args.kwargs.get("complete_on") is json_complete


@pytest.mark.asyncio
async def test_planner_replan_includes_loop_avoidance_when_loop_detected() -> None:
    """replan appends explicit loop-avoidance guidance only when loop_detected."""
    planner = Planner()
    failed = PlanStep(step_id="s1", description="bad", status="failed")

    # loop_detected=True → the prompt steers the planner off the repeated action.
    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response='{"steps": []}')
        await planner.replan("goal", "ctx", [], failed, loop_detected=True)

    loop_prompt = mock_q.call_args.kwargs["prompt"]
    assert "repeated the same" in loop_prompt
    assert "fundamentally different approach" in loop_prompt

    # Default loop_detected=False → no loop-avoidance guidance leaks in.
    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response='{"steps": []}')
        await planner.replan("goal", "ctx", [], failed)

    plain_prompt = mock_q.call_args.kwargs["prompt"]
    assert "repeated the same" not in plain_prompt
    assert "fundamentally different approach" not in plain_prompt


# --- Executor tests ---

@pytest.mark.asyncio
async def test_executor_success():
    executor = Executor()
    step = PlanStep(step_id="s1", description="Do something")

    with patch("maestro.executor.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="Task done successfully")
        result = await executor.execute(step, "/tmp", "identity context")

    assert result.success is True
    assert "Task done" in result.output
    assert result.error is None


@pytest.mark.asyncio
async def test_executor_empty_response_is_failure():
    """When query() returns an empty response, the step should fail."""
    executor = Executor()
    step = PlanStep(step_id="s1", description="Do something")

    with patch("maestro.executor.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="")
        result = await executor.execute(step, "/tmp", "identity context")

    assert result.success is False
    assert result.error is not None


@pytest.mark.asyncio
async def test_executor_failure():
    executor = Executor()
    step = PlanStep(step_id="s1", description="Will fail")

    with patch("maestro.executor.query", new_callable=AsyncMock) as mock_q:
        mock_q.side_effect = RuntimeError("SDK crashed")
        result = await executor.execute(step, "/tmp", "identity context")

    assert result.success is False
    assert result.error == "SDK crashed"
    assert result.output == ""


@pytest.mark.asyncio
async def test_executor_empty_description_returns_failure():
    """Bug 20: A step with empty description should fail without calling the API."""
    executor = Executor()
    step = PlanStep(step_id="s1", description="")

    with patch("maestro.executor.query", new_callable=AsyncMock) as mock_query:
        result = await executor.execute(step, "/workspace", "identity")

    assert result.success is False
    assert result.error is not None
    assert "description" in result.error.lower() or "empty" in result.error.lower()
    mock_query.assert_not_called()


@pytest.mark.asyncio
async def test_executor_whitespace_description_returns_failure():
    """Bug 20: A step with whitespace-only description should fail."""
    executor = Executor()
    step = PlanStep(step_id="s2", description="   \n  ")

    with patch("maestro.executor.query", new_callable=AsyncMock) as mock_query:
        result = await executor.execute(step, "/workspace", "identity")

    assert result.success is False
    mock_query.assert_not_called()


@pytest.mark.asyncio
async def test_executor_valid_description_calls_api():
    """Bug 20: A step with valid description should proceed normally."""
    executor = Executor()
    step = PlanStep(step_id="s3", description="Implement feature X")

    mock_result = QueryResult(response="Done implementing feature X")
    with patch("maestro.executor.query", new_callable=AsyncMock, return_value=mock_result):
        result = await executor.execute(step, "/workspace", "identity")

    assert result.success is True
    assert result.output == "Done implementing feature X"


@pytest.mark.asyncio
async def test_executor_includes_artifacts_dir_in_prompt():
    """When artifacts_dir is provided, it should appear in the prompt sent to query."""
    executor = Executor()
    step = PlanStep(step_id="s1", description="Generate report")

    mock_result = QueryResult(response="Report generated")
    with patch("maestro.executor.query", new_callable=AsyncMock, return_value=mock_result) as mock_q:
        await executor.execute(step, "/workspace", "identity", artifacts_dir="/tmp/artifacts/abc123")

    prompt = mock_q.call_args.kwargs.get("prompt", mock_q.call_args[0][0] if mock_q.call_args[0] else "")
    assert "/tmp/artifacts/abc123" in prompt
    assert "artifacts" in prompt.lower()


@pytest.mark.asyncio
async def test_executor_omits_artifacts_dir_when_none():
    """When artifacts_dir is None, the prompt should not mention an artifacts directory."""
    executor = Executor()
    step = PlanStep(step_id="s1", description="Simple task")

    mock_result = QueryResult(response="Done")
    with patch("maestro.executor.query", new_callable=AsyncMock, return_value=mock_result) as mock_q:
        await executor.execute(step, "/workspace", "identity", artifacts_dir=None)

    prompt = mock_q.call_args.kwargs.get("prompt", mock_q.call_args[0][0] if mock_q.call_args[0] else "")
    assert "Artifacts directory:" not in prompt


@pytest.mark.asyncio
async def test_executor_with_messaging_includes_mcp_tools():
    """Executor with notifier and message_store should add messaging tools."""
    from unittest.mock import MagicMock

    from maestro.notifications.base import NullNotifier

    notifier = NullNotifier()
    mock_store = MagicMock()
    executor = Executor(notifier=notifier, message_store=mock_store)
    step = PlanStep(step_id="s1", description="Do task with messaging")

    mock_result = QueryResult(response="Done with messaging")
    with (
        patch("maestro.executor.query", new_callable=AsyncMock, return_value=mock_result) as mock_q,
        patch("maestro.tools.create_inproc_mcp_server", return_value="mock_server"),
    ):
        await executor.execute(step, "/workspace", "identity")

    options = mock_q.call_args.kwargs.get("options")
    assert options is not None
    assert "mcp__messaging__send_message_to_thomas" in options.allowed_tools
    assert "mcp__messaging__read_messages_from_thomas" in options.allowed_tools
    assert "messaging" in options.mcp_servers

    prompt = mock_q.call_args.kwargs.get("prompt") or mock_q.call_args[0][0]
    assert "send_message_to_thomas" in prompt
    assert "read_messages_from_thomas" in prompt


@pytest.mark.asyncio
async def test_executor_without_messaging_no_mcp_tools():
    """Executor without notifier/message_store should work as before."""
    executor = Executor()
    step = PlanStep(step_id="s1", description="Do task without messaging")

    mock_result = QueryResult(response="Done")
    with patch("maestro.executor.query", new_callable=AsyncMock, return_value=mock_result) as mock_q:
        await executor.execute(step, "/workspace", "identity")

    options = mock_q.call_args.kwargs.get("options")
    assert options is not None
    assert "mcp__messaging__send_message_to_thomas" not in options.allowed_tools
    assert options.mcp_servers is None

    prompt = mock_q.call_args.kwargs.get("prompt") or mock_q.call_args[0][0]
    assert "send_message_to_thomas" not in prompt


@pytest.mark.asyncio
async def test_executor_passes_mcp_servers():
    """MCP servers from constructor should be forwarded to QueryOptions."""
    mcp = {"github": {"command": "npx", "args": ["-y", "pkg"], "env": {"TOKEN": "x"}}}
    executor = Executor(mcp_servers=mcp)
    step = PlanStep(step_id="s1", description="Do work")

    mock_result = QueryResult(response="Done")
    with patch("maestro.executor.query", new_callable=AsyncMock, return_value=mock_result) as mock_q:
        await executor.execute(step, "/workspace", "identity")

    opts = mock_q.call_args.kwargs.get("options")
    assert opts is not None
    assert opts.mcp_servers == mcp


@pytest.mark.asyncio
async def test_executor_no_mcp_servers_default():
    """When no MCP servers are configured, mcp_servers should be None."""
    executor = Executor()
    step = PlanStep(step_id="s1", description="Do work")

    mock_result = QueryResult(response="Done")
    with patch("maestro.executor.query", new_callable=AsyncMock, return_value=mock_result) as mock_q:
        await executor.execute(step, "/workspace", "identity")

    opts = mock_q.call_args.kwargs.get("options")
    assert opts is not None
    assert opts.mcp_servers is None


@pytest.mark.asyncio
async def test_executor_with_email_mcp_includes_email_tools():
    """When the email MCP server is registered, its tool names must appear in allowed_tools."""
    from maestro.email_tools import SERVER_KEY, TOOL_NAMES

    email_mcp = {"type": "sdk", "name": SERVER_KEY, "instance": "mock"}
    executor = Executor(mcp_servers={SERVER_KEY: email_mcp})
    step = PlanStep(step_id="s1", description="Task with email")

    mock_result = QueryResult(response="Done")
    with patch("maestro.executor.query", new_callable=AsyncMock, return_value=mock_result) as mock_q:
        await executor.execute(step, "/workspace", "identity")

    options = mock_q.call_args.kwargs.get("options")
    assert options is not None
    for tool_name in TOOL_NAMES:
        assert tool_name in options.allowed_tools
    assert SERVER_KEY in options.mcp_servers

    prompt = mock_q.call_args.kwargs.get("prompt") or mock_q.call_args[0][0]
    assert "email_send" in prompt
    assert "email_inbox" in prompt
    assert "email_read" in prompt


@pytest.mark.asyncio
async def test_executor_without_email_mcp_no_email_tools():
    """When no email MCP server, email tool names must not appear."""
    executor = Executor()
    step = PlanStep(step_id="s1", description="Task without email")

    mock_result = QueryResult(response="Done")
    with patch("maestro.executor.query", new_callable=AsyncMock, return_value=mock_result) as mock_q:
        await executor.execute(step, "/workspace", "identity")

    options = mock_q.call_args.kwargs.get("options")
    assert options is not None
    assert "mcp__maestro_email__email_send" not in options.allowed_tools

    prompt = mock_q.call_args.kwargs.get("prompt") or mock_q.call_args[0][0]
    assert "email_send" not in prompt
