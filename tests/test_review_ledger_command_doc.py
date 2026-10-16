"""Regression: the cross-session review-findings ledger stays self-consistent.

`/review-changes` and `/polish` gained a durable, per-branch findings ledger at
`~/.claude/reviews/<branch-slug>.json` so a review's findings survive across
sessions — the review-side analog of the plan file `/impl` reads back. Before
this, a review's output lived only in the chat response: an interrupted review,
or a `/polish` resumed in a fresh session, had no memory of what was already
raised or already fixed.

The contract these tests pin:

* **`/review-changes` is the writer.** It recalls the ledger before reviewing,
  reconciles this run's findings against it (matching by file + title, never by
  drifting line numbers), and writes the merged result back on *every* run —
  clean or not — so a clean review still flips previously-open findings to
  `fixed`. The status lifecycle is `new` → `open` → `fixed`, with a reappearing
  `fixed` finding flipped back to `open` as a regression.
* **Cross-checkout safety.** The ledger embeds `repo` + `branch`; a read whose
  `repo`/`branch` do not match is treated as absent, so a sibling checkout that
  reused the branch name (or a slug collision) can never read another tree's
  findings.
* **`/polish` is the reader/closer.** It recalls the ledger so prior findings
  stay in scope even with no findings list in `$ARGUMENTS`, and marks the
  findings it resolves `fixed` after gates pass — never one whose fix was
  reverted.
* **Both writes stay outside the repo.** The ledger under `~/.claude/reviews/`
  is the only file either command writes outside the working tree; each
  command's Safety section says so, and `/review-changes` no longer claims it
  modifies *no* files at all.
* **Two sessions, not one.** `/review-changes` no longer invokes the `polish`
  skill in-session; reviewing and fixing are separate sessions bridged by the
  ledger, which also carries the **plan context** (`plan_context`/`plan_path`)
  so `/polish` fixes respect the original design intent. A review that recovers
  no plan context must not wipe a prior ledger's `plan_context`.

These tests pin the wording so a future edit cannot silently drop the ledger,
break the cross-checkout guard, or let a command write the repo by surprise.
"""

from __future__ import annotations

from pathlib import Path

_COMMANDS = Path(__file__).resolve().parent.parent / ".claude" / "commands"
_REVIEW_DOC = _COMMANDS / "review-changes.md"
_POLISH_DOC = _COMMANDS / "polish.md"


def _section_between(text: str, start_header: str, end_header: str) -> str:
    start = text.index(start_header)
    end = text.index(end_header, start + len(start_header))
    return text[start:end]


def test_review_changes_defines_per_branch_ledger() -> None:
    doc = _REVIEW_DOC.read_text(encoding="utf-8")
    section = _section_between(doc, "## Review-findings ledger", "## What to skip")

    assert "~/.claude/reviews/<branch-slug>.json" in section, (
        "The ledger must live per-branch under ~/.claude/reviews/."
    )
    # Status lifecycle is the load-bearing contract of the reconciled format.
    assert "new | open | fixed" in section, (
        "The ledger must document the new | open | fixed status lifecycle."
    )
    # Line numbers drift, so the match key must be file + title, not line.
    assert "never the match key" in section, (
        "Reconciliation must match by file + title, never by drifting line."
    )


def test_review_changes_ledger_guards_against_foreign_checkout() -> None:
    doc = _REVIEW_DOC.read_text(encoding="utf-8")
    section = _section_between(doc, "## Review-findings ledger", "## What to skip")

    # A sibling checkout reusing the branch name, or a slug collision, must not
    # leak another tree's findings into this review.
    assert "never read another tree's findings" in section, (
        "Recall must reject a ledger whose repo/branch do not match this tree."
    )
    # The generic phrase above survives an AND regression, so pin the disjunction
    # explicitly: the guard must fire when *either* repo or branch mismatches.
    assert (
        "does not equal this working directory or its `branch` does not equal"
        in section
    ), "Recall must reject when repo OR branch mismatches, not only when both do."


def test_review_changes_ledger_reconciles_fixed_and_regressions() -> None:
    doc = _REVIEW_DOC.read_text(encoding="utf-8")
    section = _section_between(doc, "## Review-findings ledger", "## What to skip")

    assert "fixed since last review" in section, (
        "A recalled finding absent this run must be reconciled to `fixed`."
    )
    assert "a regression" in section, (
        "A previously-fixed finding that reappears must flip back to `open` as "
        "a regression."
    )
    # Best-effort atomic write, mirroring the plan-pointer convention.
    assert "~/.claude/reviews/<branch-slug>.json.tmp.<pid>" in section, (
        "The ledger write must be atomic (tmp file + rename)."
    )


