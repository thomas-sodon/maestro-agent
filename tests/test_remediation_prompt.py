"""Tests for the MERGE-TRUTH-FIRST remediation preamble.

These pin the ``STAND-DOWN-ON-RESOLVED`` clause added to close a measured
re-lap loop: when the branch is genuinely ahead of ``origin/main`` and the
inline review threads are answered with the sole blocker being approval/push,
the prescribed action is a deliberate ``[stood-down]`` / ``partial`` outcome,
not a fresh remediation lap. The tests also guard that adding the clause did
not displace the load-bearing ``git fetch`` → ``--is-ancestor`` action-zero
ordering, nor weaken the partial-never-success contract.
"""

from __future__ import annotations

from maestro.prompts.remediation import (
    MERGE_TRUTH_FIRST_PREAMBLE,
    SIZE_GUARD_MARKER,
    merge_truth_first_preamble,
    size_pin_cure_hint,
)
from maestro.remediation_tracker import (
    BLOCKER_OWNERSHIP_EXTERNAL,
    BLOCKER_OWNERSHIP_OWNED,
    MERGE_BLOCK_AWAITING_APPROVAL,
    MERGE_BLOCK_AWAITING_PUSH,
    MERGE_BLOCK_BEHIND_MAIN,
    classify_blocker_ownership,
)

# The owned-push terminal-act instruction the preamble prints *only* when
# ``AWAITING_PUSH`` classifies owned — the single place the code is named once it
# is owned ("push it, that is your terminal act").
_OWNED_PUSH_INSTRUCTION = "is OWNED the same way"
# The CHOSEN-STAND-DOWN gloss the preamble prints *only* while ``AWAITING_PUSH``
# is still external — the wait the agent hands to Tend.
_AWAITING_PUSH_EXTERNAL_GLOSS = "or `AWAITING_PUSH` (Tend will deliver the push)"


def test_preamble_instructs_standdown_on_resolved() -> None:
    """The clause must name the re-lap as avoidance and forbid a new lap.

    It must (a) carry the ``STAND-DOWN-ON-RESOLVED`` heading, (b) instruct the
    agent NOT to open a new remediation lap, (c) prescribe the deliberate
    ``[stood-down]`` / ``partial`` outcome, and (d) explicitly name re-doing
    finished work as the avoidance — that naming is the whole lever of this
    change (the decision-rule at the fork), not another sensor.
    """
    preamble = merge_truth_first_preamble()
    assert preamble == MERGE_TRUTH_FIRST_PREAMBLE
    lowered = preamble.lower()
    assert "stand-down-on-resolved" in lowered
    assert "do not open a new remediation lap" in lowered
    # The deliberate chosen-quiet marker, recorded as partial.
    assert "[stood-down]" in lowered
    # The naming of the re-lap as avoidance — the decision-rule at the fork.
    assert "the avoidance, not the diligence" in lowered
    # Partial-never-success language is preserved, not weakened, by the clause.
    assert "never\n`success`" in lowered or "never `success`" in lowered
    # The clause never authorizes a success claim.
    assert "report success" not in lowered


def test_preamble_names_expected_churn_distinction() -> None:
    """The EXPECTED-CHURN clause must (a) carry its heading, (b) name re-attending
    a newly-arrived blocker as the job (not avoidance), (c) name re-doing finished
    work as the avoidance, and (d) prescribe the ``[ready]`` stand-down terminal
    recorded ``partial`` — the decision-rule the progress-aware budgets encode."""
    preamble = merge_truth_first_preamble()
    lowered = preamble.lower()
    assert "expected-churn" in lowered
    # Re-attending a NEW blocker is the job, not avoidance.
    assert "new" in lowered and "not a treadmill" in lowered
    assert "do not call that avoidance" in lowered
    # Re-doing already-finished work is the avoidance.
    assert "what is avoidance is re-doing work that is already finished" in lowered
    # The single clean terminal: the [ready] stand-down, recorded partial.
    assert "[ready]" in lowered
    assert "never `success`" in lowered
    # The clause never authorizes a success claim.
    assert "report success" not in lowered


