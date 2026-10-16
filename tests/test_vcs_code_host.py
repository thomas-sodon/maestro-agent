"""Tests for SubprocessCodeHost (GitHub/code host operations)."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from maestro.ci_diagnosis import CheckRun
from maestro.vcs import (
    _RUN_LOG_MAX_CHARS,
    IssueClosure,
    IssueMeta,
    LabelRemoval,
    PrMergeState,
    RefCheckRuns,
    SubprocessCodeHost,
    VCSError,
    _first_failing_run_id,
)

FAKE_DIR = Path("/fake/workspace")


# =====================================================================
# _run timeout handling
# =====================================================================


def test_subprocess_code_host_run_timeout_zero_honored() -> None:
    """Passing timeout=0 must use 0, not fall back to default timeout."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR, timeout=30)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="",
        )
        host._run(["gh", "pr", "list"], timeout=0)

    assert mock_run.call_args[1]["timeout"] == 0


def test_code_host_run_catches_oserror() -> None:
    """SubprocessCodeHost._run() should wrap OSError in VCSError."""
    host = SubprocessCodeHost("owner/repo", timeout=5)
    with patch("subprocess.run", side_effect=OSError("Permission denied")), \
         pytest.raises(VCSError, match="Permission denied"):
        host._run(["gh", "some", "command"])


def test_code_host_run_error_includes_captured_stderr() -> None:
    """SubprocessCodeHost._run surfaces gh's stderr in the VCSError.

    Without this, a ``gh pr create`` failure with a meaningful error like
    ``GraphQL error: Pull request already exists`` would reach the crash
    report as a bare status code, matching the same diagnostic blind spot
    that masked issue #1314 in the git path.
    """
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    exc = subprocess.CalledProcessError(
        1,
        ["gh", "pr", "create"],
        output="partial gh output before failure",
        stderr="GraphQL error: Pull request already exists",
    )
    with patch("maestro.vcs.subprocess.run", side_effect=exc), \
         pytest.raises(VCSError) as excinfo:
        host._run(["gh", "pr", "create"])

    message = str(excinfo.value)
    assert "exit status 1" in message
    assert "stderr:" in message
    assert "GraphQL error: Pull request already exists" in message
    assert "stdout:" in message
    assert "partial gh output before failure" in message
    assert excinfo.value.__cause__ is exc


def test_code_host_cwd_passed() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="https://github.com/o/r/pull/1\n",
        )
        host.create_pr("T", "B")

    assert mock_run.call_args[1]["cwd"] == str(FAKE_DIR)


# =====================================================================
# create_pr
# =====================================================================


def test_create_pr() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="https://github.com/owner/repo/pull/99\n",
        )
        url = host.create_pr("Title", "Body")

    assert url == "https://github.com/owner/repo/pull/99"
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "gh"
    assert "--draft" not in cmd
    assert "--repo" in cmd
    assert "owner/repo" in cmd


def test_create_pr_failure() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        with pytest.raises(VCSError):
            host.create_pr("T", "B")


def test_create_pr_nonzero_exit_with_url_returns_url() -> None:
    """A gh pr create that created the PR but failed a follow-up returns the URL (#4553)."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh",
            output="https://github.com/owner/repo/pull/4545\n",
            stderr="pull request update failed: GraphQL: Something went wrong",
        )
        url = host.create_pr("T", "B")
    assert url == "https://github.com/owner/repo/pull/4545"


def test_create_pr_nonzero_exit_with_url_no_repo_returns_url() -> None:
    host = SubprocessCodeHost(workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh", output="https://github.com/x/y/pull/7\n",
        )
        url = host.create_pr("T", "B")
    assert url == "https://github.com/x/y/pull/7"


@pytest.mark.parametrize("stdout", [None, "", "some error text\n"])
def test_create_pr_nonzero_exit_without_url_raises(stdout: str | None) -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh", output=stdout,
        )
        with pytest.raises(VCSError):
            host.create_pr("T", "B")


def test_create_pr_nonzero_exit_foreign_repo_url_raises() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh", output="https://github.com/other/repo/pull/3\n",
        )
        with pytest.raises(VCSError):
            host.create_pr("T", "B")


def test_create_pr_timeout_still_raises() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(
            "gh", 5, output=b"https://github.com/owner/repo/pull/1",
        )
        with pytest.raises(VCSError):
            host.create_pr("T", "B")


def _pr_row(number: int, state: str, head: str = "maestro/evolve/p1") -> dict[str, object]:
    return {
        "number": number, "headRefName": head, "state": state,
        "url": f"https://github.com/owner/repo/pull/{number}",
    }


def _find_pr(rows: object) -> PrMergeState | None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(rows),
        )
        result = host.find_evolution_pr_for_branch("maestro/evolve/p1")
    cmd = mock_run.call_args[0][0]
    assert cmd[cmd.index("--head") + 1] == "maestro/evolve/p1"
    assert cmd[cmd.index("--state") + 1] == "all"
    return result


def test_find_evolution_pr_for_branch_open() -> None:
    result = _find_pr([_pr_row(5, "OPEN")])
    assert result is not None
    assert result.number == 5
    assert result.state == "OPEN"


def test_find_evolution_pr_for_branch_merged_newest_wins() -> None:
    result = _find_pr([_pr_row(3, "MERGED"), _pr_row(9, "MERGED"), _pr_row(12, "CLOSED")])
    assert result is not None
    assert result.number == 9
    assert result.url == "https://github.com/owner/repo/pull/9"


def test_find_evolution_pr_for_branch_closed_ignored() -> None:
    assert _find_pr([_pr_row(4, "CLOSED")]) is None


def test_find_evolution_pr_for_branch_other_head_ignored() -> None:
    assert _find_pr([_pr_row(4, "OPEN", head="maestro/evolve/p10")]) is None


def test_find_evolution_pr_for_branch_empty() -> None:
    assert _find_pr([]) is None


def test_find_evolution_pr_for_branch_gh_failure_raises() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        with pytest.raises(VCSError):
            host.find_evolution_pr_for_branch("maestro/evolve/p1")


def test_find_evolution_pr_for_branch_bad_json_raises() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json",
        )
        with pytest.raises(VCSError):
            host.find_evolution_pr_for_branch("maestro/evolve/p1")


def test_find_evolution_pr_for_branch_no_repo_infers_from_cwd() -> None:
    host = SubprocessCodeHost(workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]",
        )
        assert host.find_evolution_pr_for_branch("maestro/evolve/p1") is None
    cmd = mock_run.call_args[0][0]
    assert "--repo" not in cmd


def test_create_pr_degraded_success_repo_match_is_case_insensitive() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    url = "https://github.com/Owner/Repo/pull/7"
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.CalledProcessError(
            1, "gh", output=url + "\n", stderr="reviewer failed",
        ),
    ):
        assert host.create_pr("T", "B") == url


def test_create_pr_no_repo_args() -> None:
    host = SubprocessCodeHost(workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="https://github.com/x/y/pull/1\n",
        )
        host.create_pr("T", "B")

    cmd = mock_run.call_args[0][0]
    assert "--repo" not in cmd


def test_create_pr_draft_appends_flag() -> None:
    """``draft=True`` adds ``--draft`` to the gh args."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="https://github.com/owner/repo/pull/77\n",
        )
        url = host.create_pr("Title", "Body", draft=True)

    assert url == "https://github.com/owner/repo/pull/77"
    cmd = mock_run.call_args[0][0]
    assert "--draft" in cmd


def test_create_pr_with_reviewer() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR, reviewer="thomas-sodon")
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="https://github.com/owner/repo/pull/99\n",
        )
        host.create_pr("Title", "Body")

    cmd = mock_run.call_args[0][0]
    assert "--reviewer" in cmd
    reviewer_idx = cmd.index("--reviewer")
    assert cmd[reviewer_idx + 1] == "thomas-sodon"


def test_create_pr_without_reviewer() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="https://github.com/owner/repo/pull/99\n",
        )
        host.create_pr("Title", "Body")

    cmd = mock_run.call_args[0][0]
    assert "--reviewer" not in cmd


def test_pr_creation_timeout() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR, pr_creation_timeout=120)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="https://github.com/x/y/pull/1\n",
        )
        host.create_pr("T", "B")

    assert mock_run.call_args[1]["timeout"] == 120


# =====================================================================
# is_pr_merged
# =====================================================================


def test_is_pr_merged_true() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"state": "MERGED"}',
        )
        assert host.is_pr_merged(42) is True

    cmd = mock_run.call_args[0][0]
    assert cmd == ["gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "state"]


def test_is_pr_merged_false() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"state": "OPEN"}',
        )
        assert host.is_pr_merged(42) is False


def test_is_pr_merged_failure() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        assert host.is_pr_merged(42) is False


# =====================================================================
# is_pr_merged_strict
# =====================================================================


