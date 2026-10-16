"""Regression: bug-fix.md pins the `started_at` comparison as string equality.

The *File format* paragraph in `.claude/commands/bug-fix.md` names the
terminal-path identity check "byte-for-byte" — language that implicitly
forbids parse-then-reformat, but only implicitly. An implementer reading
the per-site rules (e.g., the *Re-checks before mutating* bullets) in
isolation could choose to `datetime.fromisoformat`-parse both sides
before comparing, which silently round-trips `Z` to `+00:00` and turns
a no-race condition into `error=progress_claim_lost`.

This test guards two things:

* The canonical paragraph that contains `byte-for-byte` also names the
  check as a string comparison and explicitly forbids parse-then-reformat.
* The per-site rules under *Concurrent sessions* / *Re-checks before
  mutating* reference the canonical-format pin so an implementer reading
  those bullets in isolation still sees the constraint.
"""

from __future__ import annotations

from pathlib import Path

_BUG_FIX_DOC = (
    Path(__file__).resolve().parent.parent / ".claude" / "commands" / "bug-fix.md"
)


def _paragraph_containing(text: str, needle: str) -> str:
    paragraphs = text.split("\n\n")
    matches = [p for p in paragraphs if needle in p]
    assert len(matches) == 1, (
        f"Expected exactly one paragraph containing {needle!r}, "
        f"found {len(matches)}."
    )
    return matches[0]


def _section_between(text: str, start_header: str, end_header: str) -> str:
    start = text.index(start_header)
    end = text.index(end_header, start + len(start_header))
    return text[start:end]


def test_started_at_comparison_calls_out_string_equality() -> None:
    doc = _BUG_FIX_DOC.read_text(encoding="utf-8")

    canonical = _paragraph_containing(doc, "pinned to ISO 8601 UTC")
    assert "byte-for-byte" in canonical, (
        "The canonical *File format* paragraph must still pin the identity "
        f"check as byte-for-byte. Paragraph:\n{canonical}"
    )
    assert "string comparison" in canonical, (
        "The canonical *File format* paragraph must explicitly name the "
        "check as a string comparison so implementers do not parse-then-"
        f"reformat. Paragraph:\n{canonical}"
    )
    assert "parse-then-reformat" in canonical, (
        "The canonical *File format* paragraph must explicitly forbid "
        "parse-then-reformat — the round-trip that silently rewrites `Z` "
        f"to `+00:00` and produces a phantom mismatch. Paragraph:\n{canonical}"
    )

    re_checks = _section_between(
        doc, "### Re-checks before mutating", "### Release the own claim"
    )
    assert "byte-for-byte" in re_checks or "string comparison" in re_checks, (
        "The *Re-checks before mutating* section must reference the "
        "canonical-format pin so an implementer reading the per-site rules "
        "in isolation still sees the string-equality constraint."
    )


def test_shared_re_pick_section_is_direction_agnostic() -> None:
    """The shared re-pick section must not pin a single iteration direction.

    The *Auto-pick path: re-pick on stale claim* section was broadened to cover
    BOTH auto-pick paths ("In either auto-pick path (oldest-first or `--rca`)").
    The `--rca` path iterates newest-to-oldest, so its next entry is the
    next-*newest*. Pinning the shared resume instruction to "next-oldest entry
    of the original list" — correct only for the oldest-first path — would tell
    an agent on the `--rca` path to step back toward already-skipped newer
    issues. The resume wording must name both directions instead.
    """
    doc = _BUG_FIX_DOC.read_text(encoding="utf-8")

    section = _section_between(
        doc,
        "### Auto-pick path: re-pick on stale claim",
        "### Race outcomes are loud, not silent",
    )
    assert "either auto-pick path" in section, (
        "Precondition: the shared re-pick section must cover both auto-pick "
        f"paths (oldest-first and `--rca`). Section:\n{section}"
    )
    assert "next-oldest entry of the original list" not in section, (
        "The shared re-pick section governs the newest-first `--rca` path too, "
        "so it must not pin the single oldest-first direction "
        f"('next-oldest entry of the original list'). Section:\n{section}"
    )
    assert "next-newest" in section, (
        "The shared re-pick section must name the `--rca` path's newest-first "
        "iteration direction so an agent does not resume toward already-skipped "
        f"newer issues. Section:\n{section}"
    )


