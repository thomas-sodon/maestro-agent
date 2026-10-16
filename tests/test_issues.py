"""Tests for IssueTracker — CodeHost calls are mocked via protocol."""

from __future__ import annotations

from unittest.mock import MagicMock

from maestro.issues import IssueTracker
from maestro.vcs import LabelRemoval


def _tracker(host: MagicMock | None = None, repo: str | None = "owner/repo") -> IssueTracker:
    return IssueTracker(github_repo=repo, code_host=host)


# ── list_open / list_open_or_none ─────────────────────────────────────


def test_list_open_no_repo() -> None:
    tracker = IssueTracker(github_repo=None)
    assert tracker.list_open() == []


def test_list_open_or_none_no_repo_returns_none() -> None:
    """list_open_or_none returns None (not []) when repo is not configured."""
    tracker = IssueTracker(github_repo=None)
    assert tracker.list_open_or_none() is None


def test_list_open_issues() -> None:
    host = MagicMock()
    host.list_issues.return_value = [
        {
            "number": 1,
            "title": "Fix crash",
            "labels": [{"name": "bug"}],
            "body": "Details here",
            "createdAt": "2026-01-01T00:00:00Z",
        },
        {
            "number": 2,
            "title": "Slow query",
            "labels": [{"name": "performance"}, {"name": "maestro-evolution"}],
            "body": "",
            "createdAt": "2026-01-02T00:00:00Z",
        },
    ]
    tracker = _tracker(host)

    issues = tracker.list_open()

    assert len(issues) == 2
    assert issues[0].number == 1
    assert issues[0].title == "Fix crash"
    assert issues[0].labels == ["bug"]
    assert issues[1].labels == ["performance", "maestro-evolution"]
    host.list_issues.assert_called_once_with(
        state="open", labels=None, exclude_labels=None, page_size=None,
        title_contains=None,
    )


def test_list_open_with_labels() -> None:
    host = MagicMock()
    host.list_issues.return_value = []
    tracker = _tracker(host)

    tracker.list_open(labels=["bug", "security"])

    host.list_issues.assert_called_once_with(
        state="open", labels=["bug", "security"], exclude_labels=None, page_size=None,
        title_contains=None,
    )


def test_list_open_passes_page_size() -> None:
    host = MagicMock()
    host.list_issues.return_value = []
    tracker = _tracker(host)

    tracker.list_open(page_size=5)

    host.list_issues.assert_called_once_with(
        state="open", labels=None, exclude_labels=None, page_size=5,
        title_contains=None,
    )


def test_list_open_passes_exclude_labels() -> None:
    host = MagicMock()
    host.list_issues.return_value = []
    tracker = _tracker(host)

    tracker.list_open(exclude_labels=["crash"])

    host.list_issues.assert_called_once_with(
        state="open", labels=None, exclude_labels=["crash"], page_size=None,
        title_contains=None,
    )


def test_list_open_subprocess_failure() -> None:
    host = MagicMock()
    host.list_issues.return_value = None
    tracker = _tracker(host)

    issues = tracker.list_open()

    assert issues == []


def test_list_open_or_none_returns_none_on_failure() -> None:
    """list_open_or_none should return None (not empty list) on API failure."""
    host = MagicMock()
    host.list_issues.return_value = None
    tracker = _tracker(host)

    result = tracker.list_open_or_none()

    assert result is None


def test_list_open_or_none_returns_empty_on_success() -> None:
    """list_open_or_none should return empty list when no issues found."""
    host = MagicMock()
    host.list_issues.return_value = []
    tracker = _tracker(host)

    result = tracker.list_open_or_none()

    assert result == []


def test_list_open_returns_oldest_first() -> None:
    """Issues should be returned sorted oldest-first by creation date."""
    host = MagicMock()
    host.list_issues.return_value = [
        {"number": 10, "title": "Newest", "labels": [], "body": "", "createdAt": "2026-03-01T00:00:00Z"},
        {"number": 5, "title": "Middle", "labels": [], "body": "", "createdAt": "2026-02-01T00:00:00Z"},
        {"number": 1, "title": "Oldest", "labels": [], "body": "", "createdAt": "2026-01-01T00:00:00Z"},
    ]
    tracker = _tracker(host)

    issues = tracker.list_open()

    assert [i.number for i in issues] == [1, 5, 10]


