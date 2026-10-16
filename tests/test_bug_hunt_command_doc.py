"""Regression: bug-hunt.md validates `--focus` values and keeps examples in-rule.

Two defects flagged on PR #2626 and pinned here so the spec cannot regress:

* `--focus` had no value-validation step (unlike `--area`), so a recognized
  flag form carrying an out-of-set value (`--focus=bug`, `--focus=security`)
  slipped past the token-rejection rule and ran an empty/unmatched category
  hunt that silently found nothing instead of failing with
  `error=invalid_argument`. Both the flag description and the parse step must
  now reject out-of-set `--focus` values.
* The example titles modelled over-length strings, contradicting the file's
  own "under 80 characters" rule and giving the LLM contradictory guidance.
  Every `[bug-hunt]` example title must obey that rule.

Three further instruction-level inconsistencies flagged on PR #2946 (the
multi-finding extension) and pinned here:

* The dry-run separator was a bare `---`, but every issue body template embeds
  a `---` horizontal rule before its footer, so the same token appeared both
  inside and between printed bodies — an ambiguous boundary. The dry-run step
  must use a distinctive header separator that cannot collide with the rule.
* The step-5 dedup criterion keyed on "same root cause", but root cause is not
  formally determined until step 6, a circular ordering. Step-5 dedup must key
  only on signals available at step 5 (code location + the preliminary defect
  classification from the deep-read).
* Step 10's `--dry-run` branch prints the drafts "and stop", yet step 11 still
  enumerated `--dry-run` as a "nothing was posted" case it summarizes — a dead,
  unreachable clause that told an executing agent to both stop at step 10 and
  reach step 11. Step 11 must not claim to handle dry-run; it must instead state
  that dry-run never reaches it.

One further gap pinned here: bug-hunt.md read today's tree first-hand but never
landed on a current one, so a run from a merged task branch or a stale local
`main` hunted stale code and could file issues for bugs already fixed upstream.
The `## Switch to main if on a merged branch` section — mirroring the plan-family
commands — must switch to `main` when provably safe and, when already on a clean
`main`, fetch `origin/main` and fast-forward local `main`; the whole section is a
no-op under `--dry-run`, and `## Safety` must admit the one safe git-state change.
"""

from __future__ import annotations

import re
from pathlib import Path

_BUG_HUNT_DOC = (
    Path(__file__).resolve().parent.parent / ".claude" / "commands" / "bug-hunt.md"
)

# `--focus` and its rejection clause must co-occur within a single sentence
# (no sentence-ending period between them) so the rule reads as one statement.
_FOCUS_REJECTION = re.compile(r"--focus[^.]*error=invalid_argument", re.DOTALL)


def _focus_flag_bullet(doc: str) -> str:
    matches = [
        line.strip()
        for line in doc.splitlines()
        if line.strip().startswith("- `--focus=")
    ]
    assert len(matches) == 1, (
        f"Expected exactly one `--focus=` flag bullet, found {len(matches)}."
    )
    return matches[0]


def _parse_step(doc: str) -> str:
    start = doc.index("1. **Parse")
    end = doc.index("2. **Authentication", start)
    return doc[start:end]


def _example_titles(doc: str) -> list[str]:
    return [
        line.strip()[len("- `") : -1]
        for line in doc.splitlines()
        if line.strip().startswith("- `[bug-hunt]") and line.strip().endswith("`")
    ]


def _post_or_preview_step(doc: str) -> str:
    start = doc.index("10. **Post or preview")
    end = doc.index("11. **Return", start)
    return doc[start:end]


def _return_step(doc: str) -> str:
    start = doc.index("11. **Return")
    end = doc.index("## Issue body template", start)
    return doc[start:end]


def _dedupe_clause(doc: str) -> str:
    start = doc.index("5. **Deep-read and confirm")
    end = doc.index("6. **Determine the root cause", start)
    confirm_step = doc[start:end]
    return confirm_step[confirm_step.index("Dedupe confirmed findings") :]


