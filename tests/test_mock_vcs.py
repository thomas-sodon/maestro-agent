"""Unit tests for MockVCS and MockCodeHost."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from maestro.ci_diagnosis import CheckRun
from maestro.vcs import IssueClosure, IssueMeta, LabelRemoval, PrMergeState, RefCheckRuns, VCSError
from tests.component.mock_vcs import MockCodeHost, MockVCS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path) -> Path:
    """Create a fresh git repo at *path* and return it."""
    path.mkdir(parents=True, exist_ok=True)

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=str(path),
            check=True, capture_output=True, text=True,
        )

    _git("init")
    _git("config", "user.email", "test@test.com")
    _git("config", "user.name", "Test")
    _git("config", "commit.gpgsign", "false")
    (path / ".gitkeep").touch()
    _git("add", ".")
    _git("commit", "-m", "init")
    return path


# ---------------------------------------------------------------------------
# MockVCS
# ---------------------------------------------------------------------------


class TestMockVCS:
    def test_local_ops_delegate_to_real_git(self, tmp_path: Path) -> None:
        workspace = _init_git_repo(tmp_path / "ws")
        vcs = MockVCS(workspace=workspace)
        assert vcs.get_current_branch() in ("master", "main")
        assert vcs.get_head_hash() is not None
        assert not vcs.has_changes()

    def test_checkout_and_commit(self, tmp_path: Path) -> None:
        workspace = _init_git_repo(tmp_path / "ws")
        vcs = MockVCS(workspace=workspace)
        vcs.checkout_new_branch("feature")
        assert vcs.get_current_branch() == "feature"
        (workspace / "new.txt").write_text("content")
        assert vcs.has_changes()
        vcs.stage_all()
        vcs.commit("add file")
        assert not vcs.has_changes()

    def test_push_recorded_in_memory(self, tmp_path: Path) -> None:
        workspace = _init_git_repo(tmp_path / "ws")
        vcs = MockVCS(workspace=workspace)
        vcs.push("main")
        vcs.push("feature", set_upstream=True)
        assert len(vcs.pushes) == 2
        assert vcs.pushes[0] == {"branch": "main", "set_upstream": False}
        assert vcs.pushes[1] == {"branch": "feature", "set_upstream": True}

    def test_push_failure(self, tmp_path: Path) -> None:
        workspace = _init_git_repo(tmp_path / "ws")
        vcs = MockVCS(workspace=workspace)
        vcs.fail_push = True
        with pytest.raises(VCSError, match="Mock push failure"):
            vcs.push("main")

    def test_delete_remote_branch_recorded_in_memory(
        self, tmp_path: Path,
    ) -> None:
        # The new VCSClient method the create_pr reconcile path calls: a
        # complete mock records the remote deletion in-memory rather than
        # raising AttributeError, so a component test can exercise the
        # delete-and-retry orphan reconciliation end-to-end (#2867).
        workspace = _init_git_repo(tmp_path / "ws")
        vcs = MockVCS(workspace=workspace)
        vcs.delete_remote_branch("maestro/evolve/e4d7194ebf77")
        assert vcs.deleted_remote_branches == ["maestro/evolve/e4d7194ebf77"]

    def test_delete_branch(self, tmp_path: Path) -> None:
        workspace = _init_git_repo(tmp_path / "ws")
        vcs = MockVCS(workspace=workspace)
        vcs.checkout_new_branch("temp")
        default = "master" if "master" in subprocess.run(
            ["git", "branch"], cwd=str(workspace),
            capture_output=True, text=True,
        ).stdout else "main"
        vcs.checkout(default)
        vcs.delete_branch("temp")

    def test_get_commits_between(self, tmp_path: Path) -> None:
        workspace = _init_git_repo(tmp_path / "ws")
        vcs = MockVCS(workspace=workspace)
        h1 = vcs.get_head_hash()
        (workspace / "a.txt").write_text("a")
        vcs.stage_all()
        vcs.commit("add a")
        h2 = vcs.get_head_hash()
        assert h1 and h2
        commits = vcs.get_commits_between(h1, h2)
        assert len(commits) == 1
        assert commits[0].subject == "add a"

    def test_get_diff(self, tmp_path: Path) -> None:
        workspace = _init_git_repo(tmp_path / "ws")
        vcs = MockVCS(workspace=workspace)
        h1 = vcs.get_head_hash()
        (workspace / "b.txt").write_text("b")
        vcs.stage_all()
        vcs.commit("add b")
        h2 = vcs.get_head_hash()
        assert h1 and h2
        diff = vcs.get_diff(h1, h2)
        assert "b.txt" in diff


# ---------------------------------------------------------------------------
# MockCodeHost
# ---------------------------------------------------------------------------


class TestMockCodeHost:
    def test_get_ref_check_runs(self) -> None:
        runs = RefCheckRuns("a" * 40, (CheckRun("tests", "failure"),))
        host = MockCodeHost(ref_check_runs={"a" * 40: runs})
        assert host.get_ref_check_runs("a" * 40) == runs
        assert host.get_ref_check_runs("b" * 40) is None
        host.soft_fail_count = 1
        assert host.get_ref_check_runs("a" * 40) is None
        assert host.ref_check_run_reads == ["a" * 40, "b" * 40, "a" * 40]

    def test_create_pr(self) -> None:
        host = MockCodeHost()
        url = host.create_pr("Title", "Body")
        assert url == "https://github.com/test-owner/test-repo/pull/1"
        assert len(host.created_prs) == 1
        assert host.created_prs[0]["draft"] is False

    def test_create_pr_draft_recorded(self) -> None:
        host = MockCodeHost()
        host.create_pr("Title", "Body", draft=True)
        assert host.created_prs[0]["draft"] is True

    def test_next_pr_url_override_does_not_increment_counter(self) -> None:
        """Using next_pr_url should not advance _next_pr_number."""
        host = MockCodeHost()
        host.next_pr_url = "https://github.com/test-owner/test-repo/pull/99"
        url1 = host.create_pr("First", "override")
        assert url1 == "https://github.com/test-owner/test-repo/pull/99"
        # Auto-generated URL should still start at 1
        url2 = host.create_pr("Second", "auto")
        assert url2 == "https://github.com/test-owner/test-repo/pull/1"

    def test_is_pr_merged(self) -> None:
        host = MockCodeHost(merged_prs={10})
        assert host.is_pr_merged(10) is True
        assert host.is_pr_merged(20) is False

    def test_is_pr_merged_strict_matches_best_effort_by_default(self) -> None:
        """With the toggle off the strict read answers exactly like its sibling,
        so every existing ``merged_prs`` setup reads the same through the
        chokepoint."""
        host = MockCodeHost(merged_prs={10})
        assert host.is_pr_merged_strict(10) is True
        assert host.is_pr_merged_strict(20) is False

    def test_is_pr_merged_strict_raises_when_toggled(self) -> None:
        """``raise_on_merge_check`` drives the host-unreachable path (#2954)
        without patching — the shape that unlocks the git-subject fallback."""
        host = MockCodeHost(merged_prs={10}, raise_on_merge_check=True)
        with pytest.raises(VCSError):
            host.is_pr_merged_strict(10)
        # The best-effort sibling is unaffected by the toggle.
        assert host.is_pr_merged(10) is True

    def test_is_pr_merged_strict_delegates_to_best_effort_sibling(self) -> None:
        """Overriding only ``is_pr_merged`` steers the strict read too.

        The strict variant delegates rather than repeating the ``merged_prs``
        membership test, so a test that stubs the best-effort read cannot leave
        the two mock oracles disagreeing about the same PR.
        """
        host = MockCodeHost(merged_prs=set())
        host.is_pr_merged = lambda pr_number: pr_number == 42  # type: ignore[method-assign]
        assert host.is_pr_merged_strict(42) is True
        assert host.is_pr_merged_strict(10) is False

    def test_is_pr_closed(self) -> None:
        host = MockCodeHost(closed_prs={7})
        assert host.is_pr_closed(7) is True
        assert host.is_pr_closed(10) is False

    def test_get_pr_base_branch_default(self) -> None:
        host = MockCodeHost()
        assert host.get_pr_base_branch(99) == "main"

    def test_get_pr_review_bodies(self) -> None:
        host = MockCodeHost(pr_review_bodies={5: ["review one", "review two"]})
        assert host.get_pr_review_bodies(5) == ["review one", "review two"]
        assert host.get_pr_review_bodies(6) == []

    def test_get_pr_comment_bodies(self) -> None:
        host = MockCodeHost(pr_comment_bodies={5: ["ack comment"]})
        assert host.get_pr_comment_bodies(5) == ["ack comment"]
        assert host.get_pr_comment_bodies(6) == []

    def test_get_pr_base_branch_configured(self) -> None:
        host = MockCodeHost(pr_base_branches={5: "develop"})
        assert host.get_pr_base_branch(5) == "develop"
        assert host.get_pr_base_branch(6) == "main"

    def test_list_issues(self) -> None:
        host = MockCodeHost(issues=[{"number": 1, "title": "Bug"}])
        result = host.list_issues()
        assert result is not None
        assert len(result) == 1

    def test_list_issues_filters_by_state(self) -> None:
        host = MockCodeHost(issues=[
            {"number": 1, "title": "Open", "state": "open", "labels": []},
            {"number": 2, "title": "Closed", "state": "closed", "labels": []},
        ])
        open_issues = host.list_issues(state="open")
        assert open_issues is not None
        assert len(open_issues) == 1
        assert open_issues[0]["number"] == 1

    def test_list_issues_filters_by_labels(self) -> None:
        host = MockCodeHost(issues=[
            {"number": 1, "title": "Bug", "state": "open", "labels": ["bug"]},
            {"number": 2, "title": "Feature", "state": "open", "labels": ["enhancement"]},
        ])
        bug_issues = host.list_issues(labels=["bug"])
        assert bug_issues is not None
        assert len(bug_issues) == 1
        assert bug_issues[0]["number"] == 1

    def test_list_issues_failure(self) -> None:
        host = MockCodeHost(soft_fail_count=1)
        assert host.list_issues() is None

    def test_create_issue(self) -> None:
        host = MockCodeHost()
        num = host.create_issue("Bug", "Description", labels=["bug"])
        assert num == 1
        assert len(host.created_issues) == 1
        assert host.created_issues[0]["labels"] == ["bug"]

    def test_close_issue(self) -> None:
        host = MockCodeHost(issues=[{"number": 1, "title": "Bug"}])
        host.close_issue(1)
        assert host.closed_issues == [1]
        assert len(host.issues) == 1
        assert host.issues[0]["state"] == "closed"

    def test_close_issue_visible_in_closed_list(self) -> None:
        """Closed issues appear in list_issues(state='closed')."""
        host = MockCodeHost(issues=[
            {"number": 1, "title": "Bug", "state": "open", "labels": []},
            {"number": 2, "title": "Feature", "state": "open", "labels": []},
        ])
        host.close_issue(1)
        open_issues = host.list_issues(state="open")
        closed_issues = host.list_issues(state="closed")
        assert open_issues is not None
        assert closed_issues is not None
        assert len(open_issues) == 1
        assert open_issues[0]["number"] == 2
        assert len(closed_issues) == 1
        assert closed_issues[0]["number"] == 1

    def test_close_and_reopen_issue_return_bool(self) -> None:
        host = MockCodeHost(issues=[{"number": 1, "title": "Bug", "state": "open"}])
        assert host.close_issue(1) is True
        assert host.reopen_issue(1) is True
        assert host.reopened_issues == [1]
        assert host.issues[0]["state"] == "open"
        assert host.reopen_issue(9) is True
        failing = MockCodeHost(soft_fail_count=2)
        assert failing.close_issue(1) is False
        assert failing.reopen_issue(1) is False

    def test_issue_label_actor_and_closure(self) -> None:
        closure = IssueClosure(actor="thomas", via_commit=False)
        host = MockCodeHost(
            issue_label_actors={(3, "lbl"): "thomas"}, issue_closures={3: closure},
        )
        assert host.get_issue_label_actor(3, "lbl") == "thomas"
        assert host.get_issue_label_actor(3, "other") is None
        assert host.get_issue_closure(3) == closure
        assert host.get_issue_closure(4) == IssueClosure("", via_commit=False, closed=False)
        assert MockCodeHost(issue_closures={5: None}).get_issue_closure(5) is None

    def test_issue_meta(self) -> None:
        host = MockCodeHost(
            issues=[{"number": 3, "title": "t", "labels": ["a"], "state": "closed"},
                    {"number": 5},
                    {"number": 6, "labels": [{"name": "gap:maestro"}, "b"]}],
            gone_issues={7},
        )
        assert host.get_issue_meta(6) == IssueMeta("open", "", ("gap:maestro", "b"))
        assert host.get_issue_meta(3) == IssueMeta("closed", "t", ("a",))
        assert host.get_issue_meta(5) == IssueMeta("open", "", ())
        assert host.get_issue_meta(7) == IssueMeta("gone")
        assert host.get_issue_meta(9) is None

    def test_list_issues_title_contains(self) -> None:
        host = MockCodeHost(issues=[
            {"number": 1, "title": "phase:launch — x", "labels": []},
            {"number": 2, "title": "other", "labels": []},
        ])
        found = host.list_issues(title_contains="phase:launch")
        assert found is not None
        assert [i["number"] for i in found] == [1]

    def test_pr_diff(self) -> None:
        host = MockCodeHost(pr_diffs={5: "diff content"})
        assert host.get_pr_diff(5) == "diff content"
        assert host.get_pr_diff(99) == ""

    def test_pr_info(self) -> None:
        host = MockCodeHost(pr_info={5: {"title": "PR", "body": "text"}})
        assert host.get_pr_info(5)["title"] == "PR"
        assert host.get_pr_info(99) == {"title": "", "body": ""}

    def test_review_threads(self) -> None:
        threads = [{"id": "RT1", "isResolved": False}]
        host = MockCodeHost(review_threads={5: threads})
        assert host.get_pr_review_threads(5) == threads
        assert host.get_pr_review_threads(99) == []

    def test_reply_to_review_comment(self) -> None:
        host = MockCodeHost()
        host.reply_to_review_comment(5, 100, "Fixed")
        assert len(host.reply_calls) == 1
        assert host.reply_calls[0]["body"] == "Fixed"

    def test_resolve_review_thread(self) -> None:
        host = MockCodeHost()
        host.resolve_review_thread("PRRT_1")
        host.resolve_review_thread("PRRT_2")
        assert host.resolve_calls == ["PRRT_1", "PRRT_2"]

    def test_post_pr_review(self) -> None:
        host = MockCodeHost()
        host.post_pr_review(5, {"body": "LGTM", "event": "APPROVE"})
        assert len(host.created_reviews) == 1
        assert host.created_reviews[0]["event"] == "APPROVE"

    def test_create_pr_returns_unique_urls(self) -> None:
        host = MockCodeHost()
        url1 = host.create_pr("first", "body1")
        url2 = host.create_pr("second", "body2")
        assert url1 != url2
        assert "/pull/1" in url1
        assert "/pull/2" in url2

    def test_create_pr_next_pr_url_cleared_after_use(self) -> None:
        host = MockCodeHost(next_pr_url="https://github.com/o/r/pull/99")
        url1 = host.create_pr("first", "body1")
        url2 = host.create_pr("second", "body2")
        assert url1 == "https://github.com/o/r/pull/99"
        assert url2 != url1  # auto-incremented, not the override

    def test_next_pr_number_not_advanced_by_custom_url(self) -> None:
        """Using next_pr_url does not advance _next_pr_number."""
        host = MockCodeHost(next_pr_url="https://github.com/o/r/pull/99")
        host.create_pr("custom", "body")  # consumes next_pr_url, counter unchanged
        url = host.create_pr("auto", "body")  # should be /pull/1 (counter not bumped)
        assert "/pull/1" in url

    def test_close_issue_soft_fails(self) -> None:
        host = MockCodeHost(soft_fail_count=1)
        host.close_issue(42)  # silently no-ops (no exception)
        assert host.closed_issues == []
        # After soft_fail_count exhausted, close works
        host.close_issue(42)
        assert host.closed_issues == [42]

    def test_list_issues_soft_fails(self) -> None:
        host = MockCodeHost(issues=[{"number": 1}], soft_fail_count=1)
        assert host.list_issues() is None
        assert host.list_issues() == [{"number": 1}]

    def test_create_issue_soft_fails(self) -> None:
        host = MockCodeHost(soft_fail_count=1)
        assert host.create_issue("title", "body") is None
        assert host.create_issue("title", "body") == 1

    def test_list_issues_labels_use_and_semantics(self) -> None:
        """Multiple labels filter requires ALL labels to match (AND, not OR)."""
        host = MockCodeHost(issues=[
            {"number": 1, "title": "Both", "state": "open",
             "labels": ["bug", "urgent"]},
            {"number": 2, "title": "Bug only", "state": "open",
             "labels": ["bug"]},
            {"number": 3, "title": "Urgent only", "state": "open",
             "labels": ["urgent"]},
        ])
        result = host.list_issues(labels=["bug", "urgent"])
        assert result is not None
        assert len(result) == 1
        assert result[0]["number"] == 1

    def test_list_issues_labels_dict_format(self) -> None:
        """Labels stored as dicts (with 'name' key) are handled correctly."""
        host = MockCodeHost(issues=[
            {"number": 1, "title": "Bug", "state": "open",
             "labels": [{"name": "bug"}, {"name": "urgent"}]},
            {"number": 2, "title": "Feature", "state": "open",
             "labels": [{"name": "enhancement"}]},
        ])
        result = host.list_issues(labels=["bug"])
        assert result is not None
        assert len(result) == 1
        assert result[0]["number"] == 1

    def test_list_issues_excludes_labels(self) -> None:
        host = MockCodeHost(issues=[
            {"number": 1, "title": "Crash", "state": "open", "labels": ["crash"]},
            {"number": 2, "title": "Bug", "state": "open", "labels": ["bug"]},
            {"number": 3, "title": "Both", "state": "open", "labels": ["bug", "crash"]},
        ])
        result = host.list_issues(exclude_labels=["crash"])
        assert result is not None
        assert len(result) == 1
        assert result[0]["number"] == 2

    def test_list_issues_excludes_labels_dict_format(self) -> None:
        host = MockCodeHost(issues=[
            {"number": 1, "title": "Crash", "state": "open",
             "labels": [{"name": "crash"}]},
            {"number": 2, "title": "Bug", "state": "open",
             "labels": [{"name": "bug"}]},
        ])
        result = host.list_issues(exclude_labels=["crash"])
        assert result is not None
        assert len(result) == 1
        assert result[0]["number"] == 2

    def test_list_issues_page_size(self) -> None:
        host = MockCodeHost(issues=[
            {"number": 1, "title": "First", "state": "open", "labels": []},
            {"number": 2, "title": "Second", "state": "open", "labels": []},
            {"number": 3, "title": "Third", "state": "open", "labels": []},
        ])
        # page_size truncates to the first N filtered issues.
        result = host.list_issues(page_size=1)
        assert result is not None
        assert len(result) == 1
        assert result[0]["number"] == 1
        # page_size=None returns every issue (the other arm of the slice branch).
        unbounded = host.list_issues(page_size=None)
        assert unbounded is not None
        assert len(unbounded) == 3
        assert [i["number"] for i in unbounded] == [1, 2, 3]

    def test_fail_count_decrements(self) -> None:
        host = MockCodeHost(fail_count=1)
        with pytest.raises(VCSError, match="Mock failure"):
            host.create_pr("t", "b")
        url = host.create_pr("t", "b")
        assert url is not None

    def test_comment_issue_recorded(self) -> None:
        host = MockCodeHost()
        assert host.comment_issue(42, "hello") is True
        assert host.reply_calls == [{"issue_number": 42, "body": "hello"}]

    def test_comment_issue_soft_fails(self) -> None:
        host = MockCodeHost(soft_fail_count=1)
        assert host.comment_issue(1, "x") is False
        # Counter decremented — next call succeeds.
        assert host.comment_issue(1, "y") is True

    def test_get_open_evolution_pr_states_filters_by_prefix(self) -> None:
        state = PrMergeState(
            number=1, head_ref="maestro/evolve/abc", title="t", body="b",
            state="OPEN", mergeable="MERGEABLE", merge_state_status="CLEAN",
            review_decision="", created_at="", updated_at="", url="u",
        )
        other = PrMergeState(
            number=2, head_ref="feature/x", title="t", body="b",
            state="OPEN", mergeable="MERGEABLE", merge_state_status="CLEAN",
            review_decision="", created_at="", updated_at="", url="u",
        )
        host = MockCodeHost(pr_merge_states={
            state.head_ref: state, other.head_ref: other,
        })
        result = host.get_open_evolution_pr_states("maestro/evolve/")
        assert set(result) == {"maestro/evolve/abc"}

    def test_get_open_evolution_pr_states_skips_closed(self) -> None:
        closed = PrMergeState(
            number=1, head_ref="maestro/evolve/closed", title="t", body="b",
            state="CLOSED", mergeable="", merge_state_status="",
            review_decision="", created_at="", updated_at="", url="",
        )
        host = MockCodeHost(pr_merge_states={closed.head_ref: closed})
        assert host.get_open_evolution_pr_states("maestro/evolve/") == {}

    def test_get_open_evolution_pr_states_soft_fail(self) -> None:
        host = MockCodeHost(soft_fail_count=1)
        assert host.get_open_evolution_pr_states("maestro/evolve/") == {}

    def test_get_open_evolution_pr_states_strict_returns_matching(self) -> None:
        # strict=True only changes the failure branch; the success path still
        # returns the matching open PR states (mirrors SubprocessCodeHost). This
        # is the reconcile-path call shape — get_open_evolution_pr_states(
        # "maestro/evolve/", strict=True) — so the mock must accept the kwarg.
        state = PrMergeState(
            number=1, head_ref="maestro/evolve/abc", title="t", body="b",
            state="OPEN", mergeable="MERGEABLE", merge_state_status="CLEAN",
            review_decision="", created_at="", updated_at="", url="u",
        )
        host = MockCodeHost(pr_merge_states={state.head_ref: state})
        result = host.get_open_evolution_pr_states(
            "maestro/evolve/", strict=True,
        )
        assert set(result) == {"maestro/evolve/abc"}

    def test_get_open_evolution_pr_states_strict_raises_on_soft_fail(
        self,
    ) -> None:
        # strict=True must fail loud rather than fail open: a soft host failure
        # raises VCSError instead of returning {} — the create_pr orphan
        # reconcile guard treats an empty dict as positive 'no open PR'
        # confirmation, so a transient failure must never be swallowed (#2813).
        host = MockCodeHost(soft_fail_count=1)
        with pytest.raises(
            VCSError, match="Cannot confirm open evolution PR state",
        ):
            host.get_open_evolution_pr_states("maestro/evolve/", strict=True)

    @pytest.mark.parametrize("pr_state", ["OPEN", "MERGED"])
    def test_find_evolution_pr_for_branch_returns_live(
        self, pr_state: str,
    ) -> None:
        state = PrMergeState(
            number=1, head_ref="maestro/evolve/abc", title="t", body="b",
            state=pr_state, mergeable="", merge_state_status="",
            review_decision="", created_at="", updated_at="", url="u",
        )
        host = MockCodeHost(pr_merge_states={state.head_ref: state})
        assert host.find_evolution_pr_for_branch("maestro/evolve/abc") is state

    def test_find_evolution_pr_for_branch_ignores_closed(self) -> None:
        closed = PrMergeState(
            number=1, head_ref="maestro/evolve/abc", title="t", body="b",
            state="CLOSED", mergeable="", merge_state_status="",
            review_decision="", created_at="", updated_at="", url="u",
        )
        host = MockCodeHost(pr_merge_states={closed.head_ref: closed})
        assert host.find_evolution_pr_for_branch("maestro/evolve/abc") is None

    def test_find_evolution_pr_for_branch_soft_fail_raises(self) -> None:
        host = MockCodeHost(soft_fail_count=1)
        with pytest.raises(VCSError, match="Cannot look up PR for branch"):
            host.find_evolution_pr_for_branch("maestro/evolve/abc")


class TestMockVCSTending:
    def test_fetch_records_call(self, tmp_path: Path) -> None:
        workspace = _init_git_repo(tmp_path / "ws")
        vcs = MockVCS(workspace=workspace)
        vcs.fetch()
        vcs.fetch("upstream", ref="main")
        vcs.fetch("origin", prune=True)
        assert vcs.fetches == [
            {"remote": "origin", "ref": None, "prune": False},
            {"remote": "upstream", "ref": "main", "prune": False},
            {"remote": "origin", "ref": None, "prune": True},
        ]

    def test_fetch_failure(self, tmp_path: Path) -> None:
        workspace = _init_git_repo(tmp_path / "ws")
        vcs = MockVCS(workspace=workspace)
        vcs.fail_fetch = True
        with pytest.raises(VCSError, match="Mock fetch failure"):
            vcs.fetch()

    def test_rev_parse_round_trip(self, tmp_path: Path) -> None:
        workspace = _init_git_repo(tmp_path / "ws")
        vcs = MockVCS(workspace=workspace)
        head = vcs.get_head_hash()
        assert head is not None
        current = vcs.get_current_branch()
        assert vcs.rev_parse(current) == head
        assert vcs.rev_parse("does-not-exist") is None

    def test_commits_ahead_behind_is_zero_on_same_ref(
        self, tmp_path: Path,
    ) -> None:
        workspace = _init_git_repo(tmp_path / "ws")
        vcs = MockVCS(workspace=workspace)
        current = vcs.get_current_branch()
        assert vcs.commits_ahead_behind(current, current) == (0, 0)

    def test_list_remote_branches_empty_without_remote(
        self, tmp_path: Path,
    ) -> None:
        workspace = _init_git_repo(tmp_path / "ws")
        vcs = MockVCS(workspace=workspace)
        assert vcs.list_remote_branches() == []

    def test_merge_from_clean_and_conflict(self, tmp_path: Path) -> None:
        workspace = _init_git_repo(tmp_path / "ws")
        vcs = MockVCS(workspace=workspace)
        default = vcs.get_current_branch()
        # Create a conflicting branch.
        vcs.checkout_new_branch("feat")
        (workspace / "a.txt").write_text("feat\n")
        vcs.stage_all()
        vcs.commit("feat change")
        vcs.checkout(default)
        (workspace / "a.txt").write_text("main\n")
        vcs.stage_all()
        vcs.commit("main change")

        merge = vcs.merge_from("feat")
        assert merge.success is False
        assert merge.conflicted_files == ["a.txt"]

        # Re-render the in-progress conflict in diff3 style: the ``|||||||`` base
        # section (absent from default-style markers) must now be present.
        assert "|||||||" not in (workspace / "a.txt").read_text()
        vcs.render_conflict_diff3("a.txt")
        assert "|||||||" in (workspace / "a.txt").read_text()

        vcs.abort_merge()
        assert not vcs.has_changes()

    def test_reset_hard_restores_tree(self, tmp_path: Path) -> None:
        workspace = _init_git_repo(tmp_path / "ws")
        vcs = MockVCS(workspace=workspace)
        head = vcs.get_head_hash()
        assert head is not None
        (workspace / "dirty.txt").write_text("dirty")
        vcs.stage_all()
        vcs.commit("dirty commit")
        vcs.reset_hard(head)
        assert vcs.get_head_hash() == head


def test_mock_issue_label_verbs() -> None:
    host = MockCodeHost(issues=[{"number": 1, "title": "t", "labels": ["gap:maestro"]}])
    assert host.add_issue_label(1, "needs-decision") is True
    assert host.add_issue_label(1, "needs-decision") is True
    assert host.issues[0]["labels"] == ["gap:maestro", "needs-decision"]
    assert host.get_issue_label_removed_at(1, "needs-decision") is None
    host.label_removed_stamp = "2026-10-14T01:00:00Z"
    assert host.remove_issue_label(1, "needs-decision") is True
    assert host.remove_issue_label(1, "needs-decision") is True
    assert host.issues[0]["labels"] == ["gap:maestro"]
    assert host.get_issue_label_removed_at(1, "needs-decision") == LabelRemoval("2026-10-14T01:00:00Z", "maestro-bot")
    assert host.add_issue_label(1, "needs-decision") is True
    assert host.get_issue_label_removed_at(1, "needs-decision") is None
    assert host.label_edits[0] == ("add", 1, "needs-decision")
    assert host.add_issue_label(9, "x") is False
    assert host.remove_issue_label(9, "x") is False
    host.label_removed_stamp = "2026-10-14T02:00:00Z"
    assert host.remove_issue_label(1, "absent") is True
    assert host.get_issue_label_removed_at(1, "absent") is None
    host.label_edit_fail = True
    assert host.add_issue_label(1, "x") is False
    assert host.remove_issue_label(1, "x") is False
