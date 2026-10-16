"""Regression: the plan-family commands refresh local `main` when already on it.

`/plan`, `/plan-proposal`, `/bug-fix`, and `/graduate-proposal` all open their
`## Switch to main if on a merged branch` section with "land on a current tree
first", and the *merged-branch* path fetches `origin/main` and fast-forwards
local `main` before grounding. But the on-`main` sub-bullet used to
short-circuit the common case with "already current; skip the switch and
proceed", bypassing the fetch and the fast-forward — so a local `main` trailing
`origin/main` (routine after an upstream merge the clone has not pulled) grounds
the plan or triage against stale code, the exact failure the section exists to
prevent. This mirrors `test_bug_fix_command_doc.py`'s merged-branch guard for
the already-on-`main` path across all these docs.

Static-text only: reads the command docs, no `mem_fs`/subprocess/network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_COMMANDS_DIR = Path(__file__).resolve().parent.parent / ".claude" / "commands"

_PLAN_FAMILY_DOCS = (
    "plan.md",
    "plan-proposal.md",
    "bug-fix.md",
    "graduate-proposal.md",
)


def _switch_section(doc: str) -> str:
    """Slice `## Switch to main …` up to the next top-level `## ` heading.

    The three docs end the section with different headings (`## Read the
    concern`, `## Resolve the proposal`, `## Progress tracking`), so the slice
    stops at the next top-level heading rather than a fixed end header.
    """
    start_header = "## Switch to main if on a merged branch"
    start = doc.index(start_header)
    end = doc.index("\n## ", start + len(start_header))
    return doc[start:end]


def _on_main_bullet(section: str) -> str:
    """Slice the on-`main` sub-bullet, from the `main` arrow up to `HEAD`."""
    start = section.index("- `main`")
    end = section.index("- `HEAD`", start)
    return section[start:end]


def _section_intro(section: str) -> str:
    """Slice the section intro — the header/preamble up to the first `1. ` step."""
    end = section.index("1. ")
    return section[:end]


@pytest.mark.parametrize("doc_name", _PLAN_FAMILY_DOCS)
def test_on_main_refreshes_local_main(doc_name: str) -> None:
    doc = (_COMMANDS_DIR / doc_name).read_text(encoding="utf-8")
    on_main = _on_main_bullet(_switch_section(doc))

    assert "already current; skip the switch and proceed" not in on_main, (
        f"{doc_name}: the on-`main` bullet must no longer short-circuit with "
        "'already current; skip the switch and proceed' — that bypass grounds "
        f"against a stale local `main`. Bullet:\n{on_main}"
    )
    assert "git fetch origin main" in on_main, (
        f"{doc_name}: the on-`main` bullet must run the best-effort "
        "`git fetch origin main` so grounding reads the latest `main`. "
        f"Bullet:\n{on_main}"
    )
    assert "git merge --ff-only origin/main" in on_main, (
        f"{doc_name}: the on-`main` bullet must fast-forward local `main` to "
        "`origin/main` with `git merge --ff-only origin/main`, not skip the "
        f"refresh. Bullet:\n{on_main}"
    )
    assert "dirty" in on_main.lower(), (
        f"{doc_name}: the on-`main` bullet must skip the refresh on a dirty "
        f"tree, leaving uncommitted changes untouched. Bullet:\n{on_main}"
    )
    assert "warn" in on_main.lower() and "proceed on the current" in on_main.lower(), (
        f"{doc_name}: the on-`main` bullet must warn and proceed on the current "
        "`main` when the fetch is unavailable or the fast-forward fails, like "
        f"every other fallback in the section. Bullet:\n{on_main}"
    )


def test_bug_fix_dry_run_excludes_on_main_refresh() -> None:
    """`bug-fix.md`'s `--dry-run` carve-out must cover the on-`main` refresh.

    The on-`main` refresh (`git fetch origin main` + `git merge --ff-only
    origin/main`) is a git-state mutation. If the section intro's `--dry-run`
    guard excludes only the branch *switch*, a dry-run while already on `main`
    (no switch to skip) still fast-forwards local `main`, breaking the "dry-run
    changes no git state" contract. The guard must declare the whole section —
    switch and refresh — a no-op under `--dry-run`, mirroring `plan.md`.
    """
    doc = (_COMMANDS_DIR / "bug-fix.md").read_text(encoding="utf-8")
    intro = _section_intro(_switch_section(doc))

    assert "--dry-run" in intro, (
        "bug-fix.md: the switch-section intro must carry a `--dry-run` "
        f"carve-out. Intro:\n{intro}"
    )
    assert "changes no git state" in intro, (
        "bug-fix.md: the `--dry-run` carve-out must declare the section changes "
        "no git state, so the on-`main` refresh is not reachable under "
        f"`--dry-run`. Intro:\n{intro}"
    )
    assert "refresh" in intro, (
        "bug-fix.md: the `--dry-run` carve-out must name the on-`main` refresh "
        "(not only the switch) as a skipped git-state mutation, else a dry-run "
        f"on `main` fast-forwards local `main`. Intro:\n{intro}"
    )