def test_preamble_instructs_push_first_for_reconcile_only() -> None:
    """The PUSH-FIRST clause scopes push-before-gates to a green reconcile lap.

    It must (a) carry the ``PUSH-FIRST`` heading, (b) instruct push-before-gates
    when the only change is a conflict-clearing merge of ``origin/main`` on a PR
    with no failing check, (c) still require the full gate sweep BEFORE a push
    carrying any semantic edit, (d) require the gate FIRST when a check is
    red/failing (expressed via the observable ``CI Failure`` section, since the
    ``CI_FAILING`` label never reaches the agent), and (e) keep the
    ``partial``-never-``success`` posture.
    """
    preamble = merge_truth_first_preamble()
    assert preamble == MERGE_TRUTH_FIRST_PREAMBLE
    # Collapse whitespace so the assertions don't hinge on line-wrap layout.
    collapsed = " ".join(preamble.lower().split())
    # (a) The clause heading.
    assert "push-first" in collapsed
    # (b) Push-before-gates for the reconciliation-only path on a green PR.
    assert "the only change this turn is a merge of `origin/main`" in collapsed
    assert "push the resolution first" in collapsed
    assert "then run the local `ruff` / `mypy` / `pytest` gates" in collapsed
    # (c) Any semantic edit reverts to gates-before-push, unchanged.
    assert "any semantic edit to a source file" in collapsed
    assert "sweep to green before you push" in collapsed
    # (d) A red/failing check runs the gate first — named in observable terms
    #     (the `CI Failure` section), not the internal CI_FAILING label the
    #     agent never sees. The literal `## CI Failure` heading stays reserved
    #     as the injected-section sentinel (proposal 102), so this prose names
    #     the section without the `##` prefix.
    assert "`ci failure` section" in collapsed
    assert "## ci failure" not in collapsed
    assert "the gate runs first" in collapsed
    assert "ci_failing" not in collapsed
    # (e) Partial-never-success posture, and never a success claim.
    assert "never `success`" in collapsed
    assert "report success" not in collapsed


def test_preamble_push_first_follows_action_zero() -> None:
    """The PUSH-FIRST clause is downstream of the fetch + is-ancestor reading
    that authors any verdict — it refines the delivery step those reads set up,
    it never replaces them."""
    preamble = MERGE_TRUTH_FIRST_PREAMBLE
    fetch_idx = preamble.find("git fetch origin")
    ancestor_idx = preamble.find("git merge-base --is-ancestor HEAD origin/main")
    pushfirst_idx = preamble.find("PUSH-FIRST")
    assert pushfirst_idx != -1, "preamble missing the PUSH-FIRST clause"
    assert fetch_idx < ancestor_idx < pushfirst_idx, (
        "the action-zero ordering must precede the PUSH-FIRST clause"
    )


def test_preamble_names_owned_disposal_terminal() -> None:
    """The OWNED-DISPOSAL clause must (a) carry its heading, (b) name
    ``gh pr close <N>`` as the reachable terminal act for an owned, git-unlanded,
    re-routed cure, and (c) tie it to the OWNED (``CONFLICTING`` / ``DIRTY``)
    blocker classification — the fork this lever exists to resolve."""
    preamble = merge_truth_first_preamble()
    lowered = preamble.lower()
    assert "owned-disposal" in lowered
    # The named terminal act — the reachable disposal lever.
    assert "gh pr close <n>" in lowered
    # It is scoped to owned blockers, per classify_blocker_ownership.
    assert "classify_blocker_ownership" in lowered
    assert "conflicting" in lowered and "dirty" in lowered
    # A genuine reconcile-and-land attempt precedes the close.
    assert "reconcile" in lowered
    # The disposal is recorded partial, never success.
    assert "never `success`" in lowered
    assert "report success" not in lowered


def test_preamble_forbids_deferral_for_owned_blocker() -> None:
    """The clause must explicitly forbid the external-deferral verdict for an
    owned blocker — the false handoff this lever exists to name and refuse.

    It must forbid ``awaiting tend push`` / external-deferral language for an
    OWNED (``CONFLICTING`` / ``DIRTY``) blocker, while still scoping that verdict
    as honest only for a genuinely external (``AWAITING_APPROVAL``) blocker
    (``AWAITING_PUSH`` is now owned, proposal 098).
    """
    preamble = merge_truth_first_preamble()
    lowered = preamble.lower()
    # The deferral phrase is named as forbidden for an owned blocker.
    assert "awaiting tend push" in lowered
    assert "may not write" in lowered
    assert "external-deferral verdict for an owned" in lowered
    # The prohibition is scoped: deferral stays honest only for the external
    # approval wait — AWAITING_PUSH is owned, not part of the honest set.
    assert "awaiting_approval" in lowered
    # It names the mislabel as a false handoff.
    assert "false handoff" in lowered