def test_is_pr_merged_strict_true() -> None:
    """The same ``gh pr view --json state`` read as the best-effort sibling."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"state": "MERGED"}',
        )
        assert host.is_pr_merged_strict(42) is True

    cmd = mock_run.call_args[0][0]
    assert cmd == ["gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "state"]


def test_is_pr_merged_strict_false() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"state": "OPEN"}',
        )
        assert host.is_pr_merged_strict(42) is False


def test_is_pr_merged_strict_raises_on_gh_failure() -> None:
    """The whole point of the strict variant: a failing ``gh`` propagates.

    Its best-effort sibling swallows this into ``False``, which is what left the
    merge-truth chokepoint's git-subject fallback unreachable (#2954).
    """
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        with pytest.raises(VCSError):
            host.is_pr_merged_strict(42)


def test_is_pr_merged_strict_raises_on_unparseable_payload() -> None:
    """An unparseable host payload propagates too, rather than reading unmerged."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json",
        )
        with pytest.raises(json.JSONDecodeError):
            host.is_pr_merged_strict(42)


# =====================================================================
# has_merged_at
# =====================================================================


def test_has_merged_at_true() -> None:
    """A non-null host ``mergedAt`` timestamp reads as merged (squash-merge signal)."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"mergedAt": "2026-07-01T00:00:00Z"}',
        )
        assert host.has_merged_at(42) is True

    cmd = mock_run.call_args[0][0]
    assert cmd == [
        "gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "mergedAt",
    ]


def test_has_merged_at_false_when_null() -> None:
    """A null ``mergedAt`` reads as not-merged — no false-positive settlement."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"mergedAt": null}',
        )
        assert host.has_merged_at(42) is False


def test_has_merged_at_failure() -> None:
    """A host/parse failure is best-effort — degrades to not-merged."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        assert host.has_merged_at(42) is False


# =====================================================================
# post_pr_review
# =====================================================================


def test_post_pr_review() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    payload = {"event": "COMMENT", "body": "LGTM", "comments": []}
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="",
        )
        host.post_pr_review(10, payload)

    cmd = mock_run.call_args[0][0]
    assert "repos/owner/repo/pulls/10/reviews" in cmd
    assert mock_run.call_args[1]["input"] == json.dumps(payload)


def test_post_pr_review_no_repo() -> None:
    host = SubprocessCodeHost(workspace=FAKE_DIR)
    with pytest.raises(VCSError, match=r"(?i)no repo configured"):
        host.post_pr_review(10, {})


def test_post_pr_review_failure() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        with pytest.raises(VCSError):
            host.post_pr_review(10, {})


def test_post_pr_review_slash_only_repo_raises() -> None:
    host = SubprocessCodeHost(repo="/")
    with pytest.raises(VCSError):
        host.post_pr_review(1, {"body": "test"})


def test_post_pr_review_owner_slash_empty_repo_raises() -> None:
    host = SubprocessCodeHost(repo="owner/")
    with pytest.raises(VCSError):
        host.post_pr_review(1, {"body": "test"})


def test_post_pr_review_empty_slash_repo_raises() -> None:
    host = SubprocessCodeHost(repo="/repo")
    with pytest.raises(VCSError):
        host.post_pr_review(1, {"body": "test"})


def test_post_pr_review_double_slash_repo_raises() -> None:
    host = SubprocessCodeHost(repo="owner//repo")
    with pytest.raises(VCSError):
        host.post_pr_review(1, {"body": "test"})


# =====================================================================
# get_pr_review_threads (GraphQL)
# =====================================================================


def test_get_pr_review_threads() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    graphql_response = json.dumps({
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {
                                "id": "PRRT_1",
                                "isResolved": False,
                                "comments": {
                                    "nodes": [
                                        {"databaseId": 100, "path": "a.py", "body": "Bug here"},
                                    ],
                                },
                            },
                            {
                                "id": "PRRT_2",
                                "isResolved": True,
                                "comments": {
                                    "nodes": [
                                        {"databaseId": 200, "path": "b.py", "body": "Fix this"},
                                        {"databaseId": 201, "path": "b.py", "body": "Resolved via #42"},
                                    ],
                                },
                            },
                        ],
                    },
                },
            },
        },
    })
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=graphql_response,
        )
        threads = host.get_pr_review_threads(10)

    assert len(threads) == 2
    assert threads[0]["id"] == "PRRT_1"
    assert threads[1]["isResolved"] is True
    # Verify GraphQL call
    cmd = mock_run.call_args[0][0]
    assert "graphql" in cmd
    body = json.loads(mock_run.call_args[1]["input"])
    assert "reviewThreads" in body["query"]
    # ``isOutdated`` is requested so the tend phase can skip threads whose
    # flagged code has changed (mirrors maestro_review's processor).
    assert "isOutdated" in body["query"]
    # ``author{login}`` is requested so the tend phase can attribute human
    # threads and exclude other bots' threads.
    assert "author{login}" in body["query"]
    assert body["variables"]["pr"] == 10


def test_get_pr_review_threads_no_repo(caplog: pytest.LogCaptureFixture) -> None:
    """No repo configured stays a silent ``[]`` — not a warning traceback."""
    host = SubprocessCodeHost(workspace=FAKE_DIR)
    with caplog.at_level(logging.WARNING, logger="maestro.vcs"):
        assert host.get_pr_review_threads(10) == []
    assert caplog.records == []


def test_get_pr_review_threads_failure() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        assert host.get_pr_review_threads(10) == []


def test_get_pr_review_threads_bad_json() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json",
        )
        assert host.get_pr_review_threads(10) == []


def test_get_pr_review_threads_trailing_slash_repo() -> None:
    """'owner/' splits into ['owner', ''] — must return []."""
    host = SubprocessCodeHost("owner/", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        result = host.get_pr_review_threads(10)

    assert result == []
    mock_run.assert_not_called()


def test_get_pr_review_threads_leading_slash_repo() -> None:
    """'/repo' splits into ['', 'repo'] — must return []."""
    host = SubprocessCodeHost("/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        result = host.get_pr_review_threads(10)

    assert result == []
    mock_run.assert_not_called()


def test_get_pr_review_threads_repo_no_slash() -> None:
    """Repo string without '/' should not crash — return empty list."""
    host = SubprocessCodeHost(repo="noslash")
    result = host.get_pr_review_threads(1)
    assert result == []


def test_get_pr_review_threads_repo_multiple_slashes() -> None:
    """Repo with extra slashes like 'org/repo/extra' should return empty."""
    host = SubprocessCodeHost(
        "org/repo/extra",
        workspace=Path("/tmp"),
        timeout=5,
    )
    result = host.get_pr_review_threads(1)
    assert result == []


# =====================================================================
# reply_to_review_comment (REST)
# =====================================================================


def test_reply_to_review_comment() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="{}",
        )
        host.reply_to_review_comment(10, 100, "Still broken")

    cmd = mock_run.call_args[0][0]
    assert "repos/owner/repo/pulls/10/comments" in cmd
    body = json.loads(mock_run.call_args[1]["input"])
    assert body["body"] == "Still broken"
    assert body["in_reply_to"] == 100


def test_reply_to_review_comment_failure() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        # Should not raise
        host.reply_to_review_comment(10, 100, "Still broken")


def test_reply_to_review_comment_no_repo() -> None:
    host = SubprocessCodeHost(workspace=FAKE_DIR)
    # Should be a no-op, no exception
    host.reply_to_review_comment(10, 100, "Still broken")


# =====================================================================
# resolve_review_thread (GraphQL)
# =====================================================================


def test_resolve_review_thread() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="{}",
        )
        host.resolve_review_thread("PRRT_1")

    cmd = mock_run.call_args[0][0]
    assert "graphql" in cmd
    body = json.loads(mock_run.call_args[1]["input"])
    assert "resolveReviewThread" in body["query"]
    assert body["variables"]["threadId"] == "PRRT_1"


def test_resolve_review_thread_failure() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        # Should not raise
        host.resolve_review_thread("PRRT_1")


def test_resolve_review_thread_no_repo() -> None:
    host = SubprocessCodeHost(workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        # No configured repo → no-op, no gh call, no exception
        host.resolve_review_thread("PRRT_1")
    mock_run.assert_not_called()


# =====================================================================
# watch_pr_checks
# =====================================================================


# =====================================================================
# get_pr_diff
# =====================================================================


def test_get_pr_diff() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="diff --git a/f.py b/f.py\n+new line\n",
        )
        diff = host.get_pr_diff(10)

    assert "+new line" in diff
    cmd = mock_run.call_args[0][0]
    assert "--repo" in cmd


def test_get_pr_diff_failure() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        diff = host.get_pr_diff(10)

    assert diff == ""


# =====================================================================
# get_pr_review_bodies / get_pr_comment_bodies (proposal 064)
# =====================================================================


def test_get_pr_review_bodies() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    reviews_json = json.dumps({
        "reviews": [
            {"body": "First review", "state": "COMMENTED"},
            {"body": None, "state": "APPROVED"},
            "not-a-dict",
            {"state": "COMMENTED"},
            {"body": "Latest review with overflow"},
        ],
    })
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=reviews_json,
        )
        bodies = host.get_pr_review_bodies(10)

    # Non-dict entries are dropped; null/missing bodies become "".
    assert bodies == ["First review", "", "", "Latest review with overflow"]
    cmd = mock_run.call_args[0][0]
    assert "--json" in cmd
    assert "reviews" in cmd
    assert "10" in cmd


def test_get_pr_review_bodies_null_reviews() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"reviews": None}),
        )
        assert host.get_pr_review_bodies(10) == []


def test_get_pr_review_bodies_failure() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        assert host.get_pr_review_bodies(10) == []


def test_get_pr_review_bodies_bad_json() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json",
        )
        assert host.get_pr_review_bodies(10) == []


def test_get_pr_review_bodies_non_dict_json() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(["a list"]),
        )
        assert host.get_pr_review_bodies(10) == []


def test_get_pr_comment_bodies() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    comments_json = json.dumps({
        "comments": [
            {"body": "<!-- maestro:overflow-ack -->\nof-aaaaaaaa — done"},
            {"body": None},
            "not-a-dict",
        ],
    })
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=comments_json,
        )
        bodies = host.get_pr_comment_bodies(7)

    assert bodies == ["<!-- maestro:overflow-ack -->\nof-aaaaaaaa — done", ""]
    cmd = mock_run.call_args[0][0]
    assert "--json" in cmd
    assert "comments" in cmd
    assert "7" in cmd


def test_get_pr_comment_bodies_failure() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        assert host.get_pr_comment_bodies(7) == []


def test_get_pr_comment_bodies_bad_json() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json",
        )
        assert host.get_pr_comment_bodies(7) == []


def test_get_pr_comment_bodies_non_dict_json() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps([1, 2]),
        )
        assert host.get_pr_comment_bodies(7) == []


# =====================================================================
# get_pr_info
# =====================================================================


def test_get_pr_info() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    info_json = json.dumps({"title": "Fix bug", "body": "Details"})
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=info_json,
        )
        info = host.get_pr_info(10)

    assert info == {"title": "Fix bug", "body": "Details"}


def test_get_pr_info_failure() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        info = host.get_pr_info(10)

    assert info == {"title": "", "body": ""}


def test_get_pr_info_bad_json() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json",
        )
        info = host.get_pr_info(10)

    assert info == {"title": "", "body": ""}


# =====================================================================
# list_issues
# =====================================================================


def test_list_issues() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    raw = [{"number": 1, "title": "Bug", "labels": [], "body": "", "createdAt": ""}]
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(raw),
        )
        result = host.list_issues()

    assert result == raw


def test_list_issues_with_labels() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]",
        )
        host.list_issues(labels=["bug", "p0"])

    cmd = mock_run.call_args[0][0]
    assert cmd.count("--label") == 2
    assert "bug" in cmd
    assert "p0" in cmd


def test_list_issues_failure(caplog: pytest.LogCaptureFixture) -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run, \
         caplog.at_level("WARNING", logger="maestro.vcs"):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh", stderr="auth required",
        )
        result = host.list_issues()

    assert result is None
    assert "auth required" in caplog.text


def test_list_issues_no_repo() -> None:
    host = SubprocessCodeHost(workspace=FAKE_DIR)
    assert host.list_issues() is None


def test_list_issues_bad_json() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json",
        )
        result = host.list_issues()

    assert result is None


def test_list_issues_catches_oserror() -> None:
    """SubprocessCodeHost.list_issues should handle OSError gracefully."""
    host = SubprocessCodeHost("owner/repo", timeout=5)
    with patch("subprocess.run", side_effect=OSError("Permission denied")):
        result = host.list_issues(state="open")
    assert result is None


def test_list_issues_with_page_size() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]",
        )
        host.list_issues(page_size=5)

    cmd = mock_run.call_args[0][0]
    assert "--limit" in cmd
    assert "5" in cmd


def test_list_issues_without_page_size_no_limit() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]",
        )
        host.list_issues()

    cmd = mock_run.call_args[0][0]
    assert "--limit" not in cmd


def test_list_issues_with_exclude_labels() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]",
        )
        host.list_issues(exclude_labels=["crash"])

    cmd = mock_run.call_args[0][0]
    assert "--search" in cmd
    idx = cmd.index("--search")
    assert cmd[idx + 1] == '-label:"crash"'


def test_list_issues_with_multiple_exclude_labels() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]",
        )
        host.list_issues(exclude_labels=["crash", "wontfix"])

    cmd = mock_run.call_args[0][0]
    assert "--search" in cmd
    idx = cmd.index("--search")
    assert cmd[idx + 1] == '-label:"crash" -label:"wontfix"'


def test_list_issues_exclude_label_with_spaces() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]",
        )
        host.list_issues(exclude_labels=["won't fix"])

    cmd = mock_run.call_args[0][0]
    assert "--search" in cmd
    idx = cmd.index("--search")
    assert cmd[idx + 1] == '-label:"won\'t fix"'


def test_list_issues_without_exclude_labels_no_search() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]",
        )
        host.list_issues()

    cmd = mock_run.call_args[0][0]
    assert "--search" not in cmd


# =====================================================================
# create_issue
# =====================================================================


def test_create_issue() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="https://github.com/owner/repo/issues/42\n",
        )
        number = host.create_issue("Title", "Body", labels=["bug"])

    assert number == 42
    cmd = mock_run.call_args[0][0]
    assert "--label" in cmd
    assert "bug" in cmd
    # Body is passed on stdin via --body-file -, not in argv
    assert "--body-file" in cmd
    assert "-" in cmd
    assert "--body" not in cmd
    assert "Body" not in cmd
    assert mock_run.call_args[1]["input"] == "Body"


def test_create_issue_body_via_stdin_for_long_bodies() -> None:
    """Long bodies should be streamed via stdin rather than put on argv."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    long_body = "x" * 5000
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="https://github.com/owner/repo/issues/1\n",
        )
        host.create_issue("T", long_body)

    assert mock_run.call_args[1]["input"] == long_body
    assert long_body not in mock_run.call_args[0][0]


