"""Unit tests for ``maestro.loop.task_setup.prepare_task_workspace``.

The stage is the verbatim lift of ``run``'s ``# 2b.`` workspace-preparation
block (proposal 106): the executor action gate's owned-writable branch set
(``_gate_allowed_branches``) and the per-task artifacts directory
(``config.artifacts_dir / task_id``, created via ``mkdir``). These tests call
the method directly with keyword args and pin both arms; the ``mem_fs`` fixture
(active via ``make_agent``) patches ``Path.mkdir`` to a no-op, so the directory
creation touches no real filesystem and needs no cleanup.

Behaviour-preservation of the whole block is proven by the full-``run``
regression harness (``tests/test_agent_task_branch.py``'s artifacts-dir suites), which now
flows through this function unchanged; these unit tests exercise the extracted
stage in isolation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from maestro.loop.task_setup import prepare_task_workspace


def test_branchless_returns_none_allowed_branches_and_artifacts_path(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(a) ``branch_name=None`` keeps the gate fail-closed (``allowed_branches
    is None``) and still builds the per-task artifacts path."""
    agent = make_agent()

    allowed_branches, artifacts_path = prepare_task_workspace(agent,
        branch_name=None, task_id="tid",
    )

    assert allowed_branches is None
    assert artifacts_path == agent.config.artifacts_dir / "tid"


def test_branch_set_returns_gate_result_and_artifacts_path(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(b) A branch delegates the owned-writable set to
    ``_gate_allowed_branches`` (returned verbatim) and builds the artifacts
    path."""
    agent = make_agent()
    sentinel = frozenset({"maestro/task/tid", "maestro/evolve/ep1"})
    agent._gate_allowed_branches = MagicMock(return_value=sentinel)

    allowed_branches, artifacts_path = prepare_task_workspace(agent,
        branch_name="maestro/task/tid", task_id="tid",
    )

    assert allowed_branches is sentinel
    agent._gate_allowed_branches.assert_called_once_with("maestro/task/tid")
    assert artifacts_path == agent.config.artifacts_dir / "tid"


def test_artifacts_dir_created_parents_and_exist_ok(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(c) The per-task artifacts directory is created idempotently —
    ``mkdir(parents=True, exist_ok=True)`` on the ``artifacts_dir / task_id``
    target, so a resume/re-run against an already-present directory does not
    raise. ``mem_fs`` no-ops ``mkdir``, so re-patch it with an ``autospec`` spy
    (which records the receiver) to pin the target *and* the kwargs a
    regression could otherwise silently drop."""
    agent = make_agent()
    expected_path = agent.config.artifacts_dir / "tid"

    with patch.object(Path, "mkdir", autospec=True) as mock_mkdir:
        _, artifacts_path = prepare_task_workspace(agent,
            branch_name=None, task_id="tid",
        )

    assert artifacts_path == expected_path
    mock_mkdir.assert_called_once_with(
        expected_path, parents=True, exist_ok=True,
    )