def test_preamble_owned_disposal_follows_action_zero() -> None:
    """The OWNED-DISPOSAL clause is downstream of the fetch + is-ancestor
    reading that authors any verdict — it must come after both, never replace
    them."""
    preamble = MERGE_TRUTH_FIRST_PREAMBLE
    fetch_idx = preamble.find("git fetch origin")
    ancestor_idx = preamble.find("git merge-base --is-ancestor HEAD origin/main")
    owned_idx = preamble.find("OWNED-DISPOSAL")
    assert owned_idx != -1, "preamble missing the OWNED-DISPOSAL clause"
    assert fetch_idx < ancestor_idx < owned_idx, (
        "the action-zero ordering must precede the OWNED-DISPOSAL clause"
    )


def test_preamble_names_ownership_fork_before_awaiting() -> None:
    """The OWNERSHIP-FORK step must (a) instruct the ``git rev-parse HEAD`` vs
    ``origin/<branch>`` compare, and (b) forbid ``awaiting`` phrasing when HEAD is
    ahead — the obligation-fork verdict floor's action-zero (criterion 1)."""
    preamble = merge_truth_first_preamble()
    assert preamble == MERGE_TRUTH_FIRST_PREAMBLE
    # (a) The rev-parse ownership-fork compare instruction.
    assert "OWNERSHIP FORK" in preamble
    assert "git rev-parse HEAD" in preamble
    assert "git rev-parse origin/<branch>" in preamble
    lowered = preamble.lower()
    # (b) A clause forbidding 'awaiting' when HEAD is ahead / push is owed.
    assert "the push is\n  owed" in lowered or "the push is owed" in lowered
    assert "may\n  not write" in lowered or "may not write" in lowered
    assert "awaiting" in lowered
    # The owed push is named as the agent's own terminal act.
    assert "git push origin head:<branch>" in lowered


def test_preamble_ownership_fork_follows_action_zero() -> None:
    """The OWNERSHIP-FORK step is downstream of the fetch + is-ancestor reading
    and upstream of the CHOSEN STAND-DOWN clause — it forks the verdict the
    action-zero reads author, never replaces them."""
    preamble = MERGE_TRUTH_FIRST_PREAMBLE
    fetch_idx = preamble.find("git fetch origin")
    ancestor_idx = preamble.find("git merge-base --is-ancestor HEAD origin/main")
    fork_idx = preamble.find("OWNERSHIP FORK")
    standdown_idx = preamble.find("CHOSEN STAND-DOWN")
    assert fork_idx != -1, "preamble missing the OWNERSHIP FORK step"
    assert fetch_idx < ancestor_idx < fork_idx < standdown_idx, (
        "the OWNERSHIP FORK must sit after action-zero, before CHOSEN STAND-DOWN"
    )


def test_preamble_names_awaiting_push_only_in_owned_push_instruction() -> None:
    """``AWAITING_PUSH`` is named exactly once — in the owned-push instruction —
    and never in an external stand-down (proposal 098).

    This is the step's binding acceptance: ``AWAITING_PUSH`` classifies owned, so
    it appears only in the one "push it, that is your terminal act" instruction
    and never in a stand-down phrase.
    """
    on = merge_truth_first_preamble()
    # Named exactly once — the owned-push terminal-act instruction.
    assert on.count("AWAITING_PUSH") == 1
    idx = on.find("AWAITING_PUSH")
    owned_window = on[idx : idx + 200]
    assert "is OWNED the same way" in owned_window
    assert "git push origin HEAD:<branch>" in owned_window
    # The external stand-down clauses no longer name it.
    assert "or `AWAITING_PUSH` (Tend will deliver the push)" not in on
    assert "approval or push" not in on
    assert "Tend's push" not in on
    # The genuinely-external approval wait stays the stand-down blocker.
    assert "AWAITING_APPROVAL" in on


def test_preamble_owned_disposal_excludes_awaiting_push_from_external() -> None:
    """The OWNED-DISPOSAL external-wait lists exclude ``AWAITING_PUSH`` while
    keeping ``AWAITING_APPROVAL`` and ``BEHIND_MAIN`` (proposal 098).

    ``BEHIND_MAIN`` is untouched — a behind-but-clean branch is genuinely Tend's
    mechanical catch-up, out of scope for this proposal.
    """
    on = merge_truth_first_preamble()
    assert "`AWAITING_APPROVAL` / `BEHIND_MAIN` wait" in on
    assert "external\n(`AWAITING_APPROVAL`). Applying it" in on
    assert "BEHIND_MAIN" in on


