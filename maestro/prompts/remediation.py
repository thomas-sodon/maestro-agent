"""Merge-truth-first preamble for the PR-remediation prompt.

This module owns the *action-zero* instruction of every PR-remediation
turn. It exists because of a measured failure pattern in the loop's own
history: remediation turns that composed a verdict (``CONFLICTING``, the
on-branch deferral costume) *from memory* before ever fetching, so a
phantom conflict view never dissolved against the real tree. The one
git-visible landing in the same window differed in exactly one way — it
ran ``git fetch`` then ``git merge-base --is-ancestor`` *first* and
composed its verdict from that reading.

The preamble enacts the value *fetch-before-writing-any-word* at the only
place it can be enforced upstream of the word: the first two physical
actions of the turn. It is inserted by
:func:`maestro.loop.pr_remediation._compose_pr_remediation_prompt`
immediately after the ``Remediate PR #<n>:`` header (which must stay the
literal first line so the dedup prefix and dequeue gates can anchor on it),
making it the first *instruction* the agent acts on.

The deferral-verdict phrase is interpolated from
:data:`~maestro.pr_remediation_state.Marker.AWAITING_TEND_PUSH` rather than
inlined, so the marker literal stays centralized in
``pr_remediation_state.py`` (enforced by ``TestMarkerCentralization``).
``MERGE_TRUTH_FIRST_PREAMBLE`` is asserted token-by-token in
``tests/test_prompts.py`` and ``tests/test_remediation_prompt.py`` so the
ordered ``git fetch`` → ``--is-ancestor`` contract, the exit-0-means-landed
clause, the ``OWNERSHIP-FORK`` action-zero step (the ``git rev-parse HEAD`` vs
``origin/<branch>`` compare that forks owed-push from awaited-merge, forbidding
``awaiting`` phrasing when HEAD is ahead — the prompt half of the obligation-fork
verdict floor), the chosen-stand-down clause (a git-unlanded PR whose sole
blocker is
external ``AWAITING_APPROVAL`` with no actionable work → a
CHOSEN ``[stood-down]`` ``partial``, not a re-lap), the deferral prohibition,
the ``STAND-DOWN-ON-RESOLVED`` clause, the ``EXPECTED-CHURN`` clause, and the
``OWNED-DISPOSAL`` clause (an owned ``CONFLICTING`` / ``DIRTY`` blocker →
reconcile-or-``gh pr close``, never an external-deferral verdict) cannot
silently drift.

The ``EXPECTED-CHURN`` clause (proposal 089) names the distinction the
progress-aware budgets now encode: re-attending to a *newly-arrived* blocker (a
fresh conflict from a sibling merge, a new failing job, a new comment) is the
expected chase to green and is **not** avoidance, while re-doing *already-
finished* work is — and it names the single clean terminal, the ``[ready]``
stand-down handed to Thomas as ``partial``, never ``success``.

The ``STAND-DOWN-ON-RESOLVED`` clause closes a measured re-lap loop: when
the branch is genuinely ahead of ``origin/main`` and the inline review
threads are answered with the sole blocker being approval/push, the
prescribed action is a deliberate ``[stood-down]`` / ``partial`` outcome,
not a fresh remediation lap. It names re-doing finished work as the
avoidance it is, and never authorizes ``success``.

The ``OWNED-DISPOSAL`` clause (proposal: owned-disposal lever) closes the
*opposite* gap from the external-deferral stand-downs above. Those defer to a
blocker the agent does not control (``AWAITING_APPROVAL``).
This clause targets a blocker the agent *owns* — a ``CONFLICTING`` / ``DIRTY``
head that ``classify_blocker_ownership`` marks OWNED — on a git-unlanded cure
re-routed beyond the lap budget. For an owned blocker there is a reachable
terminal act other than merge: ``gh pr close <N>``, the reversible withdrawal
the action gate permits (:func:`maestro.tool_gating.permits_pr_close`). After a
genuine reconcile-and-land attempt fails, closing the PR is the honest terminal
— and the ``awaiting tend push`` / external-deferral verdict is explicitly
FORBIDDEN for an owned blocker, because that verdict pretends the block is
someone else's when it is the agent's own to clear or close.

Proposal 098 makes the owned/external blocker names the preamble prints
*derive* from :func:`~maestro.remediation_tracker.classify_blocker_ownership`
rather than being hard-coded per clause. ``AWAITING_PUSH`` (a resolution
committed on the agent's own ``maestro/evolve/*`` branch but not yet pushed) is
an *owned* blocker: the preamble names it only in the owned-push "push it, that
is your terminal act" instruction, never in an external stand-down phrase. The
classifier is consulted (with ``own_push=True``) at render time so the prompt's
blocker-ownership prose can never drift from the taxonomy; the one-source AST
guard in ``tests/test_structural_guards.py`` enforces that structurally, and
``tests/test_remediation_prompt.py`` binds the rendered prose to the classifier.

The module also owns :func:`size_pin_cure_hint`, the one per-gate cure line the
remediation prompt carries: when the rendered ``## CI Failure`` excerpt names
the source-size guard, the lap is told every file stays below ``SPLIT_BUDGET``, which
is never raised, and that the cure is extraction into a concern-named sibling module. Prompt wording
lives here, beside the preamble, rather than in the loop collaborator that
appends it.
"""