def test_review_changes_persists_on_every_run() -> None:
    doc = _REVIEW_DOC.read_text(encoding="utf-8")

    persist = [
        line
        for line in doc.splitlines()
        if "**Persist the review-findings ledger.**" in line
    ]
    assert len(persist) == 1, (
        "review-changes must have exactly one persist step in its numbered flow."
    )
    # The write must not be gated on the /polish hand-off (which is skipped on a
    # clean review) — it runs on every review so `fixed` reconciliation happens.
    assert "every" in persist[0] and "clean or not" in persist[0], (
        "The persist step must run on every review, clean or not, so a clean "
        f"review still flips open findings to `fixed`. Step:\n{persist[0]}"
    )


def test_review_changes_safety_carves_out_the_ledger_write() -> None:
    doc = _REVIEW_DOC.read_text(encoding="utf-8")
    safety = doc[doc.index("## Safety") :]  # Safety is the final section
    # review-changes used to claim it modifies *no* files; that absolute is now
    # false (it writes the ledger), so the carve-out must be explicit.
    assert "Do not modify any files in this command." not in doc, (
        "The old 'modify any files' absolute must be qualified now the ledger "
        "is written."
    )
    assert "review-findings ledger** under `~/.claude/reviews/`" in safety, (
        "Safety must name the ledger as this command's sole (out-of-repo) write."
    )


def test_polish_recalls_and_closes_the_ledger() -> None:
    doc = _POLISH_DOC.read_text(encoding="utf-8")
    section = _section_between(doc, "## Review-findings ledger", "## What to skip")

    # Recall lets findings survive into a fresh session with no $ARGUMENTS list.
    assert "union them, de-duplicated by `file` + `title`" in section, (
        "polish must union recalled ledger findings with any $ARGUMENTS list."
    )
    # Only close findings whose fix actually landed and passed gates.
    assert "do not mark a finding fixed if its fix was reverted or never landed" in section, (
        "polish must not mark a reverted/unlanded finding as fixed."
    )


def test_polish_safety_names_the_ledger_write() -> None:
    doc = _POLISH_DOC.read_text(encoding="utf-8")
    safety = doc[doc.index("## Safety") :]
    assert "review-findings ledger** under `~/.claude/reviews/`" in safety, (
        "polish Safety must name the ledger as its only out-of-repo write."
    )


def test_polish_ledger_guards_against_foreign_checkout() -> None:
    doc = _POLISH_DOC.read_text(encoding="utf-8")
    section = _section_between(doc, "## Review-findings ledger", "## What to skip")

    # polish is the *fix-applier*, so a wrong-tree read is the costlier mistake:
    # its recall guard must be at least as strict as review-changes', rejecting
    # the ledger when *either* repo or branch mismatches (OR, not a weaker AND).
    assert "never read another tree's findings" in section, (
        "polish recall must reject a ledger whose repo/branch do not match this "
        "tree, matching the review-changes guard."
    )
    # Pin the OR, not just the generic phrase: the guard must reject when either
    # repo or branch mismatches, so an AND weakening cannot slip past this test.
    assert (
        "does not equal this working directory or its `branch` does not equal"
        in section
    ), "polish recall must reject when repo OR branch mismatches, not only both."


def test_review_changes_does_not_run_polish_in_session() -> None:
    doc = _REVIEW_DOC.read_text(encoding="utf-8")

    # The old in-session hand-off (invoking the polish skill) must be gone:
    # reviewing and fixing are now two sessions bridged by the ledger.
    assert "invoke the `polish` skill via the Skill tool" not in doc, (
        "review-changes must no longer invoke the polish skill in-session."
    )
    assert "followed by invoking the `polish` skill" not in doc, (
        "The Output closing line must not invoke the polish skill in-session."
    )
    # And the decoupling must be stated explicitly, both in the step and Safety.
    assert "never invokes the `polish` skill" in doc, (
        "review-changes must explicitly state it never invokes the polish skill."
    )
    assert "Do not invoke the `polish` skill from this command." in doc, (
        "review-changes Safety must forbid invoking the polish skill."
    )
    assert "separate session" in doc, (
        "review-changes must point the user at /polish in a separate session."
    )


