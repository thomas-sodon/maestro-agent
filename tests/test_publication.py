"""Unit tests for :mod:`maestro.publication`.

Exercises the :class:`Publisher` internals: staging-time secret scan
(hard-block, redact-only, binary), the publishable-files whitelist,
``_git_init_commit`` shape, and the TOCTOU-safe step-1 flip.

The publication pipeline walks real directories via ``Path.read_text``
and ``Path.is_dir``, so these unit tests use ``tmp_path`` directly
rather than the in-memory ``mem_fs`` fixture (mirroring
``tests/test_artifact_review.py``). All git/gh subprocesses are mocked
via the injected ``runner`` and ``host``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro.config import Config
from maestro.journal import Journal
from maestro.memory import (
    DuplicateProposalError,
    Memory,
    PlanStep,
    PublicationProposal,
    TaskRecord,
)
from maestro.publication import (
    _FIFO_SORT_SENTINEL,
    PublicationBlockedError,
    PublicationFailedError,
    Publisher,
    _parse_iso,
)
from maestro.vcs import VCSError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    return memory_dir, workspace, artifacts


def _make_config(memory_dir: Path, workspace: Path, **overrides: Any) -> Config:
    kwargs: dict[str, Any] = {
        "memory_dir": memory_dir,
        "workspace": workspace,
        "enable_session_reuse": False,
    }
    kwargs.update(overrides)
    return Config(**kwargs)


def _make_publisher(
    memory_dir: Path,
    workspace: Path,
    *,
    host: Any | None = None,
    notifier: Any | None = None,
    runner: Any | None = None,
    clock: datetime = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC),
    **config_overrides: Any,
) -> tuple[Publisher, Memory, Any]:
    config = _make_config(memory_dir, workspace, **config_overrides)
    memory = Memory(memory_dir)
    journal = Journal(memory_dir)
    _host = host or _make_host()
    _notifier = notifier or _make_notifier()
    _runner = runner or MagicMock(
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )
    publisher = Publisher(
        memory=memory,
        journal=journal,
        notifier=_notifier,
        host=_host,
        config=config,
        clock=lambda: clock,
        runner=_runner,
    )
    return publisher, memory, _runner


def _make_host(
    *,
    owner: str = "thomas-sodon",
    create_url: str | None = None,
) -> Any:
    host = MagicMock()
    host.resolve_owner.return_value = owner
    host.repo_exists.return_value = False
    host.create_public_repo.return_value = (
        create_url if create_url is not None
        else f"https://github.com/{owner}/auto"
    )
    return host


def _make_notifier() -> Any:
    notifier = MagicMock()
    notifier.notify = AsyncMock()
    notifier.resolve_publication = AsyncMock()
    return notifier


def _seed_task(memory: Memory, task_id: str, artifacts_dir: Path) -> TaskRecord:
    task = TaskRecord(
        task_id=task_id,
        goal="demo task",
        plan=[PlanStep(step_id="s1", description="do work")],
        outcome="success",
        result_summary="done",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-04-24T00:00:00+00:00",
        source="self_directed",
        artifacts_dir=str(artifacts_dir),
    )
    memory.record_task(task)
    return task


def _seed_proposal(
    memory: Memory,
    *,
    proposal_id: str = "p001",
    task_id: str = "t001",
    status: str = "approved",
    publishable_files: list[str] | None = None,
    proposed_readme: str = "# demo\n\nLook at this thing.\n",
    proposed_name: str = "demo-repo",
    description: str = "demo description",
    created_at: str = "2026-04-23T00:00:00+00:00",
    reviewed_at: str = "2026-04-24T01:00:00+00:00",
    executing_started_at: str | None = None,
    published_url: str | None = None,
) -> PublicationProposal:
    proposal = PublicationProposal(
        proposal_id=proposal_id,
        task_id=task_id,
        review_id="r001",
        proposed_name=proposed_name,
        description=description,
        proposed_readme=proposed_readme,
        publishable_files=publishable_files
        if publishable_files is not None
        else ["main.py", "data.txt"],
        status=status,  # type: ignore[arg-type]
        created_at=created_at,
        reviewed_at=reviewed_at,
        scrub_findings=[],
        executing_started_at=executing_started_at,
        published_url=published_url,
    )
    memory.write_publication_proposal(proposal)
    return proposal


# ---------------------------------------------------------------------------
# _stage
# ---------------------------------------------------------------------------


def test_stage_writes_scrubbed_files_license_and_readme(paths: tuple[Path, Path, Path]) -> None:
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text(
        "# /home/operator/secret/path is here\nprint('hi')\n",
        encoding="utf-8",
    )
    (artifacts / "data.txt").write_text(
        "user contact: operator@example.com",
        encoding="utf-8",
    )
    publisher, memory, _ = _make_publisher(memory_dir, workspace)
    proposal = _seed_proposal(memory)
    staging = memory_dir / "publications" / proposal.proposal_id
    staging.mkdir(parents=True)

    findings = publisher._stage(proposal, artifacts, staging)

    # Scrubbed file content was written to the staging dir.
    main_py = (staging / "main.py").read_text(encoding="utf-8")
    assert "<redacted-path>" in main_py
    assert "/home/operator/" not in main_py
    data_txt = (staging / "data.txt").read_text(encoding="utf-8")
    assert "<redacted-email>" in data_txt
    assert "operator@example.com" not in data_txt

    # README + LICENSE materialised. .gitignore is dropped because a .py
    # file was staged.
    assert (staging / "README.md").read_text(encoding="utf-8").startswith("# demo")
    license_text = (staging / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in license_text
    assert "2026 Thomas Sodon" in license_text
    assert (staging / ".gitignore").exists()

    # Redact findings flow back to the caller for checkpoint persistence.
    assert any(f.startswith("redacted-path") for f in findings)
    assert any(f.startswith("redacted-email") for f in findings)


def test_stage_raises_publication_blocked_on_hard_block(
    paths: tuple[Path, Path, Path],
) -> None:
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text(
        "AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'\n", encoding="utf-8",
    )
    publisher, memory, _ = _make_publisher(memory_dir, workspace)
    proposal = _seed_proposal(
        memory, publishable_files=["main.py"],
    )
    staging = memory_dir / "publications" / proposal.proposal_id
    staging.mkdir(parents=True)

    with pytest.raises(PublicationBlockedError, match="secret_scan_blocked"):
        publisher._stage(proposal, artifacts, staging)


def test_stage_raises_publication_blocked_on_pkcs8_private_key(
    paths: tuple[Path, Path, Path],
) -> None:
    memory_dir, workspace, artifacts = paths
    (artifacts / "key.pem").write_text(
        "-----BEGIN PRIVATE KEY-----\nMIIE...\n-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    publisher, memory, _ = _make_publisher(memory_dir, workspace)
    proposal = _seed_proposal(
        memory, publishable_files=["key.pem"],
    )
    staging = memory_dir / "publications" / proposal.proposal_id
    staging.mkdir(parents=True)

    with pytest.raises(PublicationBlockedError, match="secret_scan_blocked"):
        publisher._stage(proposal, artifacts, staging)


def test_stage_raises_for_binary_file(paths: tuple[Path, Path, Path]) -> None:
    memory_dir, workspace, artifacts = paths
    (artifacts / "image.bin").write_bytes(b"\xff\xfe\x00binary")
    publisher, memory, _ = _make_publisher(memory_dir, workspace)
    proposal = _seed_proposal(
        memory, publishable_files=["image.bin"],
    )
    staging = memory_dir / "publications" / proposal.proposal_id
    staging.mkdir(parents=True)

    with pytest.raises(PublicationBlockedError, match=r"binary_in_publishable_files:image\.bin"):
        publisher._stage(proposal, artifacts, staging)


def test_stage_rejects_absolute_path_in_publishable_files(
    paths: tuple[Path, Path, Path],
) -> None:
    memory_dir, workspace, artifacts = paths
    publisher, memory, _ = _make_publisher(memory_dir, workspace)
    proposal = _seed_proposal(
        memory, publishable_files=["/etc/passwd"],
    )
    staging = memory_dir / "publications" / proposal.proposal_id
    staging.mkdir(parents=True)

    with pytest.raises(
        PublicationBlockedError,
        match=r"unsafe_path_in_publishable_files",
    ):
        publisher._stage(proposal, artifacts, staging)


def test_stage_rejects_parent_traversal_in_publishable_files(
    paths: tuple[Path, Path, Path],
) -> None:
    memory_dir, workspace, artifacts = paths
    outside = artifacts.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    publisher, memory, _ = _make_publisher(memory_dir, workspace)
    proposal = _seed_proposal(
        memory, publishable_files=["../outside.txt"],
    )
    staging = memory_dir / "publications" / proposal.proposal_id
    staging.mkdir(parents=True)

    with pytest.raises(
        PublicationBlockedError,
        match=r"unsafe_path_in_publishable_files",
    ):
        publisher._stage(proposal, artifacts, staging)


def test_stage_does_not_read_outside_artifacts_dir_when_path_unsafe(
    paths: tuple[Path, Path, Path],
) -> None:
    """Guard runs before read_text; out-of-tree files are never opened."""
    memory_dir, workspace, artifacts = paths
    outside = artifacts.parent / "secret.txt"
    outside.write_text("DO NOT READ", encoding="utf-8")
    publisher, memory, _ = _make_publisher(memory_dir, workspace)
    proposal = _seed_proposal(
        memory, publishable_files=["../secret.txt"],
    )
    staging = memory_dir / "publications" / proposal.proposal_id
    staging.mkdir(parents=True)

    with pytest.raises(PublicationBlockedError):
        publisher._stage(proposal, artifacts, staging)
    assert not (staging / "secret.txt").exists()
    assert not list(staging.glob("**/*.txt"))


def test_stage_rejects_when_path_resolution_raises(
    paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSError from ``Path.resolve`` maps to ``unsafe_path_in_publishable_files``."""
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text("ok", encoding="utf-8")
    publisher, memory, _ = _make_publisher(memory_dir, workspace)
    proposal = _seed_proposal(memory, publishable_files=["main.py"])
    staging = memory_dir / "publications" / proposal.proposal_id
    staging.mkdir(parents=True)

    real_resolve = Path.resolve
    state = {"count": 0}

    def fake_resolve(self: Path, strict: bool = False) -> Path:
        state["count"] += 1
        if state["count"] >= 3:
            raise OSError("simulated symlink loop")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    with pytest.raises(
        PublicationBlockedError,
        match=r"unsafe_path_in_publishable_files",
    ):
        publisher._stage(proposal, artifacts, staging)