def test_merged_branch_switch_fast_forwards_local_main() -> None:
    """The merged-branch switch must advance local `main` to the fetched tip.

    `git fetch origin main` updates only the `origin/main` remote-tracking
    ref, so a plain `git switch main` lands on the *local* `main` branch,
    which the fetch never fast-forwards. When the task branch was cut from an
    old local `main` and merged upstream, local `main` is an ancestor of the
    branch just left, so the switch moves the working tree backward — triaging
    staler code, the exact failure the section claims to prevent. The exit-0
    path must fast-forward local `main` to `origin/main`.
    """
    doc = _BUG_FIX_DOC.read_text(encoding="utf-8")

    section = _section_between(
        doc, "## Switch to main if on a merged branch", "## Progress tracking"
    )
    assert "git merge --ff-only origin/main" in section, (
        "The merged-branch switch must fast-forward local `main` to "
        "`origin/main` after `git switch main`, or it lands on a stale local "
        f"`main` the fetch never advanced. Section:\n{section}"
    )


def test_merged_branch_switch_has_switch_failure_fallback() -> None:
    """The exit-0 switch path must warn-and-proceed when the switch/ff fails.

    `git switch main` fatally aborts when `main` is already checked out in a
    sibling worktree — a usage context this same file documents in *Progress
    tracking* — and `git merge --ff-only origin/main` can fail on a diverged
    local `main`. Every other branch of the procedure (detached HEAD, dirty
    tree, fetch failure, unmerged branch) warns and proceeds on the current
    branch; the exit-0 mutation path must degrade the same way instead of
    stranding the command mid-flight with no described recovery.
    """
    doc = _BUG_FIX_DOC.read_text(encoding="utf-8")

    section = _section_between(
        doc, "## Switch to main if on a merged branch", "## Progress tracking"
    )
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


def test_plan_write_prescribes_atomic_temp_rename() -> None:
    """Regression: the plan-file write must prescribe atomic temp+rename.

    "single open-write-close" reads as protective but provides no atomicity:
    opening a file in write/truncate mode truncates it immediately, so a
    sibling reader that lands between the truncate and the final flush sees an
    empty or partial plan no matter how few `write()` calls the writer issues.
    Reducing the write count does nothing to close the window. Only the
    temp-file-plus-`os.rename` discipline the command already uses for the
    in-progress claim and the last-plan-by-folder pointer guarantees a
    concurrent reader sees either the prior plan or the new one — never a
    truncated file. Both plan-write sites must prescribe that pattern, and the
    false "open-write-close" claim must be gone everywhere in the doc.
    """
    doc = _BUG_FIX_DOC.read_text(encoding="utf-8")

    assert "open-write-close" not in doc.lower(), (
        "The misleading 'open-write-close' wording must be gone from the doc — "
        "it provides no atomicity against a concurrent reader; the plan write "
        "must prescribe a temp-file-plus-rename instead."
    )

    plan_path = _section_between(doc, "## Plan path", "### Write the plan file")
    assert "fix-<NNN>-<slug>.md.tmp.<pid>" in plan_path, (
        "The *Plan path* write step must prescribe writing the plan to a "
        "sibling temp file `~/.claude/plans/fix-<NNN>-<slug>.md.tmp.<pid>` "
        f"before renaming it into place. Section:\n{plan_path}"
    )
    assert "rename" in plan_path, (
        "The *Plan path* write step must `os.rename` the temp file into place "
        "so a concurrent reader never sees a truncated plan. "
        f"Section:\n{plan_path}"
    )

    re_checks = _section_between(
        doc, "### Re-checks before mutating", "### Release the own claim"
    )
    assert "fix-<NNN>-<slug>.md.tmp.<pid>" in re_checks, (
        "The *Re-checks before mutating* plan-path bullet must prescribe the "
        "same atomic temp-file write "
        "(`~/.claude/plans/fix-<NNN>-<slug>.md.tmp.<pid>`) so the per-site "
        f"rule reads consistently with the *Plan path* step. Section:\n{re_checks}"
    )
    assert "rename" in re_checks, (
        "The *Re-checks before mutating* plan-path bullet must `os.rename` the "
        f"temp file into place, matching the *Plan path* step. Section:\n{re_checks}"
    )