def test_create_issue_no_repo() -> None:
    host = SubprocessCodeHost(workspace=FAKE_DIR)
    assert host.create_issue("T", "B") is None


def test_create_issue_failure_logs_stderr(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """On CalledProcessError, stderr must be surfaced in the warning log."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run, \
         caplog.at_level("WARNING", logger="maestro.vcs"):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh", stderr="label 'needs-review' not found",
        )
        result = host.create_issue("T", "B")

    assert result is None
    assert "label 'needs-review' not found" in caplog.text


def test_create_issue_retries_after_ensuring_labels(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """On first failure with labels, ensure labels exist then retry once."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    labels = ["crash", "bug", "needs-review"]

    success = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout="https://github.com/owner/repo/issues/77\n",
    )
    first_failure = subprocess.CalledProcessError(
        1, "gh", stderr="could not add label: 'needs-review' not found",
    )

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "issue", "create"]:
            if fake_run.create_calls == 0:  # type: ignore[attr-defined]
                fake_run.create_calls += 1  # type: ignore[attr-defined]
                raise first_failure
            fake_run.create_calls += 1  # type: ignore[attr-defined]
            return success
        if cmd[:3] == ["gh", "label", "create"]:
            fake_run.label_calls.append(cmd[3])  # type: ignore[attr-defined]
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="")
        raise AssertionError(f"unexpected command: {cmd}")

    fake_run.create_calls = 0  # type: ignore[attr-defined]
    fake_run.label_calls = []  # type: ignore[attr-defined]

    with patch("maestro.vcs.subprocess.run", side_effect=fake_run), \
         caplog.at_level("WARNING", logger="maestro.vcs"):
        number = host.create_issue("T", "B", labels=labels)

    assert number == 77
    assert fake_run.create_calls == 2  # type: ignore[attr-defined]
    assert fake_run.label_calls == labels  # type: ignore[attr-defined]
    assert "Retrying gh issue create after ensuring labels" in caplog.text


def test_create_issue_no_retry_on_non_label_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A first failure unrelated to labels short-circuits without the retry."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    labels = ["crash", "bug"]

    non_label_failure = subprocess.CalledProcessError(
        1, "gh", stderr="HTTP 503: server temporarily unavailable",
    )

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "issue", "create"]:
            fake_run.create_calls += 1  # type: ignore[attr-defined]
            raise non_label_failure
        if cmd[:3] == ["gh", "label", "create"]:
            fake_run.label_calls.append(cmd[3])  # type: ignore[attr-defined]
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="")
        raise AssertionError(f"unexpected command: {cmd}")

    fake_run.create_calls = 0  # type: ignore[attr-defined]
    fake_run.label_calls = []  # type: ignore[attr-defined]

    with patch("maestro.vcs.subprocess.run", side_effect=fake_run), \
         caplog.at_level("WARNING", logger="maestro.vcs"):
        result = host.create_issue("T", "B", labels=labels)

    # (a) returns None, (b) exactly one create call — no retry,
    # (c) no label-ensure churn on a failure labels can't cure.
    assert result is None
    assert fake_run.create_calls == 1  # type: ignore[attr-defined]
    assert fake_run.label_calls == []  # type: ignore[attr-defined]
    assert "Retrying gh issue create after ensuring labels" not in caplog.text

    # A non-CalledProcessError first failure (timeout) also skips the retry:
    # _run_create_issue reports stderr=None, which the gate treats as
    # not-label-related.
    def fake_run_timeout(
        cmd: list[str], **_: object,
    ) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "issue", "create"]:
            fake_run_timeout.create_calls += 1  # type: ignore[attr-defined]
            raise subprocess.TimeoutExpired("gh", 1)
        if cmd[:3] == ["gh", "label", "create"]:
            fake_run_timeout.label_calls.append(cmd[3])  # type: ignore[attr-defined]
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="")
        raise AssertionError(f"unexpected command: {cmd}")

    fake_run_timeout.create_calls = 0  # type: ignore[attr-defined]
    fake_run_timeout.label_calls = []  # type: ignore[attr-defined]

    with patch("maestro.vcs.subprocess.run", side_effect=fake_run_timeout):
        timeout_result = host.create_issue("T", "B", labels=labels)

    assert timeout_result is None
    assert fake_run_timeout.create_calls == 1  # type: ignore[attr-defined]
    assert fake_run_timeout.label_calls == []  # type: ignore[attr-defined]


