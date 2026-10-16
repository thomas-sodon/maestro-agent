"""Regression: create-pr.md discards a stale folder pointer even with no `branch_issue`.

Step 7 of `.claude/commands/create-pr.md` gathers issue-closing candidates
from the branch name (`branch_issue`), the resolved plan (`plan_issue`), and
the commit messages (`commit_issue`). The folder-pointer fallback
(`~/.claude/last-plan-by-folder.json`) is *not* branch-aware and records the
most recently written plan for the directory, which may belong to an unrelated
branch.

The original stale-pointer cross-check only fired *when `branch_issue`
exists* ("if `branch_issue` exists and the pointer plan's number ≠
`branch_issue`, discard it"). For a `feat`/`refactor`/`docs` branch with no
embedded `fix-<NNN>` segment, `branch_issue` is absent, so the cross-check
never ran: the stale pointer became the sole `plan_issue` candidate and the
"only one source yielded a number → that is the issue" reconciliation rule
trusted it, auto-closing an unrelated issue on merge — the very bug this
command is meant to prevent.

This test pins the fallback bullet so it also discards the pointer when
`branch_issue` is absent unless `commit_issue` corroborates the same number.
"""

from __future__ import annotations

from pathlib import Path

_CREATE_PR_DOC = (
    Path(__file__).resolve().parent.parent / ".claude" / "commands" / "create-pr.md"
)


def _line_containing(text: str, needle: str) -> str:
    matches = [line for line in text.splitlines() if needle in line]
    assert len(matches) == 1, (
        f"Expected exactly one line containing {needle!r}, found {len(matches)}."
    )
    return matches[0]


def test_folder_pointer_fallback_guards_absent_branch_issue() -> None:
    doc = _CREATE_PR_DOC.read_text(encoding="utf-8")

    fallback = _line_containing(doc, "last-plan-by-folder.json")

    assert "`branch_issue` exists and the pointer plan's number ≠ `branch_issue`" in (
        fallback
    ), (
        "The folder-pointer fallback must still discard a pointer whose number "
        "disagrees with an embedded `branch_issue`. Bullet:\n" + fallback
    )
    assert "`branch_issue` is absent" in fallback, (
        "The folder-pointer fallback must explicitly cover the case where the "
        "branch carries no embedded `fix-<NNN>` and `branch_issue` is absent — "
        "the gap that let a stale pointer auto-close an unrelated issue. "
        "Bullet:\n" + fallback
    )
    assert "`commit_issue` corroborates" in fallback, (
        "When `branch_issue` is absent, the pointer must only survive if "
        "`commit_issue` corroborates the same number; otherwise an "
        "uncorroborated folder pointer must never add a closing keyword. "
        "Bullet:\n" + fallback
    )


def test_branch_plan_strip_constrained_to_uid_shape() -> None:
    """Regression (PR #2936, 💡): the trailing-segment strip must match the uid shape.

    The branch-derived plan lookup strips a trailing `-<token>` when the exact
    `<slug>.md` is missing, to undo the `-<uid>` that `/impl` appends. Stripping
    *any* trailing token is over-broad: when the exact plan file is genuinely
    absent, it removes a real slug word and can false-match a shorter, unrelated
    plan (`feat/add-caching-layer` → `add-caching.md`), flowing a wrong number
    into `plan_issue` and adding a wrong `Closes #<NNN>` to the PR body. The
    strip must therefore only fire on a token matching the documented uid shape
    (a 12-digit timestamp optionally followed by hex).
    """
    doc = _CREATE_PR_DOC.read_text(encoding="utf-8")

    bullet = _line_containing(doc, "strip that one uid-shaped trailing segment")

    assert "^[0-9]{12}[0-9a-f]*$" in bullet, (
        "The strip bullet must constrain the trailing token to the `/impl` uid "
        "shape via the anchored regex `^[0-9]{12}[0-9a-f]*$`, not strip any "
        f"trailing segment. Bullet:\n{bullet}"
    )
    assert "uid shape" in bullet, (
        "The strip bullet must name the `/impl` uid shape as the strip "
        f"condition. Bullet:\n{bullet}"
    )
    assert "add-caching-layer" in bullet, (
        "The strip bullet must show that an ordinary slug word is left intact "
        "(`feat/add-caching-layer` is not stripped to `add-caching`), the "
        f"false-match this constraint prevents. Bullet:\n{bullet}"
    )
    assert "only the uid shape is ever stripped" in bullet, (
        "The strip bullet must reassert branch-specificity now that only a "
        f"uid-shaped segment is stripped. Bullet:\n{bullet}"
    )
    # The old unconstrained wording — strip *any* trailing segment — must be gone.
    assert "strip that one trailing segment and retry the lookup once" not in doc, (
        "The unconditional 'strip that one trailing segment' wording must be "
        "replaced by the uid-shape-constrained strip."
    )