def test_focus_values_validated_against_category_set() -> None:
    doc = _BUG_HUNT_DOC.read_text(encoding="utf-8")

    bullet = _focus_flag_bullet(doc)
    assert _FOCUS_REJECTION.search(bullet), (
        "The `--focus=` flag description must reject an out-of-set value with "
        f"`error=invalid_argument`, parallel to the `--area` rule. Bullet:\n{bullet}"
    )

    parse_step = _parse_step(doc)
    assert _FOCUS_REJECTION.search(parse_step), (
        "Step 1 (Parse $ARGUMENTS) must validate each `--focus` value against "
        "the category set and stop with `error=invalid_argument` on an "
        f"unrecognized value, so the parse contract is enforced. Step:\n{parse_step}"
    )


def test_example_titles_obey_under_80_rule() -> None:
    doc = _BUG_HUNT_DOC.read_text(encoding="utf-8")

    titles = _example_titles(doc)
    assert len(titles) == 3, (
        f"Expected three `[bug-hunt]` example titles, found {len(titles)}: {titles}"
    )
    for title in titles:
        assert len(title) < 80, (
            f"Example title violates the file's own 'under 80 characters' rule "
            f"({len(title)} chars): {title!r}"
        )


def test_dry_run_separator_does_not_collide_with_body_rule() -> None:
    doc = _BUG_HUNT_DOC.read_text(encoding="utf-8")

    # The issue body template embeds a `---` horizontal rule before its footer,
    # so every printed body already contains a bare `---` line. This is the
    # collision the dry-run separator must avoid; keep it as the precondition.
    template = doc[doc.index("## Issue body template") :]
    assert "\n---\n" in template, (
        "The issue body template must still contain the `---` horizontal rule "
        "that the dry-run separator is guarded against colliding with."
    )

    dry_run = _post_or_preview_step(doc)
    # The dry-run separator must be a distinctive header, never a bare `---`
    # divider, or the between-finding boundary is ambiguous with the in-body rule.
    assert "===== FINDING" in dry_run, (
        "Dry-run must separate printed findings with a distinctive "
        "`===== FINDING <n>/<total> =====` header, not a `---` divider that "
        f"collides with the body's horizontal rule. Step 10:\n{dry_run}"
    )
    assert "`---` divider" not in dry_run, (
        "Dry-run must not describe a bare `---` divider as the separator — it "
        f"collides with the `---` rule inside each body. Step 10:\n{dry_run}"
    )


def test_dry_run_stops_at_step10_and_step11_does_not_claim_it() -> None:
    doc = _BUG_HUNT_DOC.read_text(encoding="utf-8")

    # Step 10's dry-run branch terminates the run ("and stop"), so step 11's
    # summary is unreachable in dry-run mode. This is the precondition that
    # makes step 11's old dry-run clause dead code.
    post_step = _post_or_preview_step(doc)
    assert "and stop" in post_step, (
        "Step 10's `--dry-run` branch must terminate the run with 'and stop', "
        f"so dry-run never reaches step 11's summary. Step 10:\n{post_step}"
    )

    # Step 11 must not enumerate `--dry-run` as a 'nothing was posted' case it
    # summarizes — that clause is unreachable once dry-run stops at step 10.
    return_step = _return_step(doc)
    assert "or `--dry-run`" not in return_step, (
        "Step 11 must not list `--dry-run` as a 'nothing was posted' case — "
        "step 10's 'and stop' makes that clause unreachable, so it contradicts "
        f"step 10. Step 11:\n{return_step}"
    )
    # It must instead state explicitly that dry-run never reaches step 11.
    assert "never reaches this step" in return_step, (
        "Step 11 must state that `--dry-run` never reaches it (it prints and "
        f"stops at step 10), resolving the contradiction. Step 11:\n{return_step}"
    )


def test_step5_dedup_keys_on_step5_signals_not_step6_root_cause() -> None:
    doc = _BUG_HUNT_DOC.read_text(encoding="utf-8")

    dedupe = _dedupe_clause(doc)
    # Root cause is not formally determined until step 6, so the step-5 dedup
    # criterion must not key on it — that is the circular ordering being fixed.
    assert "same root cause" not in dedupe, (
        "Step-5 dedup must not key on 'same root cause' — root cause is not "
        f"determined until step 6. Dedupe clause:\n{dedupe}"
    )
    # It must instead key on a signal available at step 5.
    assert "preliminary defect classification" in dedupe, (
        "Step-5 dedup must key on the preliminary defect classification from "
        f"the deep-read, available at step 5. Dedupe clause:\n{dedupe}"
    )