def test_create_issue_retries_then_fails_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When both attempts fail, return None and log both stderrs."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)

    # First stderr is label-related so the retry gate fires; both attempts fail.
    first = subprocess.CalledProcessError(
        1, "gh", stderr="could not add label: 'crash' not found",
    )
    second = subprocess.CalledProcessError(1, "gh", stderr="second stderr")

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "issue", "create"]:
            if fake_run.create_calls == 0:  # type: ignore[attr-defined]
                fake_run.create_calls += 1  # type: ignore[attr-defined]
                raise first
            fake_run.create_calls += 1  # type: ignore[attr-defined]
            raise second
        if cmd[:3] == ["gh", "label", "create"]:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="")
        raise AssertionError(f"unexpected command: {cmd}")

    fake_run.create_calls = 0  # type: ignore[attr-defined]

    with patch("maestro.vcs.subprocess.run", side_effect=fake_run), \
         caplog.at_level("WARNING", logger="maestro.vcs"):
        result = host.create_issue("T", "B", labels=["crash"])

    assert result is None
    assert fake_run.create_calls == 2  # type: ignore[attr-defined]
    assert "could not add label: 'crash' not found" in caplog.text
    assert "second stderr" in caplog.text


def test_create_issue_no_retry_without_labels() -> None:
    """Without labels there's nothing to ensure — no retry."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh", stderr="nope")
        result = host.create_issue("T", "B")

    assert result is None
    assert mock_run.call_count == 1


def test_ensure_labels_swallows_label_create_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_ensure_labels must never raise, even if gh label create fails."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if cmd[3] == "fails-called":
            raise subprocess.CalledProcessError(1, "gh", stderr="perm denied")
        if cmd[3] == "fails-timeout":
            raise subprocess.TimeoutExpired("gh", 1)
        if cmd[3] == "fails-oserror":
            raise OSError("no gh binary")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="")

    with patch("maestro.vcs.subprocess.run", side_effect=fake_run), \
         caplog.at_level("WARNING", logger="maestro.vcs"):
        host._ensure_labels(["fails-called", "fails-timeout", "fails-oserror", "ok"])

    assert "fails-called" in caplog.text
    assert "perm denied" in caplog.text
    assert "fails-timeout" in caplog.text
    assert "fails-oserror" in caplog.text


def test_ensure_labels_no_op_when_repo_unset() -> None:
    """_ensure_labels no-ops (never raises, no subprocess) when repo is unset.

    The "never raises" contract must hold even with an unset repo: neither
    the assertion this replaced (enabled under pytest) nor a malformed
    ``--repo`` call under ``python -O`` should occur.
    """
    host = SubprocessCodeHost(None, workspace=FAKE_DIR)

    with patch("maestro.vcs.subprocess.run") as mock_run:
        host._ensure_labels(["anything"])

    mock_run.assert_not_called()


def test_create_issue_unparseable_url() -> None:
    """create_issue returns None when the URL cannot be parsed as an issue number."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="not-a-url\n",
        )
        result = host.create_issue("T", "B")

    assert result is None


def test_create_issue_catches_oserror() -> None:
    """SubprocessCodeHost.create_issue should handle OSError gracefully."""
    host = SubprocessCodeHost("owner/repo", timeout=5)
    with patch("subprocess.run", side_effect=OSError("Permission denied")):
        result = host.create_issue("title", "body")
    assert result is None


def test_create_issue_catches_timeout() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired("gh", 1)
        assert host.create_issue("T", "B") is None


# =====================================================================
# close_issue
# =====================================================================


def test_close_issue() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="",
        )
        host.close_issue(42)

    cmd = mock_run.call_args[0][0]
    assert "close" in cmd
    assert "42" in cmd


def test_close_issue_no_repo() -> None:
    host = SubprocessCodeHost(workspace=FAKE_DIR)
    host.close_issue(42)  # should not raise


def test_close_issue_failure(caplog: pytest.LogCaptureFixture) -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run, \
         caplog.at_level("WARNING", logger="maestro.vcs"):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh", stderr="not found",
        )
        host.close_issue(42)  # should not raise

    mock_run.assert_called_once()
    assert "not found" in caplog.text


def test_close_issue_catches_oserror() -> None:
    """SubprocessCodeHost.close_issue should handle OSError gracefully."""
    host = SubprocessCodeHost("owner/repo", timeout=5)
    with patch("subprocess.run", side_effect=OSError("Permission denied")):
        # Should not raise
        host.close_issue(42)


# =====================================================================
# comment_issue
# =====================================================================


def test_comment_issue() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="",
        )
        assert host.comment_issue(42, "a comment body") is True

    cmd = mock_run.call_args[0][0]
    assert cmd[:3] == ["gh", "issue", "comment"]
    assert "42" in cmd
    # Body is passed on stdin via --body-file -, not in argv
    assert "--body-file" in cmd
    assert "-" in cmd
    assert "a comment body" not in cmd
    assert mock_run.call_args[1]["input"] == "a comment body"


def test_comment_issue_no_repo() -> None:
    host = SubprocessCodeHost(workspace=FAKE_DIR)
    assert host.comment_issue(42, "body") is False


def test_comment_issue_failure(caplog: pytest.LogCaptureFixture) -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run, \
         caplog.at_level("WARNING", logger="maestro.vcs"):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh", stderr="forbidden",
        )
        assert host.comment_issue(42, "body") is False
    assert "forbidden" in caplog.text


def test_comment_issue_catches_oserror() -> None:
    host = SubprocessCodeHost("owner/repo", timeout=5)
    with patch("subprocess.run", side_effect=OSError("denied")):
        assert host.comment_issue(42, "body") is False


def test_comment_issue_timeout() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired("gh", 1)
        assert host.comment_issue(42, "body") is False


# =====================================================================
# is_pr_closed
# =====================================================================


def test_is_pr_closed_true() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"state": "CLOSED"}',
        )
        assert host.is_pr_closed(42) is True

    cmd = mock_run.call_args[0][0]
    assert cmd == ["gh", "pr", "view", "42", "--repo", "owner/repo", "--json", "state"]


def test_is_pr_closed_false_when_merged() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"state": "MERGED"}',
        )
        assert host.is_pr_closed(42) is False


def test_is_pr_closed_false_when_open() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"state": "OPEN"}',
        )
        assert host.is_pr_closed(42) is False


def test_is_pr_closed_failure_returns_false() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        assert host.is_pr_closed(42) is False


# =====================================================================
# get_pr_base_branch
# =====================================================================


def test_get_pr_base_branch_returns_name() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"baseRefName": "main"}',
        )
        result = host.get_pr_base_branch(7)

    assert result == "main"
    cmd = mock_run.call_args[0][0]
    assert cmd == [
        "gh", "pr", "view", "7", "--repo", "owner/repo", "--json", "baseRefName",
    ]


def test_get_pr_base_branch_failure_returns_empty_string() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        result = host.get_pr_base_branch(7)

    assert result == ""


# =====================================================================
# get_commit_sha_of_pr
# =====================================================================


def test_get_commit_sha_of_pr_returns_oid() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"mergeCommit": {"oid": "abc123def"}}',
        )
        result = host.get_commit_sha_of_pr(42)

    assert result == "abc123def"
    cmd = mock_run.call_args[0][0]
    assert cmd == [
        "gh", "pr", "view", "42", "--repo", "owner/repo",
        "--json", "mergeCommit",
    ]


def test_get_commit_sha_of_pr_missing_merge_commit_returns_empty() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"mergeCommit": null}',
        )
        assert host.get_commit_sha_of_pr(42) == ""


def test_get_commit_sha_of_pr_non_dict_merge_commit_returns_empty() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"mergeCommit": "not-a-dict"}',
        )
        assert host.get_commit_sha_of_pr(42) == ""


def test_get_commit_sha_of_pr_subprocess_failure_returns_empty() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        assert host.get_commit_sha_of_pr(42) == ""


def test_get_commit_sha_of_pr_invalid_json_returns_empty() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json",
        )
        assert host.get_commit_sha_of_pr(42) == ""


# =====================================================================
# get_pr_check_conclusion
# =====================================================================


def test_get_pr_check_conclusion_all_success_returns_success() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "statusCheckRollup": [
                    {"conclusion": "SUCCESS"},
                    {"conclusion": "SUCCESS"},
                ],
            }),
        )
        result = host.get_pr_check_conclusion(42)

    assert result == "success"
    cmd = mock_run.call_args[0][0]
    assert cmd == [
        "gh", "pr", "view", "42", "--repo", "owner/repo",
        "--json", "statusCheckRollup",
    ]


def test_get_pr_check_conclusion_any_failure_returns_failure() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "statusCheckRollup": [
                    {"conclusion": "SUCCESS"},
                    {"conclusion": "FAILURE"},
                ],
            }),
        )
        assert host.get_pr_check_conclusion(42) == "failure"


