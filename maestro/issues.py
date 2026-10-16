"""GitHub Issues integration — list, create, close issues for work tracking."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from maestro.vcs import SubprocessCodeHost

if TYPE_CHECKING:
    from maestro.vcs import CodeHost, IssueClosure, IssueMeta, LabelRemoval

logger = logging.getLogger("maestro")


@dataclass
class Issue:
    number: int
    title: str
    labels: list[str]
    body: str
    created_at: str


class IssueTracker:
    """Thin wrapper around a :class:`CodeHost` for GitHub Issues."""

    def __init__(
        self,
        github_repo: str | None = None,
        timeout: int = 30,
        code_host: CodeHost | None = None,
    ) -> None:
        self._repo = github_repo
        self._host = code_host or SubprocessCodeHost(github_repo, timeout=timeout)

    def list_open(
        self,
        labels: list[str] | None = None,
        *,
        exclude_labels: list[str] | None = None,
        page_size: int | None = None,
        title_contains: str | None = None,
    ) -> list[Issue]:
        """List open issues. Returns empty list when repo is not configured or on failure."""
        result = self.list_open_or_none(
            labels, exclude_labels=exclude_labels, page_size=page_size,
            title_contains=title_contains,
        )
        return result if result is not None else []

    def list_open_or_none(
        self,
        labels: list[str] | None = None,
        *,
        exclude_labels: list[str] | None = None,
        page_size: int | None = None,
        title_contains: str | None = None,
    ) -> list[Issue] | None:
        """List open issues. Returns None when repo is not configured or on failure."""
        return self._list_or_none(
            "open", labels, exclude_labels=exclude_labels, page_size=page_size,
            title_contains=title_contains,
        )

    def list_closed_or_none(
        self,
        labels: list[str] | None = None,
        *,
        page_size: int | None = None,
        title_contains: str | None = None,
    ) -> list[Issue] | None:
        """List closed issues. Returns None when repo is not configured or on failure."""
        return self._list_or_none(
            "closed", labels, page_size=page_size, title_contains=title_contains,
        )

    def _list_or_none(
        self,
        state: str,
        labels: list[str] | None,
        *,
        exclude_labels: list[str] | None = None,
        page_size: int | None = None,
        title_contains: str | None = None,
    ) -> list[Issue] | None:
        if not self._repo:
            return None

        raw: list[dict[str, Any]] | None = self._host.list_issues(
            state=state, labels=labels, exclude_labels=exclude_labels,
            page_size=page_size, title_contains=title_contains,
        )
        if raw is None:
            return None

        issues: list[Issue] = []
        for item in raw:
            try:
                raw_number = item["number"]
                if isinstance(raw_number, bool):
                    raise TypeError("bool is not a valid issue number")
                if not isinstance(raw_number, int):
                    raw_number = int(raw_number)
                if raw_number <= 0:
                    raise ValueError("issue number must be positive")
                issue_labels = []
                for lbl in item.get("labels") or []:
                    if isinstance(lbl, dict):
                        name = lbl.get("name")
                        if name is not None:
                            issue_labels.append(str(name))
                    elif lbl is not None:
                        issue_labels.append(str(lbl))
                issues.append(Issue(
                    number=raw_number,
                    title=item.get("title") or "",
                    labels=issue_labels,
                    body=item.get("body") or "",
                    created_at=item.get("createdAt") or "",
                ))
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        issues.sort(key=lambda i: (i.created_at or "9999-99-99T99:99:99Z", i.number))
        return issues

    def create(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> int | None:
        """Create an issue. Returns issue number, or None on failure."""
        if not self._repo:
            return None
        if not title or not title.strip():
            logger.warning("Refusing to create issue with empty title")
            return None
        return self._host.create_issue(title, body, labels=labels)

    def close(self, number: int) -> bool:
        """Close an issue; ``True`` on success. Logs a warning on failure."""
        if not self._repo:
            return False
        try:
            return bool(self._host.close_issue(number))
        except Exception:
            logger.warning("Failed to close issue #%d", number, exc_info=True)
            return False

    def reopen(self, number: int) -> bool:
        """Reopen a closed issue; ``True`` on success, ``False`` when unset."""
        if not self._repo:
            return False
        return bool(self._host.reopen_issue(number))

    def label_actor(self, number: int, label: str) -> str | None:
        """The login that last applied *label*; ``None`` when unset or unknown."""
        if not self._repo:
            return None
        return self._host.get_issue_label_actor(number, label)

    def add_label(self, number: int, label: str) -> bool:
        """Add *label* to *number*; ``False`` when unset or on failure."""
        if not self._repo:
            return False
        return bool(self._host.add_issue_label(number, label))

    def remove_label(self, number: int, label: str) -> bool:
        """Remove *label* from *number*; ``False`` when unset or on failure."""
        if not self._repo:
            return False
        return bool(self._host.remove_issue_label(number, label))

    def label_removed_at(self, number: int, label: str) -> LabelRemoval | None:
        """When and by whom *label* was last removed, if that is its latest event."""
        if not self._repo:
            return None
        return self._host.get_issue_label_removed_at(number, label)

    def meta(self, number: int) -> IssueMeta | None:
        """State, title and labels of *number* in one read; ``None`` when unset or unreadable."""
        if not self._repo:
            return None
        return self._host.get_issue_meta(number)

    def closure(self, number: int) -> IssueClosure | None:
        """Who closed *number* last; ``None`` when unset or unknown."""
        if not self._repo:
            return None
        return self._host.get_issue_closure(number)

    def comment(self, number: int, body: str) -> bool:
        """Post a comment on an issue.

        Returns ``True`` on success, ``False`` if the repo is not configured
        or the comment could not be posted. Never raises.
        """
        if not self._repo:
            return False
        if not body or not body.strip():
            logger.warning("Refusing to post empty comment on issue #%d", number)
            return False
        try:
            return bool(self._host.comment_issue(number, body))
        except Exception:
            logger.warning("Failed to comment on issue #%d", number, exc_info=True)
            return False
