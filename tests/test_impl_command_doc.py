"""Regression: impl.md's proposal-mode auto-reconcile flow stays self-consistent.

PR #2685 added the *Auto-reconcile the proposal document* routine. Review
surfaced two coherence gaps in that flow:

* **Orphaned reconcile edits (🐛).** The all-steps-done / pre-selection
  reconcile paths invoke `/update-proposals` (which writes `docs/proposals/**`
  and `README.md`) and then terminate at `proposal_complete` *before* the
  *Create the implementation branch* section ever runs. If HEAD is `main`, the
  edits orphan on `main`, and `/commit-push` (which refuses `main`) cannot ship
  them.
  The fix makes the routine create a feature branch *before* any write, so the
  edits always have a shippable home — and drops the old, now-false claim that
  "branch creation (1b) carries the edits onto the new feature branch."

* **Pre-committed terminal outcome (💡).** The *No pending step* bullet
  hard-coded "then stop with `proposal_complete`", contradicting the routine's
  *Re-select* step, which can legitimately surface a reverted-out `[x]`→`[ ]`
  step to implement. The fix defers the terminal outcome to *Re-select*.

* **Phantom repo-root README target (💡).** Step 1 named `README.md` as a
  write target separate from `docs/proposals/**`, but `/update-proposals` only
  ever edits `docs/proposals/**/*.md` and `docs/proposals/README.md` — never
  the repo-root README. The stray reference contradicted step 3 and the
  dirty-tree whitelist; the fix names `docs/proposals/README.md` under
  `docs/proposals/**`.

* **Branch-before-dirty-tree-gate ordering (💡).** Step 1 created a feature
  branch via `git checkout -b` whenever HEAD is `main`, but two of the routine's
  triggers (*Corroborate* and *No pending step*, in Proposal-mode step 3) fire
  *before* the flow reaches *Review*'s Dirty-tree gate (1a). On those paths the
  branch was auto-created over a possibly-unrelated dirty tree, contradicting
  1a's "never auto-branch over an unrelated dirty tree" invariant. The fix has
  step 1 apply the dirty-tree gate *before* creating any branch.

These tests pin the corrected wording so a future edit cannot silently
reintroduce any of these contradictions.
"""

from __future__ import annotations

from pathlib import Path

from tests.support.command_docs import line_containing

_IMPL_DOC = Path(__file__).resolve().parent.parent / ".claude" / "commands" / "impl.md"


def _section_between(text: str, start_header: str, end_header: str) -> str:
    start = text.index(start_header)
    end = text.index(end_header, start + len(start_header))
    return text[start:end]


def test_no_pending_step_defers_terminal_outcome_to_reselect() -> None:
    doc = _IMPL_DOC.read_text(encoding="utf-8")

    bullet = line_containing(doc, "**No pending step.**")

    # Defers the terminal decision to the Re-select routine rather than
    # pre-committing to proposal_complete (the 💡 finding).
    assert "Re-select" in bullet, (
        "The *No pending step* bullet must hand the terminal outcome to the "
        "*Re-select* routine, which can surface a now-pending step. Bullet:\n"
        f"{bullet}"
    )
    assert "pre-commit" in bullet, (
        "The *No pending step* bullet must explicitly forbid pre-committing to "
        f"`proposal_complete`, deferring to *Re-select*. Bullet:\n{bullet}"
    )
    # The old hard-coded outcome — "then stop with `proposal_complete`" right
    # after invoking reconcile — must be gone.
    assert "then stop with `proposal_complete`" not in bullet, (
        "The *No pending step* bullet must not hard-code 'then stop with "
        "`proposal_complete`' immediately after reconcile; that contradicts "
        f"*Re-select*. Bullet:\n{bullet}"
    )


