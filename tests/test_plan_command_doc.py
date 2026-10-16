"""Regression: plan.md's merged-branch switch advances local main and is safe.

`plan.md` copied its *Switch to main if on a merged branch* section from
`plan-proposal.md`, including the two invariants
`tests/test_plan_proposal_command_doc.py` exists to protect: the exit-0 path
must fast-forward local `main` to `origin/main` (a plain `git switch main`
lands on the *local* branch, which `git fetch origin main` never advances, so
the working tree can move backward into the stale code the section claims to
prevent), and it must warn-and-proceed when the switch or fast-forward fails.
Nothing gated that copy, so these mirror the sibling's coverage.

The third test covers the `--dry-run` exemption: `--dry-run` is documented as
writing nothing, and `git switch main` + `git merge --ff-only` are local git
mutations, so a dry run must not perform them (`/bug-fix` exempts `--dry-run`
from the same section for the same reason).
"""

from __future__ import annotations

from pathlib import Path

_PLAN_DOC = (
    Path(__file__).resolve().parent.parent / ".claude" / "commands" / "plan.md"
)

_SWITCH_HEADER = "## Switch to main if on a merged branch"
_NEXT_HEADER = "## Read the concern"


def _section_between(text: str, start_header: str, end_header: str) -> str:
    start = text.index(start_header)
    end = text.index(end_header, start + len(start_header))
    return text[start:end]


def _switch_section() -> str:
    doc = _PLAN_DOC.read_text(encoding="utf-8")
    return _section_between(doc, _SWITCH_HEADER, _NEXT_HEADER)


def test_merged_branch_switch_fast_forwards_local_main() -> None:
    section = _switch_section()

    assert "git merge --ff-only origin/main" in section, (
        "The merged-branch switch must fast-forward local `main` to "
        "`origin/main` after `git switch main`, or grounding reads a stale "
        f"local `main` the fetch never advanced. Section:\n{section}"
    )


def test_merged_branch_switch_has_switch_failure_fallback() -> None:
    """The exit-0 switch path must warn-and-proceed when the switch/ff fails.

    `git switch main` fatally aborts when `main` is already checked out in a
    sibling worktree, and `git merge --ff-only origin/main` can fail on a
    diverged local `main`. Every other branch of the procedure (detached HEAD,
    dirty tree, fetch failure, unmerged branch) warns and proceeds on the
    current branch; the exit-0 mutation path must degrade the same way instead
    of stranding the command mid-flight with no described recovery.
    """
    section = _switch_section()

    assert "already checked out" in section, (
        "The exit-0 switch path must name the sibling-worktree failure mode "
        "(`git switch main` aborts when `main` is already checked out "
        f"elsewhere). Section:\n{section}"
    )
    assert "could not complete and proceed on the current branch" in section, (
        "The exit-0 switch path must warn and proceed on the current branch "
        "when `git switch main` or the `--ff-only` fails, like every other "
        f"fallback in the section. Section:\n{section}"
    )


def test_merged_branch_switch_is_skipped_under_dry_run() -> None:
    """`--dry-run` writes nothing, so it must not mutate local git state.

    The section is skipped under `--no-switch`; without the same exemption for
    `--dry-run`, a run documented as "write nothing" still performs a
    `git switch main` and a `git merge --ff-only`, moving the user off their
    branch. `/bug-fix` exempts `--dry-run` from its identical section.
    """
    section = _switch_section()

    assert "--dry-run" in section, (
        "The merged-branch switch section must name `--dry-run` as a skip "
        "condition, since a dry run changes no git state. "
        f"Section:\n{section}"
    )