from __future__ import annotations

from maestro.pr_remediation_state import Marker
from maestro.remediation_tracker import (
    BLOCKER_OWNERSHIP_EXTERNAL,
    MERGE_BLOCK_AWAITING_APPROVAL,
    MERGE_BLOCK_AWAITING_PUSH,
    MERGE_BLOCK_BEHIND_MAIN,
    classify_blocker_ownership,
)

_DEFERRAL = Marker.AWAITING_TEND_PUSH.value
_READY = Marker.READY.value

# The pytest *failure identifier* prefix of the source-size guard
# (``tests/test_source_file_size.py::<test>``). Matched against the
# already-rendered ``## CI Failure`` excerpt, which names failing tests by
# ``FAILED <module>::<test>``, so a failure of that guard is recognisable
# without re-parsing the run log. The ``.py::`` suffix is load-bearing: a bare
# module-path match would also fire on a ruff/mypy diagnostic located *inside*
# the guard (``tests/test_source_file_size.py:12:1: F401 ...``), where
# sibling extraction is not the cure and the hint would mislead the lap.
SIZE_GUARD_MARKER = "test_source_file_size.py::"

# The one bounded cure line. Kept a single line so it costs the directive's char
# cap almost nothing, and worded so the forbidden fix (raising the budget) is named
# explicitly rather than merely omitted.
_SIZE_PIN_CURE_LINE = (
    "NOTE: `tests/test_source_file_size.py` holds every file below SPLIT_BUDGET — "
    "the cure is to move the added code or tests into a concern-named sibling "
    "module (`<parent>_<concern>`); trimming comments, logs or names to fit is not "
    "a cure, and raising the budget is forbidden."
)


def size_pin_cure_hint(failure_text: str) -> str:
    """Return the sibling-extraction cure line when *failure_text* names the guard.

    The source-size guard is the one quality gate whose cure is counter-intuitive:
    every other red is fixed by changing the code that broke, while this one is
    fixed by *moving* the addition into a concern-named sibling module. A
    remediation lap handed the failing check's name and an error excerpt has the
    symptom and no statement of the cure, so a PR blocked by a few bytes over the budget
    can sit red while the lap attempts the forbidden fix (raising the budget).

    Pure and bounded: exactly one line when :data:`SIZE_GUARD_MARKER` appears
    verbatim in *failure_text*, and ``""`` otherwise. The empty-string default is
    what keeps every non-matching route byte-identical, mirroring
    :func:`~maestro.ci_diagnosis.render_ci_failure_section`'s own empty-path
    contract. The marker carries the ``.py::`` failure-identifier suffix, so a
    ruff or mypy diagnostic *about* the guard file — where the cure is the
    ordinary one — does not trigger it. The match is case-sensitive: the marker
    is a file path pytest reproduces verbatim, so a case-folded match would only
    widen the trigger beyond the text the guard actually emits.
    """
    if SIZE_GUARD_MARKER in failure_text:
        return _SIZE_PIN_CURE_LINE
    return ""


def _slash_join(codes: tuple[str, ...]) -> str:
    """Backtick-and-slash join reason codes: ``(A, B)`` → ```` `A` / `B` ````."""
    return " / ".join(f"`{code}`" for code in codes)