def test_get_pr_check_conclusion_status_context_state_used() -> None:
    """StatusContext rows expose ``state`` (not ``conclusion``)."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "statusCheckRollup": [
                    {"state": "SUCCESS"},
                    {"state": "FAILURE"},
                ],
            }),
        )
        assert host.get_pr_check_conclusion(42) == "failure"


def test_get_pr_check_conclusion_neutral_and_skipped_count_as_success() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "statusCheckRollup": [
                    {"conclusion": "SUCCESS"},
                    {"conclusion": "NEUTRAL"},
                    {"conclusion": "SKIPPED"},
                ],
            }),
        )
        assert host.get_pr_check_conclusion(42) == "success"


def test_get_pr_check_conclusion_pending_returns_unknown() -> None:
    """Pending checks (no conclusion yet) yield ``""`` — neither success nor failure."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "statusCheckRollup": [
                    {"conclusion": "SUCCESS"},
                    {"conclusion": None, "status": "IN_PROGRESS"},
                ],
            }),
        )
        assert host.get_pr_check_conclusion(42) == ""


def test_get_pr_check_conclusion_empty_rollup_returns_none() -> None:
    """An empty rollup means no CI is configured — ``"none"``, distinct from
    the pending/error ``""`` sentinel so the caller proceeds (see #2109)."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"statusCheckRollup": []}),
        )
        assert host.get_pr_check_conclusion(42) == "none"


def test_get_pr_check_conclusion_missing_rollup_returns_none() -> None:
    """No ``statusCheckRollup`` key at all is also "no CI" → ``"none"``."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({}),
        )
        assert host.get_pr_check_conclusion(42) == "none"


def test_get_pr_check_conclusion_skips_non_dict_rollup_items() -> None:
    """Non-dict rollup entries are skipped; the rest is reduced normally."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "statusCheckRollup": [
                    "stray-string",
                    {"conclusion": "SUCCESS"},
                ],
            }),
        )
        assert host.get_pr_check_conclusion(42) == "success"


def test_get_pr_check_conclusion_all_non_dict_rollup_returns_unknown() -> None:
    """When every rollup item is non-dict, ``states`` stays empty → ``""``."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "statusCheckRollup": ["stray-string", 123],
            }),
        )
        assert host.get_pr_check_conclusion(42) == ""


def test_get_pr_check_conclusion_subprocess_failure_returns_empty() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        assert host.get_pr_check_conclusion(42) == ""


def test_get_pr_check_conclusion_invalid_json_returns_empty() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json",
        )
        assert host.get_pr_check_conclusion(42) == ""


def test_get_pr_check_conclusion_excludes_advisory_review() -> None:
    """A red advisory ``review`` check alone does not yield ``failure`` (#2440).

    The AI code-review verdict is informational, not a CI quality gate, so a
    PR whose only red check is ``review`` (with the real ``quality`` gate
    green) must read ``success`` and stay on the main-merge path.
    """
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "statusCheckRollup": [
                    {"name": "review", "conclusion": "FAILURE"},
                    {"name": "quality", "conclusion": "SUCCESS"},
                ],
            }),
        )
        assert host.get_pr_check_conclusion(42) == "success"


def test_get_pr_check_conclusion_excludes_advisory_reply_coverage() -> None:
    """A red advisory ``reply-coverage`` check alone does not yield ``failure``.

    The reply-coverage audit shows red by design while a reviewer finding sits
    unreplied — a signal, not a broken build — so a PR whose only red check is
    ``reply-coverage`` (with the real ``quality`` gate green) must read
    ``success`` and stay on the main-merge path rather than be treated as
    CI-failed and routed into a remediation loop.
    """
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "statusCheckRollup": [
                    {"name": "reply-coverage", "conclusion": "FAILURE"},
                    {"name": "quality", "conclusion": "SUCCESS"},
                ],
            }),
        )
        assert host.get_pr_check_conclusion(42) == "success"


def test_get_pr_check_conclusion_real_quality_failure_still_fails() -> None:
    """A genuine ``quality`` gate failure still concludes ``failure`` (#2440)."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "statusCheckRollup": [
                    {"name": "review", "conclusion": "SUCCESS"},
                    {"name": "quality", "conclusion": "FAILURE"},
                ],
            }),
        )
        assert host.get_pr_check_conclusion(42) == "failure"


def test_get_pr_check_conclusion_review_quality_not_masked() -> None:
    """``review-quality`` is a real gate — exact-name match must not mask it (#2440)."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "statusCheckRollup": [
                    {"name": "review-quality", "conclusion": "FAILURE"},
                ],
            }),
        )
        assert host.get_pr_check_conclusion(42) == "failure"


def test_get_pr_check_conclusion_only_advisory_returns_unknown() -> None:
    """An all-advisory rollup leaves ``states`` empty → ``""`` (not success) (#2440)."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "statusCheckRollup": [
                    {"name": "review", "conclusion": "SUCCESS"},
                ],
            }),
        )
        assert host.get_pr_check_conclusion(42) == ""


def test_get_pr_check_conclusion_advisory_match_is_case_insensitive() -> None:
    """Advisory exclusion matches the name case-insensitively (#2440)."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "statusCheckRollup": [
                    {"name": "Review", "conclusion": "FAILURE"},
                    {"name": "quality", "conclusion": "SUCCESS"},
                ],
            }),
        )
        assert host.get_pr_check_conclusion(42) == "success"


# =====================================================================
# get_pr_failing_check_names
# =====================================================================


def test_get_pr_failing_check_names_excludes_advisory() -> None:
    """Names the real failing gate(s), skipping the advisory ``review`` (#2440)."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "statusCheckRollup": [
                    {"name": "review", "conclusion": "FAILURE"},
                    {"name": "quality", "conclusion": "FAILURE"},
                    {"name": "browser-tests", "conclusion": "SUCCESS"},
                ],
            }),
        )
        assert host.get_pr_failing_check_names(42) == ["quality"]


def test_get_pr_failing_check_names_excludes_advisory_reply_coverage() -> None:
    """``reply-coverage`` is never named as a failing gate for a remediation."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "statusCheckRollup": [
                    {"name": "reply-coverage", "conclusion": "FAILURE"},
                    {"name": "quality", "conclusion": "FAILURE"},
                ],
            }),
        )
        assert host.get_pr_failing_check_names(42) == ["quality"]


def test_get_pr_failing_check_names_reads_legacy_context() -> None:
    """Legacy status contexts expose ``context`` rather than ``name`` (#2440)."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "statusCheckRollup": [
                    {"context": "quality", "state": "FAILURE"},
                ],
            }),
        )
        assert host.get_pr_failing_check_names(42) == ["quality"]


def test_get_pr_failing_check_names_empty_when_all_pass() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "statusCheckRollup": [
                    {"name": "quality", "conclusion": "SUCCESS"},
                ],
            }),
        )
        assert host.get_pr_failing_check_names(42) == []


def test_get_pr_failing_check_names_non_list_rollup_returns_empty() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"statusCheckRollup": None}),
        )
        assert host.get_pr_failing_check_names(42) == []


def test_get_pr_failing_check_names_skips_unnamed_and_non_dict() -> None:
    """Non-dict rows and failing rows without a name are skipped (#2440)."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "statusCheckRollup": [
                    "stray-string",
                    {"conclusion": "FAILURE"},
                    {"name": "quality", "conclusion": "FAILURE"},
                ],
            }),
        )
        assert host.get_pr_failing_check_names(42) == ["quality"]


def test_get_pr_failing_check_names_subprocess_failure_returns_empty() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        assert host.get_pr_failing_check_names(42) == []


def test_get_pr_failing_check_names_invalid_json_returns_empty() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json",
        )
        assert host.get_pr_failing_check_names(42) == []


# =====================================================================
# _first_failing_run_id (rollup → workflow run id)
# =====================================================================


def test_first_failing_run_id_resolves_from_details_url() -> None:
    """A failing check run's ``detailsUrl`` yields its workflow run id."""
    rollup = [
        {
            "name": "quality",
            "conclusion": "FAILURE",
            "detailsUrl": "https://github.com/o/r/actions/runs/123456/job/7",
        },
    ]
    assert _first_failing_run_id(rollup) == "123456"


def test_first_failing_run_id_falls_back_to_target_url() -> None:
    """A legacy status context exposes the run id under ``targetUrl``."""
    rollup = [
        {
            "context": "ci/legacy",
            "state": "FAILURE",
            "targetUrl": "https://github.com/o/r/actions/runs/444",
        },
    ]
    assert _first_failing_run_id(rollup) == "444"


def test_first_failing_run_id_skips_non_dict_advisory_and_passing() -> None:
    """Non-dict, advisory, and passing rows are skipped; the real failure wins."""
    rollup = [
        "stray-string",
        {"name": "review", "conclusion": "FAILURE",
         "detailsUrl": "x/runs/111"},
        {"name": "quality", "conclusion": "SUCCESS",
         "detailsUrl": "x/runs/222"},
        {"name": "quality", "conclusion": "FAILURE",
         "detailsUrl": "https://github.com/o/r/actions/runs/333/job/1"},
    ]
    assert _first_failing_run_id(rollup) == "333"


def test_first_failing_run_id_none_when_no_parseable_id() -> None:
    """A failing row without a ``/runs/<id>`` url, or no failure, yields None."""
    assert _first_failing_run_id(
        [{"name": "quality", "conclusion": "FAILURE",
          "detailsUrl": "no-run-here"}],
    ) is None
    assert _first_failing_run_id(
        [{"name": "quality", "conclusion": "SUCCESS"}],
    ) is None