def test_auto_reconcile_branches_before_writing() -> None:
    doc = _IMPL_DOC.read_text(encoding="utf-8")

    section = _section_between(
        doc,
        "## Auto-reconcile the proposal document",
        "## Refresh `main` before branching",
    )

    # A branch is created before any write so the edits never orphan on main
    # (the 🐛 finding).
    assert "Ensure a shippable branch first" in section, (
        "The Auto-reconcile routine must create a feature branch before "
        "invoking `/update-proposals`, so the doc edits are never orphaned on "
        "`main` when the flow terminates at `proposal_complete`."
    )
    assert "feat/<NNN>-reconcile" in section, (
        "The branch-first step must name a `feat/<NNN>-reconcile` branch for "
        "the all-steps-done / pre-selection case where no step number exists."
    )

    # The old false guarantee — that branch creation always carries the edits —
    # must be gone; the terminal proposal_complete path never reaches the
    # *Create the implementation branch* section at all.
    assert "carries the edits onto the new feature branch" not in section, (
        "The Auto-reconcile routine must no longer claim *Create the "
        "implementation branch* carries the reconcile edits onto the branch — "
        "the terminal `proposal_complete` path never reaches that section."
    )


def test_auto_reconcile_renumbered_steps_are_sequential() -> None:
    doc = _IMPL_DOC.read_text(encoding="utf-8")

    section = _section_between(
        doc,
        "## Auto-reconcile the proposal document",
        "## Refresh `main` before branching",
    )

    # Top-level ordered steps (no leading indent) must read 1..5 with the new
    # branch-first step inserted, so the prose's "Step 1 already ensured ..."
    # cross-reference stays accurate.
    step_numbers = [
        int(line[0])
        for line in section.splitlines()
        if line[:2] in {f"{n}." for n in range(1, 10)}
    ]
    assert step_numbers == [1, 2, 3, 4, 5], (
        "The Auto-reconcile routine's top-level steps must be sequential 1..5 "
        f"after inserting the branch-first step; found {step_numbers}."
    )
    cross_ref = line_containing(doc, "Step 1 already ensured")
    assert "non-`main` feature branch" in cross_ref, (
        "Step 3 must reference the branch ensured by the new step 1."
    )


def test_auto_reconcile_applies_dirty_tree_gate_before_branching() -> None:
    doc = _IMPL_DOC.read_text(encoding="utf-8")

    step1 = line_containing(doc, "Ensure a shippable branch first")

    # The dirty-tree gate (*Review*'s 1a) must run before any branch is
    # created: two step-3 triggers fire before *Review*'s own 1a, so without
    # this the branch would be auto-created over a possibly-unrelated dirty
    # tree, contradicting 1a's invariant (the 💡 finding).
    assert "Dirty-tree gate" in step1, (
        "Step 1 must apply the Dirty-tree gate (*Review*'s 1a) before "
        f"creating a branch on the pre-*Review* trigger paths. Step 1:\n{step1}"
    )
    assert step1.index("Dirty-tree gate") < step1.index(
        "auto-create a feature branch"
    ), (
        "The Dirty-tree gate must be applied *before* the branch is "
        f"auto-created in step 1, not after. Step 1:\n{step1}"
    )
    assert "never auto-branch over an unrelated dirty tree" in step1, (
        "Step 1 must preserve 1a's 'never auto-branch over an unrelated dirty "
        f"tree' invariant for the pre-*Review* trigger paths. Step 1:\n{step1}"
    )


def test_auto_reconcile_step1_names_proposals_readme_not_repo_root() -> None:
    doc = _IMPL_DOC.read_text(encoding="utf-8")

    step1 = line_containing(doc, "Ensure a shippable branch first")

    # /update-proposals edits docs/proposals/README.md, never the repo-root
    # README; Step 1 must name the reconciled README by its real path so it
    # stays consistent with step 3 ("touches `docs/proposals/**` and nothing
    # else") and the dirty-tree whitelist (the 💡 finding).
    assert "docs/proposals/README.md" in step1, (
        "Step 1 must name the reconciled README by its real path "
        "`docs/proposals/README.md` (which lives under `docs/proposals/**`). "
        f"Step 1:\n{step1}"
    )
    # The old contradiction — a bare repo-root `README.md` listed as a target
    # separate from `docs/proposals/**` — must be gone.
    assert "`docs/proposals/**` and `README.md`" not in step1, (
        "Step 1 must not name a bare repo-root `README.md` as a write target "
        "separate from `docs/proposals/**`; `/update-proposals` never edits the "
        f"repo-root README. Step 1:\n{step1}"
    )


