"""In-memory implementations of ``VCSClient`` and ``CodeHost`` protocols.

``MockVCS`` delegates local git operations to a real ``SubprocessVCS``
bound to the test's isolated ``workspace_dir`` (a fresh git repo inside
``pytest tmp_path``).  Remote operations (``push``) are recorded
in-memory and never touch the network.

``MockCodeHost`` is a fully in-memory implementation of every
``CodeHost`` method.  Tests configure it by setting attributes directly
(e.g. ``code_host.issues = [...]``) instead of calling HTTP admin
endpoints on a Docker mock server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from maestro.vcs import (
    CommitInfo,
    IssueClosure,
    IssueMeta,
    LabelRemoval,
    MergeResult,
    PrMergeState,
    RefCheckRuns,
    SubprocessVCS,
    VCSError,
)

# ---------------------------------------------------------------------------
# MockVCS
# ---------------------------------------------------------------------------


@dataclass
class MockVCS:
    """``VCSClient`` that delegates local ops to real git, records remote ops.

    Isolation: each test gets its own *workspace* (fresh git repo in
    ``pytest tmp_path``).  Local operations run against this isolated
    temp repo.  Remote operations are recorded in-memory.
    """

    workspace: Path
    pushes: list[dict[str, Any]] = field(default_factory=list)
    fetches: list[dict[str, str | None]] = field(default_factory=list)
    deleted_remote_branches: list[str] = field(default_factory=list)
    fail_push: bool = False
    fail_fetch: bool = False

    def __post_init__(self) -> None:
        self._real = SubprocessVCS(
            self.workspace, timeout=10, quick_timeout=5, push_timeout=10,
        )

    # -- Local ops (delegate to real git) -----------------------------------

    def get_current_branch(self) -> str:
        return self._real.get_current_branch()

    def get_head_hash(self) -> str | None:
        return self._real.get_head_hash()

    def checkout_new_branch(self, branch: str) -> None:
        self._real.checkout_new_branch(branch)

    def checkout(self, branch: str) -> None:
        self._real.checkout(branch)

    def delete_branch(self, branch: str) -> None:
        self._real.delete_branch(branch)

    def has_changes(self) -> bool:
        return self._real.has_changes()

    def porcelain_status(self) -> str:
        return self._real.porcelain_status()

    def discard_changes(self) -> None:
        self._real.discard_changes()

    def stage_all(self) -> None:
        self._real.stage_all()

    def commit(self, message: str) -> None:
        self._real.commit(message)

    def amend_commit(self) -> None:
        self._real.amend_commit()

    def get_commits_between(
        self, old_hash: str, new_hash: str,
    ) -> list[CommitInfo]:
        return self._real.get_commits_between(old_hash, new_hash)

    def get_diff_stat(self, old_hash: str, new_hash: str) -> str:
        return self._real.get_diff_stat(old_hash, new_hash)

    def get_diff(
        self, old_hash: str, new_hash: str, *, max_chars: int = 50_000,
    ) -> str:
        return self._real.get_diff(old_hash, new_hash, max_chars=max_chars)

    def get_file_log_p(
        self, rev_range: str, file_path: str, *, max_chars: int = 8000,
    ) -> str:
        return self._real.get_file_log_p(
            rev_range, file_path, max_chars=max_chars,
        )

    # -- Remote ops (in-memory) ---------------------------------------------

    def push(self, branch: str, *, set_upstream: bool = False) -> None:
        if self.fail_push:
            raise VCSError("Mock push failure")
        self.pushes.append({"branch": branch, "set_upstream": set_upstream})

    def delete_remote_branch(self, branch: str) -> None:
        # In-memory recorder for the scoped orphan disposal that
        # ``Evolution.create_pr`` performs when a fresh evolution branch's
        # first push is rejected non-fast-forward against a PR-less orphan
        # (#2867). Recording the deletion keeps ``MockVCS`` a complete
        # ``VCSClient`` so a component test can drive that reconcile path
        # end-to-end instead of hitting ``AttributeError``.
        self.deleted_remote_branches.append(branch)

    def fetch(
        self, remote: str = "origin", ref: str | None = None,
        *, prune: bool = False,
    ) -> None:
        if self.fail_fetch:
            raise VCSError("Mock fetch failure")
        self.fetches.append({"remote": remote, "ref": ref, "prune": prune})

    def merge_from(
        self, source_ref: str, *, message: str | None = None,
    ) -> MergeResult:
        return self._real.merge_from(source_ref, message=message)

    def abort_merge(self) -> None:
        self._real.abort_merge()

    def render_conflict_diff3(self, path: str) -> None:
        self._real.render_conflict_diff3(path)

    def merge_tree_conflicts(self, ref_a: str, ref_b: str) -> bool:
        return self._real.merge_tree_conflicts(ref_a, ref_b)

    def merge_tree_text(self, ours_ref: str, theirs_ref: str) -> str:
        return self._real.merge_tree_text(ours_ref, theirs_ref)

    def reset_hard(self, ref: str) -> None:
        self._real.reset_hard(ref)

    def reset_branch_to(self, branch: str, ref: str) -> None:
        self._real.reset_branch_to(branch, ref)

    def list_remote_branches(self, remote: str = "origin") -> list[str]:
        return self._real.list_remote_branches(remote)

    def commits_ahead_behind(self, a: str, b: str) -> tuple[int, int]:
        return self._real.commits_ahead_behind(a, b)

    def commits_not_on_remote(self, ref: str, remote: str = "origin") -> int:
        return self._real.commits_not_on_remote(ref, remote)

    def non_merge_commits_ahead(self, ref: str, base: str) -> int | None:
        return self._real.non_merge_commits_ahead(ref, base)

    def lap_commits_since(self, ref: str, bases: tuple[str, ...]) -> int | None:
        return self._real.lap_commits_since(ref, bases)

    def rev_parse(self, ref: str) -> str | None:
        return self._real.rev_parse(ref)

    def merge_base(self, ref_a: str, ref_b: str) -> str | None:
        return self._real.merge_base(ref_a, ref_b)

    def changed_paths(self, base: str, head: str) -> tuple[str, ...]:
        return self._real.changed_paths(base, head)

    def tracked_paths(self) -> frozenset[str]:
        return self._real.tracked_paths()


# ---------------------------------------------------------------------------
# MockCodeHost
# ---------------------------------------------------------------------------


@dataclass
class MockCodeHost:
    """Fully in-memory ``CodeHost`` implementation for testing."""

    # Pre-configured state (set by tests)
    issues: list[dict[str, Any]] = field(default_factory=list)
    merged_prs: set[int] = field(default_factory=set)
    # PRs whose host ``mergedAt`` is non-null but which are NOT the branch head's
    # linear ancestor (the squash-merge shape). Members read ``has_merged_at``
    # True while a caller may still hold ``is_pr_merged`` False, exercising the
    # ``is_pr_settled`` squash-merge override.
    squash_merged_prs: set[int] = field(default_factory=set)
    closed_prs: set[int] = field(default_factory=set)
    issue_states: dict[int, str] = field(default_factory=dict)
    pr_diffs: dict[int, str] = field(default_factory=dict)
    pr_info: dict[int, dict[str, str]] = field(default_factory=dict)
    pr_base_branches: dict[int, str] = field(default_factory=dict)
    pr_merge_commits: dict[int, str] = field(default_factory=dict)
    pr_check_conclusions: dict[int, str] = field(default_factory=dict)
    pr_failing_check_names: dict[int, list[str]] = field(default_factory=dict)
    pr_run_logs: dict[int, str] = field(default_factory=dict)
    #: ``sha -> RefCheckRuns`` served by ``get_ref_check_runs`` (``None`` when absent).
    ref_check_runs: dict[str, RefCheckRuns] = field(default_factory=dict)
    review_threads: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    pr_review_bodies: dict[int, list[str]] = field(default_factory=dict)
    pr_comment_bodies: dict[int, list[str]] = field(default_factory=dict)
    # Keyed by ``head_ref`` to mirror the SubprocessCodeHost contract.
    pr_merge_states: dict[str, PrMergeState] = field(default_factory=dict)
    next_pr_url: str | None = None
    # Owner/repo introspection knobs for proposals 030-033.
    mock_owner: str = "test-owner"
    existing_repos: set[str] = field(default_factory=set)
    resolve_owner_fail: bool = False
    # Counter: when > 0, the next ``create_public_repo`` call raises VCSError
    # and decrements. Lets tests simulate a transient ``gh repo create`` fail.
    create_public_repo_fail_count: int = 0
    # Hard failure counter: _maybe_fail() raises VCSError (for create_pr,
    # post_pr_review).
    fail_count: int = 0
    # Soft failure counter: _maybe_fail_soft() returns True (for list_issues,
    # create_issue, close_issue — these return None / silently no-op on failure,
    # matching the CodeHost protocol contract).
    soft_fail_count: int = 0
    # When True, the fail-loud ``is_pr_merged_strict`` raises VCSError — the
    # host-unreachable shape that unlocks the merge-truth chokepoint's
    # git-subject fallback (#2954). Off by default so the strict oracle answers
    # exactly like its best-effort sibling and every ``merged_prs`` setup keeps
    # reading through the chokepoint unchanged.
    raise_on_merge_check: bool = False

    # Recorded actions (asserted by tests)
    created_prs: list[dict[str, Any]] = field(default_factory=list)
    created_reviews: list[dict[str, Any]] = field(default_factory=list)
    created_issues: list[dict[str, Any]] = field(default_factory=list)
    closed_issues: list[int] = field(default_factory=list)
    reopened_issues: list[int] = field(default_factory=list)
    #: ``(number, label) -> login`` the latest ``labeled`` event names.
    issue_label_actors: dict[tuple[int, str], str] = field(default_factory=dict)
    issue_closures: dict[int, IssueClosure | None] = field(default_factory=dict)
    #: ``(number, label) -> created_at`` of the newest ``unlabeled`` event,
    #: kept only while that is the label's latest event.
    issue_label_removed_at: dict[tuple[int, str], LabelRemoval] = field(default_factory=dict)
    #: ``(verb, number, label)`` for each label add/remove that succeeded.
    label_edits: list[tuple[str, int, str]] = field(default_factory=list)
    #: The ``created_at`` a successful ``remove_issue_label`` stamps.
    label_removed_stamp: str = "2026-01-01T00:00:00Z"
    label_editor_login: str = "maestro-bot"
    #: When True, ``add_issue_label`` / ``remove_issue_label`` fail.
    label_edit_fail: bool = False
    gone_issues: set[int] = field(default_factory=set)
    reply_calls: list[dict[str, Any]] = field(default_factory=list)
    ref_check_run_reads: list[str] = field(default_factory=list)
    resolve_calls: list[str] = field(default_factory=list)
    created_public_repos: list[dict[str, str]] = field(default_factory=list)

    _next_issue_number: int = field(default=1, repr=False)
    _next_pr_number: int = field(default=1, repr=False)

    # -- Helpers ------------------------------------------------------------

    def _maybe_fail(self) -> None:
        """Raise VCSError if fail_count > 0 (hard failure)."""
        if self.fail_count > 0:
            self.fail_count -= 1
            raise VCSError("Mock failure")

    def _maybe_fail_soft(self) -> bool:
        """Return True if soft_fail_count > 0 (soft failure — caller returns None)."""
        if self.soft_fail_count > 0:
            self.soft_fail_count -= 1
            return True
        return False

    # -- Must-succeed methods -----------------------------------------------

    def create_pr(
        self, title: str, body: str, *, draft: bool = False,
    ) -> str:
        self._maybe_fail()
        if self.next_pr_url is not None:
            url = self.next_pr_url
            self.next_pr_url = None
        else:
            url = f"https://github.com/test-owner/test-repo/pull/{self._next_pr_number}"
            self._next_pr_number += 1
        pr: dict[str, Any] = {
            "title": title, "body": body,
            "html_url": url, "draft": draft,
        }
        self.created_prs.append(pr)
        return url

    def post_pr_review(
        self, pr_number: int, payload: dict[str, Any],
    ) -> None:
        self._maybe_fail()
        review: dict[str, Any] = {
            "id": len(self.created_reviews) + 1,
            "pr_number": pr_number,
            "body": payload.get("body", ""),
            "event": payload.get("event", "COMMENT"),
            "comments": payload.get("comments", []),
        }
        self.created_reviews.append(review)

    # -- Best-effort methods (plus the one fail-loud merge read) ------------

    def is_pr_merged(self, pr_number: int) -> bool:
        return pr_number in self.merged_prs

    def is_pr_merged_strict(self, pr_number: int) -> bool:
        """Fail-loud sibling of ``is_pr_merged`` (see the CodeHost protocol).

        Raises when ``raise_on_merge_check`` is set, so a test can drive the
        host-unreachable path without patching; otherwise it *delegates* to the
        best-effort sibling above rather than repeating the ``merged_prs``
        membership test, so a test that overrides only ``is_pr_merged`` on the
        mock steers this read to the same verdict (the drift the conftest
        delegation shim avoids for the ``MagicMock`` host).
        """
        if self.raise_on_merge_check:
            raise VCSError("Mock host unreachable")
        return self.is_pr_merged(pr_number)

    def has_merged_at(self, pr_number: int) -> bool:
        return pr_number in self.merged_prs or pr_number in self.squash_merged_prs

    def is_pr_closed(self, pr_number: int) -> bool:
        return pr_number in self.closed_prs

    def get_issue_state(self, number: int) -> str | None:
        return self.issue_states.get(number)

    def get_pr_base_branch(self, pr_number: int) -> str:
        return self.pr_base_branches.get(pr_number, "main")

    def get_commit_sha_of_pr(self, pr_number: int) -> str:
        return self.pr_merge_commits.get(pr_number, "")

    def get_pr_check_conclusion(self, pr_number: int) -> str:
        return self.pr_check_conclusions.get(pr_number, "")

    def get_pr_failing_check_names(self, pr_number: int) -> list[str]:
        return self.pr_failing_check_names.get(pr_number, [])

    def get_ref_check_runs(self, sha: str) -> RefCheckRuns | None:
        self.ref_check_run_reads.append(sha)
        if self._maybe_fail_soft():
            return None
        return self.ref_check_runs.get(sha)

    def read_run_log_failed(self, pr_number: int) -> str:
        return self.pr_run_logs.get(pr_number, "")

    def get_pr_diff(self, pr_number: int) -> str:
        return self.pr_diffs.get(pr_number, "")

    def get_pr_info(self, pr_number: int) -> dict[str, str]:
        return self.pr_info.get(pr_number, {"title": "", "body": ""})

    def get_pr_review_threads(
        self, pr_number: int,
    ) -> list[dict[str, Any]]:
        return self.review_threads.get(pr_number, [])

    def get_pr_review_bodies(self, pr_number: int) -> list[str]:
        return self.pr_review_bodies.get(pr_number, [])

    def get_pr_comment_bodies(self, pr_number: int) -> list[str]:
        return self.pr_comment_bodies.get(pr_number, [])

    def get_pr_review_threads_strict(
        self, pr_number: int,
    ) -> list[dict[str, Any]]:
        return self.get_pr_review_threads(pr_number)

    def get_pr_review_bodies_strict(self, pr_number: int) -> list[str]:
        return self.get_pr_review_bodies(pr_number)

    def get_pr_comment_bodies_strict(self, pr_number: int) -> list[str]:
        return self.get_pr_comment_bodies(pr_number)

    def reply_to_review_comment(
        self, pr_number: int, comment_id: int, body: str,
    ) -> None:
        self.reply_calls.append({
            "pr_number": pr_number,
            "comment_id": comment_id,
            "body": body,
        })

    def resolve_review_thread(self, thread_id: str) -> None:
        self.resolve_calls.append(thread_id)

    def list_issues(
        self,
        *,
        state: str = "open",
        labels: list[str] | None = None,
        exclude_labels: list[str] | None = None,
        page_size: int | None = None,
        title_contains: str | None = None,
    ) -> list[dict[str, Any]] | None:
        if self._maybe_fail_soft():
            return None
        exclude_set = set(exclude_labels) if exclude_labels else set()
        filtered = [
            i for i in self.issues
            if i.get("state", "open") == state
            and (
                labels is None
                or all(
                    requested in {
                        (lbl["name"] if isinstance(lbl, dict) else lbl)
                        for lbl in i.get("labels", [])
                    }
                    for requested in labels
                )
            )
            and not exclude_set.intersection(
                (lbl["name"] if isinstance(lbl, dict) else lbl)
                for lbl in i.get("labels", [])
            )
            and (
                title_contains is None
                or title_contains in str(i.get("title", ""))
            )
        ]
        if page_size is not None:
            filtered = filtered[:page_size]
        return filtered

    def create_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> int | None:
        if self._maybe_fail_soft():
            return None
        number = self._next_issue_number
        self._next_issue_number += 1
        issue: dict[str, Any] = {
            "number": number, "title": title, "body": body,
            "labels": labels or [],
            "html_url": f"https://github.com/test-owner/test-repo/issues/{number}",
        }
        self.created_issues.append(issue)
        return number

    def close_issue(self, number: int) -> bool:
        if self._maybe_fail_soft():
            return False
        self.closed_issues.append(number)
        for issue in self.issues:
            if issue.get("number") == number:
                issue["state"] = "closed"
                break
        return True

    def reopen_issue(self, number: int) -> bool:
        if self._maybe_fail_soft():
            return False
        self.reopened_issues.append(number)
        for issue in self.issues:
            if issue.get("number") == number:
                issue["state"] = "open"
                break
        return True

    def get_issue_label_actor(self, number: int, label: str) -> str | None:
        return self.issue_label_actors.get((number, label))

    def _issue_labels(self, number: int) -> list[Any] | None:
        for issue in self.issues:
            if issue.get("number") == number:
                labels: list[Any] = issue.setdefault("labels", [])
                return labels
        return None

    def add_issue_label(self, number: int, label: str) -> bool:
        if self.label_edit_fail:
            return False
        labels = self._issue_labels(number)
        if labels is None:
            return False
        if label not in labels:
            labels.append(label)
        self.issue_label_removed_at.pop((number, label), None)
        self.label_edits.append(("add", number, label))
        return True

    def remove_issue_label(self, number: int, label: str) -> bool:
        if self.label_edit_fail:
            return False
        labels = self._issue_labels(number)
        if labels is None:
            return False
        if label in labels:
            # GitHub writes an ``unlabeled`` event only for a label present.
            labels.remove(label)
            self.issue_label_removed_at[(number, label)] = LabelRemoval(
                self.label_removed_stamp, self.label_editor_login,
            )
        self.label_edits.append(("remove", number, label))
        return True

    def get_issue_label_removed_at(self, number: int, label: str) -> LabelRemoval | None:
        return self.issue_label_removed_at.get((number, label))

    def get_issue_closure(self, number: int) -> IssueClosure | None:
        return self.issue_closures.get(
            number, IssueClosure("", via_commit=False, closed=False),
        )

    def get_issue_meta(self, number: int) -> IssueMeta | None:
        if number in self.gone_issues:
            return IssueMeta("gone")
        for issue in self.issues:
            if issue.get("number") == number:
                return IssueMeta(
                    state=str(issue.get("state", "open")),
                    title=str(issue.get("title", "")),
                    labels=tuple(
                        lbl["name"] if isinstance(lbl, dict) else lbl
                        for lbl in issue.get("labels") or ()
                    ),
                )
        return None

    def comment_issue(self, number: int, body: str) -> bool:
        if self._maybe_fail_soft():
            return False
        self.reply_calls.append(
            {"issue_number": number, "body": body},
        )
        return True

    def get_open_evolution_pr_states(
        self, branch_prefix: str, *, strict: bool = False,
    ) -> dict[str, PrMergeState]:
        # Mirror the ``CodeHost`` contract (vcs.py): ``strict=True`` must fail
        # loud, never fail open. A soft host failure therefore raises VCSError
        # instead of returning ``{}`` — an empty dict a caller reading absence
        # as positive 'no open PR' confirmation would be defeated by. The
        # ``create_pr`` orphan-reconcile guard passes ``strict=True`` for
        # exactly this reason (#2813); without the kwarg a component test
        # driving that reconcile path end-to-end would raise ``TypeError``
        # rather than exercise the intended delete-and-retry (#2867).
        if self._maybe_fail_soft():
            if strict:
                raise VCSError(
                    "Cannot confirm open evolution PR state: mock host failure",
                )
            return {}
        return {
            head_ref: state
            for head_ref, state in self.pr_merge_states.items()
            if head_ref.startswith(branch_prefix)
            and state.state == "OPEN"
        }

    def find_evolution_pr_for_branch(self, branch: str) -> PrMergeState | None:
        if self._maybe_fail_soft():
            raise VCSError("Cannot look up PR for branch: mock host failure")
        state = self.pr_merge_states.get(branch)
        if state is not None and state.state in ("OPEN", "MERGED"):
            return state
        return None

    def resolve_owner(self) -> str:
        if self.resolve_owner_fail:
            return ""
        return self.mock_owner

    def repo_exists(self, owner: str, name: str) -> bool:
        return f"{owner}/{name}" in self.existing_repos

    def create_public_repo(
        self, name: str, description: str, *, owner: str = "",
    ) -> str:
        if self.create_public_repo_fail_count > 0:
            self.create_public_repo_fail_count -= 1
            raise VCSError("Mock create_public_repo failure")
        resolved = owner or self.resolve_owner()
        if not resolved:
            raise VCSError("create_public_repo: owner not resolvable")
        url = f"https://github.com/{resolved}/{name}"
        self.created_public_repos.append({
            "owner": resolved, "name": name, "description": description,
            "url": url,
        })
        self.existing_repos.add(f"{resolved}/{name}")
        return url
