"""Prompt size bounds for ``ArtifactReviewer`` task metadata (#3990).

The bundle is byte-capped by ``_read_artifact_bundle``; these tests pin the
caps on the task metadata ``_format_task_block`` prepends (goal, summary)
and the whole-input budget ``review()`` assembles.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from maestro.artifact_review import (
    _MAX_GOAL_BYTES,
    _MAX_SUMMARY_BYTES,
    _TRUNCATED_SENTINEL,
    ArtifactReviewer,
    _cap_utf8,
)
from maestro.config import Config
from maestro.journal import Journal
from maestro.memory import Memory, TaskRecord
from maestro.prompts.artifact_review import ARTIFACT_REVIEW_PROMPT
from maestro.sdk import QueryResult


def _reviewer() -> tuple[ArtifactReviewer, Config]:
    memory_dir = Path("/mem/memory")
    config = Config(
        memory_dir=memory_dir, workspace=Path("/mem/workspace"),
        enable_session_reuse=False,
    )
    reviewer = ArtifactReviewer(
        Memory(memory_dir), Journal(memory_dir), config,
        clock=lambda: datetime(2026, 4, 24, tzinfo=UTC),
    )
    return reviewer, config


def _task(
    task_id: str, *, goal: str = "do a thing", summary: str = "done",
    artifacts_dir: str | None = None,
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id, goal=goal, plan=[], outcome="success",
        result_summary=summary, errors=[], duration_secs=1.0,
        timestamp="2026-04-01T00:00:00+00:00", source="self_directed",
        artifacts_dir=artifacts_dir,
    )


def test_cap_utf8_short_text_unchanged() -> None:
    assert _cap_utf8("héllo", 100) == "héllo"


def test_cap_utf8_budget_smaller_than_sentinel_returns_sentinel() -> None:
    assert _cap_utf8("x" * 50, 1) == _TRUNCATED_SENTINEL


@pytest.mark.parametrize(
    "goal", ["g" * 100_000, "é" * 50_000], ids=["ascii", "multibyte"],
)
def test_format_task_block_caps_oversized_goal(
    mem_fs: dict[str, str], goal: str,
) -> None:
    reviewer, _ = _reviewer()
    block = reviewer._format_task_block(_task("t-long", goal=goal), "", [])
    start = block.index("Goal: ") + len("Goal: ")
    rendered = block[start:block.index("\nOutcome:", start)]
    assert len(rendered.encode("utf-8")) <= _MAX_GOAL_BYTES
    assert rendered.endswith(_TRUNCATED_SENTINEL)
    assert block.startswith("--- Task t-long ---\n")


def test_format_task_block_caps_oversized_summary(mem_fs: dict[str, str]) -> None:
    reviewer, _ = _reviewer()
    block = reviewer._format_task_block(
        _task("t-sum", summary="s" * 100_000), "", [],
    )
    start = block.index("Summary: ") + len("Summary: ")
    rendered = block[start:block.index("\nFiles:", start)]
    assert len(rendered.encode("utf-8")) <= _MAX_SUMMARY_BYTES
    assert rendered.endswith(_TRUNCATED_SENTINEL)


def test_format_task_block_short_goal_unchanged(mem_fs: dict[str, str]) -> None:
    reviewer, _ = _reviewer()
    block = reviewer._format_task_block(
        _task("t-short", goal="thinking"), "body", ["a.md"],
    )
    expected = (
        "--- Task t-short ---\nGoal: thinking\nOutcome: success\n"
        "Summary: done\nFiles: a.md\n\nbody\n"
    )
    assert block == expected


@pytest.mark.asyncio
async def test_review_prompt_respects_total_input_budget(
    mem_fs: dict[str, str],
) -> None:
    reviewer, config = _reviewer()
    tasks = [
        _task(f"t-{i}", goal="g" * 100_000, summary="s" * 100_000)
        for i in range(config.artifact_review_batch_size)
    ]
    # Oversized bundle and file list: the block trim must still hold.
    bundle = ("b" * (config.artifact_review_max_bundle_bytes * 2), ["f.md"] * 500)
    mock = AsyncMock(return_value=QueryResult(response=json.dumps([])))
    with (
        patch.object(reviewer, "_read_artifact_bundle", return_value=bundle),
        patch("maestro.artifact_review.query", mock),
    ):
        await reviewer.review(tasks)
    prompt = mock.await_args.kwargs["prompt"]
    budget = len(tasks) * (reviewer._block_budget_bytes() + 1)
    template_bytes = len(ARTIFACT_REVIEW_PROMPT.format(bundle_text="").encode())
    assert len(prompt.encode("utf-8")) - template_bytes <= budget
    for task in tasks:
        assert f"--- Task {task.task_id} ---" in prompt
    assert "g" * (_MAX_GOAL_BYTES + 1) not in prompt