def test_unique_branch_suffix_random_component_is_mandatory() -> None:
    """Regression (PR #2936, 🐛): the uid's random component must be mandatory.

    *Create the implementation branch* tells the collision-retry to "regenerate
    the suffix and retry". A collision can only occur when a prior invocation created the same
    branch in the *same* wall-clock second, so a timestamp-only generator
    (`date -u +%y%m%d%H%M%S`) regenerates the identical value and the retry
    loops without progress. Making the random hex mandatory guarantees every
    generated *and* regenerated suffix is distinct, so the retry always
    advances.
    """
    doc = _IMPL_DOC.read_text(encoding="utf-8")

    section = _section_between(
        doc,
        "## Unique branch suffix",
        "## Refresh `main` before branching",
    )

    assert "mandatory" in section, (
        "The *Unique branch suffix* section must state the random component is "
        f"mandatory, not optional. Section:\n{section}"
    )
    assert "openssl rand" in section, (
        "The section must keep the random-hex generator so every suffix carries "
        f"entropy. Section:\n{section}"
    )
    assert "loop without progress" in section, (
        "The section must explain *why* random entropy is mandatory: a "
        "timestamp-only suffix makes the collision-retry loop without progress. "
        f"Section:\n{section}"
    )
    # The old wording that marked the random component optional must be gone —
    # it is exactly what left the retry non-functional.
    assert "optionally with a couple of random hex characters" not in section, (
        "The section must no longer describe the random hex as optional; that "
        f"wording is the defect. Section:\n{section}"
    )

    # The same defect by another route: `UID` is read-only in bash, so assigning
    # to it fails and `$UID` expands to the numeric user id — a branch with no
    # entropy at all, exactly what the mandatory-random rule above forbids.
    assert "never assign to a variable named `UID`" in section, (
        "The section must forbid assigning to `UID`; bash treats it as read-only, "
        f"so the suffix silently degrades to a numeric uid. Section:\n{section}"
    )
    assert "read-only" in section, (
        "The section must say *why* `UID` is forbidden — bash treats it as "
        f"read-only — not merely that it is. Section:\n{section}"
    )


def test_collision_retry_draws_fresh_entropy() -> None:
    """Regression (PR #2936, 🐛): the retry must draw fresh entropy, not re-clock.

    The *Create the implementation branch* retry line must make explicit that regenerating the
    suffix draws fresh random hex, so the new token differs even within the same
    wall-clock second — otherwise re-reading a second-granularity clock yields
    the identical suffix and the retry cannot make progress.
    """
    doc = _IMPL_DOC.read_text(encoding="utf-8")

    retry = line_containing(doc, "fresh random hex")

    assert "regenerate the suffix" in retry, (
        "The retry line must still regenerate the suffix on a collision. "
        f"Line:\n{retry}"
    )
    assert "same wall-clock second" in retry, (
        "The retry line must state the regenerated token differs even within "
        f"the same wall-clock second. Line:\n{retry}"
    )


def _create_branch_section(doc: str) -> str:
    return _section_between(
        doc, "## Create the implementation branch", "## Implement the plan"
    )


def _review_section(doc: str) -> str:
    return _section_between(doc, "## Review the plan against today's tree", "## Decide")


