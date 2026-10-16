"""Regression: commit-push.md step 5 probes for an upstream ref explicitly.

`.claude/commands/commit-push.md`'s *Merge upstream changes* step 5 decides
whether to merge before pushing. It must probe for the branch's upstream
tracking ref with an explicit `git rev-parse ... @{u}` and branch on that
probe's exit status — not lean on `git log HEAD..@{u}` failing with a noisy
`fatal: no upstream configured` on stderr to stand in for "no upstream"
(issue #1254). Leaning on the log error conflates "no upstream configured"
with "an upstream exists but carries no new commits" behind one outcome, and
the `fatal:` noise can be misread as a genuine push blocker.

This is a pure file-content read of a repo-tracked doc, mirroring
`tests/test_merge_main_command_doc.py`, so no `mem_fs` / mocked externals are
needed.
"""

from __future__ import annotations

from pathlib import Path

_COMMIT_PUSH_DOC = (
    Path(__file__).resolve().parent.parent / ".claude" / "commands" / "commit-push.md"
)

# Step 5's own exit-status branching wording. Scoped to this phrase rather than
# the bare `exits non-zero` substring, which also appears in step 1's unrelated
# preflight description ("It exits non-zero with a one-line diagnostic..."); a
# bare-substring check would pass even if step 5 stopped branching on the
# probe's exit status while step 1 kept the substring alive.
_STEP5_EXIT_STATUS_PHRASE = "If this probe exits non-zero"


def test_step5_probes_upstream_explicitly() -> None:
    doc = _COMMIT_PUSH_DOC.read_text(encoding="utf-8")
    lines = doc.splitlines()

    # An explicit upstream probe must be present in the doc.
    probe_indices = [
        i
        for i, line in enumerate(lines)
        if "git rev-parse" in line and "@{u}" in line
    ]
    assert len(probe_indices) == 1, (
        "Step 5 must contain exactly one explicit upstream probe "
        f"(`git rev-parse ... @{{u}}`); found {len(probe_indices)}."
    )

    # The probe must silence stderr and the doc must branch on the probe's exit
    # status. Without `2>/dev/null` the missing-upstream case reintroduces the
    # exact noisy `fatal: no upstream configured` #1254 targets, and without an
    # explicit exit-status branch the doc would again lean on the `git log`
    # error to stand in for "no upstream" — the two states this fix separates.
    probe_line = lines[probe_indices[0]]
    assert "2>/dev/null" in probe_line, (
        "The upstream probe must silence stderr with `2>/dev/null` so a missing "
        "upstream never surfaces a noisy `fatal: no upstream configured`. "
        f"Probe line:\n{probe_line}"
    )
    assert _STEP5_EXIT_STATUS_PHRASE in doc, (
        "Step 5 must branch on the probe's exit status via its own "
        f"'{_STEP5_EXIT_STATUS_PHRASE}' wording (→ skip to push), not on whether "
        "the `git log HEAD..@{u}` command errors out. Matching the bare 'exits "
        "non-zero' substring would spuriously pass on step 1's unrelated "
        "preflight description."
    )

    # The probe must appear before the first `git log HEAD..@{u}` reference so
    # a missing upstream is decided by the probe's exit status, not inferred
    # from the log command erroring out.
    log_indices = [i for i, line in enumerate(lines) if "git log HEAD..@{u}" in line]
    assert log_indices, "Expected a `git log HEAD..@{u}` reference in the doc."
    assert probe_indices[0] < log_indices[0], (
        "The explicit `git rev-parse ... @{u}` probe must appear before the "
        "first `git log HEAD..@{u}` reference so a missing upstream is detected "
        "by the probe's exit status, not inferred from the log command's error."
    )

    # The doc must no longer fold "no upstream tracking ref" into the outcome of
    # the `git log HEAD..@{u}` diff — that old wording is what conflated the two
    # states and leaned on the log's stderr.
    assert "no upstream tracking ref or there are no new commits" not in doc, (
        "The doc must not infer 'no upstream tracking ref' from the "
        "`git log HEAD..@{u}` diff; the explicit probe's exit status decides it."
    )


def test_exit_status_phrase_excludes_step1_preflight_wording() -> None:
    """The scoped exit-status phrase must not collide with step 1's preflight.

    Step 1's preflight description also says 'exits non-zero' ("It exits
    non-zero with a one-line diagnostic..."), so the bare `'exits non-zero'`
    substring the earlier assertion used would pass even if step 5 stopped
    branching on the probe's exit status. Scoping to
    ``_STEP5_EXIT_STATUS_PHRASE`` closes that gap: it matches step 5's wording
    only, so the regression guard fails the moment step 5's branching wording
    is dropped, regardless of step 1.
    """
    step1_preflight = (
        "It exits non-zero with a one-line diagnostic on the first failed check."
    )
    step5_branch = (
        "If this probe exits non-zero, the branch has no upstream tracking ref"
    )

    # The scoped phrase distinguishes step 5's wording from step 1's.
    assert _STEP5_EXIT_STATUS_PHRASE not in step1_preflight
    assert _STEP5_EXIT_STATUS_PHRASE in step5_branch

    # The bare substring the scoped phrase replaces would have matched step 1's
    # unrelated preflight wording — the collision this scoping fixes.
    assert "exits non-zero" in step1_preflight