def test_pre_existing_pr_detected_before_create() -> None:
    """Regression (#1193): detect a pre-existing open PR before `gh pr create`.

    `/create-pr` pushes the current branch (Step 5) and then ran `gh pr create`
    unconditionally, so re-invoking it on a branch that already carries an open
    PR hit a raw `gh` "a pull request already exists" error with no guidance.
    Because GitHub auto-updates an open PR whenever its branch is pushed, the
    push has already delivered the new commits to the existing PR; the correct
    outcome is to detect the open PR and report its (already-updated) URL rather
    than attempt a duplicate create. This test pins a `gh pr view` existence
    check that runs *before* the `gh pr create` call and short-circuits — but
    only for an open PR, so a closed/merged PR still allows a fresh create.
    """
    doc = _CREATE_PR_DOC.read_text(encoding="utf-8")
    lines = doc.splitlines()

    view_line = _line_containing(doc, "gh pr view --json number,url,state")
    create_line = _line_containing(doc, "gh pr create --title")

    assert lines.index(view_line) < lines.index(create_line), (
        "The `gh pr view` existence check must appear before the `gh pr create` "
        "call so a pre-existing open PR short-circuits creation instead of "
        "hitting a duplicate-PR error."
    )

    open_pr = _line_containing(doc, "An open PR already exists")
    assert "report its URL" in open_pr, (
        "When an open PR already exists for the branch, the step must report its "
        "(already push-updated) URL instead of creating a duplicate. Bullet:\n"
        + open_pr
    )

    guard = _line_containing(doc, "closed or merged PR on the same branch name")
    assert "must not block a fresh `gh pr create`" in guard, (
        "The short-circuit must be scoped to open PRs: a previously closed or "
        "merged PR on the same branch name must not block a fresh create. "
        "Bullet:\n" + guard
    )


def test_pre_existing_pr_check_distinguishes_transient_gh_failure() -> None:
    """Regression (PR #3201, 💡): a transient `gh pr view` failure must not fall through to create.

    `gh pr view` exits non-zero both when no PR exists (the state Step 6 wants to
    detect) *and* when the call fails for an unrelated reason — an auth error, a
    network/rate-limit failure, a misconfigured `gh`. Treating *any* non-zero
    exit as "no PR → continue to create" means a transient failure while an open
    PR actually exists drops through to `gh pr create`, re-hitting the raw "a
    pull request already exists" error #1193 set out to prevent — one step later.
    The step must therefore treat a non-zero exit as "no PR" only when the output
    says "no pull requests found", and stop-and-surface on any other failure.
    """
    doc = _CREATE_PR_DOC.read_text(encoding="utf-8")

    no_pr = _line_containing(doc, "because no PR exists")
    assert "no pull requests found" in no_pr, (
        "A non-zero `gh pr view` exit may be treated as 'no PR' (continue to "
        "create) only when its output says 'no pull requests found'. Bullet:\n"
        + no_pr
    )

    transient = _line_containing(doc, "errored for an unrelated reason")
    assert 'do **not** treat this as "no PR"' in transient, (
        "The transient-failure bullet must explicitly forbid treating an "
        "unrelated `gh` failure (auth, network/rate-limit, misconfigured gh) as "
        "'no PR' and falling through to `gh pr create`. Bullet:\n" + transient
    )
    assert "Stop and surface the failure" in transient, (
        "A non-zero `gh pr view` exit that does not indicate 'no pull requests "
        "found' must stop and surface the failure to the user rather than "
        "re-hitting the duplicate-PR error at `gh pr create`. Bullet:\n"
        + transient
    )