def test_stage_rejects_when_resolve_raises_oserror(
    paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSError (e.g. ELOOP, EACCES) during path resolution is a hard reject."""
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text("ok\n", encoding="utf-8")
    publisher, memory, _ = _make_publisher(memory_dir, workspace)
    proposal = _seed_proposal(memory, publishable_files=["main.py"])
    staging = memory_dir / "publications" / proposal.proposal_id
    staging.mkdir(parents=True)

    real_resolve = Path.resolve

    def maybe_raise(self: Path, *args: Any, **kwargs: Any) -> Path:
        if self.name == "main.py":
            raise OSError("simulated ELOOP")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", maybe_raise)

    with pytest.raises(
        PublicationBlockedError,
        match=r"unsafe_path_in_publishable_files:main\.py",
    ):
        publisher._stage(proposal, artifacts, staging)


def test_stage_dest_check_fires_when_only_dest_escapes_staging(
    paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dest-side check is reachable independently of src-side check."""
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text("ok", encoding="utf-8")
    publisher, memory, _ = _make_publisher(memory_dir, workspace)
    proposal = _seed_proposal(memory, publishable_files=["main.py"])
    staging = memory_dir / "publications" / proposal.proposal_id
    staging.mkdir(parents=True)

    real_is_relative_to = Path.is_relative_to
    state = {"count": 0}

    def fake_is_relative_to(self: Path, other: Any) -> bool:
        state["count"] += 1
        if state["count"] == 2:
            return False
        return real_is_relative_to(self, other)

    monkeypatch.setattr(Path, "is_relative_to", fake_is_relative_to)

    with pytest.raises(
        PublicationBlockedError,
        match=r"unsafe_path_in_publishable_files",
    ):
        publisher._stage(proposal, artifacts, staging)


def test_stage_rejects_when_staging_dest_resolves_outside_staging_root(
    paths: tuple[Path, Path, Path],
) -> None:
    """Defense-in-depth: a symlink inside staging that escapes is rejected
    even when the src side is safely inside artifacts_dir."""
    import os
    memory_dir, workspace, artifacts = paths
    (artifacts / "data.txt").write_text("hi", encoding="utf-8")
    publisher, memory, _ = _make_publisher(memory_dir, workspace)
    proposal = _seed_proposal(memory, publishable_files=["data.txt"])
    staging = memory_dir / "publications" / proposal.proposal_id
    staging.mkdir(parents=True)
    escape_target = artifacts.parent / "escape_target.txt"
    escape_target.write_text("placeholder", encoding="utf-8")
    os.symlink(str(escape_target), str(staging / "data.txt"))

    with pytest.raises(
        PublicationBlockedError,
        match=r"unsafe_path_in_publishable_files:data\.txt",
    ):
        publisher._stage(proposal, artifacts, staging)


def test_stage_does_not_write_gitignore_when_no_python_files(
    paths: tuple[Path, Path, Path],
) -> None:
    memory_dir, workspace, artifacts = paths
    (artifacts / "data.txt").write_text("hello", encoding="utf-8")
    publisher, memory, _ = _make_publisher(memory_dir, workspace)
    proposal = _seed_proposal(
        memory, publishable_files=["data.txt"],
    )
    staging = memory_dir / "publications" / proposal.proposal_id
    staging.mkdir(parents=True)

    publisher._stage(proposal, artifacts, staging)

    assert not (staging / ".gitignore").exists()


def test_stage_only_copies_files_in_publishable_files_whitelist(
    paths: tuple[Path, Path, Path],
) -> None:
    """Files in the artifacts dir but not in publishable_files must be skipped."""
    memory_dir, workspace, artifacts = paths
    (artifacts / "wanted.txt").write_text("ok", encoding="utf-8")
    (artifacts / "secret_log.txt").write_text("nope", encoding="utf-8")
    publisher, memory, _ = _make_publisher(memory_dir, workspace)
    proposal = _seed_proposal(
        memory, publishable_files=["wanted.txt"],
    )
    staging = memory_dir / "publications" / proposal.proposal_id
    staging.mkdir(parents=True)

    publisher._stage(proposal, artifacts, staging)

    assert (staging / "wanted.txt").exists()
    assert not (staging / "secret_log.txt").exists()


# ---------------------------------------------------------------------------
# _git_init_commit
# ---------------------------------------------------------------------------


def test_git_init_commit_runs_init_add_and_commit(
    paths: tuple[Path, Path, Path],
) -> None:
    memory_dir, workspace, _artifacts = paths
    runner = MagicMock(
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )
    publisher, _memory, _ = _make_publisher(
        memory_dir, workspace, runner=runner,
    )
    staging = memory_dir / "publications" / "p1"
    staging.mkdir(parents=True)

    publisher._git_init_commit(staging, "task-42")

    cmds = [call.args[0] for call in runner.call_args_list]
    assert cmds[0] == ["git", "init", "-b", "main"]
    assert cmds[1] == ["git", "add", "-A"]
    # Per-invocation identity (-c user.email / -c user.name) — never
    # mutate persistent git config in the staging dir.
    commit_cmd = cmds[2]
    assert commit_cmd[0] == "git"
    assert "-c" in commit_cmd
    assert any("user.email=" in tok for tok in commit_cmd)
    assert any("user.name=" in tok for tok in commit_cmd)
    assert "commit.gpgsign=false" in " ".join(commit_cmd)
    assert commit_cmd[-1] == "Initial publication for task task-42"


def test_git_init_commit_propagates_failure_as_publication_failed_error(
    paths: tuple[Path, Path, Path],
) -> None:
    memory_dir, workspace, _ = paths
    runner = MagicMock(
        side_effect=subprocess.CalledProcessError(
            128, "git", stderr="fatal: cannot init",
        ),
    )
    publisher, _memory, _ = _make_publisher(
        memory_dir, workspace, runner=runner,
    )
    staging = memory_dir / "publications" / "p1"
    staging.mkdir(parents=True)
    with pytest.raises(PublicationFailedError, match="git init"):
        publisher._git_init_commit(staging, "task-42")


# ---------------------------------------------------------------------------
# _publish — TOCTOU on step 1
# ---------------------------------------------------------------------------


def test_publish_skips_when_proposal_not_approved(
    paths: tuple[Path, Path, Path],
) -> None:
    """A proposal already moved out of ``approved`` must skip silently."""
    memory_dir, workspace, artifacts = paths
    publisher, memory, runner = _make_publisher(memory_dir, workspace)
    # Seed a *rejected* proposal — the step-1 expect_status="approved"
    # check inside _publish must bail before any git/gh side effect.
    proposal = _seed_proposal(memory, status="rejected")
    _seed_task(memory, "t001", artifacts)

    asyncio.run(publisher._publish(proposal))

    runner.assert_not_called()
    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "rejected"


def test_publish_marks_failed_when_artifacts_missing(
    paths: tuple[Path, Path, Path],
) -> None:
    memory_dir, workspace, _ = paths
    host = _make_host()
    publisher, memory, runner = _make_publisher(
        memory_dir, workspace, host=host,
    )
    # Approved proposal but no task_record → artifacts_missing.
    proposal = _seed_proposal(memory)

    asyncio.run(publisher._publish(proposal))

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error == "artifacts_missing"
    host.create_public_repo.assert_not_called()
    runner.assert_not_called()


def test_publish_survives_archive_while_pending_approval(
    paths: tuple[Path, Path, Path],
) -> None:
    """A task archived while its proposal awaited approval still publishes (#4326)."""
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text("print('hi')\n", encoding="utf-8")
    host = _make_host()
    publisher, memory, _ = _make_publisher(memory_dir, workspace, host=host)
    # The seeded task's fixed 2026-04 timestamp is well past a 30-day threshold.
    _seed_task(memory, "t001", artifacts)
    proposal = _seed_proposal(
        memory, status="pending_approval", publishable_files=["main.py"],
    )

    memory.archive(threshold_days=30)
    memory.update_proposal_status(proposal.proposal_id, "approved")
    approved = memory.get_proposal(proposal.proposal_id)
    assert approved is not None
    asyncio.run(publisher._publish(approved))

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.error != "artifacts_missing"
    host.create_public_repo.assert_called_once()


def test_publish_full_pipeline_persists_published_url_after_create(
    paths: tuple[Path, Path, Path],
) -> None:
    """After ``gh repo create`` succeeds, ``published_url`` is persisted
    *before* the push step runs — that's the durable checkpoint."""
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text(
        "print('hi')\n", encoding="utf-8",
    )
    host = _make_host(create_url="https://github.com/thomas-sodon/demo-repo")
    publisher, memory, runner = _make_publisher(
        memory_dir, workspace, host=host,
    )
    _seed_task(memory, "t001", artifacts)
    proposal = _seed_proposal(
        memory, publishable_files=["main.py"],
    )

    asyncio.run(publisher._publish(proposal))

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "published"
    assert refreshed.published_url == "https://github.com/thomas-sodon/demo-repo"
    host.create_public_repo.assert_called_once()
    # Push step ran at least once.
    pushed = [
        call.args[0] for call in runner.call_args_list
        if call.args[0][:2] == ["git", "push"]
    ]
    assert len(pushed) >= 1


def test_publish_keeps_published_url_when_push_exhausts(
    paths: tuple[Path, Path, Path],
) -> None:
    """After checkpoint, push failure must KEEP ``published_url`` so
    Thomas can find the orphan repo."""
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text("print('hi')\n", encoding="utf-8")

    def runner_side_effect(cmd: list[str], **_: Any) -> Any:
        if cmd[:2] == ["git", "push"]:
            raise subprocess.CalledProcessError(
                1, "git", stderr="rejected",
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    runner = MagicMock(side_effect=runner_side_effect)
    host = _make_host(create_url="https://github.com/thomas-sodon/demo-repo")
    publisher, memory, _ = _make_publisher(
        memory_dir, workspace, host=host, runner=runner,
        publication_push_retry_limit=2,
    )
    _seed_task(memory, "t001", artifacts)
    proposal = _seed_proposal(
        memory, publishable_files=["main.py"],
    )

    asyncio.run(publisher._publish(proposal))

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error is not None
    assert refreshed.error.startswith("repo_created_but_push_failed:")
    # Critical: orphan URL must remain visible so Thomas can clean up.
    assert refreshed.published_url == "https://github.com/thomas-sodon/demo-repo"


# ---------------------------------------------------------------------------
# _parse_iso helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, ""])
def test_parse_iso_returns_none_for_missing_value(value: str | None) -> None:
    assert _parse_iso(value) is None


def test_parse_iso_returns_none_for_invalid_string() -> None:
    assert _parse_iso("not-a-date") is None


def test_parse_iso_attaches_utc_to_naive_datetime() -> None:
    parsed = _parse_iso("2026-04-25T12:00:00")
    assert parsed is not None
    assert parsed.tzinfo is UTC


# ---------------------------------------------------------------------------
# _reconcile_stale_executing — missing started_at
# ---------------------------------------------------------------------------


def test_reconcile_treats_executing_without_started_at_as_immediately_stale(
    paths: tuple[Path, Path, Path],
) -> None:
    """Executing rows with no/unparseable ``executing_started_at`` are
    treated as stale on the first reconcile pass — otherwise they would
    block the queue forever."""
    memory_dir, workspace, _ = paths
    publisher, memory, _ = _make_publisher(memory_dir, workspace)
    proposal = _seed_proposal(
        memory, status="executing", executing_started_at=None,
    )

    asyncio.run(publisher._reconcile_stale_executing())

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error == "stale_executing_pre_create"


def test_reconcile_routes_to_resume_when_repo_already_exists(
    paths: tuple[Path, Path, Path],
) -> None:
    """A crash between Step 6 (``create_public_repo``) and Step 7 (the
    ``published_url`` checkpoint) leaves the row ``executing`` +
    ``published_url=None`` while the public repo already exists on GitHub.
    The reconcile pass must probe ``repo_exists``, checkpoint the
    reconstructed URL, and hand the row to the resume pass — never stamp
    ``stale_executing_pre_create`` (which lies about a never-created repo)
    nor wipe the staging tree the resume push still needs."""
    memory_dir, workspace, _ = paths
    notifier = _make_notifier()
    host = _make_host(owner="thomas-sodon")
    host.repo_exists.return_value = True
    # Empty ``[publication] owner`` is the sentinel that selects the
    # ``resolve_owner`` fallback, which is the branch this reconcile probe
    # exercises; the committed default names the organization instead.
    publisher, memory, _ = _make_publisher(
        memory_dir, workspace, host=host, notifier=notifier,
        publication_owner="",
    )
    proposal = _seed_proposal(
        memory,
        status="executing",
        executing_started_at="2026-04-25T11:00:00+00:00",  # 1h < clock
    )
    staging = publisher._staging_dir_for(proposal.proposal_id)
    staging.mkdir(parents=True)
    (staging / "keep.txt").write_text("x", encoding="utf-8")

    asyncio.run(publisher._reconcile_stale_executing())

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    # Routed to resume, not failed.
    assert refreshed.status == "executing"
    assert (
        refreshed.published_url == "https://github.com/thomas-sodon/demo-repo"
    )
    # Probed with the Step-6-resolved owner + proposed_name.
    host.repo_exists.assert_called_once_with("thomas-sodon", "demo-repo")
    # Staging tree preserved for the resume pass; no failure notification.
    assert staging.exists()
    notifier.notify.assert_not_called()


def test_reconcile_keeps_stale_label_and_names_repo_when_repo_absent(
    paths: tuple[Path, Path, Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the repo is truly absent (the real pre-create crash, and the
    conservative fallback for an inconclusive ``repo_exists`` probe), the
    stale row is still failed with ``stale_executing_pre_create`` — and the
    warning and failure notification now name ``proposed_name`` so an
    operator can find any orphan from a lone surviving log line."""
    memory_dir, workspace, _ = paths
    notifier = _make_notifier()
    # _make_host default stubs repo_exists=False (the absent/inconclusive
    # probe result the conservative fallback keys on).
    publisher, memory, _ = _make_publisher(
        memory_dir, workspace, notifier=notifier,
    )
    proposal = _seed_proposal(
        memory,
        status="executing",
        executing_started_at="2026-04-25T11:00:00+00:00",
    )

    with caplog.at_level("WARNING", logger="maestro"):
        asyncio.run(publisher._reconcile_stale_executing())

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error == "stale_executing_pre_create"
    # The warning names the repo so a surviving log line enables cleanup.
    named_warnings = [
        record.getMessage() for record in caplog.records
        if record.levelname == "WARNING"
        and proposal.proposal_id in record.getMessage()
        and proposal.proposed_name in record.getMessage()
    ]
    assert len(named_warnings) == 1
    # The failure notification also names the repo.
    notifier.notify.assert_awaited_once()
    notify_text = notifier.notify.await_args.args[0]
    assert proposal.proposed_name in notify_text


def test_reconcile_skips_when_row_flips_out_of_executing_at_checkpoint(
    paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the repo exists but a racing surface flips the row out of
    ``executing`` just before the reconcile checkpoint write, the write
    no-ops (``expect_status`` mismatch). The pass must not claim it routed
    to resume, and must not fall through to the stale-fail path — mirroring
    Step 7. The racing surface owns the row."""
    memory_dir, workspace, _ = paths
    notifier = _make_notifier()
    host = _make_host(owner="thomas-sodon")
    host.repo_exists.return_value = True
    publisher, memory, _ = _make_publisher(
        memory_dir, workspace, host=host, notifier=notifier,
    )
    proposal = _seed_proposal(
        memory,
        status="executing",
        executing_started_at="2026-04-25T11:00:00+00:00",  # 1h < clock
    )
    staging = publisher._staging_dir_for(proposal.proposal_id)
    staging.mkdir(parents=True)

    real_update = memory.update_proposal_status

    def wrapping_update(
        proposal_id: str, status: str, **kwargs: Any,
    ) -> bool:
        # Just before the reconcile checkpoint's expect_status="executing"
        # recheck runs under the file_lock, a racing surface flips the row
        # to "rejected". The recheck then sees the wrong state and no-ops.
        if status == "executing" and kwargs.get("expect_status") == "executing":
            real_update(proposal_id, "rejected")
        return real_update(proposal_id, status, **kwargs)

    monkeypatch.setattr(memory, "update_proposal_status", wrapping_update)

    with caplog.at_level("WARNING", logger="maestro.publication"):
        asyncio.run(publisher._reconcile_stale_executing())

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    # The racing write owns the row — reconcile neither checkpointed nor
    # failed it.
    assert refreshed.status == "rejected"
    assert refreshed.published_url is None
    assert refreshed.error is None
    # Skip warning fired; no false "routing to resume" claim.
    skip_warnings = [
        record.getMessage() for record in caplog.records
        if proposal.proposal_id in record.getMessage()
        and "no longer executing at reconcile checkpoint" in record.getMessage()
    ]
    assert len(skip_warnings) == 1
    routed_warnings = [
        record.getMessage() for record in caplog.records
        if "routing to resume" in record.getMessage()
    ]
    assert routed_warnings == []
    # No stale-fail side effects: staging preserved, no failure notify.
    assert staging.exists()
    notifier.notify.assert_not_called()


def test_reconcile_skips_fail_side_effects_when_row_flips_out_of_executing(
    paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the repo is absent and a racing surface flips the row out of
    ``executing`` just before the stale-fail write, the write no-ops
    (``expect_status`` mismatch). The pass must not wipe the staging tree
    the winning surface owns nor send a misleading "timed out" failure
    notification — mirroring Step 7 and the resume branch's guard."""
    memory_dir, workspace, _ = paths
    notifier = _make_notifier()
    # _make_host default stubs repo_exists=False — the stale-fail path.
    publisher, memory, _ = _make_publisher(
        memory_dir, workspace, notifier=notifier,
    )
    proposal = _seed_proposal(
        memory,
        status="executing",
        executing_started_at="2026-04-25T11:00:00+00:00",  # 1h < clock
    )
    staging = publisher._staging_dir_for(proposal.proposal_id)
    staging.mkdir(parents=True)
    (staging / "owned.txt").write_text("x", encoding="utf-8")

    real_update = memory.update_proposal_status

    def wrapping_update(
        proposal_id: str, status: str, **kwargs: Any,
    ) -> bool:
        # Just before the stale-fail write's expect_status="executing"
        # recheck runs under the file_lock, a racing surface flips the
        # row to "rejected". The recheck then sees the wrong state and
        # no-ops.
        if status == "failed" and kwargs.get("expect_status") == "executing":
            real_update(proposal_id, "rejected")
        return real_update(proposal_id, status, **kwargs)

    monkeypatch.setattr(memory, "update_proposal_status", wrapping_update)

    with caplog.at_level("WARNING", logger="maestro.publication"):
        asyncio.run(publisher._reconcile_stale_executing())

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    # The racing write owns the row — reconcile did not fail it.
    assert refreshed.status == "rejected"
    assert refreshed.error is None
    # Skip warning fired; no misleading "timed out" claim.
    skip_warnings = [
        record.getMessage() for record in caplog.records
        if proposal.proposal_id in record.getMessage()
        and "no longer executing at reconcile fail" in record.getMessage()
    ]
    assert len(skip_warnings) == 1
    timeout_warnings = [
        record.getMessage() for record in caplog.records
        if "timed out in executing" in record.getMessage()
    ]
    assert timeout_warnings == []
    # No stale-fail side effects: staging preserved, no failure notify.
    assert staging.exists()
    assert (staging / "owned.txt").exists()
    notifier.notify.assert_not_called()


# ---------------------------------------------------------------------------
# _publish — additional failure branches
# ---------------------------------------------------------------------------


def test_publish_marks_failed_when_artifacts_dir_path_not_a_dir(
    paths: tuple[Path, Path, Path],
) -> None:
    """``task.artifacts_dir`` is set but the path is not a directory
    (e.g. it was a file or got deleted) — fail before any git/gh work."""
    memory_dir, workspace, _ = paths
    bogus = memory_dir / "not-a-dir.txt"
    bogus.write_text("nope", encoding="utf-8")
    host = _make_host()
    publisher, memory, runner = _make_publisher(
        memory_dir, workspace, host=host,
    )
    # Seed a task whose artifacts_dir points at a file, not a directory.
    task = TaskRecord(
        task_id="t001",
        goal="demo",
        plan=[PlanStep(step_id="s1", description="x")],
        outcome="success",
        result_summary="done",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-04-24T00:00:00+00:00",
        source="self_directed",
        artifacts_dir=str(bogus),
    )
    memory.record_task(task)
    proposal = _seed_proposal(memory)

    asyncio.run(publisher._publish(proposal))

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error == "artifacts_missing"
    host.create_public_repo.assert_not_called()
    runner.assert_not_called()


def test_publish_marks_failed_when_staging_mkdir_raises_oserror(
    paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text("print('hi')\n", encoding="utf-8")
    host = _make_host()
    publisher, memory, runner = _make_publisher(
        memory_dir, workspace, host=host,
    )
    _seed_task(memory, "t001", artifacts)
    proposal = _seed_proposal(memory, publishable_files=["main.py"])

    real_mkdir = Path.mkdir

    def fake_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        if "publications" in self.parts:
            raise OSError("disk full")
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)

    asyncio.run(publisher._publish(proposal))

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error == "staging_mkdir_failed"
    host.create_public_repo.assert_not_called()
    runner.assert_not_called()


def test_publish_marks_failed_when_stage_raises_publication_blocked(
    paths: tuple[Path, Path, Path],
) -> None:
    """A hard-block finding at publish time aborts before any git/gh
    work — the public repo must never be created if a secret leaks
    through the LLM head-truncated review."""
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text(
        "AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'\n", encoding="utf-8",
    )
    host = _make_host()
    publisher, memory, runner = _make_publisher(
        memory_dir, workspace, host=host,
    )
    _seed_task(memory, "t001", artifacts)
    proposal = _seed_proposal(memory, publishable_files=["main.py"])

    asyncio.run(publisher._publish(proposal))

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error is not None
    assert refreshed.error.startswith("secret_scan_blocked:")
    host.create_public_repo.assert_not_called()
    runner.assert_not_called()


def test_publish_marks_failed_when_proposed_name_carries_hard_block(
    paths: tuple[Path, Path, Path],
) -> None:
    """Backstop for issue #1417: a hard-block secret in the proposed repo
    name aborts before any git/gh work, even when every staged file is
    clean (e.g. an old proposal persisted before the emission guard)."""
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text("print('hi')\n", encoding="utf-8")
    host = _make_host()
    publisher, memory, runner = _make_publisher(
        memory_dir, workspace, host=host,
    )
    _seed_task(memory, "t001", artifacts)
    proposal = _seed_proposal(
        memory,
        publishable_files=["main.py"],
        proposed_name="AKIAIOSFODNN7EXAMPLE",
    )

    asyncio.run(publisher._publish(proposal))

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error is not None
    assert refreshed.error.startswith("secret_scan_blocked:")
    host.create_public_repo.assert_not_called()
    runner.assert_not_called()


def test_publish_marks_failed_when_description_carries_hard_block(
    paths: tuple[Path, Path, Path],
) -> None:
    """Backstop for issue #1417: a hard-block secret in the proposed
    description aborts before any git/gh work."""
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text("print('hi')\n", encoding="utf-8")
    host = _make_host()
    publisher, memory, runner = _make_publisher(
        memory_dir, workspace, host=host,
    )
    _seed_task(memory, "t001", artifacts)
    proposal = _seed_proposal(
        memory,
        publishable_files=["main.py"],
        proposed_name="safe-name",
        description="Use AKIAIOSFODNN7EXAMPLE to connect",
    )

    asyncio.run(publisher._publish(proposal))

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error is not None
    assert refreshed.error.startswith("secret_scan_blocked:")
    host.create_public_repo.assert_not_called()
    runner.assert_not_called()


def test_publish_marks_failed_when_stage_raises_oserror(
    paths: tuple[Path, Path, Path],
) -> None:
    """A publishable file listed but missing from disk surfaces as
    ``stage_io_failed`` rather than crashing the tick."""
    memory_dir, workspace, artifacts = paths
    # Don't write the file — make the stage raise FileNotFoundError (OSError).
    host = _make_host()
    publisher, memory, _runner = _make_publisher(
        memory_dir, workspace, host=host,
    )
    _seed_task(memory, "t001", artifacts)
    proposal = _seed_proposal(memory, publishable_files=["missing.py"])

    asyncio.run(publisher._publish(proposal))

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error == "stage_io_failed"
    host.create_public_repo.assert_not_called()


def test_publish_marks_failed_when_git_init_commit_raises(
    paths: tuple[Path, Path, Path],
) -> None:
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text("print('hi')\n", encoding="utf-8")
    runner = MagicMock(
        side_effect=subprocess.CalledProcessError(
            128, "git", stderr="fatal: cannot init",
        ),
    )
    host = _make_host()
    publisher, memory, _ = _make_publisher(
        memory_dir, workspace, host=host, runner=runner,
    )
    _seed_task(memory, "t001", artifacts)
    proposal = _seed_proposal(memory, publishable_files=["main.py"])

    asyncio.run(publisher._publish(proposal))

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error == "git_init_commit_failed"
    host.create_public_repo.assert_not_called()


def test_publish_marks_failed_when_owner_unresolvable(
    paths: tuple[Path, Path, Path],
) -> None:
    """No configured owner and ``gh`` returns blank → fail before
    ``create_public_repo`` so we never create a repo under the wrong
    namespace."""
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text("print('hi')\n", encoding="utf-8")
    host = _make_host()
    host.resolve_owner.return_value = ""
    publisher, memory, _ = _make_publisher(
        memory_dir, workspace, host=host, publication_owner="",
    )
    _seed_task(memory, "t001", artifacts)
    proposal = _seed_proposal(memory, publishable_files=["main.py"])

    asyncio.run(publisher._publish(proposal))

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error == "owner_unresolvable"
    host.create_public_repo.assert_not_called()


def test_publish_marks_failed_when_create_public_repo_raises_vcs_error(
    paths: tuple[Path, Path, Path],
) -> None:
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text("print('hi')\n", encoding="utf-8")
    host = _make_host()
    host.create_public_repo.side_effect = VCSError("gh: name taken")
    publisher, memory, _ = _make_publisher(
        memory_dir, workspace, host=host,
    )
    _seed_task(memory, "t001", artifacts)
    proposal = _seed_proposal(memory, publishable_files=["main.py"])

    asyncio.run(publisher._publish(proposal))

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error == "create_public_repo_failed"
    # No checkpoint — published_url must NOT be set.
    assert refreshed.published_url is None


def test_publish_returns_silently_when_proposal_vanishes_after_checkpoint(
    paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the proposal disappears between checkpoint write and post-create
    re-read, ``_publish`` returns without touching git push — the next
    tick can't re-resume an absent row, but the durable URL is still on
    record (caught by external dashboards/CLI)."""
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text("print('hi')\n", encoding="utf-8")
    host = _make_host(create_url="https://github.com/thomas-sodon/demo-repo")
    publisher, memory, runner = _make_publisher(
        memory_dir, workspace, host=host,
    )
    _seed_task(memory, "t001", artifacts)
    proposal = _seed_proposal(memory, publishable_files=["main.py"])

    real_get_proposal = memory.get_proposal

    def fake_get_proposal(proposal_id: str) -> PublicationProposal | None:
        # First call (none happens before checkpoint in _publish), but
        # be safe: only suppress for this proposal's lookup after step 7.
        # We unconditionally return None here — no other lookup happens
        # in the publish path that depends on a non-None result.
        return None

    monkeypatch.setattr(memory, "get_proposal", fake_get_proposal)

    asyncio.run(publisher._publish(proposal))

    # Restore so we can verify state.
    monkeypatch.setattr(memory, "get_proposal", real_get_proposal)
    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    # Checkpoint persisted before the silent return.
    assert refreshed.status == "executing"
    assert refreshed.published_url == "https://github.com/thomas-sodon/demo-repo"
    host.create_public_repo.assert_called_once()
    # No git push call ran — vanished branch returned before _push_and_finalize.
    push_calls = [
        c for c in runner.call_args_list
        if c.args[0][:2] == ["git", "push"]
    ]
    assert push_calls == []


# ---------------------------------------------------------------------------
# _publish — Step 7 TOCTOU symmetry
# ---------------------------------------------------------------------------


def test_publish_step7_passes_expect_status_executing(
    paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 7's durable checkpoint must pass ``expect_status='executing'``
    so a cross-surface race between Step 1 and Step 7 is caught — the
    positive guardrail mirroring the symmetric Step 1, ``_fail``, and
    ``_overdue_publishing_failed`` sites."""
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text("print('hi')\n", encoding="utf-8")
    host = _make_host(create_url="https://github.com/thomas-sodon/demo-repo")
    publisher, memory, _ = _make_publisher(
        memory_dir, workspace, host=host,
    )
    _seed_task(memory, "t001", artifacts)
    proposal = _seed_proposal(memory, publishable_files=["main.py"])

    calls: list[tuple[str, str, dict[str, Any]]] = []
    real_update = memory.update_proposal_status

    def spy_update(
        proposal_id: str, status: str, **kwargs: Any,
    ) -> bool:
        calls.append((proposal_id, status, dict(kwargs)))
        return real_update(proposal_id, status, **kwargs)

    monkeypatch.setattr(memory, "update_proposal_status", spy_update)

    asyncio.run(publisher._publish(proposal))

    # Call order: Step 1 (executing, expect=approved), Step 7
    # (executing, expect=executing, +published_url), push-finalize
    # (published). Anything additional is downstream of Step 7.
    assert len(calls) >= 3
    assert calls[0][1] == "executing"
    assert calls[0][2].get("expect_status") == "approved"
    step7 = calls[1]
    assert step7[0] == proposal.proposal_id
    assert step7[1] == "executing"
    assert step7[2].get("expect_status") == "executing"
    assert step7[2].get("published_url") == "https://github.com/thomas-sodon/demo-repo"
    assert isinstance(step7[2].get("scrub_findings"), list)


def test_publish_step7_aborts_when_proposal_no_longer_executing(
    paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A racing surface (Telegram / dashboard) flipping the row out of
    ``executing`` between Step 1 and Step 7 must lose its checkpoint
    write and skip the push — the row keeps the racing-write status and
    ``published_url`` stays unset."""
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text("print('hi')\n", encoding="utf-8")
    host = _make_host(create_url="https://github.com/thomas-sodon/demo-repo")
    publisher, memory, runner = _make_publisher(
        memory_dir, workspace, host=host,
    )
    _seed_task(memory, "t001", artifacts)
    proposal = _seed_proposal(memory, publishable_files=["main.py"])

    set_remote = MagicMock()
    push = MagicMock()
    monkeypatch.setattr(publisher, "_set_origin_remote", set_remote)
    monkeypatch.setattr(publisher, "_git_push", push)

    real_update = memory.update_proposal_status

    def wrapping_update(
        proposal_id: str, status: str, **kwargs: Any,
    ) -> bool:
        # Just before Step 7's expect_status="executing" recheck runs
        # inside the file_lock, a racing surface flips the row to
        # "failed". Step 7's recheck then sees the wrong state and
        # returns False.
        if status == "executing" and kwargs.get("expect_status") == "executing":
            real_update(
                proposal_id, "failed",
                error="raced_to_failed_by_racing_surface",
            )
        return real_update(proposal_id, status, **kwargs)

    monkeypatch.setattr(memory, "update_proposal_status", wrapping_update)

    with caplog.at_level("WARNING", logger="maestro.publication"):
        asyncio.run(publisher._publish(proposal))

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    # The racing write owns the row — Step 7 did not stomp it.
    assert refreshed.status == "failed"
    assert refreshed.error == "raced_to_failed_by_racing_surface"
    # Published URL was never persisted on the proposal.
    assert refreshed.published_url is None
    # No push-side work ran on the race-lost branch.
    set_remote.assert_not_called()
    push.assert_not_called()
    push_calls = [
        c for c in runner.call_args_list
        if c.args[0][:2] == ["git", "push"]
    ]
    assert push_calls == []
    # The gh repo was still created upstream of Step 7 — this is the
    # orphan the warning calls out.
    host.create_public_repo.assert_called_once()
    # Warning identifies the proposal at the step-7 race-lost branch.
    matching = [
        record for record in caplog.records
        if proposal.proposal_id in record.getMessage()
        and "no longer executing" in record.getMessage()
    ]
    assert len(matching) == 1


# ---------------------------------------------------------------------------
# _push_and_finalize — Step 10 terminal-transition TOCTOU symmetry
# ---------------------------------------------------------------------------


def test_push_and_finalize_skips_published_when_proposal_no_longer_executing(
    paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A racing surface flipping the row out of ``executing`` between the
    Step 7 checkpoint and a *successful* push must lose the terminal
    ``executing → published`` write — the row keeps the racing status and
    no phantom success notification fires. This closes the last un-gated
    mutation in the pipeline, mirroring the Step 7 checkpoint's guard."""
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text("print('hi')\n", encoding="utf-8")
    host = _make_host(create_url="https://github.com/thomas-sodon/demo-repo")
    publisher, memory, _ = _make_publisher(
        memory_dir, workspace, host=host,
    )
    _seed_task(memory, "t001", artifacts)
    proposal = _seed_proposal(memory, publishable_files=["main.py"])

    # The push succeeds so the pipeline reaches the terminal write.
    set_remote = MagicMock()
    push = MagicMock()
    monkeypatch.setattr(publisher, "_set_origin_remote", set_remote)
    monkeypatch.setattr(publisher, "_git_push", push)

    # Assert no phantom success notification fires on the race-lost branch.
    notify_success = AsyncMock()
    monkeypatch.setattr(publisher, "_notify_success", notify_success)

    real_update = memory.update_proposal_status

    def wrapping_update(
        proposal_id: str, status: str, **kwargs: Any,
    ) -> bool:
        # Just before the terminal ``published`` write runs, a racing
        # surface flips the row to ``failed``. The guarded write's
        # expect_status="executing" recheck then sees the wrong state and
        # returns False, so the row is not stomped back to ``published``.
        if status == "published":
            real_update(
                proposal_id, "failed",
                error="raced_to_failed_by_racing_surface",
            )
        return real_update(proposal_id, status, **kwargs)

    monkeypatch.setattr(memory, "update_proposal_status", wrapping_update)

    with caplog.at_level("WARNING", logger="maestro.publication"):
        asyncio.run(publisher._publish(proposal))

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    # The racing write owns the row — the terminal write did not stomp it.
    assert refreshed.status == "failed"
    assert refreshed.error == "raced_to_failed_by_racing_surface"
    # Push ran (we reached finalize), but no success notification fired.
    set_remote.assert_called_once()
    push.assert_called_once()
    notify_success.assert_not_called()
    # Warning identifies the proposal at the publish-finalize race-lost branch.
    matching = [
        record for record in caplog.records
        if proposal.proposal_id in record.getMessage()
        and "no longer executing at publish finalize" in record.getMessage()
    ]
    assert len(matching) == 1


# ---------------------------------------------------------------------------
# _resume_publish defensive guard
# ---------------------------------------------------------------------------


def test_resume_publish_returns_silently_when_no_published_url(
    paths: tuple[Path, Path, Path],
) -> None:
    """The resume pass already filters by ``published_url``, but the
    inner guard exists so a misuse can't accidentally finalize without
    a URL. Calling ``_resume_publish`` directly with a urlless proposal
    must be a no-op."""
    memory_dir, workspace, _ = paths
    publisher, memory, runner = _make_publisher(memory_dir, workspace)
    proposal = _seed_proposal(
        memory, status="executing",
        executing_started_at="2026-04-25T11:55:00+00:00",
        published_url=None,
    )

    asyncio.run(publisher._resume_publish(proposal))

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    # Untouched — still executing, no remote work attempted.
    assert refreshed.status == "executing"
    runner.assert_not_called()


# ---------------------------------------------------------------------------
# _push_and_finalize — set_origin_remote failure
# ---------------------------------------------------------------------------


def test_push_and_finalize_marks_failed_when_set_origin_remote_fails(
    paths: tuple[Path, Path, Path],
) -> None:
    """If ``git remote add`` reports the remote already exists and the
    ``git remote set-url`` fallback then also fails after the public repo
    is already created, mark failed with
    ``repo_created_but_push_failed:set_remote:...`` AND keep
    ``published_url`` so Thomas can clean up the orphan.

    The ``already exists`` stderr is what drives the fallback into
    ``set-url``; a non-already-exists ``git remote add`` failure would
    short-circuit before ``set-url`` ever runs (covered separately)."""
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text("print('hi')\n", encoding="utf-8")

    def runner_side_effect(cmd: list[str], **_: Any) -> Any:
        if cmd[:3] == ["git", "remote", "add"]:
            raise subprocess.CalledProcessError(
                128, "git", stderr="error: remote origin already exists.",
            )
        if cmd[:3] == ["git", "remote", "set-url"]:
            raise subprocess.CalledProcessError(
                128, "git", stderr="remote config broken",
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    runner = MagicMock(side_effect=runner_side_effect)
    host = _make_host(create_url="https://github.com/thomas-sodon/demo-repo")
    publisher, memory, _ = _make_publisher(
        memory_dir, workspace, host=host, runner=runner,
    )
    _seed_task(memory, "t001", artifacts)
    proposal = _seed_proposal(memory, publishable_files=["main.py"])

    asyncio.run(publisher._publish(proposal))

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error is not None
    assert refreshed.error.startswith("repo_created_but_push_failed:set_remote:")
    # Orphan URL must remain visible.
    assert refreshed.published_url == "https://github.com/thomas-sodon/demo-repo"


# ---------------------------------------------------------------------------
# _run_git — non-CalledProcessError translation
# ---------------------------------------------------------------------------


def test_run_git_translates_timeout_to_publication_failed_error(
    paths: tuple[Path, Path, Path],
) -> None:
    """``subprocess.TimeoutExpired`` and ``OSError`` from the runner
    must surface as ``PublicationFailedError`` so the pipeline's
    blanket ``except PublicationFailedError`` catches them."""
    memory_dir, workspace, _ = paths
    runner = MagicMock(
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=5),
    )
    publisher, _memory, _ = _make_publisher(
        memory_dir, workspace, runner=runner,
    )
    staging = memory_dir / "publications" / "p1"
    staging.mkdir(parents=True)

    with pytest.raises(PublicationFailedError, match="git init"):
        publisher._git_init_commit(staging, "task-42")


def test_run_git_translates_oserror_to_publication_failed_error(
    paths: tuple[Path, Path, Path],
) -> None:
    memory_dir, workspace, _ = paths
    runner = MagicMock(side_effect=OSError("git: command not found"))
    publisher, _memory, _ = _make_publisher(
        memory_dir, workspace, runner=runner,
    )
    staging = memory_dir / "publications" / "p1"
    staging.mkdir(parents=True)

    with pytest.raises(PublicationFailedError, match="git init"):
        publisher._git_init_commit(staging, "task-42")


def test_run_git_passes_default_timeout_to_runner(
    paths: tuple[Path, Path, Path],
) -> None:
    """Every Publisher subprocess call must carry an explicit timeout.

    Without this a hung ``git`` (network partition, ``gh`` waiting on
    stdin auth) would block past Docker's SIGTERM grace period and leave
    an orphan child after SIGKILL.  Regression for the audit finding
    that ``Publisher._run_git`` previously omitted ``timeout=``.
    """
    memory_dir, workspace, _ = paths
    runner = MagicMock(
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )
    publisher, _memory, _ = _make_publisher(
        memory_dir, workspace, runner=runner,
    )
    staging = memory_dir / "publications" / "p1"
    staging.mkdir(parents=True)

    publisher._git_init_commit(staging, "task-42")

    assert runner.call_args_list, "expected runner to be called"
    for call in runner.call_args_list:
        timeout = call.kwargs.get("timeout")
        assert isinstance(timeout, int) and timeout > 0, (
            f"missing/invalid timeout on {call.args[0]!r}: {timeout!r}"
        )


def test_git_push_uses_longer_push_timeout(
    paths: tuple[Path, Path, Path],
) -> None:
    """``git push`` is the slowest network step — it gets the longer
    push timeout, not the default git timeout."""
    memory_dir, workspace, _ = paths
    runner = MagicMock(
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )
    publisher, _memory, _ = _make_publisher(
        memory_dir, workspace, runner=runner,
    )
    staging = memory_dir / "publications" / "p1"
    staging.mkdir(parents=True)

    publisher._git_push(staging)

    assert len(runner.call_args_list) == 1
    push_call = runner.call_args_list[0]
    assert push_call.args[0][:2] == ["git", "push"]
    assert push_call.kwargs["timeout"] >= 120


# ---------------------------------------------------------------------------
# _notify_success / _notify_failure — best-effort exception swallowing
# ---------------------------------------------------------------------------


def test_publish_swallows_journal_failure_in_notify_success(
    paths: tuple[Path, Path, Path],
) -> None:
    """Journal record_event raising must not flip a successful publish
    into failure — telemetry is best-effort."""
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text("print('hi')\n", encoding="utf-8")
    host = _make_host(create_url="https://github.com/thomas-sodon/demo-repo")
    publisher, memory, _ = _make_publisher(
        memory_dir, workspace, host=host,
    )
    publisher._journal.record_event = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("journal disk full"),
    )
    _seed_task(memory, "t001", artifacts)
    proposal = _seed_proposal(memory, publishable_files=["main.py"])

    asyncio.run(publisher._publish(proposal))

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "published"


def test_publish_swallows_notifier_failure_in_notify_success(
    paths: tuple[Path, Path, Path],
) -> None:
    """Notifier raising in the success branch must not undo the
    ``published`` status flip."""
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text("print('hi')\n", encoding="utf-8")
    notifier = MagicMock()
    notifier.notify = AsyncMock(side_effect=RuntimeError("telegram down"))
    host = _make_host(create_url="https://github.com/thomas-sodon/demo-repo")
    publisher, memory, _ = _make_publisher(
        memory_dir, workspace, host=host, notifier=notifier,
    )
    _seed_task(memory, "t001", artifacts)
    proposal = _seed_proposal(memory, publishable_files=["main.py"])

    asyncio.run(publisher._publish(proposal))

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "published"


def test_fail_swallows_notifier_failure_in_notify_failure(
    paths: tuple[Path, Path, Path],
) -> None:
    """Notifier raising during the failure path must not surface — the
    proposal still ends up in ``failed`` status."""
    memory_dir, workspace, _ = paths
    notifier = MagicMock()
    notifier.notify = AsyncMock(side_effect=RuntimeError("telegram down"))
    host = _make_host()
    publisher, memory, _ = _make_publisher(
        memory_dir, workspace, host=host, notifier=notifier,
    )
    proposal = _seed_proposal(memory)

    # No task seeded — guarantees the artifacts_missing branch fires
    # so _fail → _notify_failure runs.
    asyncio.run(publisher._publish(proposal))

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error == "artifacts_missing"


# ---------------------------------------------------------------------------
# _publish — stale staging directory cleanup (#1580)
# ---------------------------------------------------------------------------


def test_publish_cleans_stale_staging_dir(
    paths: tuple[Path, Path, Path],
) -> None:
    """A leftover staging directory from a prior failed publish must be
    removed before re-staging so ``git add -A`` cannot pick up stale
    unscanned files."""
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text("print('hi')\n", encoding="utf-8")
    host = _make_host(create_url="https://github.com/thomas-sodon/demo-repo")
    publisher, memory, _ = _make_publisher(
        memory_dir, workspace, host=host,
    )
    _seed_task(memory, "t001", artifacts)
    proposal = _seed_proposal(memory, publishable_files=["main.py"])

    # Pre-seed a staging directory with a stale file that is NOT in
    # publishable_files — simulates a prior failed attempt that left
    # behind debris.
    staging = memory_dir / "publications" / proposal.proposal_id
    staging.mkdir(parents=True)
    stale_file = staging / "leaked_secret.txt"
    stale_file.write_text("AKIA...", encoding="utf-8")

    asyncio.run(publisher._publish(proposal))

    # The stale file must be gone — rmtree fires before re-staging.
    assert not stale_file.exists()
    # The wanted file was staged fresh.
    assert (staging / "main.py").exists()
    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "published"


# ---------------------------------------------------------------------------
# _publish_one_approved — FIFO sort for undated proposals (#1576)
# ---------------------------------------------------------------------------


def test_publish_one_approved_sorts_undated_last(
    paths: tuple[Path, Path, Path],
) -> None:
    """A legacy proposal with no ``reviewed_at`` and no ``created_at``
    must sort *after* properly-timestamped proposals — the
    ``_FIFO_SORT_SENTINEL`` far-future fallback ensures this."""
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text("print('hi')\n", encoding="utf-8")
    host = _make_host(create_url="https://github.com/thomas-sodon/demo-repo")
    publisher, memory, _ = _make_publisher(
        memory_dir, workspace, host=host,
    )
    _seed_task(memory, "t001", artifacts)

    # Undated legacy proposal — should sort last.  Both timestamps
    # must be None so the sentinel fallback fires.
    _seed_proposal(
        memory,
        proposal_id="p_undated",
        task_id="t001",
        created_at=None,  # type: ignore[arg-type]
        reviewed_at=None,  # type: ignore[arg-type]
        publishable_files=["main.py"],
    )
    # Properly-dated proposal — should sort first (earlier timestamp).
    _seed_proposal(
        memory,
        proposal_id="p_dated",
        task_id="t001",
        reviewed_at="2026-04-24T02:00:00+00:00",
        publishable_files=["main.py"],
    )

    # Run the FIFO picker — it should choose p_dated first.
    asyncio.run(publisher._publish_one_approved())

    # p_dated was picked (now executing or published), p_undated untouched.
    dated = memory.get_proposal("p_dated")
    undated = memory.get_proposal("p_undated")
    assert dated is not None
    assert undated is not None
    assert dated.status != "approved"  # was picked and processed
    assert undated.status == "approved"  # still waiting

    # Verify the sentinel constant is well-formed — it must sort after
    # any real ISO-8601 timestamp.
    assert _FIFO_SORT_SENTINEL > "2099-12-31T23:59:59+00:00"


# ---------------------------------------------------------------------------
# tick() — shutdown event gates fresh publish work
# ---------------------------------------------------------------------------


def test_tick_skips_publish_when_shutdown_set_before_call(
    paths: tuple[Path, Path, Path],
) -> None:
    """A shutdown event set before ``tick()`` runs must short-circuit
    immediately — no fresh ``gh repo create`` once SIGTERM has fired,
    or Docker's grace period may not be enough to finish."""
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text("print('hi')\n", encoding="utf-8")
    publisher, memory, _ = _make_publisher(memory_dir, workspace)
    _seed_task(memory, "t001", artifacts)
    _seed_proposal(memory, publishable_files=["main.py"])

    publisher._shutdown_event = asyncio.Event()
    publisher._shutdown_event.set()
    publisher._publish_one_approved = AsyncMock()  # type: ignore[method-assign]
    publisher._reconcile_stale_executing = AsyncMock()  # type: ignore[method-assign]
    publisher._resume_executing_after_checkpoint = AsyncMock()  # type: ignore[method-assign]

    asyncio.run(publisher.tick())

    publisher._reconcile_stale_executing.assert_not_called()
    publisher._resume_executing_after_checkpoint.assert_not_called()
    publisher._publish_one_approved.assert_not_called()


def test_tick_skips_resume_and_publish_when_shutdown_fires_during_reconcile(
    paths: tuple[Path, Path, Path],
) -> None:
    """If shutdown fires during the reconcile pass, neither resume nor
    publish should start — both gates between passes must check."""
    memory_dir, workspace, _ = paths
    publisher, _memory, _ = _make_publisher(memory_dir, workspace)
    publisher._shutdown_event = asyncio.Event()

    async def set_shutdown_during_reconcile() -> None:
        publisher._shutdown_event.set()  # type: ignore[union-attr]

    publisher._reconcile_stale_executing = AsyncMock(  # type: ignore[method-assign]
        side_effect=set_shutdown_during_reconcile,
    )
    publisher._resume_executing_after_checkpoint = AsyncMock()  # type: ignore[method-assign]
    publisher._publish_one_approved = AsyncMock()  # type: ignore[method-assign]

    asyncio.run(publisher.tick())

    publisher._reconcile_stale_executing.assert_called_once()
    publisher._resume_executing_after_checkpoint.assert_not_called()
    publisher._publish_one_approved.assert_not_called()


def test_tick_runs_resume_then_skips_publish_when_shutdown_fires_mid_tick(
    paths: tuple[Path, Path, Path],
) -> None:
    """Shutdown that fires after the resume pass still skips the fresh
    publish — resume is idempotent (safe to interrupt) but a fresh
    ``_publish_one_approved`` is not."""
    memory_dir, workspace, _ = paths
    publisher, _memory, _ = _make_publisher(memory_dir, workspace)
    publisher._shutdown_event = asyncio.Event()

    publisher._reconcile_stale_executing = AsyncMock()  # type: ignore[method-assign]

    async def set_shutdown_during_resume() -> None:
        publisher._shutdown_event.set()  # type: ignore[union-attr]

    publisher._resume_executing_after_checkpoint = AsyncMock(  # type: ignore[method-assign]
        side_effect=set_shutdown_during_resume,
    )
    publisher._publish_one_approved = AsyncMock()  # type: ignore[method-assign]

    asyncio.run(publisher.tick())

    publisher._reconcile_stale_executing.assert_called_once()
    publisher._resume_executing_after_checkpoint.assert_called_once()
    publisher._publish_one_approved.assert_not_called()


def test_tick_runs_all_passes_when_shutdown_event_unset(
    paths: tuple[Path, Path, Path],
) -> None:
    """Default behaviour: with no shutdown event (or one that's never
    set), all three passes run as before."""
    memory_dir, workspace, _ = paths
    publisher, _memory, _ = _make_publisher(memory_dir, workspace)
    publisher._shutdown_event = None

    publisher._reconcile_stale_executing = AsyncMock()  # type: ignore[method-assign]
    publisher._resume_executing_after_checkpoint = AsyncMock()  # type: ignore[method-assign]
    publisher._publish_one_approved = AsyncMock()  # type: ignore[method-assign]

    asyncio.run(publisher.tick())

    publisher._reconcile_stale_executing.assert_called_once()
    publisher._resume_executing_after_checkpoint.assert_called_once()
    publisher._publish_one_approved.assert_called_once()


# ---------------------------------------------------------------------------
# _fail / reconcile — staging dir cleanup
# ---------------------------------------------------------------------------


def test_fail_with_clear_published_url_wipes_staging_dir(
    paths: tuple[Path, Path, Path],
) -> None:
    """Pre-create failures clean the staging tree so failed proposals
    don't leave orphans under ``<memory_dir>/publications/``."""
    memory_dir, workspace, _ = paths
    publisher, memory, _ = _make_publisher(memory_dir, workspace)
    _seed_proposal(memory, status="executing")

    staging = publisher._staging_dir_for("p001")
    staging.mkdir(parents=True)
    (staging / "leftover.txt").write_text("x", encoding="utf-8")

    asyncio.run(publisher._fail("p001", "boom", "log"))

    assert not staging.exists()


def test_fail_post_create_preserves_staging_dir(
    paths: tuple[Path, Path, Path],
) -> None:
    """Post-create failures (clear_published_url=False) preserve staging
    so a manual recovery push from the checkpointed URL is still
    possible."""
    memory_dir, workspace, _ = paths
    publisher, memory, _ = _make_publisher(memory_dir, workspace)
    _seed_proposal(
        memory,
        status="executing",
        published_url="https://github.com/x/y",
    )

    staging = publisher._staging_dir_for("p001")
    staging.mkdir(parents=True)
    (staging / "needed.txt").write_text("x", encoding="utf-8")

    asyncio.run(publisher._fail(
        "p001", "boom", "log", clear_published_url=False,
    ))

    assert staging.exists()
    assert (staging / "needed.txt").exists()


def test_reconcile_stale_executing_wipes_staging_dir(
    paths: tuple[Path, Path, Path],
) -> None:
    """Stale pre-create rows (no published_url) lose their staging tree
    when reconciled to ``failed`` so the orphan doesn't accumulate."""
    memory_dir, workspace, _ = paths
    publisher, memory, _ = _make_publisher(memory_dir, workspace)
    _seed_proposal(memory, status="executing", executing_started_at=None)

    staging = publisher._staging_dir_for("p001")
    staging.mkdir(parents=True)
    (staging / "leftover.txt").write_text("x", encoding="utf-8")

    asyncio.run(publisher._reconcile_stale_executing())

    assert not staging.exists()


# ---------------------------------------------------------------------------
# _publish — resolve_publication called after status flip (#1515)
# ---------------------------------------------------------------------------


def test_publish_resolves_telegram_button_after_status_flip(
    paths: tuple[Path, Path, Path],
) -> None:
    """After the approved→executing flip, the loop must ask the notifier
    to edit any pending Telegram inline-button message, so a user
    cannot later tap a stale Approve/Reject button on a row already
    resolved via the dashboard."""
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text("print('hi')\n", encoding="utf-8")
    notifier = _make_notifier()
    publisher, memory, _ = _make_publisher(
        memory_dir, workspace, notifier=notifier,
    )
    _seed_task(memory, "t001", artifacts)
    proposal = _seed_proposal(memory, publishable_files=["main.py"])

    asyncio.run(publisher._publish(proposal))

    notifier.resolve_publication.assert_awaited_once_with(
        proposal.proposal_id, "Approved",
    )
    # The resolve call must happen AFTER the approved→executing flip,
    # not before it — otherwise a proposal that loses the TOCTOU race
    # would get its Telegram message edited prematurely.
    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "published"


def test_publish_continues_when_resolve_publication_raises(
    paths: tuple[Path, Path, Path],
) -> None:
    """A Telegram bot-API failure during the cleanup call must not
    abort the publish pipeline — the cleanup is best-effort UI
    hygiene, not a publish step."""
    memory_dir, workspace, artifacts = paths
    (artifacts / "main.py").write_text("print('hi')\n", encoding="utf-8")
    notifier = _make_notifier()
    notifier.resolve_publication = AsyncMock(side_effect=RuntimeError("bot down"))
    publisher, memory, _ = _make_publisher(
        memory_dir, workspace, notifier=notifier,
    )
    _seed_task(memory, "t001", artifacts)
    proposal = _seed_proposal(memory, publishable_files=["main.py"])

    asyncio.run(publisher._publish(proposal))

    refreshed = memory.get_proposal(proposal.proposal_id)
    assert refreshed is not None
    assert refreshed.status == "published"
    notifier.resolve_publication.assert_awaited_once()


def test_publish_skips_resolve_when_status_flip_loses_race(
    paths: tuple[Path, Path, Path],
) -> None:
    """A proposal that lost the approved→executing race must not get
    its Telegram button edited — it was not committed to publishing."""
    memory_dir, workspace, _ = paths
    notifier = _make_notifier()
    publisher, memory, _ = _make_publisher(
        memory_dir, workspace, notifier=notifier,
    )
    # Seed a rejected proposal so step-1 expect_status="approved" fails.
    proposal = _seed_proposal(memory, status="rejected")

    asyncio.run(publisher._publish(proposal))

    notifier.resolve_publication.assert_not_called()


# ---------------------------------------------------------------------------
# tick() — Pass C resolves dashboard-rejected Telegram buttons (#1515)
# ---------------------------------------------------------------------------


def test_tick_resolves_rejected_publications(
    paths: tuple[Path, Path, Path],
) -> None:
    """Each tick edits any pending Telegram inline-button message for
    proposals rejected via the dashboard. Multiple rejected rows are
    all processed in one pass."""
    memory_dir, workspace, _ = paths
    notifier = _make_notifier()
    publisher, memory, _ = _make_publisher(
        memory_dir, workspace, notifier=notifier,
    )
    _seed_proposal(memory, proposal_id="p_rej_a", status="rejected")
    _seed_proposal(memory, proposal_id="p_rej_b", status="rejected")

    asyncio.run(publisher._resolve_rejected_publications())

    assert notifier.resolve_publication.await_count == 2
    notifier.resolve_publication.assert_any_await("p_rej_a", "Rejected")
    notifier.resolve_publication.assert_any_await("p_rej_b", "Rejected")


def test_resolve_rejected_publications_skips_non_rejected(
    paths: tuple[Path, Path, Path],
) -> None:
    """The pass only touches ``rejected`` proposals — approved,
    executing, published, and failed rows are left alone (the
    Telegram-initiated path is already self-clearing, and other
    statuses have their own surfaces)."""
    memory_dir, workspace, _ = paths
    notifier = _make_notifier()
    publisher, memory, _ = _make_publisher(
        memory_dir, workspace, notifier=notifier,
    )
    _seed_proposal(memory, proposal_id="p_app", status="approved")
    _seed_proposal(memory, proposal_id="p_pub", status="published")
    _seed_proposal(memory, proposal_id="p_fail", status="failed")
    _seed_proposal(memory, proposal_id="p_rej", status="rejected")

    asyncio.run(publisher._resolve_rejected_publications())

    notifier.resolve_publication.assert_awaited_once_with("p_rej", "Rejected")


def test_resolve_rejected_publications_continues_when_notifier_raises(
    paths: tuple[Path, Path, Path],
) -> None:
    """A notifier raise on one proposal must not block the rest — the
    next rejected row in the pass still gets a cleanup attempt."""
    memory_dir, workspace, _ = paths
    notifier = _make_notifier()
    call_log: list[str] = []

    async def resolve_side_effect(proposal_id: str, label: str) -> None:
        call_log.append(proposal_id)
        if proposal_id == "p_rej_a":
            raise RuntimeError("bot down")

    notifier.resolve_publication = AsyncMock(side_effect=resolve_side_effect)
    publisher, memory, _ = _make_publisher(
        memory_dir, workspace, notifier=notifier,
    )
    _seed_proposal(memory, proposal_id="p_rej_a", status="rejected")
    _seed_proposal(memory, proposal_id="p_rej_b", status="rejected")

    asyncio.run(publisher._resolve_rejected_publications())

    assert "p_rej_a" in call_log
    assert "p_rej_b" in call_log


def test_tick_runs_resolve_rejected_pass_before_fresh_publish(
    paths: tuple[Path, Path, Path],
) -> None:
    """``tick()`` wires the resolve-rejected pass between resume and
    fresh-publish, so the new pass runs unconditionally when no
    shutdown is in flight."""
    memory_dir, workspace, _ = paths
    publisher, _memory, _ = _make_publisher(memory_dir, workspace)
    publisher._shutdown_event = None

    publisher._reconcile_stale_executing = AsyncMock()  # type: ignore[method-assign]
    publisher._resume_executing_after_checkpoint = AsyncMock()  # type: ignore[method-assign]
    publisher._resolve_rejected_publications = AsyncMock()  # type: ignore[method-assign]
    publisher._publish_one_approved = AsyncMock()  # type: ignore[method-assign]

    asyncio.run(publisher.tick())

    publisher._reconcile_stale_executing.assert_called_once()
    publisher._resume_executing_after_checkpoint.assert_called_once()
    publisher._resolve_rejected_publications.assert_called_once()
    publisher._publish_one_approved.assert_called_once()


def test_tick_skips_fresh_publish_when_shutdown_fires_during_resolve_rejected(
    paths: tuple[Path, Path, Path],
) -> None:
    """Shutdown that fires during the resolve-rejected pass must skip
    the fresh publish — the gate between Pass C and Pass D enforces
    the same SIGTERM-no-fresh-create contract as the earlier gates."""
    memory_dir, workspace, _ = paths
    publisher, _memory, _ = _make_publisher(memory_dir, workspace)
    publisher._shutdown_event = asyncio.Event()

    publisher._reconcile_stale_executing = AsyncMock()  # type: ignore[method-assign]
    publisher._resume_executing_after_checkpoint = AsyncMock()  # type: ignore[method-assign]

    async def set_shutdown_during_resolve_rejected() -> None:
        publisher._shutdown_event.set()  # type: ignore[union-attr]

    publisher._resolve_rejected_publications = AsyncMock(  # type: ignore[method-assign]
        side_effect=set_shutdown_during_resolve_rejected,
    )
    publisher._publish_one_approved = AsyncMock()  # type: ignore[method-assign]

    asyncio.run(publisher.tick())

    publisher._reconcile_stale_executing.assert_called_once()
    publisher._resume_executing_after_checkpoint.assert_called_once()
    publisher._resolve_rejected_publications.assert_called_once()
    publisher._publish_one_approved.assert_not_called()


def test_tick_skips_resolve_rejected_pass_when_shutdown_fires_during_resume(
    paths: tuple[Path, Path, Path],
) -> None:
    """If shutdown fires during the resume pass, the new resolve-rejected
    pass (and the fresh publish that follows) must both skip — the
    shutdown gate between passes is checked before this pass runs."""
    memory_dir, workspace, _ = paths
    publisher, _memory, _ = _make_publisher(memory_dir, workspace)
    publisher._shutdown_event = asyncio.Event()

    publisher._reconcile_stale_executing = AsyncMock()  # type: ignore[method-assign]

    async def set_shutdown_during_resume() -> None:
        publisher._shutdown_event.set()  # type: ignore[union-attr]

    publisher._resume_executing_after_checkpoint = AsyncMock(  # type: ignore[method-assign]
        side_effect=set_shutdown_during_resume,
    )
    publisher._resolve_rejected_publications = AsyncMock()  # type: ignore[method-assign]
    publisher._publish_one_approved = AsyncMock()  # type: ignore[method-assign]

    asyncio.run(publisher.tick())

    publisher._reconcile_stale_executing.assert_called_once()
    publisher._resume_executing_after_checkpoint.assert_called_once()
    publisher._resolve_rejected_publications.assert_not_called()
    publisher._publish_one_approved.assert_not_called()


# ---------------------------------------------------------------------------
# write_publication_proposal — duplicate scan tolerates non-dict rows
# ---------------------------------------------------------------------------


def test_write_publication_proposal_duplicate_check_skips_non_dict_lines(
    mem_fs: dict[str, str],
) -> None:
    """write_publication_proposal's duplicate scan skips non-dict JSONL rows.

    ``publication_proposals.jsonl``'s reader ``_load_all_proposals`` already
    tolerates valid-JSON-but-non-dict rows via its ``AttributeError`` catch, so
    the writer's hand-rolled scan must inherit that tolerance too: a ``"just a
    string"`` / ``42`` / ``null`` / ``[1, 2, 3]`` row is skipped (not crashed on
    with ``AttributeError``), while a genuine duplicate ``proposal_id`` sitting
    *after* such rows still raises — proving the guard ``continue``s the scan
    rather than short-circuiting it.  Uses ``mem_fs`` (not ``tmp_path``): this
    exercises only the ``Memory`` writer's ``file_lock``/``open`` primitives,
    not the real-directory publication pipeline the rest of this module drives.
    """
    memory = Memory(Path("/fake/mem"))
    proposals_path = str(Path("/fake/mem") / "publication_proposals.jsonl")
    valid_existing = json.dumps({
        "proposal_id": "existing-prop", "task_id": "t1", "review_id": "r1",
        "proposed_name": "demo", "description": "d", "proposed_readme": "# demo",
        "publishable_files": ["main.py"], "status": "pending_approval",
    })
    non_dict_rows = "\n".join(['"just a string"', "42", "null", "[1, 2, 3]"])
    mem_fs[proposals_path] = non_dict_rows + "\n" + valid_existing + "\n"

    new_proposal = PublicationProposal(
        proposal_id="new-prop", task_id="t2", review_id="r2",
        proposed_name="new", description="d", proposed_readme="# new",
        publishable_files=["main.py"],
    )
    memory.write_publication_proposal(new_proposal)
    assert any(
        p.proposal_id == "new-prop" for p in memory.get_publication_proposals()
    )

    # A duplicate placed after the non-dict rows still raises — the guard skips
    # the bad rows and keeps scanning, it does not abandon the scan.
    duplicate = PublicationProposal(
        proposal_id="existing-prop", task_id="t3", review_id="r3",
        proposed_name="dup", description="d", proposed_readme="# dup",
        publishable_files=["main.py"],
    )
    with pytest.raises(DuplicateProposalError, match="existing-prop"):
        memory.write_publication_proposal(duplicate)


# ---------------------------------------------------------------------------
# FUNDING.yml is never published
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel", [".github/FUNDING.yml", ".github/funding.yml", "./.GITHUB/Funding.YML"],
)
def test_stage_skips_artifact_supplied_funding_yml(
    paths: tuple[Path, Path, Path], rel: str,
) -> None:
    memory_dir, workspace, artifacts = paths
    (artifacts / ".github").mkdir()
    (artifacts / ".github" / "FUNDING.yml").write_text(
        "github: attacker\n", encoding="utf-8",
    )
    (artifacts / "data.txt").write_text("hello", encoding="utf-8")
    publisher, memory, _ = _make_publisher(memory_dir, workspace)
    proposal = _seed_proposal(memory, publishable_files=["data.txt", rel])
    staging = memory_dir / "publications" / proposal.proposal_id
    staging.mkdir(parents=True)

    publisher._stage(proposal, artifacts, staging)

    assert not (staging / ".github").exists()
    assert (staging / "data.txt").exists()


@pytest.mark.parametrize(
    "rel", ["FUNDING.yml", "funding.yml", "docs/FUNDING.yml", "./Docs/Funding.YML"],
)
def test_stage_skips_root_and_docs_funding_yml(
    paths: tuple[Path, Path, Path], rel: str,
) -> None:
    memory_dir, workspace, artifacts = paths
    (artifacts / "docs").mkdir()
    (artifacts / "FUNDING.yml").write_text("github: attacker\n", encoding="utf-8")
    (artifacts / "docs" / "FUNDING.yml").write_text(
        "github: attacker\n", encoding="utf-8",
    )
    (artifacts / "data.txt").write_text("hello", encoding="utf-8")
    publisher, memory, _ = _make_publisher(memory_dir, workspace)
    proposal = _seed_proposal(memory, publishable_files=["data.txt", rel])
    staging = memory_dir / "publications" / proposal.proposal_id
    staging.mkdir(parents=True)

    publisher._stage(proposal, artifacts, staging)

    assert not (staging / "FUNDING.yml").exists()
    assert not (staging / "docs").exists()
    assert (staging / "data.txt").exists()