def test_branch_creation_is_deferred_until_after_decide() -> None:
    """The branch is cut after *Decide* returns Go, not during the review.

    Creating it during *Review* left a stray empty branch behind on every run
    that stopped on a blocker. The section must therefore sit between *Decide*
    and *Implement the plan*, and *Review*'s 1b must create no branch at all.
    """
    doc = _IMPL_DOC.read_text(encoding="utf-8")

    assert (
        doc.index("## Decide")
        < doc.index("## Create the implementation branch")
        < doc.index("## Implement the plan")
    ), (
        "*Create the implementation branch* must sit between *Decide* and "
        "*Implement the plan*, so the branch is cut only after the review "
        "passed and before the first implementation edit."
    )

    gate = line_containing(doc, "Record HEAD, do not branch yet")
    assert "No branch is created in this section." in gate, (
        "*Review*'s 1b must state outright that it creates no branch; the "
        f"deferral is what keeps a blocked run from leaving a stray branch. 1b:\n{gate}"
    )

    # The prose above is a claim; this is the mechanical check behind it.
    # Re-adding a `git checkout -b` into 1b would leave every other assertion in
    # this module green, so pin the absence of the command itself.
    assert "git checkout -b" not in _review_section(doc), (
        "*Review* must contain no `git checkout -b`: branch creation is deferred "
        "to *Create the implementation branch* so a run that stops on a blocker "
        "leaves no stray empty branch behind."
    )


def test_review_1b_does_not_mis_record_an_auto_reconcile_branch() -> None:
    """Regression: a non-`main` HEAD at 1b is not necessarily *pre-existing*.

    Two *Auto-reconcile* triggers (*Corroborate against the metadata* and *No
    pending step*) fire in Proposal-mode step 3, before *Review* runs. On those
    paths the routine has already branched, so 1b observes a branch created this
    invocation. 1b disclaims itself only for the *branch decision*; its
    provenance record still feeds step 4 and the *Report* `Branch:` line, so
    recording it flatly as "pre-existing" mis-attributes an *Auto-reconcile*
    branch as one the user chose.
    """
    doc = _IMPL_DOC.read_text(encoding="utf-8")
    gate = line_containing(doc, "Record HEAD, do not branch yet")

    assert "not** necessarily pre-existing" in gate, (
        "1b must state that a non-`main` HEAD is not necessarily pre-existing, "
        f"since *Auto-reconcile* may have branched before this section. 1b:\n{gate}"
    )
    assert "created this invocation by Auto-reconcile" in gate, (
        "1b must offer the *Auto-reconcile*-created provenance alongside "
        f"pre-existing, so step 4 and *Report* are not mis-attributed. 1b:\n{gate}"
    )


def test_create_branch_reconfirms_tree_before_checkout() -> None:
    """Regression (🐛): the re-confirm must precede `git checkout -b`.

    *Review*'s dirty-tree gate (1a) now runs before the entire review pass, and
    *Decide* can pause for user confirmation — so an unrelated change may appear
    in that window. The re-check that closes it is only load-bearing if it runs
    *before* the branch is created; ordered after the checkout it cannot prevent
    branching over an unrelated tree.
    """
    doc = _IMPL_DOC.read_text(encoding="utf-8")
    section = _create_branch_section(doc)

    assert "Re-confirm the working tree first" in section, (
        "*Create the implementation branch* must re-run the dirty-tree check, "
        "since 1a ran before the whole review pass and the tree can have moved."
    )
    # Anchor on the *last* `git checkout -b` — the real command in step 3.
    # Earlier mentions are prose referring forward to it, so `index` would make
    # this assertion pass even with the steps reordered.
    assert section.index("Re-confirm the working tree first") < section.rindex(
        "git checkout -b"
    ), (
        "The working-tree re-confirm must come *before* the `git checkout -b`. "
        "A confirmation made after the branch exists cannot prevent branching "
        "over an unrelated dirty tree."
    )
    assert "never auto-branch over an unrelated dirty tree" in section, (
        "The re-confirm must preserve 1a's 'never auto-branch over an unrelated "
        "dirty tree' invariant across the deferral window."
    )

    # "Surface it and ask" leaves the decline path unstated: the old 1a said
    # outright not to proceed while an unrelated dirty tree remains, and step 5's
    # "never begin with HEAD on main" only *implies* the stop. Pin it explicitly.
    assert "If the user does not confirm the change is plan-related, stop here" in section, (
        "The re-confirm must state what happens when the user declines — stop "
        "without branching — rather than leaving the decline path to inference."
    )