def test_list_open_tiebreaks_by_number() -> None:
    """Issues with the same creation date should be ordered by number (ascending)."""
    host = MagicMock()
    host.list_issues.return_value = [
        {"number": 8, "title": "B", "labels": [], "body": "", "createdAt": "2026-01-01T00:00:00Z"},
        {"number": 3, "title": "A", "labels": [], "body": "", "createdAt": "2026-01-01T00:00:00Z"},
    ]
    tracker = _tracker(host)

    issues = tracker.list_open()

    assert [i.number for i in issues] == [3, 8]


def test_list_open_empty_created_at_sorts_last() -> None:
    """A missing created_at is treated as lowest priority, sorting after all dated issues."""
    host = MagicMock()
    host.list_issues.return_value = [
        {"number": 5, "title": "Has date", "labels": [], "body": "", "createdAt": "2026-01-01T00:00:00Z"},
        {"number": 2, "title": "No date", "labels": [], "body": "", "createdAt": ""},
    ]
    tracker = _tracker(host)

    issues = tracker.list_open()

    assert len(issues) == 2
    assert issues[0].number == 5
    assert issues[1].number == 2


# ── Malformed issue items ─────────────────────────────────────────────


def test_list_open_skips_item_missing_number() -> None:
    """Items without a 'number' key should be skipped, not crash the listing."""
    host = MagicMock()
    host.list_issues.return_value = [{"title": "test"}]
    tracker = _tracker(host)

    issues = tracker.list_open()

    assert issues == []


def test_list_open_handles_non_int_number() -> None:
    """list_open should skip items where 'number' is not an int."""
    host = MagicMock()
    host.list_issues.return_value = [
        {"number": "not-a-number", "title": "Bad", "labels": [], "body": "", "createdAt": ""},
        {"number": 42, "title": "Good", "labels": [], "body": "", "createdAt": ""},
    ]
    tracker = _tracker(host)

    issues = tracker.list_open()

    assert len(issues) == 1
    assert issues[0].number == 42


def test_list_open_skips_bool_number() -> None:
    """bool is a subclass of int — True/False should be rejected as issue numbers."""
    host = MagicMock()
    host.list_issues.return_value = [
        {
            "number": True,
            "title": "Bad issue",
            "labels": [],
            "body": "",
            "createdAt": "2026-01-01",
        },
        {
            "number": 42,
            "title": "Good issue",
            "labels": [],
            "body": "",
            "createdAt": "2026-01-02",
        },
    ]
    tracker = _tracker(host)

    issues = tracker.list_open()

    assert len(issues) == 1
    assert issues[0].number == 42


def test_list_open_skips_float_inf_number() -> None:
    """float('inf') as issue number should be skipped, not crash."""
    host = MagicMock()
    host.list_issues.return_value = [
        {"number": float("inf"), "title": "Boom", "labels": [], "body": "", "createdAt": ""},
    ]
    tracker = _tracker(host)

    result = tracker.list_open_or_none()

    assert result == []


def test_list_open_skips_float_neg_inf_number() -> None:
    """float('-inf') as issue number should be skipped, not crash."""
    host = MagicMock()
    host.list_issues.return_value = [
        {"number": float("-inf"), "title": "Boom", "labels": [], "body": "", "createdAt": ""},
    ]
    tracker = _tracker(host)

    result = tracker.list_open_or_none()

    assert result == []


def test_list_open_skips_zero_issue_number() -> None:
    """Issue number 0 is not valid and should be skipped."""
    host = MagicMock()
    host.list_issues.return_value = [
        {"number": 0, "title": "Zero", "labels": [], "body": "", "createdAt": ""},
    ]
    tracker = _tracker(host)

    result = tracker.list_open_or_none()

    assert result == []


def test_list_open_skips_negative_issue_number() -> None:
    """Negative issue numbers are not valid and should be skipped."""
    host = MagicMock()
    host.list_issues.return_value = [
        {"number": -5, "title": "Neg", "labels": [], "body": "", "createdAt": ""},
    ]
    tracker = _tracker(host)

    result = tracker.list_open_or_none()

    assert result == []


