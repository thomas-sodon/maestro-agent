"""Regression: plan-proposal.md's merged-branch switch advances local main.

`git fetch origin main` updates only the `origin/main` remote-tracking ref,
so a plain `git switch main` lands on the *local* `main` branch, which the
fetch never fast-forwards. When the task branch was cut from an old local
`main` and merged upstream, local `main` is an ancestor of the branch just
left, so the switch moves the working tree backward — grounding the picker
and verification in stale code, the exact failure the *Switch to main if on
a merged branch* section claims to prevent. The exit-0 path must fast-forward
local `main` to `origin/main`.
"""

from __future__ import annotations

from pathlib import Path

from tests.support.command_docs import (
    assert_no_restated_substate_criterion,
    line_containing,
)

_PLAN_PROPOSAL_DOC = (
    Path(__file__).resolve().parent.parent
    / ".claude"
    / "commands"
    / "plan-proposal.md"
)


def _section_between(text: str, start_header: str, end_header: str) -> str:
    start = text.index(start_header)
    end = text.index(end_header, start + len(start_header))
    return text[start:end]


def test_merged_branch_switch_fast_forwards_local_main() -> None:
    doc = _PLAN_PROPOSAL_DOC.read_text(encoding="utf-8")

    section = _section_between(
        doc, "## Switch to main if on a merged branch", "## Resolve the proposal"
    )
    assert "git merge --ff-only origin/main" in section, (
        "The merged-branch switch must fast-forward local `main` to "
        "`origin/main` after `git switch main`, or the picker and "
        "verification read a stale local `main` the fetch never advanced. "
        f"Section:\n{section}"
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
    doc = _PLAN_PROPOSAL_DOC.read_text(encoding="utf-8")

    section = _section_between(
        doc, "## Switch to main if on a merged branch", "## Resolve the proposal"
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


def test_resolve_proposal_normalization_handles_already_padded_input() -> None:
    """Step-2 normalization must specify leading-zero handling.

    An already-padded input like `040` is otherwise ambiguous: a reader could
    treat the leading zero as octal (`040` → 32) or as a decimal pass-through
    (`040` → 40). The rule must strip leading zeros before re-padding so the
    same input always normalizes one way.
    """
    doc = _PLAN_PROPOSAL_DOC.read_text(encoding="utf-8")

    section = _section_between(
        doc, "## Resolve the proposal", "## Pick the next most important proposal"
    )
    lowered = section.lower()
    assert "strip" in lowered and "leading zero" in lowered, (
        "The step-2 normalization rule must specify leading-zero handling "
        "(strip leading zeros before re-padding) so an already-padded input "
        f"like `040` normalizes deterministically. Section:\n{section}"
    )


def test_resolve_proposal_normalization_rejects_overlong_input() -> None:
    """Step-2 normalization must reject input longer than 3 digits.

    A 4-digit number like `1000` cannot be "zero-padded to 3 digits" at all.
    Without an explicit rule the command drifts into an accidental
    `error=proposal_not_found` (via an empty glob) instead of rejecting the
    impossible argument up front, so the rule must stop with
    `error=invalid_argument` for anything longer than 3 digits.
    """
    doc = _PLAN_PROPOSAL_DOC.read_text(encoding="utf-8")

    section = _section_between(
        doc, "## Resolve the proposal", "## Pick the next most important proposal"
    )
    assert "more than 3 digits" in section, (
        "The step-2 normalization rule must name the overlong-input case "
        "(more than 3 digits, e.g. `1000`) so a 4-or-more-digit argument is "
        f"rejected up front. Section:\n{section}"
    )
    # `error=invalid_argument` also appears in step 1's positive-integer guard,
    # so a section-wide check would still pass if step 2 dropped the code.
    # Scope the assertion to the step-2 line carrying the overlong case so a
    # future edit removing the code from step 2 is caught.
    overlong_idx = section.index("more than 3 digits")
    line_start = section.rfind("\n", 0, overlong_idx) + 1
    line_end = section.index("\n", overlong_idx)
    overlong_line = section[line_start:line_end]
    assert "error=invalid_argument" in overlong_line, (
        "The overlong-input case must reject with `error=invalid_argument` on "
        "the same step-2 line, not lean on step 1's code or an accidental "
        f"empty glob. Line:\n{overlong_line}"
    )


def test_picker_skip_rule_zero_pad_references_resolve_normalization() -> None:
    """The picker's own zero-pad (step 4) must point back to step 2's rule.

    The picker independently zero-pads each captured NNN before the skip-rule
    glob; without a pointer to *Resolve the proposal* step 2, that path can
    drift from the tightened leading-zero/decimal normalization rule. The
    picker section already mentions "step 2 of Resolve the proposal" for its
    hand-off, so scope this to the specific zero-pad line — the line carrying
    "before applying the skip rules" — rather than the whole section.
    """
    doc = _PLAN_PROPOSAL_DOC.read_text(encoding="utf-8")

    section = _section_between(
        doc,
        "## Pick the next most important proposal",
        "## Verify the proposal against current code",
    )
    idx = section.index("before applying the skip rules")
    line_start = section.rfind("\n", 0, idx) + 1
    line_end = section.index("\n", idx)
    zero_pad_line = section[line_start:line_end]
    lowered = zero_pad_line.lower()
    assert "step 2" in lowered and "normaliz" in lowered, (
        "The picker's skip-rule zero-pad line must reference step 2's "
        "normalization rule so the two zero-pad paths stay in sync. Line:\n"
        f"{zero_pad_line}"
    )


def test_open_proposals_parse_notes_markdown_link_column() -> None:
    """Step-2's Open Proposals parse must name the markdown-link `#` column.

    The `## Open Proposals` table renders each `#` cell as a GitHub markdown
    link (`[NNN](category/NNN-slug.md)`), not a bare number, in today's
    README. A parser that matches a bare `| NNN |` row delimiter misses every
    row, silently building an empty `{NNN → status}` map — which makes the
    picker skip every proposal (`shipped or removed`) and degrade to
    `no_actionable_proposal` even when actionable proposals exist. The spec
    must call out the link format so the parse stays unambiguous; scope the
    assertion to the step-2 line so a future edit dropping the note is caught.
    """
    doc = _PLAN_PROPOSAL_DOC.read_text(encoding="utf-8")

    section = _section_between(
        doc,
        "## Pick the next most important proposal",
        "## Verify the proposal against current code",
    )
    idx = section.index("Read the `## Open Proposals` table")
    line_start = section.rfind("\n", 0, idx) + 1
    line_end = section.index("\n", idx)
    parse_line = section[line_start:line_end]
    assert "markdown link" in parse_line, (
        "Step 2's Open Proposals parse must state that the `#` cell is a "
        "markdown link (`[NNN](category/NNN-slug.md)`), not a bare number, so "
        "a parser does not silently build an empty status map. Line:\n"
        f"{parse_line}"
    )
    assert "[NNN]" in parse_line, (
        "Step 2's Open Proposals parse must name the `[NNN]` link text as the "
        "source of the proposal number, so `NNN` is extracted from the link "
        f"rather than a bare `| NNN |` delimiter. Line:\n{parse_line}"
    )


def test_autopicked_announcement_format_is_consistent() -> None:
    """Both `Auto-picked` examples must share one canonical shape.

    The picker's mid-flow announcement (## Pick the next most important
    proposal) and the closing-message prepend (## Write the plan file)
    describe the *same* operator-visible "Auto-picked" signal. They must use
    one shape — `Auto-picked: NNN (<Title>)` (colon after the keyword, title
    in parentheses) — so operator scanning and log scraping stay consistent.
    The divergent bare `Auto-picked NNN — ` form (no colon, em-dash, no
    parens) must not survive in either section.
    """
    doc = _PLAN_PROPOSAL_DOC.read_text(encoding="utf-8")

    picker = _section_between(
        doc,
        "## Pick the next most important proposal",
        "## Verify the proposal against current code",
    )
    write = _section_between(
        doc, "## Write the plan file", "## Record the plan pointer"
    )

    for name, section in (("picker", picker), ("write", write)):
        idx = section.index("Auto-picked")
        example = section[idx:]
        newline = example.find("\n")
        head = example if newline == -1 else example[:newline]

        assert head.startswith("Auto-picked: "), (
            f"The {name} section's `Auto-picked` example must use the canonical "
            "colon form `Auto-picked: NNN (<Title>)`, not the bare "
            f"`Auto-picked NNN` form. Example line:\n{head}"
        )
        assert "(" in head, (
            f"The {name} section's `Auto-picked` example must parenthesize the "
            f"title, as in `Auto-picked: NNN (<Title>)`. Example line:\n{head}"
        )
        assert ")" in head, (
            f"The {name} section's `Auto-picked` example must close the "
            f"parenthesized title. Example line:\n{head}"
        )
        assert "Auto-picked NNN — " not in section, (
            "The divergent bare `Auto-picked NNN — ` form (no colon, em-dash, "
            f"no parens) must not survive in the {name} section. Section:\n"
            f"{section}"
        )


def test_write_plan_file_documents_tilde_expansion() -> None:
    """The write instructions must require resolving `~` before any file write.

    The command writes two files under the home directory — the plan file and
    the per-folder pointer — both named with a leading `~/.claude/...`. The
    tilde is a shell display convention, not a POSIX-absolute path: the Write
    tool performs no shell expansion and `os.rename`/`open` treat `~` as a
    literal path component, so a bare `~/.claude/...` write target either fails
    or silently writes under a `~` directory in the cwd. The *Write the plan
    file* section must therefore name an explicit tilde-to-absolute resolution
    step (via `$HOME` or `os.path.expanduser`) before any file API sees the
    path, the way `.claude/commands/review-changes.md` already does.
    """
    doc = _PLAN_PROPOSAL_DOC.read_text(encoding="utf-8")

    section = _section_between(
        doc, "## Write the plan file", "## Record the plan pointer"
    )
    assert "os.path.expanduser" in section or "$HOME" in section, (
        "The *Write the plan file* section must require resolving a "
        "`~/.claude/...` write target to an absolute path (via "
        "`os.path.expanduser` or `$HOME`) before it reaches any file API; "
        "the bare tilde is not expanded by the Write tool or `os.rename`. "
        f"Section:\n{section}"
    )


def test_status_vocabulary_names_the_burn_in_substates() -> None:
    """The valid-status enumeration must list the three burn-in substates.

    `/plan-proposal` reads the Open Proposals status column to decide whether a
    proposal is planning fodder. An enumeration that names a bare `burn-in`
    describes a token no row carries and omits the three that rows actually do.
    """
    doc = _PLAN_PROPOSAL_DOC.read_text(encoding="utf-8")
    line = line_containing(doc, "valid status values")

    for token in ("burn-in:ready", "burn-in:observing", "burn-in:graduated"):
        assert token in line, (
            f"The valid-status enumeration must name {token!r}; the burn-in "
            f"status is typed and the bare form is rejected. Line:\n{line}"
        )


def test_burn_in_status_flip_picks_a_substate() -> None:
    """The flip-to-burn-in rule must write a substate, chosen from the Status Key.

    Mirrors `test_burn_in_status_flip_picks_a_substate` in
    `tests/test_update_proposals_command_doc.py`: both commands carry the same
    reconciliation rule, so both need the same *positive* guard.
    `test_burn_in_criteria_are_not_restated` below only forbids a second copy of
    the entry criteria — it passes just as happily on a doc that drops the
    Status Key pointer altogether, leaving the writer to guess the substate or
    fall back to the bare `burn-in` the vocabulary lint rejects.
    """
    doc = _PLAN_PROPOSAL_DOC.read_text(encoding="utf-8")
    rule = line_containing(doc, "leave those deferred")

    assert "`burn-in:<substate>`" in rule, (
        "The flip rule must write a `burn-in:<substate>` token, not a bare "
        f"`burn-in`. Rule:\n{rule}"
    )
    for token in ("burn-in:ready", "burn-in:observing", "burn-in:graduated"):
        assert token in rule, (
            f"The flip rule must name {token!r} as one of the three choices, so "
            f"the substate is chosen rather than guessed. Rule:\n{rule}"
        )
    assert "Status Key" in rule, (
        "The flip rule must send the writer to the README Status Key for the "
        "entry criteria rather than carrying them itself — see "
        f"`test_burn_in_criteria_are_not_restated`. Rule:\n{rule}"
    )


def test_burn_in_readme_row_rule_writes_a_substate_token() -> None:
    """The Open Proposals row rule must write a substate token, never a bare one.

    `tests/test_proposal_status_vocabulary.py` rejects a bare `burn-in` cell and
    binds it to the linked doc's own `Status`, so an instruction to write the
    bare token turns the next status reconciliation into a red CI run.
    """
    doc = _PLAN_PROPOSAL_DOC.read_text(encoding="utf-8")
    rule = line_containing(doc, "keep its row in **Open Proposals**")

    assert "`burn-in:<substate>`" in rule, (
        "The Open Proposals row rule must write the `burn-in:<substate>` token "
        f"shape into the Status cell. Rule:\n{rule}"
    )
    assert "(Status `burn-in`)" not in rule, (
        "The rule must not instruct writing a bare `burn-in` status cell — the "
        f"status vocabulary lint rejects it. Rule:\n{rule}"
    )
    assert "remaining work and tripwires" in rule, (
        "The burn-in callout entry carries only remaining work and tripwires; "
        "the rollout position is carried by the substate token alone, so the "
        f"rule must say not to restate it in the bullet. Rule:\n{rule}"
    )


def test_burn_in_criteria_are_not_restated() -> None:
    """This doc must not carry its own copy of the substate entry criteria.

    The criteria that pick between `burn-in:ready` / `burn-in:observing` /
    `burn-in:graduated` live in one place — the `### Status Key` of
    `docs/proposals/README.md`. A paraphrase here is a second carrier of the same
    fact, and it drifts: the copy this guard replaced had already dropped
    `burn-in:graduated`'s "or the observation evidence is in" branch — the branch
    that types proposal 111 — so a writer following this file would have re-typed
    111 wrongly. Naming the three tokens is fine; defining them here is not.
    """
    assert_no_restated_substate_criterion(
        _PLAN_PROPOSAL_DOC.read_text(encoding="utf-8"), "plan-proposal.md"
    )