def test_preamble_preserves_merge_truth_ordering() -> None:
    """The action-zero ordering must still precede the new clause.

    ``git fetch`` must come before ``git merge-base --is-ancestor``, and both
    must come before the ``STAND-DOWN-ON-RESOLVED`` heading — the stand-down
    decision is downstream of the fetch + is-ancestor reading that authors it,
    never a substitute for it.
    """
    preamble = MERGE_TRUTH_FIRST_PREAMBLE
    fetch_idx = preamble.find("git fetch origin")
    ancestor_idx = preamble.find("git merge-base --is-ancestor HEAD origin/main")
    standdown_idx = preamble.find("STAND-DOWN-ON-RESOLVED")
    assert fetch_idx != -1, "preamble missing `git fetch origin`"
    assert ancestor_idx != -1, "preamble missing the full `--is-ancestor` form"
    assert standdown_idx != -1, "preamble missing the STAND-DOWN-ON-RESOLVED clause"
    assert fetch_idx < ancestor_idx, "`git fetch` must precede `--is-ancestor`"
    assert ancestor_idx < standdown_idx, (
        "the action-zero ordering must precede the STAND-DOWN-ON-RESOLVED clause"
    )


# ── Preamble-to-classifier binding (proposal 098; relocated from the retired
# ``tests/test_blocker_ownership_binding.py`` by proposal 109) ──────────────────
#
# These assert the preamble's owned/external prose *tracks the classifier's
# verdict* (whether a phrase is present is decided by ``classify_blocker_ownership``,
# never an independent prose switch) — complementing, not duplicating, the
# literal-prose tests above. Their one-source AST twin now lives in the
# structural-guard registry (``tests/test_structural_guards.py``).


def test_awaiting_push_prose_placement_follows_classifier() -> None:
    """``AWAITING_PUSH`` is named owned exactly as the classifier says.

    The binding: whether the pivot reason code appears in the owned-push
    instruction or in the external stand-down gloss is decided by
    ``classify_blocker_ownership(..., own_push=True)``, never by an independent
    prose switch. The classifier marks ``AWAITING_PUSH`` owned, so the preamble
    names it only in the owned-push instruction — never in the external
    stand-down gloss — and exactly once.
    """
    preamble = merge_truth_first_preamble()
    owned = (
        classify_blocker_ownership(MERGE_BLOCK_AWAITING_PUSH, own_push=True)
        == BLOCKER_OWNERSHIP_OWNED
    )
    assert owned  # sanity: the classifier marks the pivot code owned
    # Owned ⇒ named in the owned-push instruction; never in the external
    # stand-down gloss. The presence of each phrase tracks the classifier.
    assert (_OWNED_PUSH_INSTRUCTION in preamble) is owned
    assert (_AWAITING_PUSH_EXTERNAL_GLOSS in preamble) is (not owned)
    # Once owned, the code survives only in that single owned-push instruction
    # — it is scrubbed from every external stand-down phrase.
    assert preamble.count(MERGE_BLOCK_AWAITING_PUSH) == 1


def test_always_external_codes_stay_external_waits() -> None:
    """``AWAITING_APPROVAL`` and ``BEHIND_MAIN`` stay external waits.

    Proves the binding does not over-reach: the human-approval gate and the
    behind-but-clean catch-up classify external (the classifier says so — only
    ``AWAITING_PUSH`` moved to owned), and the preamble keeps naming them, so the
    honest "wait for Thomas / Tend's catch-up" stand-down is untouched.
    """
    preamble = merge_truth_first_preamble()
    for code in (MERGE_BLOCK_AWAITING_APPROVAL, MERGE_BLOCK_BEHIND_MAIN):
        assert (
            classify_blocker_ownership(code, own_push=True)
            == BLOCKER_OWNERSHIP_EXTERNAL
        )
        assert code in preamble