# =====================================================================
# read_run_log_failed (bounded, redacted failing-run log)
# =====================================================================


def _failing_rollup(run_id: str = "123456") -> subprocess.CompletedProcess[str]:
    """A rollup CompletedProcess with a single failing ``quality`` check."""
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({
            "statusCheckRollup": [
                {
                    "name": "quality",
                    "conclusion": "FAILURE",
                    "detailsUrl":
                        f"https://github.com/o/r/actions/runs/{run_id}/job/7",
                },
            ],
        }),
    )


def test_read_run_log_failed_returns_redacted_log() -> None:
    """The failing-run log is fetched, secret-redacted, and returned."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    raw_log = (
        "quality\tRun\tghp_" + "a" * 36 + "\n"
        "quality\tRun\tAuthorization: Bearer sometoken1234567890\n"
        "quality\tRun\ttelegram 123456789:" + "A" * 35 + "\n"
        "quality\tRun\tFAILED tests/test_x.py::t\n"
    )
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = [
            _failing_rollup("123456"),
            subprocess.CompletedProcess(args=[], returncode=0, stdout=raw_log),
        ]
        result = host.read_run_log_failed(42)

    assert "ghp_" not in result
    assert "Bearer" not in result
    assert "123456789:" not in result
    assert "<redacted>" in result
    # The concrete failure line survives redaction.
    assert "FAILED tests/test_x.py::t" in result
    # The second gh call targets the resolved run id.
    second_argv = mock_run.call_args_list[1][0][0]
    assert second_argv == [
        "gh", "run", "view", "123456", "--repo", "owner/repo", "--log-failed",
    ]


def test_read_run_log_failed_truncates_to_cap() -> None:
    """A log exceeding the char cap is truncated to ``_RUN_LOG_MAX_CHARS``."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    raw_log = "error: boom\n" * 500
    assert len(raw_log) > _RUN_LOG_MAX_CHARS
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = [
            _failing_rollup(),
            subprocess.CompletedProcess(args=[], returncode=0, stdout=raw_log),
        ]
        result = host.read_run_log_failed(42)
    assert len(result) == _RUN_LOG_MAX_CHARS


def test_read_run_log_failed_keeps_tail_where_errors_live() -> None:
    """Recall regression: a verbose log's trailing failure summary survives.

    ``gh run view --log-failed`` puts a pytest step's ``FAILED`` summary at the
    END, past a head window of collection/progress noise. Capping to the tail
    (not the head) keeps the concrete error the diagnosis needs (ledger r1).
    """
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    noise = "quality\tRun tests\tcollecting ...\n" * 400
    assert len(noise) > _RUN_LOG_MAX_CHARS
    raw_log = noise + "quality\tRun tests\tFAILED tests/test_z.py::t\n"
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = [
            _failing_rollup(),
            subprocess.CompletedProcess(args=[], returncode=0, stdout=raw_log),
        ]
        result = host.read_run_log_failed(42)
    assert len(result) == _RUN_LOG_MAX_CHARS
    assert "FAILED tests/test_z.py::t" in result


def test_read_run_log_failed_non_list_rollup_returns_empty() -> None:
    """A rollup that is not a list short-circuits before the log fetch."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = [
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps({}),
            ),
        ]
        assert host.read_run_log_failed(42) == ""
        assert mock_run.call_count == 1


def test_read_run_log_failed_no_failing_run_id_returns_empty() -> None:
    """No resolvable failing run id short-circuits before the log fetch."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = [
            subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=json.dumps({
                    "statusCheckRollup": [
                        {"name": "quality", "conclusion": "SUCCESS"},
                    ],
                }),
            ),
        ]
        assert host.read_run_log_failed(42) == ""
        assert mock_run.call_count == 1


def test_read_run_log_failed_rollup_fetch_failure_returns_empty() -> None:
    """A ``gh pr view`` failure returns ``""`` without raising."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        assert host.read_run_log_failed(42) == ""


def test_read_run_log_failed_log_fetch_failure_returns_empty() -> None:
    """A ``gh run view`` failure (after a good rollup) returns ``""``."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = [
            _failing_rollup(),
            subprocess.CalledProcessError(1, "gh"),
        ]
        assert host.read_run_log_failed(42) == ""


def test_read_run_log_failed_invalid_json_returns_empty() -> None:
    """An unparseable rollup payload returns ``""``."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = [
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="not json",
            ),
        ]
        assert host.read_run_log_failed(42) == ""


# =====================================================================
# get_open_evolution_pr_states
# =====================================================================


def test_get_open_evolution_pr_states_parses_rows() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    rows = [
        {
            "number": 42,
            "headRefName": "maestro/evolve/abc",
            "title": "t",
            "body": "b",
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "reviewDecision": "",
            "createdAt": "2026-04-20T00:00:00Z",
            "updatedAt": "2026-04-21T00:00:00Z",
            "url": "https://github.com/owner/repo/pull/42",
        },
        # Out-of-scope branches that the unfiltered ``gh pr list`` returns —
        # the client-side prefix filter is the sole selector that drops them.
        {
            "number": 43,
            "headRefName": "feature/unrelated",
            "title": "",
            "body": "",
            "state": "OPEN",
            "mergeable": "",
            "mergeStateStatus": "",
            "reviewDecision": "",
            "createdAt": "",
            "updatedAt": "",
            "url": "",
        },
        {
            "number": 44,
            "headRefName": "main",
            "title": "",
            "body": "",
            "state": "OPEN",
            "mergeable": "",
            "mergeStateStatus": "",
            "reviewDecision": "",
            "createdAt": "",
            "updatedAt": "",
            "url": "",
        },
    ]
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps(rows),
        )
        states = host.get_open_evolution_pr_states("maestro/evolve/")

    assert set(states) == {"maestro/evolve/abc"}
    state = states["maestro/evolve/abc"]
    assert isinstance(state, PrMergeState)
    assert state.number == 42
    assert state.merge_state_status == "CLEAN"
    assert state.url.endswith("/pull/42")

    # The command no longer narrows server-side: no ``--search`` / ``head:``
    # qualifier, so correctness cannot depend on undocumented ``head:`` prefix
    # semantics (issue #1339).
    cmd = mock_run.call_args[0][0]
    assert cmd[:2] == ["gh", "pr"]
    assert "--search" not in cmd
    assert not any("head:" in str(arg) for arg in cmd)
    assert "--state" in cmd and "open" in cmd


def test_get_open_evolution_pr_states_no_repo_returns_empty() -> None:
    host = SubprocessCodeHost(workspace=FAKE_DIR)
    assert host.get_open_evolution_pr_states("maestro/evolve/") == {}


def test_get_open_evolution_pr_states_command_failure_returns_empty() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "gh"),
    ):
        assert host.get_open_evolution_pr_states("maestro/evolve/") == {}


def test_get_open_evolution_pr_states_bad_json_returns_empty() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="not json",
        )
        assert host.get_open_evolution_pr_states("maestro/evolve/") == {}


def test_get_open_evolution_pr_states_strict_no_repo_raises() -> None:
    """#2813: in strict mode an unset repo is a failure to confirm PR state,
    not a definitive 'no PR' — it raises rather than returning ``{}``."""
    host = SubprocessCodeHost(workspace=FAKE_DIR)
    with pytest.raises(VCSError, match="no repo configured"):
        host.get_open_evolution_pr_states("maestro/evolve/", strict=True)


def test_get_open_evolution_pr_states_strict_command_failure_raises() -> None:
    """#2813: strict mode re-raises the host ``gh`` failure instead of
    swallowing it into ``{}``, so a caller cannot read a transient error as
    positive confirmation that no PR exists."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "gh"),
    ), pytest.raises(VCSError):
        host.get_open_evolution_pr_states("maestro/evolve/", strict=True)


def test_get_open_evolution_pr_states_strict_bad_json_raises() -> None:
    """#2813: strict mode treats malformed ``gh`` JSON as an unconfirmed
    listing (raises), never as an empty result."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="not json",
        )
        with pytest.raises(VCSError, match="Invalid JSON"):
            host.get_open_evolution_pr_states("maestro/evolve/", strict=True)


# =====================================================================
# resolve_owner — proposal 030 read helper
# =====================================================================


def test_resolve_owner_returns_login_from_gh_api() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="thomas-sodon\n",
        )
        owner = host.resolve_owner()

    assert owner == "thomas-sodon"
    cmd = mock_run.call_args[0][0]
    assert cmd == ["gh", "api", "user", "--jq", ".login"]


def test_resolve_owner_strips_json_quoting() -> None:
    """gh api --jq occasionally emits quoted output; strip the quotes."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='"thomas-sodon"\n',
        )
        assert host.resolve_owner() == "thomas-sodon"


def test_resolve_owner_caches_after_first_call() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="thomas-sodon\n",
        )
        first = host.resolve_owner()
        second = host.resolve_owner()

    assert first == "thomas-sodon"
    assert second == "thomas-sodon"
    assert mock_run.call_count == 1


def test_resolve_owner_returns_empty_on_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run, \
         caplog.at_level("WARNING", logger="maestro.vcs"):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh", stderr="authentication required",
        )
        owner = host.resolve_owner()

    assert owner == ""
    assert "resolve_owner failed" in caplog.text