def test_create_branch_rereads_head_rather_than_trusting_review() -> None:
    """Regression (🐛): HEAD must be re-read, not carried from *Review*'s 1b.

    In proposal mode *Auto-reconcile the proposal document* can create a branch
    *after* 1b observed `main`. Acting on that stale record would cut a second,
    redundant branch over the reconcile branch.
    """
    doc = _IMPL_DOC.read_text(encoding="utf-8")
    section = _create_branch_section(doc)

    assert "Re-read HEAD" in section, (
        "*Create the implementation branch* must re-read HEAD itself rather "
        "than trusting the value *Review*'s 1b recorded."
    )
    # Anchor on the full clause, not the bare word "stale": that word appearing
    # anywhere in the ~10-line section would satisfy the assertion without
    # binding the rationale it claims to pin.
    assert "the recorded value can be stale" in section, (
        "The section must say *why* HEAD is re-read: *Auto-reconcile* may have "
        "branched after 1b, making the recorded value stale."
    )

    gate = line_containing(doc, "Record HEAD, do not branch yet")
    assert "re-reads HEAD itself" in gate, (
        "1b must disclaim being the input to the branch decision, so a reader "
        f"cannot wire the stale record into the branch choice. 1b:\n{gate}"
    )


def test_branch_confirmation_line_covers_all_three_outcomes() -> None:
    """The printed `Branch:` line must offer every outcome step 4 records.

    Step 4 records three provenances (auto-created here, pre-created by
    *Auto-reconcile*, pre-existing) and the *Report* section lists all three, but
    the confirmation template originally offered only two — silently
    mis-reporting an *Auto-reconcile* branch as one the user chose.
    """
    doc = _IMPL_DOC.read_text(encoding="utf-8")
    section = _create_branch_section(doc)

    confirm = line_containing(doc, "Confirm before implementing")
    for outcome in ("auto-created from main", "pre-created by auto-reconcile", "pre-existing"):
        assert outcome in confirm, (
            f"The printed `Branch:` template must offer the {outcome!r} outcome, "
            f"matching the three step 4 records. Line:\n{confirm}"
        )

    # Top-level steps stay sequential so the "step 3" cross-references from
    # *Auto-reconcile* (the shared `git checkout -b`) keep pointing at the
    # checkout step.
    step_numbers = [
        int(line[0])
        for line in section.splitlines()
        if line[:2] in {f"{n}." for n in range(1, 10)}
    ]
    assert step_numbers == [1, 2, 3, 4, 5], (
        "*Create the implementation branch* steps must read 1..5 so the "
        f"'step 3' cross-references stay accurate; found {step_numbers}."
    )


def test_auto_reconcile_cross_references_the_checkout_step() -> None:
    """*Auto-reconcile* shares the checkout step, so its pointers must track it.

    Both references name the step that runs `git checkout -b`; if the section is
    renumbered without updating them they silently point at the wrong step.
    """
    doc = _IMPL_DOC.read_text(encoding="utf-8")
    section = _create_branch_section(doc)

    # The checkout must fall under step 3 — between the step-3 and step-4
    # headings — since that is the number both cross-references name.
    step3 = section.index("3. **If the freshly-read HEAD is `main`")
    step4 = section.index("4. **Record the outcome.**")
    checkout = section.rindex("git checkout -b")
    assert step3 < checkout < step4, (
        "The `git checkout -b` must live under step 3, the number the "
        "*Auto-reconcile* cross-references name."
    )
    # Scope the cross-reference assertions to *Auto-reconcile* itself. Searching
    # the whole doc would keep passing even if both references migrated out of
    # the section that owns them, which is the coupling this test exists to pin.
    auto_reconcile = _section_between(
        doc,
        "## Auto-reconcile the proposal document",
        "## Refresh `main` before branching",
    )
    for ref in (
        "the same `git checkout -b` as *Create the implementation branch* step 3",
        "sanitized per *Create the implementation branch* step 3",
    ):
        assert ref in auto_reconcile, (
            f"*Auto-reconcile* must cross-reference the checkout step as it is "
            f"currently numbered; missing: {ref!r}"
        )


