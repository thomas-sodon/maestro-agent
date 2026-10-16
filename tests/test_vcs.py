"""Tests for SubprocessVCS (git operations) and evolution_branch helper."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from maestro.vcs import (
    _CHANGED_PATHS_MAX,
    _COMMIT_SEP,
    CommitInfo,
    MergeResult,
    SubprocessVCS,
    VCSError,
    evolution_branch,
    open_evolution_plan_ids,
    task_branch,
)

FAKE_DIR = Path("/fake/workspace")
HASH_A = "aaa1111111111111111111111111111111111111"
HASH_B = "bbb2222222222222222222222222222222222222"


# =====================================================================
# evolution_branch helper
# =====================================================================


def test_open_evolution_plan_ids_inverts_evolution_branch() -> None:
    """Only ``maestro/evolve/*`` heads map back to their plan ids."""
    heads = {evolution_branch("abc123"): object(), "feat/other": object()}
    assert open_evolution_plan_ids(lambda: heads) == {"abc123"}


def test_open_evolution_plan_ids_unknown_on_listing_failure() -> None:
    """A failed listing is unknown (``None``), never an empty open set."""
    def _fail() -> dict[str, object]:
        raise VCSError("gh down")

    assert open_evolution_plan_ids(_fail) is None


# =====================================================================
# task_branch helper
# =====================================================================


def test_task_branch() -> None:
    assert task_branch("abc123def456") == "maestro/task/abc123def456"


def test_task_branch_preserves_id_verbatim() -> None:
    """task_branch returns the task_id verbatim inside the branch name."""
    import uuid
    tid = uuid.uuid4().hex[:12]
    assert task_branch(tid) == f"maestro/task/{tid}"


# =====================================================================
# evolution_branch helper
# =====================================================================


def test_evolution_branch() -> None:
    assert evolution_branch("plan42") == "maestro/evolve/plan42"


def test_current_plan_ids_unaffected_by_sanitization() -> None:
    """uuid4().hex[:12] plan IDs contain only hex chars and are returned verbatim."""
    import uuid
    plan_id = uuid.uuid4().hex[:12]
    assert evolution_branch(plan_id) == f"maestro/evolve/{plan_id}"


def test_evolution_branch_no_sanitization_at_lookup_time() -> None:
    """evolution_branch must return f"maestro/evolve/{plan_id}" for any input.

    Sanitisation happens at plan-ID creation time; lookup must use the stored
    plan_id verbatim so callers always target the branch that was actually
    created.
    """
    # A plan_id with special characters — the branch name must NOT be sanitized.
    plan_id = "abc~def^123"
    assert evolution_branch(plan_id) == f"maestro/evolve/{plan_id}"

    # Plain alphanumeric IDs are unchanged either way.
    assert evolution_branch("abc123def456") == "maestro/evolve/abc123def456"

    # Empty-ish but valid hex segment
    assert evolution_branch("000000000000") == "maestro/evolve/000000000000"


def test_sanitize_branch_segment_removed() -> None:
    """_sanitize_branch_segment must not exist in maestro/vcs.py (dead code removed).

    This test confirms that the dead-code function _sanitize_branch_segment has
    been removed from the production module.  plan_ids are always uuid4().hex[:12]
    (already hex-safe), so sanitisation is never needed.
    """
    import ast
    import pathlib

    maestro_dir = pathlib.Path(__file__).parent.parent / "maestro"
    prod_files = list(maestro_dir.rglob("*.py"))

    definitions = []
    for fpath in prod_files:
        source = fpath.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_sanitize_branch_segment":
                definitions.append(str(fpath))

    assert len(definitions) == 0, (
        f"_sanitize_branch_segment is still defined in production files: "
        f"{definitions}. It is dead code and should be removed."
    )


# =====================================================================
# SubprocessVCS — must-succeed methods
# =====================================================================


def test_get_current_branch() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="main\n",
        )
        result = vcs.get_current_branch()

    assert result == "main"
    cmd = mock_run.call_args[0][0]
    assert cmd == ["git", "rev-parse", "--abbrev-ref", "HEAD"]


def test_get_current_branch_failure() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        with pytest.raises(VCSError):
            vcs.get_current_branch()


def test_get_current_branch_detached_head() -> None:
    """On detached HEAD, git rev-parse --abbrev-ref HEAD returns 'HEAD'.
    This is not a valid branch name and should raise VCSError."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="HEAD\n", stderr="",
        )
        with pytest.raises(VCSError, match="detached"):
            vcs.get_current_branch()


def test_checkout_new_branch() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="",
        )
        vcs.checkout_new_branch("feature/x")

    cmd = mock_run.call_args[0][0]
    assert cmd == ["git", "checkout", "-b", "feature/x"]


def test_checkout_new_branch_exists() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(128, "git")
        with pytest.raises(VCSError):
            vcs.checkout_new_branch("existing-branch")


def test_checkout() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="",
        )
        vcs.checkout("main")

    cmd = mock_run.call_args[0][0]
    assert cmd == ["git", "checkout", "main"]


def test_checkout_failure() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        with pytest.raises(VCSError):
            vcs.checkout("nonexistent")


def test_delete_branch() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="",
        )
        vcs.delete_branch("old-branch")

    cmd = mock_run.call_args[0][0]
    assert cmd == ["git", "branch", "-D", "old-branch"]


def test_delete_branch_failure() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        with pytest.raises(VCSError):
            vcs.delete_branch("nonexistent")


def test_has_changes_true() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=" M file.py\n",
        )
        assert vcs.has_changes() is True


def test_has_changes_false() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="",
        )
        assert vcs.has_changes() is False


def test_has_changes_failure() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        with pytest.raises(VCSError):
            vcs.has_changes()


def test_porcelain_status_sorts_lines() -> None:
    """``porcelain_status`` returns the raw porcelain output, sorted
    line-wise so unrelated ordering jitter does not look like a delta."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=" M zeta.py\n M alpha.py\n?? beta.py\n",
        )
        assert vcs.porcelain_status() == " M alpha.py\n M zeta.py\n?? beta.py"


def test_porcelain_status_empty_tree() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="",
        )
        assert vcs.porcelain_status() == ""


def test_stage_all() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="",
        )
        vcs.stage_all()

    cmd = mock_run.call_args[0][0]
    assert cmd == ["git", "add", "-A"]


def test_commit() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="",
        )
        vcs.commit("fix: something")

    cmd = mock_run.call_args[0][0]
    assert cmd == ["git", "commit", "-m", "fix: something"]


def test_commit_failure() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        with pytest.raises(VCSError):
            vcs.commit("msg")


def test_push_with_upstream() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="",
        )
        vcs.push("feature/x", set_upstream=True)

    cmd = mock_run.call_args[0][0]
    assert cmd == ["git", "push", "-u", "origin", "feature/x"]


def test_push_without_upstream() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="",
        )
        vcs.push("feature/x")

    cmd = mock_run.call_args[0][0]
    assert cmd == ["git", "push", "origin", "feature/x"]


def test_push_failure() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        with pytest.raises(VCSError):
            vcs.push("feature/x")


def test_push_uses_push_timeout() -> None:
    vcs = SubprocessVCS(FAKE_DIR, push_timeout=999)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="",
        )
        vcs.push("br")

    assert mock_run.call_args[1]["timeout"] == 999


def test_delete_remote_branch() -> None:
    vcs = SubprocessVCS(FAKE_DIR, push_timeout=999)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="",
        )
        vcs.delete_remote_branch("maestro/evolve/abc123")

    cmd = mock_run.call_args[0][0]
    assert cmd == ["git", "push", "origin", "--delete", "maestro/evolve/abc123"]
    # Uses the push timeout, mirroring ``push`` (network round-trip).
    assert mock_run.call_args[1]["timeout"] == 999


def test_delete_remote_branch_refuses_non_evolve_branch() -> None:
    """The guard refuses anything outside ``maestro/evolve/`` and never
    reaches the subprocess, so a caller mistake can't delete ``main``."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        with pytest.raises(VCSError, match="refusing to delete remote branch"):
            vcs.delete_remote_branch("main")
        mock_run.assert_not_called()


def test_delete_remote_branch_failure() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        with pytest.raises(VCSError):
            vcs.delete_remote_branch("maestro/evolve/abc123")


def test_timeout_raises_vcs_error() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired("git", 30)
        with pytest.raises(VCSError):
            vcs.stage_all()


def test_oserror_raises_vcs_error() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = FileNotFoundError("git not found")
        with pytest.raises(VCSError):
            vcs.stage_all()


def test_subprocess_vcs_run_timeout_zero_honored() -> None:
    """Passing timeout=0 must use 0, not fall back to default timeout."""
    vcs = SubprocessVCS(FAKE_DIR, timeout=30)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="",
        )
        vcs._run(["git", "status"], timeout=0)

    assert mock_run.call_args[1]["timeout"] == 0


# =====================================================================
# SubprocessVCS — best-effort methods
# =====================================================================


def test_get_head_hash() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f"  {HASH_A}\n",
        )
        result = vcs.get_head_hash()

    assert result == HASH_A


def test_get_head_hash_failure() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        result = vcs.get_head_hash()

    assert result is None


def test_get_head_hash_empty() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="  \n",
        )
        result = vcs.get_head_hash()

    assert result is None


def test_get_commits_between() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    log_out = (
        f"{HASH_A}\nAdd feature\nThomas\n{_COMMIT_SEP}\n"
        f"{HASH_B}\nFix bug\nMaestro\n{_COMMIT_SEP}"
    )
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=log_out,
        )
        commits = vcs.get_commits_between("old", "new")

    assert len(commits) == 2
    # git log returns newest-first; get_commits_between reverses to chronological order
    assert commits[0] == CommitInfo(hash=HASH_B, subject="Fix bug", author="Maestro")
    assert commits[1] == CommitInfo(hash=HASH_A, subject="Add feature", author="Thomas")


def test_get_commits_between_chronological_order() -> None:
    """get_commits_between must return commits oldest-first (chronological order).

    git log emits newest-first; the implementation reverses this so callers
    relying on the public API contract receive commits in oldest-first order.
    """
    hash_old = "aaa0000000000000000000000000000000000000"
    hash_mid = "bbb0000000000000000000000000000000000000"
    hash_new = "ccc0000000000000000000000000000000000000"
    # Simulate git log newest-first output: new, mid, old
    log_out = (
        f"{hash_new}\nNewest commit\nAlice\n{_COMMIT_SEP}\n"
        f"{hash_mid}\nMiddle commit\nBob\n{_COMMIT_SEP}\n"
        f"{hash_old}\nOldest commit\nCarol\n{_COMMIT_SEP}"
    )
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=log_out,
        )
        commits = vcs.get_commits_between("base", "tip")

    assert len(commits) == 3
    # Oldest commit must be first (index 0)
    assert commits[0].hash == hash_old
    assert commits[0].subject == "Oldest commit"
    # Middle commit second
    assert commits[1].hash == hash_mid
    # Newest commit must be last (index -1)
    assert commits[2].hash == hash_new
    assert commits[2].subject == "Newest commit"


def test_get_commits_between_empty() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="",
        )
        commits = vcs.get_commits_between("a", "a")

    assert commits == []


def test_get_commits_between_empty_subject() -> None:
    """Commits with empty subjects must not be silently dropped."""
    vcs = SubprocessVCS(FAKE_DIR)
    # Git format %H%n%s%n%an produces: hash\n\nauthor when subject is empty
    log_out = f"{HASH_A}\n\nThomas\n{_COMMIT_SEP}\n"
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=log_out,
        )
        commits = vcs.get_commits_between("old", "new")

    assert len(commits) == 1
    assert commits[0].hash == HASH_A
    assert commits[0].subject == ""
    assert commits[0].author == "Thomas"


def test_get_commits_between_failure() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        commits = vcs.get_commits_between("a", "b")

    assert commits == []


def test_get_commits_between_old_separator_in_subject() -> None:
    """Commit subjects containing '---commit---' must parse correctly.

    The old separator was a plain text string that could appear in real
    commit messages. The fix uses \\x01 (ASCII SOH) which git never
    includes in commit subject output.
    """
    # The fix: _COMMIT_SEP must NOT be a plain-text string that could
    # appear in commit messages.
    assert _COMMIT_SEP == "\x01", (
        f"_COMMIT_SEP should be ASCII SOH (\\x01), got {_COMMIT_SEP!r}"
    )

    vcs = SubprocessVCS(workspace="/tmp")

    # A subject with the old problematic separator text — should now
    # be handled correctly since the real separator is \x01.
    old_sep = "---commit---"
    fake_output = (
        f"abc123\n"
        f"Fix {old_sep} in output\n"
        f"Alice\n"
        f"{_COMMIT_SEP}\n"
        f"def456\n"
        f"Normal commit\n"
        f"Bob\n"
        f"{_COMMIT_SEP}\n"
    )

    with patch.object(vcs, "_run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout=fake_output, stderr="",
        )
        commits = vcs.get_commits_between("old", "new")

    assert len(commits) == 2, f"Expected 2 commits, got {len(commits)}: {commits}"
    # Chronological order (reversed from git log newest-first)
    assert commits[0].hash == "def456"
    assert commits[1].hash == "abc123"
    assert commits[1].subject == f"Fix {old_sep} in output"


def test_get_commits_between_invalid_hashes() -> None:
    """Non-hex characters in hashes should return empty list."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(128, "git")
        result = vcs.get_commits_between("not-a-hash", "abc123")
        assert result == []

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(128, "git")
        result = vcs.get_commits_between("ab", "cd")
        assert result == []


def test_get_diff_stat() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    stat = " file.py | 5 ++---\n 1 file changed"
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=stat,
        )
        result = vcs.get_diff_stat("a", "b")

    assert "file.py" in result


def test_get_diff_stat_failure() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        result = vcs.get_diff_stat("a", "b")

    assert result == ""


def test_get_commits_between_empty_hash() -> None:
    """Empty hash strings must return [] without calling subprocess."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        assert vcs.get_commits_between("", "abc123") == []
        assert vcs.get_commits_between("abc123", "") == []
        assert vcs.get_commits_between("", "") == []
    mock_run.assert_not_called()


def test_get_diff_stat_empty_hash() -> None:
    """Empty hash strings must return '' without calling subprocess."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        assert vcs.get_diff_stat("", "abc123") == ""
        assert vcs.get_diff_stat("abc123", "") == ""
        assert vcs.get_diff_stat("", "") == ""
    mock_run.assert_not_called()


def test_get_diff() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    diff_output = "diff --git a/foo.py b/foo.py\n+new line"
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=diff_output,
        )
        result = vcs.get_diff("a", "b")

    assert "foo.py" in result


def test_get_diff_failure() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        result = vcs.get_diff("a", "b")

    assert result == ""


def test_get_diff_empty_hash() -> None:
    """Empty hash strings must return '' without calling subprocess."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        assert vcs.get_diff("", "abc123") == ""
        assert vcs.get_diff("abc123", "") == ""
        assert vcs.get_diff("", "") == ""
    mock_run.assert_not_called()


def test_get_diff_truncates_large_output() -> None:
    """Diffs over the default cap are head+tail truncated with an accurate
    marker, not silently head-sliced: both ends of the diff survive and the
    marker reports the true omitted count."""
    vcs = SubprocessVCS(FAKE_DIR)
    # Distinct head/tail sentinels prove head+tail truncation (not a head
    # slice, which would drop "TAIL").
    large_diff = "HEAD" + ("x" * 59_992) + "TAIL"  # 60,000 chars total
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=large_diff,
        )
        result = vcs.get_diff("a", "b")

    # True omitted count = original length minus the two retained halves.
    expected_omitted = len(large_diff) - 2 * (50_000 // 2)
    assert f"[... {expected_omitted} chars truncated by get_diff cap ...]" in result
    # Both ends of the original diff survive the truncation.
    assert result.startswith("HEAD")
    assert result.endswith("TAIL")
    # Bounded near the cap (halves + marker), far below the 60K original.
    assert len(result) < len(large_diff)
    assert len(result) <= 50_000 + 100


def test_get_diff_uncapped_when_max_chars_zero() -> None:
    """``max_chars=0`` disables the cap: the full diff is returned unmodified,
    letting a downstream consumer be the sole truncation authority."""
    vcs = SubprocessVCS(FAKE_DIR)
    large_diff = "y" * 60_000
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=large_diff,
        )
        result = vcs.get_diff("a", "b", max_chars=0)

    assert result == large_diff
    assert len(result) == 60_000


def test_get_file_log_p_returns_log_under_cap() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    log_output = "commit abc\nMessage\ndiff --git a/x b/x\n+x"
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=log_output,
        )
        result = vcs.get_file_log_p("a..b", "x")
    assert "commit abc" in result
    cmd = mock_run.call_args.args[0]
    assert cmd[:3] == ["git", "log", "-p"]
    assert cmd[-2:] == ["--", "x"]


def test_get_file_log_p_truncates_over_max_chars() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    huge = "z" * 20_000
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=huge,
        )
        result = vcs.get_file_log_p("a..b", "x", max_chars=1000)
    assert len(result) > 1000  # cap + marker
    assert result.endswith("[truncated]")


def test_get_file_log_p_empty_inputs_skip_subprocess() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        assert vcs.get_file_log_p("", "x") == ""
        assert vcs.get_file_log_p("a..b", "") == ""
    mock_run.assert_not_called()


def test_get_file_log_p_failure_returns_empty() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        result = vcs.get_file_log_p("a..b", "x")
    assert result == ""


def test_cwd_passed_to_subprocess() -> None:
    """Both implementations pass workspace as cwd."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="main\n",
        )
        vcs.get_current_branch()

    assert mock_run.call_args[1]["cwd"] == str(FAKE_DIR)


def test_quick_timeout_used_for_head_hash() -> None:
    vcs = SubprocessVCS(FAKE_DIR, quick_timeout=5)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=HASH_A + "\n",
        )
        vcs.get_head_hash()

    assert mock_run.call_args[1]["timeout"] == 5


# =====================================================================
# SubprocessVCS — discard_changes
# =====================================================================


def test_discard_changes_runs_three_commands() -> None:
    """discard_changes runs reset, checkout, and clean in order."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="",
        )
        vcs.discard_changes()

    assert mock_run.call_count == 3
    cmds = [call[0][0] for call in mock_run.call_args_list]
    assert cmds[0] == ["git", "reset", "HEAD"]
    assert cmds[1] == ["git", "checkout", "--", "."]
    assert cmds[2] == ["git", "clean", "-fd"]


def test_discard_changes_clears_index_when_reset_fails() -> None:
    """When ``git reset HEAD`` fails (unborn/orphaned branch with no resolvable
    HEAD), discard_changes clears the index HEAD-independently *before* ``git
    clean -fd`` so a staged-but-uncommitted new file is still removed.

    Regression lock for #201: without the fallback, ``checkout -- .`` only
    restores tracked paths and ``clean -fd`` skips index-tracked files, so a
    staged new file would survive the cleanup.
    """
    vcs = SubprocessVCS(FAKE_DIR)

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["git", "reset", "HEAD"]:
            raise VCSError("fatal: ambiguous argument 'HEAD': unknown revision")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="")

    with patch.object(vcs, "_run", side_effect=fake_run) as mock_run:
        vcs.discard_changes()  # must not raise

    cmds = [call[0][0] for call in mock_run.call_args_list]
    fallback = ["git", "rm", "-r", "--cached", "--ignore-unmatch", "--", "."]
    assert fallback in cmds, "index-clear fallback must run when reset fails"
    assert cmds.index(fallback) < cmds.index(["git", "clean", "-fd"]), (
        "fallback must run before clean so the now-untracked files are swept"
    )


def test_discard_changes_tolerates_individual_failures() -> None:
    """If one command fails, the remaining commands still run.

    When ``git reset HEAD`` fails the HEAD-independent index-clear fallback
    fires before ``clean``, so the failure path issues four commands, not
    three.
    """
    vcs = SubprocessVCS(FAKE_DIR)
    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="")
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = [
            subprocess.CalledProcessError(1, "git"),  # reset fails
            ok,  # checkout succeeds
            ok,  # index-clear fallback succeeds
            ok,  # clean succeeds
        ]
        vcs.discard_changes()  # must not raise

    assert mock_run.call_count == 4


def test_discard_changes_never_raises() -> None:
    """discard_changes is best-effort — never raises even if all steps fail.

    Every step fails here, so ``git reset HEAD`` fails too and the
    index-clear fallback fires before ``clean``: four commands, none of which
    may propagate.
    """
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        vcs.discard_changes()  # must not raise

    assert mock_run.call_count == 4


def test_discard_changes_does_not_propagate_unexpected_exception() -> None:
    """discard_changes must not raise even when _run raises a non-VCSError.

    This test was written first (failing) to prove the broken promise: before
    the fix, only VCSError was caught, so an unexpected RuntimeError from _run
    would propagate out of discard_changes, violating the 'Best-effort — does
    not raise' contract documented in both the protocol and the docstring.
    """
    from unittest.mock import patch as _patch

    vcs = SubprocessVCS(FAKE_DIR)
    with _patch.object(vcs, "_run", side_effect=RuntimeError("unexpected OS error")):
        # Must not raise despite receiving a non-VCSError exception
        vcs.discard_changes()


# =====================================================================
# Tending-PR helpers: fetch, merge_from, abort_merge, reset_hard,
# list_remote_branches, commits_ahead_behind, rev_parse
# =====================================================================


def test_fetch_default_remote() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="")
        vcs.fetch()

    cmd = mock_run.call_args[0][0]
    assert cmd == ["git", "fetch", "origin"]


def test_fetch_with_ref() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="")
        vcs.fetch("upstream", ref="main")

    cmd = mock_run.call_args[0][0]
    assert cmd == ["git", "fetch", "upstream", "main"]


def test_fetch_with_prune() -> None:
    # ``prune=True`` must append ``--prune`` so remote-tracking refs for
    # branches deleted on the remote are dropped — the tend reconcile relies on
    # this so a stale ref cannot mask an unpushed commit as "still pushed."
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="")
        vcs.fetch("origin", prune=True)

    cmd = mock_run.call_args[0][0]
    assert cmd == ["git", "fetch", "origin", "--prune"]


def test_fetch_raises_on_failure() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ), pytest.raises(VCSError):
        vcs.fetch()


def test_merge_from_clean() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="")
        result = vcs.merge_from("origin/main", message="merge")

    assert isinstance(result, MergeResult)
    assert result.success is True
    assert result.conflicted_files == []
    cmd = mock_run.call_args[0][0]
    assert cmd[:5] == ["git", "merge", "--no-ff", "--no-edit", "-m"]
    assert cmd[-1] == "origin/main"


def test_merge_from_conflict_reports_files() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:2] == ["git", "merge"]:
            raise subprocess.CalledProcessError(1, cmd, stderr="conflict")
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="a.py\nb.py\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    with patch("maestro.vcs.subprocess.run", side_effect=fake_run):
        result = vcs.merge_from("origin/main")

    assert result.success is False
    assert result.conflicted_files == ["a.py", "b.py"]


def test_merge_from_non_conflict_failure_raises() -> None:
    vcs = SubprocessVCS(FAKE_DIR)

    def fake_run(cmd: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["git", "merge"]:
            raise subprocess.CalledProcessError(128, cmd, stderr="oops")
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="")
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    with patch("maestro.vcs.subprocess.run", side_effect=fake_run), \
         pytest.raises(VCSError):
        vcs.merge_from("origin/main")


def test_abort_merge_calls_git_merge_abort() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="")
        vcs.abort_merge()

    cmd = mock_run.call_args[0][0]
    assert cmd == ["git", "merge", "--abort"]


def test_abort_merge_swallows_failure() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ):
        # Must not raise.
        vcs.abort_merge()


def test_render_conflict_diff3_calls_git_checkout() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="")
        vcs.render_conflict_diff3("tests/conflicted.py")

    cmd = mock_run.call_args[0][0]
    assert cmd == [
        "git", "checkout", "--conflict=diff3", "--",
        "tests/conflicted.py",
    ]


def test_render_conflict_diff3_swallows_failure() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ):
        # Must not raise — the caller then reads default-style markers and the
        # union resolver fails safe to None.
        vcs.render_conflict_diff3("f.py")


def test_merge_tree_conflicts_reports_conflict() -> None:
    """Exit 1 from ``git merge-tree --write-tree`` means the merge conflicts."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 1, stdout="a.py\n")
        assert vcs.merge_tree_conflicts("origin/feat", "origin/main") is True

    # In-memory probe only — no checkout/merge/abort that could touch the tree.
    assert mock_run.call_count == 1
    cmd = mock_run.call_args[0][0]
    assert cmd[:4] == ["git", "merge-tree", "--write-tree", "--name-only"]
    assert cmd[-2:] == ["origin/feat", "origin/main"]
    # ``check=False`` keeps the conflict exit (1) from raising VCSError.
    assert mock_run.call_args.kwargs["check"] is False


def test_merge_tree_conflicts_clean_returns_false() -> None:
    """Exit 0 means the branches merge cleanly — no conflict."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="")
        assert vcs.merge_tree_conflicts("origin/feat", "origin/main") is False


def test_merge_tree_conflicts_other_exit_returns_false() -> None:
    """A non-0/1 exit (e.g. an unresolvable ref, exit 128) is undeterminable, so
    the probe reports no conflict and the caller keeps its prior verdict."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            [], 128, stdout="", stderr="fatal: bad ref",
        )
        assert vcs.merge_tree_conflicts("origin/feat", "origin/main") is False


def test_merge_tree_conflicts_swallows_failure() -> None:
    """A timeout / OS-level error degrades to no-conflict (best-effort)."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.TimeoutExpired("git", 30),
    ):
        assert vcs.merge_tree_conflicts("origin/feat", "origin/main") is False


def test_merge_tree_text_returns_diff_with_markers() -> None:
    """Success path: merge-base then legacy 3-arg merge-tree, output returned."""
    vcs = SubprocessVCS(FAKE_DIR)
    diff = (
        "changed in both\n"
        "  our    100644 abc tests/conflicted.py\n"
        "@@ -1 +1,5 @@\n"
        "+<<<<<<< .our\n"
    )
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = [
            subprocess.CompletedProcess([], 0, stdout="basesha\n"),
            subprocess.CompletedProcess([], 1, stdout=diff),
        ]
        out = vcs.merge_tree_text("maestro/evolve/ep1", "origin/main")
    assert out == diff
    base_cmd = mock_run.call_args_list[0][0][0]
    assert base_cmd == [
        "git", "merge-base", "maestro/evolve/ep1", "origin/main",
    ]
    tree_cmd = mock_run.call_args_list[1][0][0]
    assert tree_cmd == [
        "git", "merge-tree", "basesha", "maestro/evolve/ep1", "origin/main",
    ]
    assert mock_run.call_args_list[1].kwargs["check"] is False


def test_merge_tree_text_merge_base_failure_returns_empty() -> None:
    """A failing ``git merge-base`` degrades to ``""`` (best-effort)."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        assert vcs.merge_tree_text("a", "b") == ""


def test_merge_tree_text_empty_base_returns_empty() -> None:
    """No common ancestor (empty merge-base) short-circuits to ``""``."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="\n")
        assert vcs.merge_tree_text("a", "b") == ""
    assert mock_run.call_count == 1  # merge-tree never ran


def test_merge_tree_text_tree_failure_returns_empty() -> None:
    """A ``git merge-tree`` subprocess failure degrades to ``""``."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = [
            subprocess.CompletedProcess([], 0, stdout="basesha\n"),
            subprocess.TimeoutExpired("git", 30),
        ]
        assert vcs.merge_tree_text("a", "b") == ""


def test_reset_hard_runs_git_reset_hard() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="")
        vcs.reset_hard("origin/main")

    cmd = mock_run.call_args[0][0]
    assert cmd == ["git", "reset", "--hard", "origin/main"]


def test_reset_hard_raises_on_failure() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ), pytest.raises(VCSError):
        vcs.reset_hard("origin/main")


def test_reset_branch_to_resets_hard_when_branch_checked_out() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = [
            subprocess.CompletedProcess([], 0, stdout="maestro/evolve/x\n"),
            subprocess.CompletedProcess([], 0, stdout=""),
        ]
        vcs.reset_branch_to("maestro/evolve/x", "origin/maestro/evolve/x")

    cmd = mock_run.call_args[0][0]
    assert cmd == ["git", "reset", "--hard", "origin/maestro/evolve/x"]


def test_reset_branch_to_force_moves_named_branch_when_not_checked_out() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.side_effect = [
            subprocess.CompletedProcess([], 0, stdout="main\n"),
            subprocess.CompletedProcess([], 0, stdout=""),
        ]
        vcs.reset_branch_to("maestro/evolve/x", "origin/maestro/evolve/x")

    cmd = mock_run.call_args[0][0]
    assert cmd == [
        "git", "branch", "-f", "maestro/evolve/x", "origin/maestro/evolve/x",
    ]


def test_list_remote_branches_filters_head_and_symref() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    output = (
        "origin/HEAD -> origin/main\n"
        "origin/main\n"
        "origin/feature/x\n"
        "origin/maestro/evolve/abc\n"
    )
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout=output,
        )
        names = vcs.list_remote_branches()

    assert "main" in names
    assert "feature/x" in names
    assert "maestro/evolve/abc" in names
    # symrefs and HEAD-only lines are filtered out
    assert not any("HEAD" in n for n in names)
    assert not any(" -> " in n for n in names)


def test_list_remote_branches_returns_empty_on_failure() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ):
        assert vcs.list_remote_branches() == []


def test_commits_ahead_behind_parses_output() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="2\t3\n",
        )
        assert vcs.commits_ahead_behind("main", "origin/main") == (2, 3)


def test_commits_ahead_behind_returns_zeros_on_failure() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ):
        assert vcs.commits_ahead_behind("a", "b") == (0, 0)


def test_commits_ahead_behind_handles_bad_output() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="not-numeric\n",
        )
        assert vcs.commits_ahead_behind("a", "b") == (0, 0)


def test_commits_ahead_behind_two_non_numeric_parts() -> None:
    """Two tokens that aren't ints hits the ValueError branch."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="foo bar\n",
        )
        assert vcs.commits_ahead_behind("a", "b") == (0, 0)


def test_commits_not_on_remote_parses_count() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="3\n")
        assert vcs.commits_not_on_remote("maestro/evolve/ep1") == 3
    cmd = mock_run.call_args[0][0]
    assert cmd == [
        "git", "rev-list", "--count", "maestro/evolve/ep1",
        "--not", "--remotes=origin",
    ]


def test_commits_not_on_remote_returns_zero_on_failure() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ):
        assert vcs.commits_not_on_remote("branch") == 0


def test_commits_not_on_remote_handles_non_numeric_output() -> None:
    """Garbled stdout hits the ValueError branch and reads as zero."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="not-a-number\n",
        )
        assert vcs.commits_not_on_remote("branch", "upstream") == 0


def test_non_merge_commits_ahead_parses_count() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="2\n")
        assert vcs.non_merge_commits_ahead("maestro/task/abc", "main") == 2
    cmd = mock_run.call_args[0][0]
    # ``--no-merges`` is the whole point: a branch whose only new commit is a
    # merge of ``main`` must read zero. Range order is ``base..ref``.
    assert cmd == [
        "git", "rev-list", "--count", "--no-merges", "main..maestro/task/abc",
    ]


def test_non_merge_commits_ahead_returns_none_on_failure() -> None:
    """A failed probe reads ``None`` (unknown), never a measured zero."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ):
        assert vcs.non_merge_commits_ahead("branch", "main") is None


def test_non_merge_commits_ahead_handles_non_numeric_output() -> None:
    """Garbled stdout hits the ValueError branch and reads as unknown."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="not-a-number\n",
        )
        assert vcs.non_merge_commits_ahead("branch", "main") is None


def test_lap_commits_since_parses_count() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="3\n")
        assert vcs.lap_commits_since(
            "maestro/evolve/x", ("abc123", "def456"),
        ) == 3
    cmd = mock_run.call_args[0][0]
    assert cmd == [
        "git", "rev-list", "--count", "--no-merges", "--first-parent",
        "maestro/evolve/x", "^abc123", "^def456",
    ]


def test_lap_commits_since_returns_none_on_failure() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ):
        assert vcs.lap_commits_since("branch", ("abc",)) is None


def test_lap_commits_since_handles_non_numeric_output() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="garbled\n",
        )
        assert vcs.lap_commits_since("branch", ("abc",)) is None


def test_merge_from_status_failure_propagates_underlying_error() -> None:
    """When status probe also fails, merge_from re-raises the original merge error."""
    vcs = SubprocessVCS(FAKE_DIR)

    def fake_run(
        cmd: list[str], *args: object, **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["git", "merge"]:
            raise subprocess.CalledProcessError(128, cmd, stderr="oops")
        if cmd[:3] == ["git", "diff", "--name-only"]:
            raise subprocess.CalledProcessError(129, cmd, stderr="diff failed")
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    with patch("maestro.vcs.subprocess.run", side_effect=fake_run), \
         pytest.raises(VCSError):
        vcs.merge_from("origin/main")


def test_rev_parse_returns_hash() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout=f"{HASH_A}\n",
        )
        assert vcs.rev_parse("main") == HASH_A


def test_rev_parse_returns_none_on_failure() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ):
        assert vcs.rev_parse("bogus") is None


def test_rev_parse_returns_none_on_empty_stdout() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="",
        )
        assert vcs.rev_parse("empty") is None


def test_local_main_ahead_commits_parses_shas() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    output = "sha-newest\nsha-middle\nsha-oldest\n"
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout=output,
        )
        shas = vcs.local_main_ahead_commits()
    assert shas == ["sha-newest", "sha-middle", "sha-oldest"]
    cmd = mock_run.call_args[0][0]
    assert cmd == ["git", "rev-list", "origin/main..main"]


def test_local_main_ahead_commits_empty_when_in_sync() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="",
        )
        assert vcs.local_main_ahead_commits() == []


def test_local_main_ahead_commits_raises_on_failure() -> None:
    # Fail-closed: a swallowed failure would masquerade as "no drift" and
    # authorize a reset of main, so the method must raise, never return [].
    vcs = SubprocessVCS(FAKE_DIR)
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ), pytest.raises(VCSError):
        vcs.local_main_ahead_commits()


def test_remote_refs_containing_filters_and_strips_prefix() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    output = (
        "  origin/HEAD -> origin/main\n"
        "  origin/main\n"
        "  origin/maestro/evolve/abc\n"
    )
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout=output,
        )
        refs = vcs.remote_refs_containing("deadbeef")
    assert refs == ["main", "maestro/evolve/abc"]
    cmd = mock_run.call_args[0][0]
    assert cmd == ["git", "branch", "-r", "--contains", "deadbeef"]


def test_remote_refs_containing_returns_empty_on_failure() -> None:
    vcs = SubprocessVCS(FAKE_DIR)
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ):
        assert vcs.remote_refs_containing("deadbeef") == []


def test_run_error_includes_captured_stderr_and_stdout() -> None:
    """_run must surface captured stderr/stdout in the VCSError message.

    Without this, a ``git add -A`` exit-128 crash (see issue #1314) reaches
    the crash report as a bare status code — git's own ``fatal: ...`` line
    is dropped on the floor, which is what originally made that class of
    failure undebuggable.
    """
    vcs = SubprocessVCS(FAKE_DIR)
    exc = subprocess.CalledProcessError(
        128,
        ["git", "add", "-A"],
        output="some stdout text",
        stderr="fatal: detected dubious ownership in repository at '/workspace'",
    )
    with patch("maestro.vcs.subprocess.run", side_effect=exc), \
         pytest.raises(VCSError) as excinfo:
        vcs.stage_all()

    message = str(excinfo.value)
    assert "exit status 128" in message
    assert "stderr:" in message
    assert "fatal: detected dubious ownership" in message
    assert "stdout:" in message
    assert "some stdout text" in message
    assert excinfo.value.__cause__ is exc


# =====================================================================
# SubprocessVCS — is_ancestor
# =====================================================================


def test_is_ancestor_true() -> None:
    """Exit code 0 from git merge-base --is-ancestor means 'is ancestor' → True."""
    vcs = SubprocessVCS(FAKE_DIR, quick_timeout=5)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="",
        )
        assert vcs.is_ancestor(HASH_A, HASH_B) is True

    cmd = mock_run.call_args.args[0]
    assert cmd == ["git", "merge-base", "--is-ancestor", HASH_A, HASH_B]
    assert mock_run.call_args[1]["timeout"] == 5


def test_is_ancestor_false_on_exit_one() -> None:
    """Exit code 1 is git's documented 'not an ancestor' — False, no warning."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ), patch("maestro.vcs.logger") as mock_logger:
        assert vcs.is_ancestor(HASH_A, HASH_B) is False
        mock_logger.warning.assert_not_called()


def test_is_ancestor_other_failure_warns_and_returns_false() -> None:
    """Non-1 exit codes (e.g. 128 from missing commit) warn and return False."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.CalledProcessError(128, "git"),
    ), patch("maestro.vcs.logger") as mock_logger:
        assert vcs.is_ancestor(HASH_A, HASH_B) is False
        assert mock_logger.warning.called


def test_is_ancestor_oserror_warns_and_returns_false() -> None:
    """OSError (process spawn failure) warns and returns False."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=OSError("git not found"),
    ), patch("maestro.vcs.logger") as mock_logger:
        assert vcs.is_ancestor(HASH_A, HASH_B) is False
        assert mock_logger.warning.called


def test_is_ancestor_empty_inputs_skip_subprocess() -> None:
    """Empty candidate or descendant short-circuits to False without calling git."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        assert vcs.is_ancestor("", HASH_B) is False
        assert vcs.is_ancestor(HASH_A, "") is False
        mock_run.assert_not_called()


# =====================================================================
# SubprocessVCS — merge_base (proposal 124)
# =====================================================================


def test_merge_base_returns_hash() -> None:
    """A resolved common ancestor is returned, shelling ``git merge-base``."""
    vcs = SubprocessVCS(FAKE_DIR, quick_timeout=5)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout=f"{HASH_A}\n",
        )
        assert vcs.merge_base(HASH_B, "origin/main") == HASH_A
    cmd = mock_run.call_args.args[0]
    assert cmd == ["git", "merge-base", HASH_B, "origin/main"]
    assert mock_run.call_args[1]["timeout"] == 5


def test_merge_base_returns_none_on_failure() -> None:
    """No common ancestor / bad ref → ``None`` (mirrors rev_parse)."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ):
        assert vcs.merge_base(HASH_A, "bogus") is None


def test_merge_base_returns_none_on_empty_stdout() -> None:
    """Empty stdout (unrelated histories) coerces to ``None``, never ``""``."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="")
        assert vcs.merge_base(HASH_A, HASH_B) is None


# =====================================================================
# SubprocessVCS — changed_paths (proposal 124)
# =====================================================================


def test_changed_paths_parses_name_only_diff() -> None:
    """Non-empty stripped lines of ``git diff --name-only base...head`` are returned."""
    vcs = SubprocessVCS(FAKE_DIR)
    output = "maestro/a.py\nmaestro/b.py\n\ntests/test_a.py\n"
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout=output,
        )
        paths = vcs.changed_paths("base", "head")
    assert paths == ("maestro/a.py", "maestro/b.py", "tests/test_a.py")
    cmd = mock_run.call_args.args[0]
    assert cmd == ["git", "diff", "--name-only", "base...head"]


def test_changed_paths_empty_on_failure() -> None:
    """Any subprocess failure → empty tuple (best-effort reader)."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ):
        assert vcs.changed_paths("base", "head") == ()


def test_changed_paths_bounded_to_max() -> None:
    """A pathologically large diff is capped at ``_CHANGED_PATHS_MAX`` paths."""
    vcs = SubprocessVCS(FAKE_DIR)
    output = "\n".join(f"maestro/f{i}.py" for i in range(_CHANGED_PATHS_MAX + 50))
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout=output,
        )
        paths = vcs.changed_paths("base", "head")
    assert len(paths) == _CHANGED_PATHS_MAX


# =====================================================================
# SubprocessVCS — tracked_paths (proposal 143)
# =====================================================================


def test_tracked_paths_parses_nul_separated_ls_files() -> None:
    """``git ls-files -z`` output splits on NUL; empty fields are dropped."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch("maestro.vcs.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="maestro/a.py\0docs/my file.md\0",
        )
        paths = vcs.tracked_paths()
    assert paths == frozenset({"maestro/a.py", "docs/my file.md"})
    cmd = mock_run.call_args.args[0]
    assert cmd == ["git", "ls-files", "-z"]


def test_tracked_paths_empty_on_failure() -> None:
    """Any subprocess failure → empty set (best-effort reader)."""
    vcs = SubprocessVCS(FAKE_DIR)
    with patch(
        "maestro.vcs.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ):
        assert vcs.tracked_paths() == frozenset()
