"""Tests for ``audit_checks_phantom._git_pr_merged_at_default`` — the timed merge oracle.

Patches ``subprocess.run`` with realistic ``%H\\0%cI\\0%s`` stdout so the
subject-only discriminator and the committer-time parse are both exercised.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from maestro import audit_checks_phantom


def _timed_stdout(*rows: tuple[str, str]) -> str:
    """Build ``%H\\0%cI\\0%s``-formatted ``git log`` stdout."""
    return "".join(
        f"deadbeef{i}\x00{when}\x00{subject}\n"
        for i, (when, subject) in enumerate(rows)
    )


def _run_merged_at(stdout: str) -> datetime | None:
    with patch(
        "maestro.audit_checks_phantom.subprocess.run",
        return_value=SimpleNamespace(stdout=stdout, returncode=0),
    ):
        return audit_checks_phantom._git_pr_merged_at_default(2564)


def test_git_pr_merged_at_default_merge_subject_returns_time() -> None:
    merged_at = _run_merged_at(_timed_stdout(
        ("2026-09-01T12:00:00+02:00", "Merge pull request #2564 from x/y"),
    ))
    assert merged_at == datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def test_git_pr_merged_at_default_squash_subject_returns_time() -> None:
    merged_at = _run_merged_at(_timed_stdout(
        ("2026-09-01T10:00:00+00:00", "Fix the thing (#2564)"),
    ))
    assert merged_at == datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def test_git_pr_merged_at_default_body_only_mention_is_none() -> None:
    merged_at = _run_merged_at(_timed_stdout(
        ("2026-09-01T10:00:00+00:00", "maestro: cite #2564 as evidence"),
    ))
    assert merged_at is None


def test_git_pr_merged_at_default_skips_malformed_line() -> None:
    stdout = "deadbeef\x00Merge pull request #2564 from x/y\n" + _timed_stdout(
        ("2026-09-01T10:00:00+00:00", "Fix the thing (#2564)"),
    )
    assert _run_merged_at(stdout) == datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def test_git_pr_merged_at_default_unparseable_time_is_none() -> None:
    merged_at = _run_merged_at(_timed_stdout(
        ("garbage", "Merge pull request #2564 from x/y"),
    ))
    assert merged_at is None


def test_git_pr_merged_at_default_subprocess_failure_is_none() -> None:
    with patch(
        "maestro.audit_checks_phantom.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        assert audit_checks_phantom._git_pr_merged_at_default(2564) is None