def test_ledger_carries_plan_context_across_the_session_boundary() -> None:
    review = _REVIEW_DOC.read_text(encoding="utf-8")
    review_section = _section_between(
        review, "## Review-findings ledger", "## What to skip"
    )
    polish = _POLISH_DOC.read_text(encoding="utf-8")
    polish_section = _section_between(
        polish, "## Review-findings ledger", "## What to skip"
    )

    # The bridge artifact must carry the design intent, not just findings.
    assert '"plan_context"' in review_section, (
        "The ledger schema must include a plan_context field."
    )
    assert '"plan_path"' in review_section, (
        "The ledger schema must include a plan_path pointer."
    )
    # A review that lost the transcript must not erase the carried intent.
    assert "keep the prior ledger's `plan_context`" in review_section, (
        "A no-plan-context run must preserve the prior ledger's plan_context, "
        "not clear it."
    )
    # polish must consume the carried plan context to keep fixes plan-consistent.
    assert "plan_context" in polish_section, (
        "polish must read plan_context from the ledger so fixes respect intent."
    )


def test_polish_plan_context_decline_does_not_override_always_fixed() -> None:
    # The plan_context tie-breaker permits *declining* a fix that contradicts a
    # deliberate design choice. Step 6 makes open ledger Issues always fixed,
    # overridable only by the Safety hard stops. If plan_context could decline a
    # ledger Issue, the two rules contradict — so the decline path must be scoped
    # to fresh findings (mirroring the --fix-suggestions and Concerns carve-outs),
    # never authorizing a skip of an open ledger Issue.
    polish = _POLISH_DOC.read_text(encoding="utf-8")
    section = _section_between(polish, "## Review-findings ledger", "## What to skip")

    assert "for a *fresh* finding, declining" in section, (
        "plan_context may only *decline* a fix for a fresh finding, not a ledger "
        "Issue — mirroring how --fix-suggestions and the Concerns gate are scoped."
    )
    assert "does **not** authorize declining an open ledger Issue" in section, (
        "polish must state plan_context cannot decline an open ledger Issue, so it "
        "does not contradict Step 6's always-fixed rule."
    )


def test_polish_mark_fixed_aligns_with_always_fix_policy() -> None:
    # Step 6 makes open ledger Issues always fixed, with only the Safety hard
    # stops or a reverted/unlanded fix leaving one unfixed. The "Mark fixed"
    # parenthetical used to list "(declined concerns, skipped suggestions)" as the
    # reasons a recalled ledger finding stays unfixed — a now-impossible scenario
    # under the new policy that contradicted Step 6 and the report step. The
    # not-fixed reason must instead name a Safety hard stop (the revert case is
    # still covered by the unchanged guard pinned in test_polish_recalls_*).
    polish = _POLISH_DOC.read_text(encoding="utf-8")
    section = _section_between(polish, "## Review-findings ledger", "## What to skip")

    assert "(declined concerns, skipped suggestions)" not in section, (
        "The 'Mark fixed' parenthetical must not list declined-concern/"
        "skipped-suggestion as reasons a ledger finding stays unfixed — under the "
        "always-fix policy those are no longer valid for ledger Issues."
    )
    assert "blocked by a *Safety* hard stop" in section, (
        "'Mark fixed' must name a Safety hard stop as the reason a recalled ledger "
        "finding stays unfixed, mirroring Step 6 and the report step."
    )