def test_review_refreshes_main_before_drift_checks() -> None:
    """Regression (💡, PR #3167): the review must validate against the tree the
    branch is cut from.

    Deferring branch creation past *Decide* originally carried the `main`
    fast-forward along with it, so on the HEAD==`main` path the drift checks
    (steps 2-8) read a possibly-stale local `main` while the branch was later
    cut from freshly-fetched `origin/main` — a plan reviewed 'clean' against
    stale code got implemented on a tree that could have drifted. The fix moves
    the fetch + `--ff-only` up front, *before* the drift checks, while keeping
    the `git checkout -b` deferred (fetch/ff creates no branch), and stops
    *Create the implementation branch* from re-fetching so the branch is cut
    from the exact tree the review validated.
    """
    doc = _IMPL_DOC.read_text(encoding="utf-8")
    review = _review_section(doc)

    # A 1c sub-gate refreshes local `main` during the review, before the checks.
    gate = line_containing(doc, "Refresh local `main` before the drift checks")
    assert "git merge --ff-only origin/main" in gate, (
        "Review step 1c must fast-forward local `main` to `origin/main` before "
        f"the drift checks on the HEAD==`main` path. 1c:\n{gate}"
    )

    # The refresh must precede the first drift check (step 2, *Cited files
    # exist*), so steps 2-8 read the refreshed tree.
    assert review.index("Refresh local `main` before the drift checks") < review.index(
        "**Cited files exist.**"
    ), (
        "Step 1c's refresh must run before the drift checks (step 2 onward), so "
        "the review validates against the same tree the branch is cut from."
    )

    # Freshness must not resurrect the stray branch the deferral removed: 1c
    # refreshes `main` but creates no branch (the checkout stays deferred). The
    # no-`git checkout -b`-in-*Review* invariant is already pinned by
    # test_branch_creation_is_deferred_until_after_decide; this asserts the
    # positive half — the branch cut does not re-fetch what 1c already fetched.
    create = _create_branch_section(doc)
    assert "do **not** re-fetch here" in create, (
        "*Create the implementation branch* step 3 must cut from the local "
        "`main` already refreshed during *Review*'s 1c rather than re-fetch — a "
        "second fetch could cut the branch from code the review never saw."
    )


def test_refresh_guarantee_admits_idempotent_second_run() -> None:
    """Regression (💡, PR #3167): the *Refresh `main`* section must not promise
    the refresh runs at most once.

    On the HEAD==`main` proposal-mode path *Review*'s 1c refreshes local `main`
    first (creating no branch, so HEAD stays `main`), and then a later drift
    check — Review step 7 *Already implemented* — can trigger *Auto-reconcile*,
    whose step 1 fast-forwards a *second* time over the still-`main` HEAD. The
    old "at most once per run" / "never runs twice" wording enumerated only the
    pre-*Review* Auto-reconcile ordering and was therefore false on this path.
    The corrected wording drops the absolute claim and rests the guarantee on
    the refresh's idempotency, explaining the second run is benign because
    *Auto-reconcile*'s *Re-select* re-runs the review afterwards.
    """
    doc = _IMPL_DOC.read_text(encoding="utf-8")
    section = _section_between(
        doc,
        "## Refresh `main` before branching",
        "## Review the plan against today's tree",
    )

    # The false absolute guarantees must be gone — Review step 7 can trigger
    # *Auto-reconcile* after 1c already refreshed, so the refresh can run twice.
    assert "never runs twice" not in section, (
        "The *Refresh `main`* section must not claim the refresh 'never runs "
        "twice'; Review step 7 can trigger *Auto-reconcile* after 1c already "
        f"refreshed, so it can run a second time. Section:\n{section}"
    )
    assert "at most once per run" not in section, (
        "The *Refresh `main`* section must not claim the refresh runs 'at most "
        f"once per run'; that over-promises on the step-7 ordering. Section:\n{section}"
    )

    # The corrected wording names the step-7 ordering and rests the guarantee on
    # idempotency rather than a single invocation.
    assert "idempotent" in section, (
        "The section must state the refresh is idempotent, which is what makes "
        f"the second run on the step-7 ordering harmless. Section:\n{section}"
    )
    assert "step 7" in section, (
        "The section must name the step-7 *Already implemented* ordering that "
        f"triggers *Auto-reconcile* after 1c and re-runs the refresh. Section:\n{section}"
    )
    # The second run is only safe because *Auto-reconcile* re-runs the review, so
    # the doc must say so rather than leaving the re-fetch hazard unexplained.
    assert "Re-select" in section, (
        "The section must explain the second refresh is benign because "
        "*Auto-reconcile*'s *Re-select* re-runs the review afterwards, so the "
        f"implementation is still validated against the branch's tree. Section:\n{section}"
    )