def test_resolve_owner_empty_stdout_not_cached() -> None:
    """An empty response must not be cached so a later probe can retry."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="\n",
        )
        first = host.resolve_owner()
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="thomas-sodon\n",
        )
        second = host.resolve_owner()

    assert first == ""
    assert second == "thomas-sodon"
    assert mock_run.call_count == 2


# =====================================================================
# repo_exists — proposal 030 read helper
# =====================================================================


def test_repo_exists_true_on_clean_exit() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="",
        )
        assert host.repo_exists("thomas-sodon", "maestro") is True


def test_repo_exists_passes_owner_and_name_to_gh() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="",
        )
        host.repo_exists("thomas-sodon", "my-project")

    cmd = mock_run.call_args[0][0]
    assert cmd == [
        "gh", "api", "repos/thomas-sodon/my-project", "--silent",
    ]


def test_repo_exists_false_on_404(caplog: pytest.LogCaptureFixture) -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run, \
         caplog.at_level("WARNING", logger="maestro.vcs"):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh",
            stderr=(
                "gh: Not Found (HTTP 404)\n"
                "The repository does not exist.\n"
            ),
        )
        assert host.repo_exists("thomas-sodon", "ghost-repo") is False

    # 404 is an expected outcome — no warning log.
    assert "repo_exists inconclusive" not in caplog.text


def test_repo_exists_false_on_404_http_marker_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run, \
         caplog.at_level("WARNING", logger="maestro.vcs"):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh",
            stderr="gh: HTTP 404\nThe repository does not exist.\n",
        )
        assert host.repo_exists("thomas-sodon", "ghost-repo") is False

    # Either 404 marker should route to the silent-False branch — no warning.
    assert "repo_exists inconclusive" not in caplog.text


def test_repo_exists_false_with_warning_on_other_exit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run, \
         caplog.at_level("WARNING", logger="maestro.vcs"):
        mock_run.side_effect = subprocess.CalledProcessError(
            2, "gh", stderr="gh: network timeout",
        )
        result = host.repo_exists("thomas-sodon", "maestro")

    assert result is False
    assert "repo_exists inconclusive" in caplog.text
    assert "thomas-sodon/maestro" in caplog.text


def test_repo_exists_false_on_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run, \
         caplog.at_level("WARNING", logger="maestro.vcs"):
        mock_run.side_effect = subprocess.TimeoutExpired("gh", 30)
        result = host.repo_exists("thomas-sodon", "maestro")

    assert result is False
    assert "repo_exists inconclusive" in caplog.text


# =====================================================================
# create_public_repo — proposal 033 publication executor
# =====================================================================


def test_create_public_repo_returns_canonical_url() -> None:
    """create_public_repo issues ``gh repo create`` and returns the URL."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="",
        )
        url = host.create_public_repo(
            "demo-repo", "demo description", owner="thomas-sodon",
        )

    assert url == "https://github.com/thomas-sodon/demo-repo"
    cmd = mock_run.call_args[0][0]
    assert cmd == [
        "gh", "repo", "create",
        "thomas-sodon/demo-repo",
        "--public",
        "--description", "demo description",
    ]
    # Must NOT pass --source / --push: the publication pipeline needs
    # repo creation split from the local push so a crash between the
    # two does not orphan the public repo.
    assert "--source" not in cmd
    assert "--push" not in cmd


def test_create_public_repo_resolves_owner_when_not_supplied() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = [
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="thomas-sodon\n",
            ),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="",
            ),
        ]
        url = host.create_public_repo("demo", "desc")

    assert url == "https://github.com/thomas-sodon/demo"
    # First call resolves owner, second call creates the repo.
    assert mock_run.call_args_list[0][0][0] == [
        "gh", "api", "user", "--jq", ".login",
    ]
    second = mock_run.call_args_list[1][0][0]
    assert second[3] == "thomas-sodon/demo"


def test_create_public_repo_raises_when_owner_unresolvable() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        # resolve_owner fails → returns "" → create_public_repo must raise
        # before any ``gh repo create`` call.
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh", stderr="auth required",
        )
        with pytest.raises(VCSError, match="resolve_owner returned empty"):
            host.create_public_repo("demo", "desc")
    # Only the resolve_owner call should have run.
    assert mock_run.call_count == 1


def test_create_public_repo_propagates_gh_failure() -> None:
    """A non-zero ``gh repo create`` exit must surface as VCSError."""
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "gh", stderr="name already exists on this account",
        )
        with pytest.raises(VCSError, match="name already exists"):
            host.create_public_repo(
                "demo", "desc", owner="thomas-sodon",
            )


# -- Fail-loud ``*_strict`` review reads (#4065) -----------------------------


def test_get_pr_review_threads_strict_raises_on_host_failure() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        with pytest.raises(VCSError):
            host.get_pr_review_threads_strict(10)


def test_get_pr_review_threads_strict_raises_on_bad_json() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json",
        )
        with pytest.raises(VCSError):
            host.get_pr_review_threads_strict(10)


def test_get_pr_review_threads_strict_raises_without_repo() -> None:
    host = SubprocessCodeHost(workspace=FAKE_DIR)
    with pytest.raises(VCSError):
        host.get_pr_review_threads_strict(10)


def test_get_pr_review_bodies_strict_raises_on_host_failure() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        with pytest.raises(VCSError):
            host.get_pr_review_bodies_strict(10)


def test_get_pr_comment_bodies_strict_raises_on_bad_json() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json",
        )
        with pytest.raises(VCSError):
            host.get_pr_comment_bodies_strict(10)


# =====================================================================
# Gap-issue reads (proposal 155): reopen, label actor, closure, title search
# =====================================================================


def _completed(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout)


def _events(*rows: dict[str, object]) -> str:
    return "".join(json.dumps(r) + "\n" for r in rows)


def test_close_issue_returns_bool() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run", return_value=_completed()):
        assert host.close_issue(1) is True
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "gh", stderr="x"),
    ):
        assert host.close_issue(1) is False
    assert SubprocessCodeHost(workspace=FAKE_DIR).close_issue(1) is False


def test_reopen_issue_true_and_false() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run", return_value=_completed()) as run:
        assert host.reopen_issue(7) is True
    assert run.call_args[0][0][:4] == ["gh", "issue", "reopen", "7"]
    with patch("maestro.vcs.subprocess.run", side_effect=OSError("x")):
        assert host.reopen_issue(7) is False


def test_label_actor_latest_labeled_wins() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    out = _events(
        {"event": "labeled", "label": "gap:maestro", "actor": "bot"},
        {"event": "labeled", "label": "other", "actor": "x"},
        {"event": "unlabeled", "label": "gap:maestro", "actor": "bot"},
        {"event": "labeled", "label": "gap:maestro", "actor": "thomas"},
    ) + "\n"
    with patch("maestro.vcs.subprocess.run", return_value=_completed(out)) as run:
        assert host.get_issue_label_actor(3, "gap:maestro") == "thomas"
    cmd = run.call_args[0][0]
    assert cmd[:3] == ["gh", "api", "repos/owner/repo/issues/3/events"]
    assert "--paginate" in cmd


def test_label_actor_none_cases() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    unlabeled = _events(
        {"event": "labeled", "label": "gap:maestro", "actor": "thomas"},
        {"event": "unlabeled", "label": "gap:maestro", "actor": "thomas"},
    )
    with patch("maestro.vcs.subprocess.run", return_value=_completed(unlabeled)):
        assert host.get_issue_label_actor(3, "gap:maestro") is None
    with patch("maestro.vcs.subprocess.run", return_value=_completed("")):
        assert host.get_issue_label_actor(3, "gap:maestro") is None
    no_actor = _events({"event": "labeled", "label": "gap:maestro", "actor": None})
    with patch("maestro.vcs.subprocess.run", return_value=_completed(no_actor)):
        assert host.get_issue_label_actor(3, "gap:maestro") is None
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "gh"),
    ):
        assert host.get_issue_label_actor(3, "gap:maestro") is None
    with patch("maestro.vcs.subprocess.run", return_value=_completed("{bad")):
        assert host.get_issue_label_actor(3, "gap:maestro") is None
    with patch("maestro.vcs.subprocess.run", return_value=_completed("[1]\n")):
        assert host.get_issue_label_actor(3, "gap:maestro") is None


def test_issue_closure_latest_and_via_commit() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    out = _events(
        {"event": "closed", "actor": "thomas", "commit_id": None},
        {"event": "closed", "actor": "bot", "commit_id": "abc123"},
    )
    with patch("maestro.vcs.subprocess.run", return_value=_completed(out)):
        closure = host.get_issue_closure(3)
    assert closure is not None
    assert closure.actor == "bot"
    assert closure.via_commit is True
    assert closure.reopened_by == ""