def test_list_open_skips_negative_string_issue_number() -> None:
    """Negative string issue numbers should be skipped."""
    host = MagicMock()
    host.list_issues.return_value = [
        {"number": "-3", "title": "NegStr", "labels": [], "body": "", "createdAt": ""},
    ]
    tracker = _tracker(host)

    result = tracker.list_open_or_none()

    assert result == []


# ── Label handling ────────────────────────────────────────────────────


def test_list_open_handles_null_labels() -> None:
    """GitHub API can return 'labels': null — should be treated as empty list."""
    host = MagicMock()
    host.list_issues.return_value = [
        {"number": 1, "title": "t", "labels": None, "body": "", "createdAt": ""},
    ]
    tracker = _tracker(host)

    issues = tracker.list_open()

    assert len(issues) == 1
    assert issues[0].labels == []


def test_list_open_label_missing_name_key_skips_label_not_issue() -> None:
    """A label dict missing the 'name' key should be skipped, not drop the issue."""
    host = MagicMock()
    host.list_issues.return_value = [
        {
            "number": 5,
            "title": "Good issue",
            "labels": [{"id": 99}, {"name": "bug"}],
            "body": "desc",
            "createdAt": "2026-01-01T00:00:00Z",
        },
    ]
    tracker = _tracker(host)

    issues = tracker.list_open()

    assert len(issues) == 1
    assert issues[0].number == 5
    assert issues[0].labels == ["bug"]


def test_list_open_skips_none_label_in_list() -> None:
    """A None value in the labels list should be skipped, not become 'None'."""
    host = MagicMock()
    host.list_issues.return_value = [
        {
            "number": 1,
            "title": "Test",
            "labels": [None, "bug", None],
            "body": "",
            "createdAt": "",
        },
    ]
    tracker = _tracker(host)

    result = tracker.list_open_or_none()

    assert result is not None
    assert len(result) == 1
    assert result[0].labels == ["bug"]


# ── create ────────────────────────────────────────────────────────────


def test_create_issue() -> None:
    host = MagicMock()
    host.create_issue.return_value = 42
    tracker = _tracker(host)

    number = tracker.create("Bug title", "Bug body", labels=["bug"])

    assert number == 42
    host.create_issue.assert_called_once_with("Bug title", "Bug body", labels=["bug"])


def test_create_issue_no_repo() -> None:
    tracker = IssueTracker(github_repo=None)
    assert tracker.create("title", "body") is None


def test_create_issue_failure() -> None:
    host = MagicMock()
    host.create_issue.return_value = None
    tracker = _tracker(host)

    result = tracker.create("title", "body")

    assert result is None


def test_create_empty_title_returns_none() -> None:
    """Empty title should return None without calling code_host."""
    host = MagicMock()
    tracker = _tracker(host)

    result = tracker.create("", "body text")

    assert result is None
    host.create_issue.assert_not_called()


def test_create_whitespace_title_returns_none() -> None:
    """Whitespace-only title should return None without calling code_host."""
    host = MagicMock()
    tracker = _tracker(host)

    result = tracker.create("   \t\n  ", "body text")

    assert result is None
    host.create_issue.assert_not_called()


# ── close ─────────────────────────────────────────────────────────────


def test_close_issue() -> None:
    host = MagicMock()
    tracker = _tracker(host)

    tracker.close(42)

    host.close_issue.assert_called_once_with(42)


def test_close_issue_no_repo() -> None:
    tracker = IssueTracker(github_repo=None)
    # Should not raise
    tracker.close(42)


def test_close_issue_failure() -> None:
    host = MagicMock()
    host.close_issue.side_effect = RuntimeError("api down")
    tracker = _tracker(host)

    tracker.close(42)  # exception is swallowed and logged

    host.close_issue.assert_called_once()


# ── comment ───────────────────────────────────────────────────────────


def test_comment_issue() -> None:
    host = MagicMock()
    host.comment_issue.return_value = True
    tracker = _tracker(host)

    assert tracker.comment(42, "hello") is True
    host.comment_issue.assert_called_once_with(42, "hello")


def test_comment_issue_no_repo() -> None:
    tracker = IssueTracker(github_repo=None)
    assert tracker.comment(42, "hi") is False