def test_dirty_tree_gate_carries_plan_proposal_reconcile_edits() -> None:
    """Regression: 1a's exception must carry `/plan-proposal`'s reconcile edits too.

    `/plan-proposal`'s *Reconcile completed steps into the proposal* step leaves
    uncommitted `docs/proposals/**` edits (completed-step checkbox flips + a
    status/README-row promotion) for `/impl` to carry onto the feature branch —
    its report even calls that diff "expected". The old 1a exception whitelisted
    only `/impl`'s *own* *Auto-reconcile the proposal document* edits, so a
    faithful reading treated the byte-identical `/plan-proposal` edits as
    "clearly unrelated", surfaced them, and stopped without branching —
    blocking the very carry-forward the workflow depends on. The exception must
    now recognize *both* sources, under a confinement narrow enough that a
    genuinely-unrelated dirty tree still stops the run.
    """
    doc = _IMPL_DOC.read_text(encoding="utf-8")
    bullet = line_containing(doc, "1a — Dirty-tree gate")

    # /impl's own Auto-reconcile source must survive — the broadening adds a
    # second source, it does not replace the first.
    assert "Auto-reconcile the proposal document" in bullet, (
        "1a's exception must still recognize `/impl`'s own *Auto-reconcile the "
        f"proposal document* edits as plan-related. Bullet:\n{bullet}"
    )
    # The new source: /plan-proposal's upstream reconciliation, named by its
    # step so a reader can trace where the edits come from.
    assert "/plan-proposal" in bullet, (
        "1a's exception must now also recognize `/plan-proposal`-produced "
        f"`docs/proposals/**` edits, not only /impl's own Auto-reconcile. Bullet:\n{bullet}"
    )
    assert "Reconcile completed steps into the proposal" in bullet, (
        "1a's exception must name `/plan-proposal`'s *Reconcile completed steps "
        f"into the proposal* step as the source of the carried edits. Bullet:\n{bullet}"
    )

    # Confinement: only the *implemented* proposal's own plan + file + single
    # README row qualify, so an unrelated proposal edit is never swept in.
    assert "proposal-NNN-*" in bullet, (
        "1a's exception must confine the carry-forward to the proposal whose "
        "plan is being implemented (basename `proposal-NNN-*`), so a genuinely "
        f"unrelated proposal edit is not treated as plan-related. Bullet:\n{bullet}"
    )
    assert "docs/proposals/README.md" in bullet, (
        "1a's exception must confine the README edit to proposal NNN's own "
        f"`docs/proposals/README.md` entries, not any README row. Bullet:\n{bullet}"
    )
    # The confinement must admit *every* README axis `/plan-proposal`'s
    # reconciliation legitimately touches for this proposal — not only its status
    # row but also a track-narrative sentence naming that proposal's status
    # (plan-proposal.md's *Reconcile completed steps into the proposal* step). A
    # literal "single row" reading would classify that legitimate edit as "an
    # extra README row" and stop the run, reintroducing the exact false-negative
    # this PR removes; the failing example must instead name a *different*
    # proposal's README edit.
    assert "track-narrative sentence" in bullet, (
        "1a's exception must admit proposal NNN's own track-narrative README "
        "sentence, not only its status row, so `/plan-proposal`'s legitimate "
        f"status-axis reconciliation is not stopped as unrelated. Bullet:\n{bullet}"
    )
    assert "a README edit about a different proposal" in bullet, (
        "1a's failing-confinement example must be a README edit about a "
        "*different* proposal, not a bare 'extra README row' — the latter would "
        f"catch proposal NNN's own track-narrative sentence. Bullet:\n{bullet}"
    )

    # The invariant survives verbatim: edits failing the confinement test — a
    # different proposal, or an ad-hoc `/plan` plan — still stop the run.
    assert "never auto-branch over an unrelated dirty tree" in bullet, (
        "1a must keep the 'never auto-branch over an unrelated dirty tree' "
        "invariant for edits that fail the confinement test, so a genuinely "
        f"unrelated dirty tree still stops the run. Bullet:\n{bullet}"
    )