def test_owned_disposal_honest_set_equals_classifier_external() -> None:
    """The OWNED-DISPOSAL "deferral is honest only for external" list equals the
    classifier's external partition of the stand-down candidates.

    This is the set-equality binding the proposal names: the codes the clause
    lists as the *only* blockers for which the external-deferral verdict is honest
    must be exactly ``{AWAITING_APPROVAL, AWAITING_PUSH}`` filtered to the
    classifier's external class — so ``AWAITING_PUSH`` drops out of the honest set
    now that it is owned, and the prompt can never authorize the deferral verdict
    for a code the classifier marks owned.
    """
    preamble = merge_truth_first_preamble()
    external = [
        code
        for code in (MERGE_BLOCK_AWAITING_APPROVAL, MERGE_BLOCK_AWAITING_PUSH)
        if classify_blocker_ownership(code, own_push=True)
        == BLOCKER_OWNERSHIP_EXTERNAL
    ]
    honest_list = " / ".join(f"`{code}`" for code in external)
    # The clause reads "... honest when the sole blocker is external\n(<list>)."
    assert f"external\n({honest_list})." in preamble


def test_size_pin_cure_hint_fires_on_guard_failure_line() -> None:
    """A realistic pytest failure line naming the size guard yields the cure line.

    The trigger is the text the already-rendered ``## CI Failure`` section
    carries, so the input here is shaped the way ``render_ci_failure_section``
    shapes it — a check name plus the concrete ``FAILED <module>::<test>`` line.
    """
    section = (
        "## CI Failure\n- quality:\n"
        "  - FAILED tests/test_source_file_size.py::"
        "test_source_file_under_split_budget[maestro/agent.py] - AssertionError\n"
    )
    hint = size_pin_cure_hint(section)
    assert hint != ""
    # A single bounded line: the cure text costs the directive's char cap one line.
    assert "\n" not in hint
    assert hint == hint.strip()
    assert len(hint) < 300


def test_size_pin_cure_hint_names_extraction_and_forbids_raising() -> None:
    """The cure line states the SPLIT_BUDGET ceiling, names a concern-named
    sibling as the cure, rules out trimming, and explicitly forbids raising the
    budget — the fix that looks obvious and is exactly what the guard prevents."""
    hint = size_pin_cure_hint(f"FAILED tests/{SIZE_GUARD_MARKER}test_x")
    assert "below SPLIT_BUDGET" in hint
    assert "sibling module" in hint
    assert "<parent>_<concern>" in hint
    assert "trimming comments, logs or names to fit is not a cure" in hint
    assert "raising the budget is forbidden" in hint
    # The guard's own module is named so the lap can find the budget.
    assert "tests/test_source_file_size.py" in hint


def test_size_pin_cure_hint_is_empty_for_other_gate_failures() -> None:
    """A ruff / mypy / ordinary-test excerpt yields ``""`` — the empty-string
    default is what keeps every non-matching remediation route byte-identical."""
    for text in (
        "## CI Failure\n- quality:\n  - tests/test_widget.py:12:1: F401 unused\n",
        "## CI Failure\n- quality:\n  - maestro/agent.py:88: error: Incompatible\n",
        "## CI Failure\n- quality:\n"
        "  - FAILED tests/test_agent.py::test_run - AssertionError\n",
    ):
        assert size_pin_cure_hint(text) == ""


def test_size_pin_cure_hint_is_empty_for_empty_input() -> None:
    """The default ``ci_failure_section=""`` route appends nothing."""
    assert size_pin_cure_hint("") == ""


def test_size_pin_cure_hint_match_is_case_sensitive() -> None:
    """The marker is a file-path stem pytest reproduces verbatim, so the match is
    case-sensitive; a case-folded variant does not trigger the hint.

    Asserting the implementation's actual behaviour rather than an aspiration:
    widening to a case-insensitive match would fire on text the guard never emits.
    """
    assert size_pin_cure_hint("FAILED tests/TEST_SOURCE_FILE_SIZE.py::test_x") == ""


def test_size_pin_cure_hint_is_empty_for_lint_diagnostic_inside_the_guard() -> None:
    """A ruff / mypy diagnostic located *inside* the guard module yields ``""``.

    The guard file is ordinary source to ruff and mypy: an unused import or a
    type error there is cured the ordinary way, so the extraction-and-lower
    hint would mislead the lap. Only a pytest failure *identifier*
    (``<module>.py::<test>``) — which those diagnostics never carry — triggers
    the hint.
    """
    for text in (
        "## CI Failure\n- quality:\n"
        "  - tests/test_source_file_size.py:12:1: F401 unused import\n",
        "## CI Failure\n- quality:\n"
        "  - tests/test_source_file_size.py:44: error: Incompatible types\n",
    ):
        assert size_pin_cure_hint(text) == ""