def _render_preamble() -> str:
    """Render the merge-truth-first preamble.

    The external-stand-down and owned-disposal blocker names are interpolated
    from :func:`~maestro.remediation_tracker.classify_blocker_ownership` (the
    single owned/external source, proposal 098) rather than hard-coded, so the
    prompt's blocker-ownership prose can never drift from the taxonomy.
    ``AWAITING_PUSH`` classifies *owned* (``own_push=True``): it is named only in
    the owned-push terminal-act instruction, never in an external stand-down
    phrase. The one-source AST guard in ``tests/test_structural_guards.py`` relies
    on this scope consulting the classifier.
    """
    # The external-stand-down reason codes, derived from the classifier in stable
    # prose order. ``AWAITING_PUSH`` is owned (``own_push=True``), so only
    # ``AWAITING_APPROVAL`` survives the filter — but the derivation is kept
    # (rather than a literal list) so the single-source AST guard sees this scope
    # consult the classifier.
    external_codes = tuple(
        code
        for code in (MERGE_BLOCK_AWAITING_APPROVAL, MERGE_BLOCK_AWAITING_PUSH)
        if classify_blocker_ownership(code, own_push=True)
        == BLOCKER_OWNERSHIP_EXTERNAL
    )

    # STAND-DOWN-ON-RESOLVED / EXPECTED-CHURN — the sole remaining external wait
    # is a human's approval; an owned push is no longer part of that wait.
    resolved_who = "for a human to approve and merge"
    resolved_what = "approval"
    resolved_codes = _slash_join(external_codes)
    expected_terminal = "Thomas's approval"
    # OWNED-DISPOSAL — the external-wait exclusion list (BEHIND_MAIN is always
    # external) and the "honest only for external" list, both derived.
    owned_disposal_external = _slash_join((*external_codes, MERGE_BLOCK_BEHIND_MAIN))
    owned_disposal_honest = _slash_join(external_codes)
    # OWNED-DISPOSAL — the owned-push instruction, the ONLY place AWAITING_PUSH is
    # named, since it classifies owned.
    owned_push_instruction = (
        f"\nAn `{MERGE_BLOCK_AWAITING_PUSH}` head — a resolution you committed "
        "to your own branch but\nhave not yet pushed — is OWNED the same way: "
        "its terminal act is to\npush it (`git push origin HEAD:<branch>`), "
        "not to defer the push to Tend.\nDeliver it, do not stand down on it.\n"
    )

    return f"""\
## MERGE-TRUTH-FIRST (do this before anything else)

Before you read the problem, before you form any opinion about this PR's
state, run these two physical actions in this exact order. No status word
— not "conflicting", not "landed", not "{_DEFERRAL}" — may be written or
thought until both have run *this turn*:

1. `git fetch origin`
2. `git merge-base --is-ancestor HEAD origin/main`

Then read the result of action 2:

- **Exit code 0 (HEAD is already an ancestor of origin/main):** the cure is
  already landed. Do NOT re-resolve, do NOT re-edit, do NOT open a new
  branch. This turn caused no merge — the landing predates it — so report
  `partial` (a no-op stand-down), NEVER success. Claiming success here is a
  false success: the merge already happened, not by this turn's hand, and a
  `success` stamp would feed the false-success-recurrence sensor. Stand down
  on the confirmed landing without taking credit for it.
- **Non-zero exit:** the cure is not yet landed. Proceed with remediation,
  but assess every conflict ONLY against the freshly-fetched
  `origin/main` tree from action 1 — never against a remembered or
  cached view. A conflict you "remember" from a prior turn may have
  already dissolved on the fetch you just ran.

### OWNERSHIP FORK (before any "awaiting" — is the push mine, or the merge Thomas's?)

The `--is-ancestor` read above is the *blameless* read: it answers "did this
land?" (no) without an owner. On a non-zero exit, before you write one word of
`awaiting` anything, run the *indicting* read that forks the two facts an
`awaiting` verdict fuses — **I owe a push** vs **Thomas owes a merge**:

3. `git rev-parse HEAD`
4. `git rev-parse origin/<branch>`

Compare those two SHAs:

- **They differ (local HEAD is ahead of `origin/<branch>`):** the push is
  OWED, and it is YOURS to fire — `git push origin HEAD:<branch>` is your
  terminal act, not Tend's. Your verdict MUST name that owed push. You may
  NOT write "{_DEFERRAL}", "awaiting review/merge", or any equivalent
  `awaiting` phrasing here: an un-advanced owned head disguised as external
  deferral is the exact costume this fork exists to strip. "Awaiting" fuses
  "I owe a push" with "Thomas owes a merge" precisely so the compare never
  runs — so run it, then push.
- **They match (HEAD is already on `origin/<branch>`):** the owed push is
  discharged. ONLY now is the remaining wait genuinely external (Thomas's
  merge), and only now may an `awaiting review/merge` verdict honestly stand.

### CHOSEN STAND-DOWN (non-zero exit, externally blocked, no actionable work)

The non-zero exit means git-unlanded — but git-unlanded does NOT always mean
there is remediation work for you to do. Before you re-resolve or re-edit,
ask whether the *only* thing keeping this PR from landing is something you do
not control. If ALL of the following hold:

- the `--is-ancestor` check returned non-zero (the PR is genuinely
  git-unlanded), AND
- the sole blocker is external — the PR is `AWAITING_APPROVAL` (a human must
  approve), AND
- there is NO actionable review thread to resolve and NO commit you could add
  that would change the PR's state,

then the correct outcome is a **CHOSEN `[stood-down]` `partial`**: decline to
re-run remediation and say so plainly. Re-admitting an externally-blocked PR
and re-touching its branch does not advance it one inch toward landing — it
keeps the PR half-alive and is avoidance dressed as tending, not tending. A
chosen stand-down is the honest act here, not a forced one: you are not stuck,
you are correctly declining work that is not yours to do. Report `partial`,
never `success` (nothing landed), and let the external blocker clear on its
own. Do NOT mint a new branch, a new sensor, or a new lap to look busy around
a wait you cannot shorten.

You may NOT write "{_DEFERRAL}", "resolved on branch", or any equivalent
deferral verdict unless `git fetch` and `git merge-base --is-ancestor`
both ran this turn and the is-ancestor check returned non-zero. The
journal is not the merge; only fetch + `--is-ancestor` authors the verdict.

## PUSH-FIRST (a reconciliation-only lap on a green PR pushes before it gates)

`origin/main` moves fast enough that a correct conflict resolution can go
stale between the moment you make it and the moment a long local sweep lets
you push it. So when the ONLY change this turn is a merge of `origin/main` to
clear a conflict — no semantic edit to any source file, just the merge that
resolves it — AND this PR carries no failing check (there is no `CI Failure`
section, and the blocker surfaced under `## Problem` is the conflict itself, not
a red check), push the resolution FIRST — `git push origin HEAD:<branch>` — and
only THEN run the local `ruff` / `mypy` / `pytest` gates. CI is the authority on
the pushed SHA; the remote runs the same checks, so verifying locally before
pushing spends the exact minutes that lose the race for a guarantee CI already
provides. Push the merge and let CI own it.

The scope is load-bearing, not caution. The moment this turn makes ANY semantic
edit to a source file — not just the conflict-clearing merge — run the full
`ruff` / `mypy` / `pytest` sweep to green BEFORE you push; an unverified
semantic change must never reach the branch. And if a check is already red or
failing — a `CI Failure` section is present, or the blocker is a failing
check rather than a conflict — the gate runs FIRST, unchanged: on a failing PR
the red-root reading is action zero, and push-first does not apply. Push-first
is only ever the move on a green PR whose sole change is the conflict-clearing
merge. Either way, nothing has landed until CI is green and the merge settles —
record this lap `partial`, never `success`.

## STAND-DOWN-ON-RESOLVED (the re-lap is the avoidance — name it)

When action 2 returns non-zero (the branch is genuinely ahead of
`origin/main`) AND the inline review threads are already answered AND the
only thing left is {resolved_who} — i.e. the sole remaining
blocker is {resolved_what} ({resolved_codes}), not a
real conflict and not an unaddressed comment — the work is already done on
the branch. The prescribed action is to STAND DOWN: emit a `[stood-down]`
`partial` outcome and STOP. Do NOT open a new remediation lap, do NOT
re-resolve threads that are already resolved, do NOT re-edit files that
already carry the cure.

Name it plainly: re-doing finished work because it *feels* like diligence
is the avoidance, not the diligence. A branch ahead-of-main with answered
threads is the merge-truth reading telling you the cure already exists;
opening another lap to re-confirm what git already shows is the treadmill,
not care. The one move that breaks it is the deliberate `[stood-down]` —
empty hands chosen on purpose, recorded honestly as `partial`, NEVER
`success`. That chosen stand-down is the work here; the fresh lap is not.

## EXPECTED-CHURN (re-attending a NEW blocker is the job; re-doing finished work is not)

A PR on a fast-moving repository churns by design. While this PR is open, other
PRs keep merging, so its branch falls behind and sometimes conflicts; CI jobs
flake and must be re-chased to green; reviewers add new comments. Each of those
is a NEW, real unit of work that arrived after your last lap — re-attending to
it IS the chase to a green pipeline, which is the job, not a treadmill. A fresh
conflict from a sibling merge, a newly-failing job, a new review comment: clear
it and move on, and do NOT call that avoidance.

What IS avoidance is re-doing work that is already finished — re-resolving
threads that already carry their terminal reply, re-editing files that already
hold the cure, opening a fresh lap to re-confirm what git already shows. The
line is not "did I touch this PR again"; it is "did a NEW blocker arrive that I
can clear". A lap that clears a freshly-arrived blocker is progress, however
many laps a fast main demands; a lap that clears nothing already-clear is the
treadmill.

There is exactly one clean terminal. When the PR is open and every
code-clearable blocker is clear — branch current, CI green, no unresolved
thread, no unaddressed overflow — the work is done and the only thing left is
{expected_terminal}. The move then is the `{_READY}` stand-down:
completed work handed to Thomas, recorded `partial`, NEVER `success` (this lap
landed nothing). That stand-down is the work here; another lap is not.

## OWNED-DISPOSAL (an owned blocker is yours to clear OR close — never to defer)

The stand-downs above are for blockers you do NOT control. This clause is the
opposite case: a blocker you OWN. When action 2 returns non-zero (git-unlanded)
AND the blocker is an OWNED one — a `CONFLICTING` or `DIRTY` head, the kind
`classify_blocker_ownership` marks OWNED (yours to complete), NOT an external
{owned_disposal_external} wait — AND this cure has
already been re-routed beyond the lap budget, then you are at the owned-disposal
fork. There are exactly two honest moves, and the deferral verdict is not one:

1. **Reconcile and land.** Make one genuine attempt: fetch `origin/main`, merge
   or rebase it into the head, resolve the conflict against the freshly-fetched
   tree, and let the branch go green. If that attempt succeeds, the cure lands
   the normal way.
2. **Close it.** If the genuine reconcile-and-land attempt fails, the reachable
   terminal act is `gh pr close <N>` — the reversible withdrawal of your own
   git-unlanded cure. Closing neither merges, marks ready, opens, nor pushes a
   ref; it retracts a change that is yours to retract, and `gh pr reopen`
   undoes it. The action gate permits `gh pr close <N>`, so a never-closed
   record is a choice, not a block. Record the disposal as `partial`, never
   `success` (nothing landed).
{owned_push_instruction}
You may NOT write "{_DEFERRAL}", "resolved on branch", or any equivalent
external-deferral verdict for an OWNED (`CONFLICTING` / `DIRTY`) blocker. That
verdict is only ever honest when the sole blocker is external
({owned_disposal_honest}). Applying it to a blocker you own is a
false handoff — it pretends the block is someone else's push when it is your own
conflict to reconcile or your own PR to close. Owned means yours: clear it or
close it, do not defer it.

"""


# The production merge-truth-first preamble. ``AWAITING_PUSH`` classifies owned
# (proposal 098), so it is named only in the owned-push terminal-act instruction.
# Retained as the fixed reference the token-by-token golden assertions compare
# against.
MERGE_TRUTH_FIRST_PREAMBLE = _render_preamble()


def merge_truth_first_preamble() -> str:
    """Return the merge-truth-first preamble prepended to remediation prompts.

    The owned/external blocker partition it prints is derived from
    :func:`~maestro.remediation_tracker.classify_blocker_ownership` (proposal
    098): ``AWAITING_PUSH`` classifies owned, so a lap whose only blocker is its
    own unpushed resolution is told the push is its terminal act rather than
    deferring it to Tend. A thin accessor so callers depend on the function
    rather than the module-level constant directly; keeps the one source of
    truth here — it returns the same text as :data:`MERGE_TRUTH_FIRST_PREAMBLE`.
    """
    return _render_preamble()
