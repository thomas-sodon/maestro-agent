"""Regression: quality-gates.md re-validates after an upstream merge before pushing.

PR #2682 added a commit-and-push step (step 7) to
``.claude/commands/quality-gates.md``. Step 6 re-runs the in-scope gate set and
requires it green before step 7 proceeds, but step 7 then *commits, merges
upstream commits (possibly a merge commit or a conflict resolution), and
pushes*. Without re-validating the merged tree, an upstream change that
interacts with the fixes — or a bad conflict resolution — can push a tree that
no longer passes the gates, contradicting the command's own "never push a
still-failing tree" guarantee (Safety bullet) and feeding a broken tree into CI
and the auto-merge/tend-PR flow.

``/merge-main`` and ``/process-comments`` both close this exact gap by re-running
the in-scope gates whenever the merge changed the workspace (see
``tests/test_process_comments_command_doc.py``). This test guards that
``quality-gates.md`` keeps the same re-run-after-merge instruction so it cannot
drift back to push-without-revalidation.
"""

from __future__ import annotations

from pathlib import Path

_QUALITY_GATES_DOC = (
    Path(__file__).resolve().parent.parent
    / ".claude"
    / "commands"
    / "quality-gates.md"
)


def _line_containing(text: str, needle: str) -> str:
    matches = [line for line in text.splitlines() if needle in line]
    assert len(matches) == 1, (
        f"Expected exactly one line containing {needle!r}, found {len(matches)}."
    )
    return matches[0]


def test_push_step_reruns_gates_after_upstream_merge() -> None:
    doc = _QUALITY_GATES_DOC.read_text(encoding="utf-8")

    bullet = _line_containing(doc, "the merge brought in new upstream commits")
    lowered = bullet.lower()
    assert "re-run the in-scope gate set" in lowered, (
        "The step-7 merge instruction must tell the implementer to re-run the "
        f"in-scope gate set over the merged tree. Bullet:\n{bullet}"
    )
    assert "before pushing" in lowered, (
        "The re-run must happen before the push so a broken merge aborts the "
        f"push instead of landing on the branch. Bullet:\n{bullet}"
    )
    assert "abort the push" in lowered, (
        "A post-merge gate failure must abort the push and surface to the user, "
        f"not silently push a still-failing tree. Bullet:\n{bullet}"
    )


def test_push_step_uses_shared_git_push_tracked_helper() -> None:
    """Step 7 must push via the shared ``git-push-tracked.sh`` helper.

    Step 7 claims it reuses ``/commit-push``'s scripts "so the logic never
    drifts", so the push itself must go through ``scripts/git-push-tracked.sh``
    — the helper ``/commit-push`` and ``/merge-main`` both use, which auto-selects
    the ``-u origin HEAD`` first-push form and signals a non-fast-forward
    rejection (exit 3) for integrate-and-retry. A raw ``scripts/with-env.sh git
    push`` pushes the manual ``-u`` decision back onto the implementer (and a bare
    ``git push -u`` with no upstream fails) and drops the concurrent-push
    recovery — exactly the drift the shared helper exists to eliminate.
    """
    doc = _QUALITY_GATES_DOC.read_text(encoding="utf-8")

    push_bullet = _line_containing(doc, "scripts/git-push-tracked.sh")
    lowered = push_bullet.lower()
    assert "exit 3" in lowered, (
        "The push bullet must acknowledge the helper's non-fast-forward (exit 3) "
        f"signal so a concurrent push is integrated and retried. Bullet:\n{push_bullet}"
    )
    # The raw `git push` form is precisely the plumbing drift the shared helper
    # exists to eliminate (manual `-u` upstream detection, no NFF recovery); it
    # must not reappear as the push instruction.
    assert "scripts/with-env.sh git push" not in doc, (
        "Step 7 must push through scripts/git-push-tracked.sh, not a raw "
        "`scripts/with-env.sh git push`, so the upstream-detection and "
        "non-fast-forward recovery never drift from /commit-push and /merge-main."
    )


def test_tooling_preflight_covers_all_scope_for_browser() -> None:
    """Step 4's playwright/chromium preflight must fire for the default/``all`` scope.

    ``scripts/quality-gates.sh all`` expands to maestro+review+sdk+**browser** and
    invokes ``gate_browser`` (``pytest tests/browser/ ... --browser chromium``), so
    the default scope needs playwright and chromium. But the doc's in-scope set is a
    set of scope *tokens* (see step 3's ``In-scope set: unit + browser`` example and
    step 5's ``scripts/quality-gates.sh <scope...>``), and ``all`` is its own token
    that does not literally contain ``browser``. If step 4 only checks for
    ``browser``, the most common (default/``all``) scope skips the fail-fast tooling
    preflight and a missing playwright/chromium surfaces as a raw runtime error in
    step 5 instead of the intended install guidance. The preflight must therefore
    fire when the in-scope set includes ``browser`` *or* ``all``.
    """
    doc = _QUALITY_GATES_DOC.read_text(encoding="utf-8")

    tooling_bullet = _line_containing(doc, "python3.14 -m playwright --version")
    assert "includes `browser` or `all`" in tooling_bullet, (
        "Step 4's tooling preflight must verify playwright/chromium for the "
        "`all` scope too — `scripts/quality-gates.sh all` runs the browser gate, "
        "but the token set `{all}` does not literally contain `browser`. "
        f"Bullet:\n{tooling_bullet}"
    )