def test_open_pr_precheck_covers_all_closing_keywords() -> None:
    """Regression (#1683): the open-PR precheck must recognize every GitHub
    closing keyword, not only `Fixes`/`Closes`.

    Sibling to `/backlog`'s picker skip: the targeted-path *Open-PR precheck*
    warns-and-proceeds when an open PR already links the issue. A body saying
    `Resolves #<NNN>` (or `Fixed #<NNN>`, …) is a genuine in-flight fix, yet
    the original two-keyword union missed it. This isolates the precheck
    section so a regression to two keywords is caught (`resolve` and the
    closing-keyword-set wording both appear only after the widening — other
    `resolve` mentions in the doc live outside this section).
    """
    doc = _BUG_FIX_DOC.read_text(encoding="utf-8")

    precheck = _section_between(
        doc, "5. Open-PR precheck", "Every early precheck"
    ).lower()
    assert "resolve" in precheck, (
        "The open-PR precheck must recognize GitHub's `resolve` closing-keyword "
        "family (`resolve`/`resolves`/`resolved`), not only `fixes`/`closes`. "
        f"Section:\n{precheck}"
    )
    assert "closing keyword" in precheck, (
        "The open-PR precheck must state it covers GitHub's closing-keyword "
        f"set, not just two keywords. Section:\n{precheck}"
    )
    assert "colon" in precheck, (
        "The open-PR precheck must accept GitHub's colon-separated closing "
        "form (`Fixes: #<NNN>`) via the optional `:?` — GitHub auto-closes "
        f"it, so the whitespace-only regex failed open. Section:\n{precheck}"
    )


def test_close_path_recheck_covers_all_closing_keywords() -> None:
    """Regression (#1683): the close-path open-PR re-check must recognize every
    GitHub closing keyword, not only `Fixes`/`Closes`.

    The pre-`gh issue close` re-check stops with `error=linked_pr_appeared`
    when an open PR links the issue. It must catch a PR linked by any closing
    keyword, else the command closes an issue an implementer PR would have
    closed on merge — a double notification and phantom comment race. This
    isolates the *Close path only* section so a two-keyword regression fails.
    """
    doc = _BUG_FIX_DOC.read_text(encoding="utf-8")

    close_path = _section_between(
        doc, "Close path only:", "Plan path only:"
    ).lower()
    assert "resolve" in close_path, (
        "The close-path open-PR re-check must recognize GitHub's `resolve` "
        "closing-keyword family (`resolve`/`resolves`/`resolved`), not only "
        f"`fixes`/`closes`. Section:\n{close_path}"
    )
    assert "closing keyword" in close_path, (
        "The close-path open-PR re-check must state it covers GitHub's closing-"
        f"keyword set, not just two keywords. Section:\n{close_path}"
    )


def test_open_pr_detection_regex_bounds_issue_number() -> None:
    """Regression (#1683 follow-up): every open-PR detection regex is bounded.

    The widened regex `(close[sd]?|fix(es|ed)?|resolve[sd]?)\\s+#<NNN>` appears
    at three sites (precheck, `--rca` skip, close-path re-check) and was
    unbounded. Its trailing side false-matches `#123` when triaging `#12`; its
    leading side reads `discloses #<NNN>` as `closes #<NNN>`. The fix wraps
    every site in `\\b...\\b`. This is doc-wide so a regression at any one site
    fails. The bounded literal also carries the optional `:?` so GitHub's
    colon-separated closing form (`Fixes: #<NNN>`, which GitHub still
    auto-closes) is matched at every site; a regression that drops the colon
    fails here too.
    """
    doc = _BUG_FIX_DOC.read_text(encoding="utf-8")

    unbounded = "`(close[sd]?|fix(es|ed)?|resolve[sd]?):?\\s+#<NNN>`"
    bounded = "`\\b(close[sd]?|fix(es|ed)?|resolve[sd]?):?\\s+#<NNN>\\b`"
    assert unbounded not in doc, (
        "No open-PR detection site may use the unbounded literal "
        f"{unbounded} — it false-matches `#123` when triaging `#12`. Use the "
        f"boundary-wrapped {bounded} at every site."
    )
    assert doc.count(bounded) == 3, (
        "All three open-PR detection sites (precheck, `--rca` skip, close-path "
        f"re-check) must use the boundary-wrapped {bounded}; found "
        f"{doc.count(bounded)} occurrences."
    )

    precheck = _section_between(
        doc, "5. Open-PR precheck", "Every early precheck"
    ).lower()
    assert "non-digit" in precheck, (
        "The precheck must explain the trailing boundary bounds the issue "
        f"number on a non-digit. Section:\n{precheck}"
    )
    assert "#123" in precheck, (
        "The precheck must give the concrete false-match it now prevents "
        f"(`#12` must not match `#123`). Section:\n{precheck}"
    )
