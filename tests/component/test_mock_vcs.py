"""Component tests for ``MockVCS`` against a real git repo in ``tmp_path``.

Exercises the ``MockVCS`` local-op delegation behind ``merge_tree_text``
(PR #3145 review comment 3616375116) — the in-memory ``git merge-tree`` primitive
retained as a general, tested git helper. These assert its output directly: a
real single-line collision built in git yields conflict-marker text, and an
unresolvable ref pair fails safe to ``""``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.component.mock_vcs import MockVCS


def _git(d: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(d),
        check=True, capture_output=True, text=True,
    ).stdout


def test_mock_vcs_merge_tree_text_collision_yields_conflict_markers(
    workspace_dir: Path,
) -> None:
    """A real single-line collision built in git yields merge-tree output that
    carries conflict markers — proving MockVCS implements the whole Protocol and
    returns genuine legacy ``git merge-tree`` text mid-conflict."""
    d = workspace_dir
    target = d / "caps.py"

    # Base commit: a single-integer line at a shared base value.
    target.write_text("_MAX_LINES = 254\n", encoding="utf-8")
    _git(d, "add", ".")
    _git(d, "commit", "-m", "base cap")
    base_branch = _git(d, "rev-parse", "--abbrev-ref", "HEAD").strip()

    # ours: bump the line to 255 on a feature branch.
    _git(d, "checkout", "-b", "ours")
    target.write_text("_MAX_LINES = 255\n", encoding="utf-8")
    _git(d, "commit", "-am", "ours bumps cap")

    # theirs: bump the SAME line to a different value on the base branch.
    _git(d, "checkout", base_branch)
    target.write_text("_MAX_LINES = 260\n", encoding="utf-8")
    _git(d, "commit", "-am", "theirs bumps cap")

    vcs = MockVCS(workspace=d)
    output = vcs.merge_tree_text("ours", base_branch)

    # The Protocol method exists (no AttributeError) and returns real merge-tree
    # text carrying the collision with inline conflict markers.
    assert "_MAX_LINES" in output
    assert "<<<<<<<" in output


def test_mock_vcs_merge_tree_text_no_common_base_is_empty(
    workspace_dir: Path,
) -> None:
    """An unresolvable ref pair (no merge-base) fails safe to ``""``."""
    vcs = MockVCS(workspace=workspace_dir)
    output = vcs.merge_tree_text("main", "does-not-exist")
    assert output == ""
