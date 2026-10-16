"""Component tests for the publication execution pipeline (proposal 033).

Drives :class:`Publisher.tick` through ``Memory`` + ``MockCodeHost`` so
the multi-pass tick semantics (stale reconcile, resume, fresh publish)
are exercised end-to-end. The git ``runner`` is a mock so no real
``git push`` runs; the host is the in-memory ``MockCodeHost`` so no
``gh`` subprocess executes either.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from maestro.config import Config
from maestro.journal import Journal
from maestro.memory import (
    Memory,
    PublicationProposal,
    TaskRecord,
)
from maestro.notifications.base import NullNotifier
from maestro.publication import Publisher
from tests.component.mock_vcs import MockCodeHost


def _make_publisher(
    config: Config,
    memory_dir: Path,
    *,
    host: MockCodeHost | None = None,
    runner: Any | None = None,
    clock: datetime | None = None,
) -> tuple[Publisher, Memory, MockCodeHost, Any]:
    memory = Memory(memory_dir)
    journal = Journal(memory_dir)
    notifier = NullNotifier()
    _host = host or MockCodeHost(mock_owner="thomas-sodon")
    _runner = runner or MagicMock(
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )
    _clock = clock or datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
    publisher = Publisher(
        memory=memory,
        journal=journal,
        notifier=notifier,
        host=_host,
        config=config,
        clock=lambda: _clock,
        runner=_runner,
    )
    return publisher, memory, _host, _runner


def _seed_task_with_artifact(
    memory: Memory,
    artifacts_root: Path,
    *,
    task_id: str = "t001",
    file_name: str = "main.py",
    contents: str = "print('hi')\n",
) -> Path:
    artifacts_dir = artifacts_root / task_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / file_name).write_text(contents, encoding="utf-8")
    memory.record_task(TaskRecord(
        task_id=task_id,
        goal="produce demo",
        plan=[],
        outcome="success",
        result_summary="done",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-04-24T00:00:00+00:00",
        source="self_directed",
        artifacts_dir=str(artifacts_dir),
    ))
    return artifacts_dir


def _seed_proposal(
    memory: Memory,
    *,
    proposal_id: str = "prop-1",
    status: str = "approved",
    task_id: str = "t001",
    publishable_files: list[str] | None = None,
    reviewed_at: str = "2026-04-24T01:00:00+00:00",
    proposed_name: str = "demo-repo",
    executing_started_at: str | None = None,
    published_url: str | None = None,
) -> PublicationProposal:
    proposal = PublicationProposal(
        proposal_id=proposal_id,
        task_id=task_id,
        review_id="r-" + proposal_id,
        proposed_name=proposed_name,
        description="demo description",
        proposed_readme="# demo\nbody\n",
        publishable_files=publishable_files or ["main.py"],
        status=status,  # type: ignore[arg-type]
        created_at="2026-04-23T00:00:00+00:00",
        reviewed_at=reviewed_at,
        executing_started_at=executing_started_at,
        published_url=published_url,
    )
    memory.write_publication_proposal(proposal)
    return proposal


@pytest.fixture
def env(tmp_path: Path) -> tuple[Config, Path, Path]:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()
    config = Config(
        memory_dir=memory_dir,
        workspace=workspace,
        enable_session_reuse=False,
        publication_push_retry_limit=2,
        publication_executing_stale_timeout_seconds=600,
    )
    return config, memory_dir, artifacts_root


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_publishes_one_approved_proposal_end_to_end(
    env: tuple[Config, Path, Path],
) -> None:
    config, memory_dir, artifacts_root = env
    publisher, memory, host, runner = _make_publisher(config, memory_dir)
    _seed_task_with_artifact(memory, artifacts_root)
    proposal = _seed_proposal(memory)

    await publisher.tick()

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "published"
    # Created under [publication] owner (the organization), not the
    # authenticated account MockCodeHost reports via resolve_owner.
    assert (
        refreshed.published_url
        == "https://github.com/thomas-sodon/demo-repo"
    )
    assert host.created_public_repos == [
        {
            "owner": "thomas-sodon",
            "name": "demo-repo",
            "description": "demo description",
            "url": "https://github.com/thomas-sodon/demo-repo",
        },
    ]
    push_calls = [
        c for c in runner.call_args_list
        if c.args[0][:2] == ["git", "push"]
    ]
    assert len(push_calls) == 1


# ---------------------------------------------------------------------------
# One-per-tick fresh-publish budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_publishes_at_most_one_fresh_proposal(
    env: tuple[Config, Path, Path],
) -> None:
    config, memory_dir, artifacts_root = env
    publisher, memory, host, _ = _make_publisher(config, memory_dir)
    _seed_task_with_artifact(memory, artifacts_root, task_id="t001")
    _seed_task_with_artifact(memory, artifacts_root, task_id="t002")
    _seed_task_with_artifact(memory, artifacts_root, task_id="t003")
    p1 = _seed_proposal(
        memory, proposal_id="p1", task_id="t001", proposed_name="r1",
        reviewed_at="2026-04-24T01:00:00+00:00",
    )
    p2 = _seed_proposal(
        memory, proposal_id="p2", task_id="t002", proposed_name="r2",
        reviewed_at="2026-04-24T02:00:00+00:00",
    )
    p3 = _seed_proposal(
        memory, proposal_id="p3", task_id="t003", proposed_name="r3",
        reviewed_at="2026-04-24T03:00:00+00:00",
    )

    await publisher.tick()

    # Exactly one repo created — FIFO by reviewed_at picks p1 first.
    assert len(host.created_public_repos) == 1
    assert host.created_public_repos[0]["name"] == "r1"
    assert memory.get_proposal(p1.proposal_id).status == "published"  # type: ignore[union-attr]
    assert memory.get_proposal(p2.proposal_id).status == "approved"  # type: ignore[union-attr]
    assert memory.get_proposal(p3.proposal_id).status == "approved"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Stale-executing reconcile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_reconciles_stale_executing_pre_create(
    env: tuple[Config, Path, Path],
) -> None:
    config, memory_dir, _ = env
    now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
    stale_ts = (now - timedelta(seconds=601)).isoformat()
    publisher, memory, host, _ = _make_publisher(
        config, memory_dir, clock=now,
    )
    proposal = _seed_proposal(
        memory, status="executing",
        executing_started_at=stale_ts,
    )

    await publisher.tick()

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error == "stale_executing_pre_create"
    # The reconcile pass must NOT touch GitHub.
    assert host.created_public_repos == []


@pytest.mark.asyncio
async def test_tick_does_not_reconcile_executing_within_threshold(
    env: tuple[Config, Path, Path],
) -> None:
    config, memory_dir, _ = env
    now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
    fresh_ts = (now - timedelta(seconds=60)).isoformat()
    publisher, memory, _, _ = _make_publisher(
        config, memory_dir, clock=now,
    )
    proposal = _seed_proposal(
        memory, status="executing",
        executing_started_at=fresh_ts,
    )

    await publisher.tick()

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "executing"


# ---------------------------------------------------------------------------
# Resume after checkpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_resumes_after_checkpoint_with_existing_staging(
    env: tuple[Config, Path, Path],
) -> None:
    config, memory_dir, _ = env
    publisher, memory, host, runner = _make_publisher(config, memory_dir)
    proposal = _seed_proposal(
        memory, status="executing",
        executing_started_at="2026-04-25T11:55:00+00:00",
        published_url="https://github.com/thomas-sodon/demo-repo",
    )
    # Pretend a previous tick already created the staging dir.
    staging = memory_dir / "publications" / proposal.proposal_id
    staging.mkdir(parents=True)

    await publisher.tick()

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "published"
    # Resume path must NOT call create_public_repo a second time.
    assert host.created_public_repos == []
    push_calls = [
        c for c in runner.call_args_list
        if c.args[0][:2] == ["git", "push"]
    ]
    assert len(push_calls) == 1


@pytest.mark.asyncio
async def test_tick_resume_marks_failed_when_staging_missing(
    env: tuple[Config, Path, Path],
) -> None:
    """Checkpointed URL but staging dir gone → keep URL, mark failed."""
    config, memory_dir, _ = env
    publisher, memory, host, _ = _make_publisher(config, memory_dir)
    proposal = _seed_proposal(
        memory, status="executing",
        executing_started_at="2026-04-25T11:55:00+00:00",
        published_url="https://github.com/thomas-sodon/demo-repo",
    )
    # No staging dir on disk.

    await publisher.tick()

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error == "staging_missing_after_repo_create"
    # Critical: orphan URL must remain visible for cleanup.
    assert refreshed.published_url == "https://github.com/thomas-sodon/demo-repo"
    assert host.created_public_repos == []


@pytest.mark.asyncio
async def test_tick_resume_push_retry_exhaustion_keeps_published_url(
    env: tuple[Config, Path, Path],
) -> None:
    config, memory_dir, _ = env

    def runner_side_effect(cmd: list[str], **_: Any) -> Any:
        if cmd[:2] == ["git", "push"]:
            raise subprocess.CalledProcessError(
                1, "git", stderr="upstream rejected",
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    runner = MagicMock(side_effect=runner_side_effect)
    publisher, memory, _, _ = _make_publisher(
        config, memory_dir, runner=runner,
    )
    proposal = _seed_proposal(
        memory, status="executing",
        executing_started_at="2026-04-25T11:55:00+00:00",
        published_url="https://github.com/thomas-sodon/demo-repo",
    )
    staging = memory_dir / "publications" / proposal.proposal_id
    staging.mkdir(parents=True)

    await publisher.tick()

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error is not None
    assert refreshed.error.startswith("repo_created_but_push_failed")
    assert refreshed.published_url == "https://github.com/thomas-sodon/demo-repo"
    push_calls = [
        c for c in runner.call_args_list
        if c.args[0][:2] == ["git", "push"]
    ]
    # Honored the configured push_retry_limit (2 in env fixture).
    assert len(push_calls) == 2


# ---------------------------------------------------------------------------
# set-url idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_set_origin_falls_back_to_set_url_when_remote_exists(
    env: tuple[Config, Path, Path],
) -> None:
    """If ``git remote add origin`` fails (already exists), fall back
    to ``set-url`` so resume is idempotent."""
    config, memory_dir, _ = env

    add_calls: list[list[str]] = []
    seturl_calls: list[list[str]] = []

    def runner_side_effect(cmd: list[str], **_: Any) -> Any:
        if cmd[:3] == ["git", "remote", "add"]:
            add_calls.append(cmd)
            raise subprocess.CalledProcessError(
                128, "git", stderr="error: remote origin already exists",
            )
        if cmd[:3] == ["git", "remote", "set-url"]:
            seturl_calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    runner = MagicMock(side_effect=runner_side_effect)
    publisher, memory, _, _ = _make_publisher(
        config, memory_dir, runner=runner,
    )
    proposal = _seed_proposal(
        memory, status="executing",
        executing_started_at="2026-04-25T11:55:00+00:00",
        published_url="https://github.com/thomas-sodon/demo-repo",
    )
    staging = memory_dir / "publications" / proposal.proposal_id
    staging.mkdir(parents=True)

    await publisher.tick()

    assert len(add_calls) == 1
    assert len(seturl_calls) == 1
    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "published"


@pytest.mark.asyncio
async def test_set_origin_does_not_fall_back_on_non_already_exists_failure(
    env: tuple[Config, Path, Path],
) -> None:
    """A ``git remote add`` failure that is *not* the already-exists case
    must re-raise the original error, never fall back to ``set-url``.

    The persisted proposal error must then quote the ``git remote add``
    stderr (the true cause), not a ``set-url`` stderr (the fallback that
    the old unconditional code would have run and surfaced instead)."""
    config, memory_dir, _ = env

    add_stderr = (
        "fatal: could not create leading directories of "
        "'.git/config': Permission denied"
    )
    add_calls: list[list[str]] = []
    seturl_calls: list[list[str]] = []

    def runner_side_effect(cmd: list[str], **_: Any) -> Any:
        if cmd[:3] == ["git", "remote", "add"]:
            add_calls.append(cmd)
            raise subprocess.CalledProcessError(
                128, "git", stderr=add_stderr,
            )
        if cmd[:3] == ["git", "remote", "set-url"]:
            seturl_calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    runner = MagicMock(side_effect=runner_side_effect)
    publisher, memory, _, _ = _make_publisher(
        config, memory_dir, runner=runner,
    )
    proposal = _seed_proposal(
        memory, status="executing",
        executing_started_at="2026-04-25T11:55:00+00:00",
        published_url="https://github.com/thomas-sodon/demo-repo",
    )
    staging = memory_dir / "publications" / proposal.proposal_id
    staging.mkdir(parents=True)

    await publisher.tick()

    assert len(add_calls) == 1
    assert seturl_calls == []
    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error is not None
    assert "could not create leading directories" in refreshed.error
    assert "git remote add" in refreshed.error
    assert "set-url" not in refreshed.error


@pytest.mark.asyncio
async def test_run_git_pins_c_locale_for_deterministic_stderr(
    env: tuple[Config, Path, Path],
) -> None:
    """Every git subprocess must run under a pinned C locale so the
    ``_set_origin_remote`` already-exists marker match is deterministic
    regardless of the deployment locale.

    Without a pinned locale, git's ``remote origin already exists`` stderr
    is translated under a non-English ``LANG``/``LC_ALL``, the English
    ``"already exists"`` marker misses, and a legitimate resumed publish
    re-raises instead of falling back to ``set-url`` — a regression versus
    the old unconditional fallback. The pinned env must *extend*
    ``os.environ`` (not replace it) so push auth — ``GITHUB_TOKEN``,
    ``HOME``, ``PATH`` — still reaches the subprocess.
    """
    config, memory_dir, artifacts_root = env
    publisher, memory, _, runner = _make_publisher(config, memory_dir)
    _seed_task_with_artifact(memory, artifacts_root)
    _seed_proposal(memory)

    await publisher.tick()

    git_calls = [c for c in runner.call_args_list if c.args[0][:1] == ["git"]]
    assert git_calls  # the publish path shells out to git
    for call in git_calls:
        passed_env = call.kwargs.get("env")
        assert passed_env is not None
        assert passed_env.get("LC_ALL") == "C"
        assert passed_env.get("LANG") == "C"
        # Extends os.environ rather than replacing it, so auth survives.
        assert set(os.environ) <= set(passed_env)


# ---------------------------------------------------------------------------
# Crash-window invariant: two ticks straddling crash → one repo creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crash_between_create_and_push_does_not_double_create_repo(
    env: tuple[Config, Path, Path],
) -> None:
    """Two consecutive tick()s straddling a simulated crash between
    steps 6 and 7 must invoke create_public_repo exactly once."""
    config, memory_dir, artifacts_root = env

    # First tick: create_public_repo succeeds, then push raises (the
    # simulated crash window — the checkpoint already wrote
    # published_url before push runs).
    push_should_fail = {"value": True}

    def runner_side_effect(cmd: list[str], **_: Any) -> Any:
        if cmd[:2] == ["git", "push"] and push_should_fail["value"]:
            raise subprocess.CalledProcessError(
                1, "git", stderr="simulated crash",
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    runner = MagicMock(side_effect=runner_side_effect)
    publisher, memory, host, _ = _make_publisher(
        config, memory_dir, runner=runner,
    )
    _seed_task_with_artifact(memory, artifacts_root)
    proposal = _seed_proposal(memory)

    # First tick — create succeeds, push fails, status flips to failed
    # but published_url is preserved on the checkpoint.
    await publisher.tick()
    after_first = memory.get_proposal(proposal.proposal_id)
    assert after_first is not None
    assert (
        after_first.published_url
        == "https://github.com/thomas-sodon/demo-repo"
    )
    assert after_first.status == "failed"
    assert len(host.created_public_repos) == 1

    # Simulate operator nudging the proposal back to executing for resume
    # (in production this would happen automatically if push had been
    # transient — here we manually re-prime to exercise the resume path).
    memory.update_proposal_status(
        proposal.proposal_id, "executing",
        expect_status="failed",
    )

    push_should_fail["value"] = False
    await publisher.tick()

    # Critical invariant: only ONE create_public_repo call across both ticks.
    assert len(host.created_public_repos) == 1
    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "published"


# ---------------------------------------------------------------------------
# Publication disabled flag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publisher_runs_independently_of_publication_enabled_flag(
    env: tuple[Config, Path, Path],
) -> None:
    """The Publisher itself does not consult ``publication_enabled`` —
    the loop's ``_phase_will_run`` handles that gate. Constructing one
    against a disabled config should still process approved proposals
    when the loop dispatches it."""
    config, memory_dir, artifacts_root = env
    config = Config(
        **{
            **{
                "memory_dir": config.memory_dir,
                "workspace": config.workspace,
                "enable_session_reuse": False,
                "publication_push_retry_limit": 2,
                "publication_executing_stale_timeout_seconds": 600,
            },
            "publication_enabled": False,
        },
    )
    publisher, memory, host, _ = _make_publisher(config, memory_dir)
    _seed_task_with_artifact(memory, artifacts_root)
    _seed_proposal(memory)

    await publisher.tick()

    # Yes — runs anyway; the gate lives in ``_phase_will_run``.
    assert len(host.created_public_repos) == 1