def test_comment_issue_empty_body() -> None:
    host = MagicMock()
    tracker = _tracker(host)
    assert tracker.comment(42, "   ") is False
    host.comment_issue.assert_not_called()


def test_comment_issue_host_failure_returns_false() -> None:
    host = MagicMock()
    host.comment_issue.return_value = False
    tracker = _tracker(host)
    assert tracker.comment(42, "body") is False


def test_comment_issue_host_exception_swallowed() -> None:
    host = MagicMock()
    host.comment_issue.side_effect = RuntimeError("boom")
    tracker = _tracker(host)
    assert tracker.comment(42, "body") is False


# ── gap-issue surface (proposal 155) ─────────────────────────────────


def test_list_closed_or_none_passes_state_and_title() -> None:
    host = MagicMock()
    host.list_issues.return_value = [
        {"number": 4, "title": "t", "labels": ["gap:maestro"], "createdAt": "2026"},
    ]
    issues = _tracker(host).list_closed_or_none(["gap:maestro"], title_contains="t")
    assert issues is not None
    assert [i.number for i in issues] == [4]
    host.list_issues.assert_called_once_with(
        state="closed", labels=["gap:maestro"], exclude_labels=None,
        page_size=None, title_contains="t",
    )


def test_list_open_title_contains_pass_through() -> None:
    host = MagicMock()
    host.list_issues.return_value = []
    _tracker(host).list_open(title_contains="phase:x")
    assert host.list_issues.call_args.kwargs["title_contains"] == "phase:x"


def test_close_returns_host_bool() -> None:
    host = MagicMock()
    host.close_issue.return_value = True
    assert _tracker(host).close(1) is True
    host.close_issue.return_value = False
    assert _tracker(host).close(1) is False
    host.close_issue.side_effect = RuntimeError("down")
    assert _tracker(host).close(1) is False


def test_reopen_label_actor_closure_delegate() -> None:
    host = MagicMock()
    host.reopen_issue.return_value = True
    host.get_issue_label_actor.return_value = "thomas"
    host.get_issue_closure.return_value = "closure"
    host.get_issue_meta.return_value = "meta"
    tracker = _tracker(host)
    assert tracker.meta(2) == "meta"
    assert tracker.reopen(2) is True
    assert tracker.label_actor(2, "gap:phase") == "thomas"
    assert tracker.closure(2) == "closure"
    host.get_issue_label_actor.assert_called_once_with(2, "gap:phase")


def test_gap_surface_unset_repo_short_circuits() -> None:
    host = MagicMock()
    tracker = _tracker(host, repo=None)
    assert tracker.list_closed_or_none() is None
    assert tracker.close(1) is False
    assert tracker.reopen(1) is False
    assert tracker.label_actor(1, "x") is None
    assert tracker.closure(1) is None
    assert tracker.meta(1) is None
    host.get_issue_meta.assert_not_called()
    host.reopen_issue.assert_not_called()
    host.get_issue_label_actor.assert_not_called()
    host.get_issue_closure.assert_not_called()


def test_label_verbs_delegate() -> None:
    host = MagicMock()
    host.add_issue_label.return_value = True
    host.remove_issue_label.return_value = False
    host.get_issue_label_removed_at.return_value = LabelRemoval("2026-10-14T00:00:00Z", "thomas")
    tracker = _tracker(host)
    assert tracker.add_label(4, "needs-decision") is True
    assert tracker.remove_label(4, "needs-decision") is False
    assert tracker.label_removed_at(4, "needs-decision") == LabelRemoval("2026-10-14T00:00:00Z", "thomas")
    host.add_issue_label.assert_called_once_with(4, "needs-decision")
    host.remove_issue_label.assert_called_once_with(4, "needs-decision")


def test_label_verbs_unset_repo_short_circuit() -> None:
    host = MagicMock()
    tracker = _tracker(host, repo=None)
    assert tracker.add_label(4, "x") is False
    assert tracker.remove_label(4, "x") is False
    assert tracker.label_removed_at(4, "x") is None
    host.add_issue_label.assert_not_called()
    host.remove_issue_label.assert_not_called()
    host.get_issue_label_removed_at.assert_not_called()
