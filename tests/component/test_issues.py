"""Component tests for issues."""

from __future__ import annotations

from unittest.mock import MagicMock

from maestro.issues import IssueTracker
from tests.component.mock_vcs import MockCodeHost

# ---- lifecycle ----


def test_list_create_close_issues(code_host):
    code_host.issues = [
        {"number": 10, "title": "Bug: crash", "labels": [{"name": "bug"}],
         "body": "App crashes", "createdAt": "2025-01-01T00:00:00Z"},
        {"number": 11, "title": "Feature: dark mode", "labels": [{"name": "enhancement"}],
         "body": "Please add", "createdAt": "2025-01-02T00:00:00Z"},
    ]
    tracker = IssueTracker(github_repo="test-owner/test-repo", code_host=code_host)
    issues = tracker.list_open()
    assert len(issues) == 2
    assert issues[0].number == 10
    new_number = tracker.create("New bug", "Description", labels=["bug"])
    assert new_number is not None
    tracker.close(10)
    assert len(code_host.created_issues) == 1
    assert 10 in code_host.closed_issues


def test_issues_failure_handling():
    code_host = MockCodeHost(soft_fail_count=3)
    tracker = IssueTracker(github_repo="test-owner/test-repo", code_host=code_host)
    assert tracker.list_open() == []
    assert tracker.create("Test", "body") is None


# ---- edge cases ----


def test_create_issue_with_labels(code_host):
    tracker = IssueTracker(github_repo="test-owner/test-repo", code_host=code_host)
    result = tracker.create("Bug report", "Details here",
                            labels=["bug", "priority:high"])
    assert result is not None
    assert len(code_host.created_issues) == 1
    created = code_host.created_issues[0]
    assert "bug" in created["labels"]
    assert "priority:high" in created["labels"]


def test_create_issue_url_parse_failure():
    """Test error handling when CodeHost returns None (unparseable output)."""
    mock_host = MagicMock()
    mock_host.create_issue.return_value = None
    tracker = IssueTracker(github_repo="test-owner/test-repo", code_host=mock_host)
    result = tracker.create("Test", "body")
    assert result is None
    mock_host.create_issue.assert_called_once_with("Test", "body", labels=None)


def test_list_issues_json_parse_failure():
    """Test error handling when CodeHost returns None (invalid JSON)."""
    mock_host = MagicMock()
    mock_host.list_issues.return_value = None
    tracker = IssueTracker(github_repo="test-owner/test-repo", code_host=mock_host)
    result = tracker.list_open_or_none()
    assert result is None
    mock_host.list_issues.assert_called_once_with(
        state="open", labels=None, exclude_labels=None, page_size=None,
        title_contains=None,
    )


def test_close_issue_no_repo():
    tracker = IssueTracker(github_repo=None)
    tracker.close(42)


def test_list_issues_with_dict_labels(code_host):
    code_host.issues = [
        {"number": 1, "title": "Test issue",
         "labels": [{"name": "bug"}, {"name": "urgent"}],
         "body": "A test", "createdAt": "2025-01-01T00:00:00Z"},
        {"number": 2, "title": "Another issue",
         "labels": ["plain-string-label"],
         "body": "Another", "createdAt": "2025-01-02T00:00:00Z"},
    ]
    tracker = IssueTracker(github_repo="test-owner/test-repo", code_host=code_host)
    issues = tracker.list_open()
    assert len(issues) == 2
    assert issues[0].labels == ["bug", "urgent"]
    assert issues[1].labels == ["plain-string-label"]


def test_list_open_or_none_no_repo_returns_none():
    """list_open_or_none must return None (not []) when repo is not configured."""
    tracker = IssueTracker(github_repo=None)
    result = tracker.list_open_or_none()
    assert result is None


def test_list_open_no_repo_returns_empty_list():
    """list_open (convenience wrapper) returns [] when repo is not configured."""
    tracker = IssueTracker(github_repo=None)
    result = tracker.list_open()
    assert result == []
