"""Component tests for PRReviewer — exercises CodeHost integration via mocks.

The Claude Agent SDK is mocked at the Python level via SDKMock.
GitHub API calls use the in-memory MockCodeHost.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro_review.github import GitHubError
from maestro_review.review import PRReviewer, ReviewFinding
from maestro_review.sdk import QueryResult, QueryUsage

SAMPLE_DIFF = """\
diff --git a/maestro/feature.py b/maestro/feature.py
--- a/maestro/feature.py
+++ b/maestro/feature.py
@@ -1,3 +1,5 @@
 import os
+import sys
+import json

 def main():
"""


def _sdk_result(findings: list[dict[str, object]], summary: str = "ok") -> QueryResult:
    return QueryResult(
        response=json.dumps({"summary": summary, "findings": findings}),
        usage=QueryUsage(total_tokens=500, total_cost_usd=0.02),
    )


@pytest.mark.component
@pytest.mark.asyncio
async def test_review_posts_inline_comments(code_host) -> None:
    """Full flow: fetch diff, run review, post inline comments."""
    code_host.pr_diffs = {5: SAMPLE_DIFF}
    code_host.pr_info = {5: {"title": "Add feature", "body": "New feature"}}

    reviewer = PRReviewer("test-owner/test-repo", code_host=code_host)

    findings_data = [
        {
            "file": "maestro/feature.py",
            "line": 2,
            "severity": "suggestion",
            "title": "Unused import",
            "description": "sys is imported but never used",
            "fix_plan": "Remove the import",
        },
    ]

    with patch("maestro_review.review.query", new_callable=AsyncMock) as mock_query:
        mock_query.return_value = _sdk_result(findings_data, "Minor issue found")
        result = await reviewer.review(5)

    assert len(result.findings) == 1
    assert result.findings[0].title == "Unused import"

    # Post the review — use result.diff to match production code path
    reviewer.post_review(5, result.findings, result.summary, diff=result.diff or None)

    # Verify the review was posted via in-memory mock
    assert len(code_host.created_reviews) == 1
    assert code_host.created_reviews[0]["pr_number"] == 5
    assert code_host.created_reviews[0]["event"] == "COMMENT"
    assert len(code_host.created_reviews[0]["comments"]) == 1
    assert code_host.created_reviews[0]["comments"][0]["path"] == "maestro/feature.py"
    assert code_host.created_reviews[0]["comments"][0]["line"] == 2


@pytest.mark.component
@pytest.mark.asyncio
async def test_review_empty_diff_posts_clean_summary(code_host) -> None:
    """When the PR has no changes, the review summary says so."""
    code_host.pr_diffs = {6: ""}

    reviewer = PRReviewer("test-owner/test-repo", code_host=code_host)
    result = await reviewer.review(6)

    assert result.summary == "No changes to review."
    assert result.findings == []


@pytest.mark.component
@pytest.mark.asyncio
async def test_review_gh_failure_exits_cleanly() -> None:
    """When gh api fails to post the review, the error is raised but doesn't crash."""
    failing_host = MagicMock()
    failing_host.list_issues.return_value = []
    failing_host.get_pr_review_threads.return_value = []
    failing_host.post_pr_review.side_effect = GitHubError("Mock failure")

    reviewer = PRReviewer("test-owner/test-repo", code_host=failing_host)
    findings = [
        ReviewFinding(
            file="a.py", line=1, severity="bug", title="Bug",
            description="Issue", fix_plan="Fix",
        ),
    ]

    with pytest.raises(GitHubError):
        reviewer.post_review(5, findings, "Summary")