# --- `## Switch to main if on a merged branch` section (mirrors the plan family) ---
#
# String-slice helper pattern reused from
# tests/test_plan_family_command_doc.py (`_switch_section` / `_on_main_bullet`
# / `_section_intro`): the section shares the plan-family layout, so the same
# slicing anchors apply.


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


def _section_intro(section: str) -> str:
    """Slice the section intro — the header/preamble up to the first `1. ` step."""
    end = section.index("1. ")
    return section[:end]


def _safety_section(doc: str) -> str:
    """Slice the trailing `## Safety` section to the end of the document."""
    return doc[doc.index("## Safety") :]


def test_switch_to_main_section_present_and_refreshes() -> None:
    doc = _BUG_HUNT_DOC.read_text(encoding="utf-8")

    assert "## Switch to main if on a merged branch" in doc, (
        "bug-hunt.md must carry the `## Switch to main if on a merged branch` "
        "section so the hunt grounds on the latest `main`, not a stale or "
        "already-merged branch."
    )
    section = _switch_section(doc)
    on_main = _on_main_bullet(section)

    assert "git fetch origin main" in on_main, (
        "The on-`main` bullet must run the best-effort `git fetch origin main` "
        f"so the hunt reads the latest `main`. Bullet:\n{on_main}"
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


def test_dry_run_carve_out_excludes_on_main_refresh() -> None:
    """The section intro's `--dry-run` carve-out must cover the on-`main` refresh.

    The on-`main` refresh (`git fetch origin main` + `git merge --ff-only
    origin/main`) is a git-state mutation. If the carve-out excludes only the
    branch *switch*, a dry-run while already on `main` (no switch to skip) still
    fast-forwards local `main`, breaking the "dry-run changes no git state"
    contract. Mirrors `test_plan_family_command_doc.py::
    test_bug_fix_dry_run_excludes_on_main_refresh`.
    """
    doc = _BUG_HUNT_DOC.read_text(encoding="utf-8")
    intro = _section_intro(_switch_section(doc))

    assert "--dry-run" in intro, (
        "The switch-section intro must carry a `--dry-run` carve-out. "
        f"Intro:\n{intro}"
    )
    assert "changes no git state" in intro, (
        "The `--dry-run` carve-out must declare the section changes no git "
        f"state, so the on-`main` refresh is not reachable under `--dry-run`. "
        f"Intro:\n{intro}"
    )
    assert "refresh" in intro, (
        "The `--dry-run` carve-out must name the on-`main` refresh (not only the "
        "switch) as a skipped git-state mutation, else a dry-run on `main` "
        f"fast-forwards local `main`. Intro:\n{intro}"
    )


def test_safety_admits_switch_and_refresh() -> None:
    """`## Safety` must admit the one safe git-state change, not claim read-only.

    The section performs a `git switch main` + `git merge --ff-only origin/main`
    fast-forward, so a Safety note that still declares the command purely
    read-only against the repo self-contradicts. The carve-out must name the
    switch and the fast-forward while keeping the prohibition on editing source,
    committing, and pushing.
    """
    doc = _BUG_HUNT_DOC.read_text(encoding="utf-8")
    safety = _safety_section(doc)

    assert "git switch main" in safety, (
        "The Safety note must admit the `git switch main` the switch section "
        f"performs, not claim the command is purely read-only. Safety:\n{safety}"
    )
    assert "fast-forward of local `main` to `origin/main`" in safety, (
        "The Safety note must admit the fast-forward of local `main` to "
        "`origin/main` the switch section performs, mirroring plan.md's Safety "
        f"bullet. Safety:\n{safety}"
    )
    assert "must not edit source files, commit, push branches" in safety, (
        "The Safety note must still forbid editing source files, committing, and "
        f"pushing branches — only the switch/refresh is carved out. Safety:\n{safety}"
    )