def test_issue_closure_reports_reopen_after_latest_close() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    out = _events(
        {"event": "closed", "actor": "thomas", "commit_id": None},
        {"event": "reopened", "actor": "thomas"},
        {"event": "closed", "actor": "thomas", "commit_id": None},
        {"event": "reopened", "actor": 7},
        {"event": "reopened", "actor": "bot"},
    )
    with patch("maestro.vcs.subprocess.run", return_value=_completed(out)):
        closure = host.get_issue_closure(3)
    assert closure == IssueClosure(
        "thomas", via_commit=False, reopened_by="bot",
        history=(
            ("closed", "thomas", False), ("reopened", "thomas", False),
            ("closed", "thomas", False), ("reopened", "", False),
            ("reopened", "bot", False),
        ),
    )
    odd = _events(
        {"event": "closed", "actor": "thomas"}, {"event": "reopened", "actor": 7},
    )
    with patch("maestro.vcs.subprocess.run", return_value=_completed(odd)):
        closure = host.get_issue_closure(3)
    assert closure == IssueClosure(
        "thomas", via_commit=False, reopened_by="",
        history=(("closed", "thomas", False), ("reopened", "", False)),
    )


def test_issue_closure_none_cases() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    labeled = _events({"event": "labeled", "label": "x", "actor": "a"})
    with patch("maestro.vcs.subprocess.run", return_value=_completed(labeled)):
        assert host.get_issue_closure(3) == IssueClosure("", via_commit=False, closed=False)
    with patch("maestro.vcs.subprocess.run", side_effect=OSError("x")):
        assert host.get_issue_closure(3) is None
    odd = _events({"event": "closed", "actor": 5})
    with patch("maestro.vcs.subprocess.run", return_value=_completed(odd)):
        closure = host.get_issue_closure(3)
    assert closure is not None
    assert closure.actor == ""
    assert closure.via_commit is False


def test_list_issues_title_contains_joins_exclude_labels() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run", return_value=_completed("[]")) as run:
        assert host.list_issues(
            title_contains='phase:"launch', exclude_labels=["crash"],
        ) == []
    cmd = run.call_args[0][0]
    search = cmd[cmd.index("--search") + 1]
    assert search == '"phase:launch" in:title -label:"crash"'


def test_issue_meta_reads_state_title_labels() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    out = '{"state": "open", "title": "t", "labels": ["gap:maestro"]}'
    with patch("maestro.vcs.subprocess.run", return_value=_completed(out)):
        meta = host.get_issue_meta(3)
    assert meta == IssueMeta("open", "t", ("gap:maestro",))
    bare = '{"state": "closed", "title": null, "labels": null}'
    with patch("maestro.vcs.subprocess.run", return_value=_completed(bare)):
        assert host.get_issue_meta(3) == IssueMeta("closed", "", ())


@pytest.mark.parametrize("code", ["404", "410"])
def test_issue_meta_gone(code: str) -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    err = subprocess.CalledProcessError(1, "gh", stderr=f"gh: Not Found (HTTP {code})")
    with patch("maestro.vcs.subprocess.run", side_effect=err):
        assert host.get_issue_meta(3) == IssueMeta("gone")


def test_issue_meta_unreadable_is_none() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    err = subprocess.CalledProcessError(1, "gh", stderr="HTTP 502")
    with patch("maestro.vcs.subprocess.run", side_effect=err):
        assert host.get_issue_meta(3) is None
    with patch("maestro.vcs.subprocess.run", return_value=_completed("{bad")):
        assert host.get_issue_meta(3) is None
    with patch("maestro.vcs.subprocess.run", return_value=_completed('{"state": "x"}')):
        assert host.get_issue_meta(3) is None
    with patch("maestro.vcs.subprocess.run", return_value=_completed("[1]")):
        assert host.get_issue_meta(3) is None


# =====================================================================
# get_ref_check_runs (proposal 155 step 2)
# =====================================================================

_SHA = "0123456789abcdef0123456789abcdef01234567"


def test_get_ref_check_runs_argv_and_mapping() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    out = _events(
        {"name": "tests", "status": "completed", "conclusion": "FAILURE"},
        {"name": "lint", "status": "in_progress", "conclusion": None},
        {"name": "review", "status": "completed", "conclusion": "failure"},
        {"name": "deploy", "status": "completed", "conclusion": "failure"},
        {"status": "queued", "conclusion": None},
    ) + "\n" + json.dumps(["not", "a", "dict"]) + "\n"
    with patch("maestro.vcs.subprocess.run", return_value=_completed(out)) as run:
        result = host.get_ref_check_runs(_SHA)
    argv = run.call_args.args[0]
    assert argv[:3] == [
        "gh", "api",
        f"repos/owner/repo/commits/{_SHA}/check-runs?filter=latest&per_page=100",
    ]
    assert "--paginate" in argv
    assert result == RefCheckRuns(_SHA, (
        CheckRun("tests", "failure"),
        CheckRun("lint", "in_progress"),
        CheckRun("", "queued"),
    ))


@pytest.mark.parametrize("sha", ["", "abc", "z" * 40, "a" * 41, "HEAD"])
def test_get_ref_check_runs_refuses_bad_sha_before_any_call(sha: str) -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as run:
        assert host.get_ref_check_runs(sha) is None
    run.assert_not_called()


def test_get_ref_check_runs_unset_repo_returns_none() -> None:
    host = SubprocessCodeHost(None, workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as run:
        assert host.get_ref_check_runs(_SHA) is None
    run.assert_not_called()


def test_get_ref_check_runs_host_failure_returns_none(caplog: pytest.LogCaptureFixture) -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with (
        patch("maestro.vcs.subprocess.run", side_effect=subprocess.CalledProcessError(1, "gh")),
        caplog.at_level(logging.WARNING, logger="maestro"),
    ):
        assert host.get_ref_check_runs(_SHA) is None
    assert "Failed to read check runs" in caplog.text


def test_get_ref_check_runs_bad_json_returns_none() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run", return_value=_completed("{nope\n")):
        assert host.get_ref_check_runs(_SHA) is None


# --- issue label verbs (proposal 155 step 5) -------------------------------------


def test_add_issue_label_argv_and_success() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run", return_value=_completed()) as run:
        assert host.add_issue_label(5, "needs-decision") is True
    assert run.call_args[0][0] == [
        "gh", "issue", "edit", "5", "--repo", "owner/repo",
        "--add-label", "needs-decision",
    ]


def test_add_issue_label_retries_after_ensuring_label() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    missing = subprocess.CalledProcessError(1, "gh", stderr="label 'x' not found")
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=[missing, _completed(), _completed()],
    ) as run:
        assert host.add_issue_label(5, "needs-decision") is True
    argvs = [c[0][0] for c in run.call_args_list]
    assert argvs[1][:4] == ["gh", "label", "create", "needs-decision"]
    assert argvs[2][:3] == ["gh", "issue", "edit"]


def test_add_issue_label_failures() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    other = subprocess.CalledProcessError(1, "gh", stderr="HTTP 500")
    with patch("maestro.vcs.subprocess.run", side_effect=other) as run:
        assert host.add_issue_label(5, "x") is False
    assert run.call_count == 1
    with patch("maestro.vcs.subprocess.run", side_effect=OSError("x")) as run:
        assert host.add_issue_label(5, "x") is False
    assert run.call_count == 1
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.TimeoutExpired("gh", 1),
    ):
        assert host.remove_issue_label(5, "x") is False
    assert SubprocessCodeHost(workspace=FAKE_DIR).add_issue_label(5, "x") is False
    assert SubprocessCodeHost(workspace=FAKE_DIR).remove_issue_label(5, "x") is False


def test_remove_issue_label_argv() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    with patch("maestro.vcs.subprocess.run", return_value=_completed()) as run:
        assert host.remove_issue_label(5, "needs-decision") is True
    assert run.call_args[0][0][-2:] == ["--remove-label", "needs-decision"]
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "gh", stderr="label gone"),
    ) as run:
        assert host.remove_issue_label(5, "needs-decision") is False
    assert run.call_count == 1


def test_label_removed_at_latest_unlabeled_only() -> None:
    host = SubprocessCodeHost("owner/repo", workspace=FAKE_DIR)
    removed = _events(
        {"event": "labeled", "label": "needs-decision", "created_at": "t1"},
        {"event": "unlabeled", "label": "needs-decision", "created_at": "t2",
         "actor": "thomas"},
        {"event": "labeled", "label": "other", "created_at": "t3"},
    )
    with patch("maestro.vcs.subprocess.run", return_value=_completed(removed)) as run:
        assert host.get_issue_label_removed_at(3, "needs-decision") == LabelRemoval("t2", "thomas")
    assert "created_at" in run.call_args[0][0][-1]
    relabeled = removed + _events(
        {"event": "labeled", "label": "needs-decision", "created_at": "t4"},
    )
    with patch("maestro.vcs.subprocess.run", return_value=_completed(relabeled)):
        assert host.get_issue_label_removed_at(3, "needs-decision") is None
    with patch("maestro.vcs.subprocess.run", return_value=_completed("")):
        assert host.get_issue_label_removed_at(3, "needs-decision") is None
    no_actor = _events({"event": "unlabeled", "label": "needs-decision", "created_at": "t5"})
    with patch("maestro.vcs.subprocess.run", return_value=_completed(no_actor)):
        assert host.get_issue_label_removed_at(3, "needs-decision") == LabelRemoval("t5", "")
    no_stamp = _events({"event": "unlabeled", "label": "needs-decision"})
    with patch("maestro.vcs.subprocess.run", return_value=_completed(no_stamp)):
        assert host.get_issue_label_removed_at(3, "needs-decision") is None
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "gh"),
    ):
        assert host.get_issue_label_removed_at(3, "needs-decision") == LabelRemoval(
            "", readable=False,
        )
