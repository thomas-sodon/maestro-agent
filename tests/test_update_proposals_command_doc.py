"""Regression: update-proposals.md grounds on the latest `main` before verifying.

`/update-proposals` reads today's tree first-hand — it reads each `Affects`
file, re-derives status, flips Implementation-Plan checkboxes, and rewrites
citations — then mutates the proposal files in place. But it used to jump
straight from its intro into `## Resolve scope`, with no step landing on a
current tree first. Run from a leftover task branch already merged into `main`,
or from a local `main` trailing `origin/main`, it verified against **stale
code**: it could flip a proposal to `shipped`, mark steps `[x]`, or rewrite a
citation to match a symbol that no longer reflects `main` — the exact
wrong-verdict failure a first-hand verification is supposed to avoid.

The `## Switch to main if on a merged branch` section — mirroring the plan-family
commands and the `/bug-hunt` precedent — must switch to `main` when provably
safe and, when already on a clean `main`, fetch `origin/main` and fast-forward
local `main` before verifying any proposal; and `## Safety` must admit that one
safe git-state change rather than claim the command is purely read-only.

Static-text only: reads the command doc, no `mem_fs`/subprocess/network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.command_docs import (
    assert_no_restated_substate_criterion,
    line_containing,
)

_UPDATE_PROPOSALS_DOC = (
    Path(__file__).resolve().parent.parent / ".claude" / "commands" / "update-proposals.md"
)

# String-slice helper pattern reused from
# tests/test_plan_family_command_doc.py (`_switch_section` / `_on_main_bullet`):
# the section shares the plan-family layout, so the same slicing anchors apply.


def _switch_section(doc: str) -> str:
    """Slice `## Switch to main …` up to the next top-level `## ` heading."""
    start_header = "## Switch to main if on a merged branch"
    start = doc.index(start_header)
    end = doc.index("\n## ", start + len(start_header))
    return doc[start:end]


def _on_main_bullet(section: str) -> str:
    """Slice the on-`main` sub-bullet, from the `main` arrow up to `HEAD`."""
    start = section.index("- `main`")
    end = section.index("- `HEAD`", start)
    return section[start:end]


def _safety_section(doc: str) -> str:
    """Slice the trailing `## Safety` section to the end of the document."""
    return doc[doc.index("## Safety") :]


def test_switch_to_main_section_present_and_refreshes() -> None:
    doc = _UPDATE_PROPOSALS_DOC.read_text(encoding="utf-8")

    assert "## Switch to main if on a merged branch" in doc, (
        "update-proposals.md must carry the `## Switch to main if on a merged "
        "branch` section so verification grounds on the latest `main`, not a "
        "stale or already-merged branch."
    )
    section = _switch_section(doc)
    on_main = _on_main_bullet(section)

    assert "git fetch origin main" in on_main, (
        "The on-`main` bullet must run the best-effort `git fetch origin main` "
        f"so verification reads the latest `main`. Bullet:\n{on_main}"
    )
    assert "git merge --ff-only origin/main" in on_main, (
        "The on-`main` bullet must fast-forward local `main` to `origin/main` "
        f"with `git merge --ff-only origin/main`, not skip the refresh. "
        f"Bullet:\n{on_main}"
    )
    assert "git merge-base --is-ancestor" in section, (
        "The section must gate the switch on the fully-merged "
        "`git merge-base --is-ancestor HEAD origin/main` test, so it fires only "
        f"when the branch adds nothing to `main`. Section:\n{section}"
    )
    assert "dirty" in on_main.lower(), (
        "The on-`main` bullet must skip the refresh on a dirty tree, leaving "
        f"uncommitted changes untouched. Bullet:\n{on_main}"
    )
    assert "warn" in on_main.lower() and "proceed on the current" in on_main.lower(), (
        "The on-`main` bullet must warn and proceed on the current `main` when "
        "the fetch is unavailable or the fast-forward fails, like every other "
        f"fallback in the section. Bullet:\n{on_main}"
    )


def test_safety_admits_switch_and_refresh() -> None:
    """`## Safety` must admit the one safe git-state change, not claim read-only.

    The switch section performs a `git switch main` + `git merge --ff-only
    origin/main` fast-forward, so a Safety note that still declares the command
    read-only against everything but the proposal files self-contradicts. The
    carve-out must name the switch and the fast-forward, mirroring bug-hunt.md's
    Safety bullet.
    """
    doc = _UPDATE_PROPOSALS_DOC.read_text(encoding="utf-8")
    safety = _safety_section(doc)

    assert "git switch main" in safety, (
        "The Safety note must admit the `git switch main` the switch section "
        f"performs, not claim the command is read-only against the repo. Safety:\n{safety}"
    )
    assert "fast-forward of local `main` to `origin/main`" in safety, (
        "The Safety note must admit the fast-forward of local `main` to "
        "`origin/main` the switch section performs, mirroring bug-hunt.md's "
        f"Safety bullet. Safety:\n{safety}"
    )


def test_burn_in_status_flip_picks_a_substate() -> None:
    """The flip-to-burn-in rule must write a substate, chosen from the Status Key.

    `burn-in` names three structurally distinct rollout positions. The flip rule
    is where the position is first asserted, so it must name the substate shape
    and send the writer to the criteria that pick between the three — otherwise
    the writer falls back to the bare token the vocabulary lint rejects.
    """
    doc = _UPDATE_PROPOSALS_DOC.read_text(encoding="utf-8")
    rule = line_containing(doc, "Do not force those deferred")

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
    assert "status flips to `burn-in`," not in rule, (
        "The rule must not instruct flipping to a bare `burn-in` status — the "
        f"vocabulary lint rejects that token. Rule:\n{rule}"
    )


def test_burn_in_readme_row_rule_writes_a_substate_token() -> None:
    """The Open Proposals row rule must write a substate token, never a bare one.

    `tests/test_proposal_status_vocabulary.py` binds the README status column to
    the linked doc's own `Status` and rejects the bare form, so writing a bare
    `burn-in` cell here turns the next reconciliation into a red CI run.
    """
    doc = _UPDATE_PROPOSALS_DOC.read_text(encoding="utf-8")
    rule = line_containing(doc, "keep the row in **Open Proposals**")

    assert "`burn-in:<substate>`" in rule, (
        "The Open Proposals row rule must write the `burn-in:<substate>` token "
        f"shape into the Status cell. Rule:\n{rule}"
    )
    assert "`Status` cell set to `burn-in`," not in rule, (
        "The rule must not instruct writing a bare `burn-in` status cell — the "
        f"status vocabulary lint rejects it. Rule:\n{rule}"
    )
    assert "do not restate it in the bullet" in rule, (
        "The callout entry carries only remaining work and tripwires; the "
        "rollout position lives in the substate token alone, so the rule must "
        f"say not to restate it in the bullet. Rule:\n{rule}"
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
        _UPDATE_PROPOSALS_DOC.read_text(encoding="utf-8"), "update-proposals.md"
    )


def test_restated_criterion_detector_catches_seeded_paraphrase() -> None:
    """Negative control: the seeded paraphrase this run removed must be caught.

    Without it the guard above could pass vacuously — a detector that matches
    nothing agrees with every doc. The seed is verbatim the wording that shipped
    in this file's flip rule and had already dropped `burn-in:graduated`'s "or
    the observation evidence is in" branch.
    """
    seeded = (
        "Pick the substate against the README Status Key's entry criteria: "
        "`burn-in:ready` when the observation window has not started (no canary "
        "deployed, the flag default-off everywhere), `burn-in:observing` when "
        "the canary is live in production and the window is running."
    )
    with pytest.raises(AssertionError, match="restates a burn-in substate"):
        assert_no_restated_substate_criterion(seeded, "seeded.md")

    # And it stays quiet on a doc that merely *names* the three tokens.
    assert_no_restated_substate_criterion(
        "Pick which of `burn-in:ready` / `burn-in:observing` / "
        "`burn-in:graduated` by reading the **Status Key**.",
        "ok.md",
    )