def test_report_notes_carried_plan_proposal_reconcile_edits() -> None:
    """The *Report* must flag `/plan-proposal` reconcile edits carried on the branch.

    When `/impl` auto-branches over `/plan-proposal`'s uncommitted
    `docs/proposals/**` edits (per the broadened 1a exception), the branch diff
    contains doc changes the operator did not make this run. The *Report*
    "Proposal reconciliation" bullet must say so — and that they ride the
    implementation PR — so those changes are not a surprise.
    """
    doc = _IMPL_DOC.read_text(encoding="utf-8")
    bullet = line_containing(doc, "**Proposal reconciliation**")

    assert "/plan-proposal" in bullet, (
        "The *Report* 'Proposal reconciliation' bullet must note when the "
        "branch carries `/plan-proposal`-produced `docs/proposals/**` "
        f"reconciliation edits for this proposal. Bullet:\n{bullet}"
    )
    assert "ride the implementation PR" in bullet, (
        "The bullet must say the carried `/plan-proposal` edits are part of the "
        "branch diff and ride the implementation PR, so the operator is not "
        f"surprised by doc changes they did not make this run. Bullet:\n{bullet}"
    )


def test_burn_in_refusal_matches_the_substate_token_shape() -> None:
    """The `error=proposal_in_burn_in` trigger must match a `burn-in` *prefix*.

    The proposal status vocabulary is typed: a burn-in proposal carries
    `burn-in:ready` / `burn-in:observing` / `burn-in:graduated`, and a bare
    `burn-in` is rejected by `tests/test_proposal_status_vocabulary.py`. A
    trigger that fires only on a `Status` *equal to* `burn-in` therefore matches
    no proposal in the tree, silently un-gating the human-gated graduation steps
    this refusal exists to protect.
    """
    doc = _IMPL_DOC.read_text(encoding="utf-8")
    bullet = line_containing(doc, "**Burn-in proposals are human-gated.**")

    assert "burn-in:ready" in bullet, (
        "The refusal trigger must name the typed substate shape "
        "(`burn-in:ready` / `burn-in:observing` / `burn-in:graduated`), so the "
        f"reader knows what a real `Status` token looks like. Bullet:\n{bullet}"
    )
    assert "*starts with* `burn-in`" in bullet, (
        "The trigger must match a `Status` token that *starts with* `burn-in`, "
        "not one equal to it — no proposal doc carries the bare token, so an "
        f"equality match never fires. Bullet:\n{bullet}"
    )
    assert "`Status` is `burn-in`" not in bullet, (
        "The trigger must not test for a `Status` *equal to* the bare "
        f"`burn-in`; that match is dead against the typed tree. Bullet:\n{bullet}"
    )