@pytest.mark.component
@pytest.mark.asyncio
async def test_review_dedup_skips_unresolved_thread(code_host) -> None:
    """Findings matching an unresolved review thread are skipped (not re-posted)."""
    code_host.pr_diffs = {7: SAMPLE_DIFF}
    code_host.pr_info = {7: {"title": "Add feature", "body": "New feature"}}
    # Pre-populate an unresolved thread matching the finding
    code_host.review_threads = {
        7: [
            {
                "id": "RT_thread1",
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": 100,
                            "path": "maestro/feature.py",
                            "body": "**Unused import**\n\nsys is imported but never used",
                        },
                    ],
                },
            },
        ],
    }

    reviewer = PRReviewer("test-owner/test-repo", code_host=code_host)

    findings_data = [
        {
            "file": "maestro/feature.py",
            "line": 2,
            "severity": "suggestion",
            "title": "Unused import",
            "description": "sys is imported but never used",
            "fix_plan": "Remove the import",
        },
    ]

    with patch("maestro_review.review.query", new_callable=AsyncMock) as mock_query:
        mock_query.return_value = _sdk_result(findings_data)
        result = await reviewer.review(7)

    reviewer.post_review(7, result.findings, result.summary, diff=result.diff or None)

    # No new review should be posted — finding was skipped
    assert len(code_host.created_reviews) == 0


@pytest.mark.component
@pytest.mark.asyncio
async def test_review_reopens_resolved_thread_without_issue(code_host) -> None:
    """A resolved bug thread without an issue link gets a defect-persists reply."""
    code_host.pr_diffs = {8: SAMPLE_DIFF}
    code_host.pr_info = {8: {"title": "Add feature", "body": "New feature"}}
    # Pre-populate a resolved thread WITHOUT an issue link
    code_host.review_threads = {
        8: [
            {
                "id": "RT_thread2",
                "isResolved": True,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": 200,
                            "path": "maestro/feature.py",
                            "body": "**Unused import**\n\nsys is imported but never used",
                        },
                        {
                            "databaseId": 201,
                            "path": "maestro/feature.py",
                            "body": "Fixed by removing the import",
                        },
                    ],
                },
            },
        ],
    }

    reviewer = PRReviewer("test-owner/test-repo", code_host=code_host)

    # Must be a bug — only bugs reopen resolved threads without issue links.
    # Nits/suggestions in resolved threads are skipped (handled by issue linking).
    findings_data = [
        {
            "file": "maestro/feature.py",
            "line": 2,
            "severity": "bug",
            "title": "Unused import",
            "description": "sys is imported but never used",
            "fix_plan": "Remove the import",
        },
    ]

    with patch("maestro_review.review.query", new_callable=AsyncMock) as mock_query:
        mock_query.return_value = _sdk_result(findings_data)
        result = await reviewer.review(8)

    reviewer.post_review(8, result.findings, result.summary, diff=result.diff or None)

    # A reply should have been posted about the defect persisting
    assert any(
        r["comment_id"] == 200 for r in code_host.reply_calls
    )

    # No new review comments should be posted — finding was reopened, not re-posted
    assert len(code_host.created_reviews) == 0


@pytest.mark.component
@pytest.mark.asyncio
async def test_review_skips_resolved_thread_with_issue_link(code_host) -> None:
    """A resolved thread with an issue link is skipped entirely."""
    code_host.pr_diffs = {9: SAMPLE_DIFF}
    code_host.pr_info = {9: {"title": "Add feature", "body": "New feature"}}
    # Pre-populate a resolved thread WITH an issue link
    code_host.review_threads = {
        9: [
            {
                "id": "RT_thread3",
                "isResolved": True,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": 300,
                            "path": "maestro/feature.py",
                            "body": "**Unused import**\n\nsys is imported but never used",
                        },
                        {
                            "databaseId": 301,
                            "path": "maestro/feature.py",
                            "body": "Filed as https://github.com/test-owner/test-repo/issues/42",
                        },
                    ],
                },
            },
        ],
    }

    reviewer = PRReviewer("test-owner/test-repo", code_host=code_host)

    findings_data = [
        {
            "file": "maestro/feature.py",
            "line": 2,
            "severity": "suggestion",
            "title": "Unused import",
            "description": "sys is imported but never used",
            "fix_plan": "Remove the import",
        },
    ]

    with patch("maestro_review.review.query", new_callable=AsyncMock) as mock_query:
        mock_query.return_value = _sdk_result(findings_data)
        result = await reviewer.review(9)

    reviewer.post_review(9, result.findings, result.summary, diff=result.diff or None)

    # No reply posted for this thread — it has an issue link
    assert not any(
        r["comment_id"] == 300 for r in code_host.reply_calls
    )

    # No new review comments posted either — finding was skipped
    assert len(code_host.created_reviews) == 0