def test_polish_append_excludes_user_declined_findings() -> None:
    # The append rule persists fresh unfixed findings so a follow-up run fixes
    # them as always-fixed open ledger Issues (Step 6). A finding the user — or
    # plan_context — explicitly *declined* must be excluded from that append:
    # once laundered into a `new` ledger entry it becomes an always-fixed Issue,
    # reversing the deliberate decline on the next run and defeating the
    # "surface to the user and let them decide" contract the same document
    # establishes. The exclusion must sit in Pass 2 beside the fixed/
    # already-tracked carve-outs, and Step 6's unconditional-append sentence must
    # agree so the two statements do not contradict.
    polish = _POLISH_DOC.read_text(encoding="utf-8")
    section = _section_between(polish, "## Review-findings ledger", "## What to skip")

    assert "nor one the user reviewed and explicitly declined" in section, (
        "Pass 2 must exclude a user-declined fresh finding from the append so a "
        "deliberate decline is not laundered into an always-fixed ledger Issue."
    )
    assert "`plan_context` declined on deliberate-design grounds" in section, (
        "Pass 2 must also exclude a fresh finding declined on plan_context "
        "grounds — the fresh-finding decline path plan_context owns."
    )
    # Step 6's append sentence lives outside the ledger section; it must carry the
    # same carve-out so the always-fix rule cannot silently reverse a decline.
    # Isolate Step 6 so a whole-doc match cannot be satisfied by Step 11's (or the
    # Flags') identical copy of the phrase — otherwise dropping Step 6's carve-out
    # would slip past this guard, leaving it a no-op for Step 6.
    step6 = _section_between(polish, "6. **Decide before fixing", "7. **Apply fixes.**")
    assert "which is surfaced in the report but not appended" in step6, (
        "Step 6's 'any fresh finding is appended' sentence must carve out the "
        "user/plan_context-declined case so it agrees with Pass 2's exclusion."
    )
    # Step 11(b) is a second, prominent unconditional-append statement — an agent
    # following the Step 11 flow rather than the detailed Pass 2 rule would re-append
    # a decline unless it carries the same carve-out. Isolate the Step 11 block so a
    # match cannot be satisfied by Step 6's identical phrase living above it.
    step11 = _section_between(
        polish, "11. **Update the ledger", "## Review-findings ledger"
    )
    assert (
        "except a fresh finding the user (or `plan_context`) explicitly declined"
        in step11
    ), (
        "Step 11(b)'s append instruction must carry the declined-finding carve-out "
        "so the prominent Step 11 flow cannot re-launder a decline Pass 2 excludes."
    )
    # The --fix-suggestions description is the third unconditional-append statement:
    # a declined fresh Suggestion must not be swept into the ledger there either.
    flags = _section_between(polish, "## Flags", "## Steps")
    assert "unless the user or `plan_context` explicitly declined them" in flags, (
        "The --fix-suggestions description must carve out a declined fresh "
        "Suggestion from the append so it agrees with Step 6/Pass 2/Step 11(b)."
    )


def test_polish_append_id_defines_empty_ledger_base_case() -> None:
    # The appended id is "max existing numeric suffix + 1"; over a brand-new
    # ledger with no findings that max is taken over an empty set — undefined.
    # The base case must be explicit so the first appended id in a freshly
    # created ledger is unambiguously r1.
    polish = _POLISH_DOC.read_text(encoding="utf-8")
    section = _section_between(polish, "## Review-findings ledger", "## What to skip")

    assert "starting at `r1` when the ledger has no existing findings" in section, (
        "The appended-id rule must state the empty-set base case (r1) so a "
        "freshly created ledger's first id is unambiguous."
    )


def test_review_changes_recalls_canonical_plan_file() -> None:
    doc = _REVIEW_DOC.read_text(encoding="utf-8")
    step1 = _section_between(doc, "1. **Pull plan context", "2. **Determine scope.**")

    # review-changes must resolve the *canonical* plan via the same folder
    # pointer /impl and /process-comments use — not only the session transcript,
    # which holds no plan for a branch built by the /plan-proposal -> /impl
    # pipeline (impl is forbidden from plan mode).
    assert "last-plan-by-folder.json" in step1, (
        "review-changes must recall the canonical plan via the folder pointer."
    )
    assert "~/.claude/plans/" in step1, (
        "review-changes must read the canonical plan file under ~/.claude/plans/."
    )
    # The plan file is primary; the transcript stays a supplement.
    assert "Canonical plan file (primary)" in step1, (
        "review-changes must treat the canonical plan file as the primary source."
    )
    assert "Session transcript (supplement)" in step1, (
        "review-changes must keep the session transcript as a supplement."
    )
    # plan_path must be recorded from the recalled plan file — no longer orphaned.
    assert "plan_path" in step1, (
        "step 1 must record plan_path from the recalled canonical plan file."
    )


def test_review_changes_nulls_plan_path_for_unrelated_plan() -> None:
    doc = _REVIEW_DOC.read_text(encoding="utf-8")
    step1 = _section_between(doc, "1. **Pull plan context", "2. **Determine scope.**")
    persist = _section_between(doc, "### Persist & reconcile", "## What to skip")

    # A plan whose paths exist but describes a *different* feature (same-repo,
    # wrong branch) must not leak into the ledger as this diff's design intent.
    # 1a only nulls plan_path when cited paths are *absent*; the Synthesize
    # "unrelated to the diff" branch must also null it, and Persist must not
    # re-record a non-null plan_path in that case.
    assert (
        "unrelated to the diff, note the mismatch, set `plan_path` to null"
        in step1
    ), "Synthesize must null plan_path when the plan is unrelated to the diff."
    assert "or when Synthesize found it unrelated to this diff" in persist, (
        "Persist must null plan_path for a same-repo plan unrelated to this diff."
    )