def test_report_step_fetches_ci_log_extract_in_check_mode() -> None:
    """Step 8's report must name the read-only CI-log fetch it quotes in check mode.

    In ``check`` mode the CI inspection still folds each CI-red class into the scope
    (step 3), so a class can be red on CI yet green under the local gate. Step 8's
    FAIL bullet asks the report to "quote the CI-log extract instead" for such a
    class — but the only ``gh run view --log-failed`` fetch instruction lives in the
    fix-only step 6 (skipped in ``check`` mode) and the fix-mode diagnosis section.
    Following the doc in ``check`` mode therefore asks for an extract that was never
    fetched, leaving the green-local/red-CI reconciliation incomplete for exactly
    the read-only mode whose whole purpose is reporting. The report bullet must
    itself name the read-only fetch, and make clear it is available in ``check``
    mode, so the extract can actually be obtained where step 6 does not run.
    """
    doc = _QUALITY_GATES_DOC.read_text(encoding="utf-8")

    report_bullet = _line_containing(doc, "stayed green locally")
    assert "gh run view <run-id> --log-failed" in report_bullet, (
        "Step 8's report bullet must name the read-only `gh run view <run-id> "
        "--log-failed` fetch so a CI-derived class that is green-local/red-CI can "
        "be log-quoted in `check` mode, where step 6's fetch instruction is "
        f"skipped. Bullet:\n{report_bullet}"
    )
    assert "check" in report_bullet.lower(), (
        "The report bullet must make clear the read-only CI-log fetch is available "
        "in `check` mode (which skips step 6), not only in fix mode. "
        f"Bullet:\n{report_bullet}"
    )


def test_exit3_recovery_reruns_gates_before_repush() -> None:
    """The exit-3 (non-fast-forward) retry must re-run gates before re-pushing.

    The first push correctly re-validates after a tree-changing merge, but the
    exit-3 recovery also re-integrates the advanced remote branch — a fetch+merge
    that changes the tree — before re-pushing. If that recovery re-pushes without
    re-running the gates, as ``/commit-push`` step 7 does (it never runs gates), a
    concurrent-push collision lands an unvalidated re-merge on the branch and
    breaks the same "never push a still-failing tree" guarantee the first-push
    re-run protects. The recovery must follow ``/merge-main`` step 7's cycle,
    which re-runs the in-scope gates over the re-merged tree and only re-pushes
    when green. This test asserts that gap cannot drift back open.
    """
    doc = _QUALITY_GATES_DOC.read_text(encoding="utf-8")

    push_bullet = _line_containing(doc, "scripts/git-push-tracked.sh")
    lowered = push_bullet.lower()

    # Isolate the exit-3 recovery clause — from "exit 3" up to the next exit-code
    # branch ("any other non-zero") — so the assertions target the recovery path
    # and not the first-push re-run earlier in the same bullet.
    start = lowered.index("exit 3")
    end = lowered.index("any other non-zero", start)
    exit3_clause = lowered[start:end]

    assert "re-run the in-scope gate" in exit3_clause, (
        "The exit-3 non-fast-forward recovery re-integrates the advanced remote "
        "branch (a tree-changing merge), so it must re-run the in-scope gates over "
        f"the re-merged tree before re-pushing. Clause:\n{exit3_clause}"
    )
    assert "re-push only when it is green" in exit3_clause, (
        "The exit-3 recovery must re-validate and only re-push a green tree, not "
        f"re-push an unvalidated re-merged tree. Clause:\n{exit3_clause}"
    )


def test_ci_log_fetch_routes_through_with_env_wrapper() -> None:
    """Every ``gh run view --log-failed`` fetch must use ``scripts/with-env.sh``.

    Step 3 mandates routing *every* ``gh`` call through ``scripts/with-env.sh``,
    which loads ``GITHUB_TOKEN`` from ``.env``; the fix-mode diagnosis (step 6) and
    the report (step 8) both fetch the failing run's log via
    ``scripts/with-env.sh gh run view <run-id> --log-failed``. The CI-reported-
    failures diagnosis bullet must match: a bare, unwrapped ``gh run view`` there
    would issue an unauthenticated call that can fail with an auth error instead of
    returning the log. This test pins that bullet — and guards against any bare
    ``gh run view`` re-appearing anywhere in the doc.
    """
    doc = _QUALITY_GATES_DOC.read_text(encoding="utf-8")

    ci_bullet = _line_containing(doc, "names the failing step")
    assert "scripts/with-env.sh gh run view <run-id> --log-failed" in ci_bullet, (
        "The CI-reported-failures diagnosis bullet must fetch the failing run's log "
        "through `scripts/with-env.sh` (step 3's rule for every `gh` call), matching "
        f"the identical fetches in steps 6 and 8. Bullet:\n{ci_bullet}"
    )
    # No bare `gh run view` may survive anywhere: `.split` on the wrapper prefix
    # leaves a fragment starting with `gh run view` only for an unwrapped call.
    for fragment in doc.split("gh run view")[:-1]:
        assert fragment.endswith("scripts/with-env.sh "), (
            "Every `gh run view` invocation in the doc must be prefixed by "
            "`scripts/with-env.sh ` so it loads GITHUB_TOKEN from .env; found a bare "
            f"one ending: ...{fragment[-40:]!r}"
        )
