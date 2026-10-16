"""Tests for the mechanical outcome authority (``decide_outcome``).

The pure function computes the landing verdict from injected git-truth alone —
``"success"`` iff the PR-landing dimension applies and the branch landed, else
``"partial"``; a ``"failure"`` candidate and a non-landing-relevant record pass
through untouched. It reads **no** prose: the prose-invariance guard asserts the
function has no summary/findings parameter to read (proposal 105, increment 1).
No I/O, no SDK — the caller supplies :class:`OutcomeFacts`.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from maestro.outcome_authority import (
    _DELIVERED_CLAUSE,
    _LAP_DELIVERING_NOTE,
    _NULL_LAP_NOTE,
    _PUSH_OWED_CLAUSE,
    _PUSH_OWED_UNVERIFIED_CLAUSE,
    OutcomeFacts,
    bind_delivery_claim_to_push_truth,
    compose_partial_delivery_clause,
    decide_outcome,
    partial_provenance,
)
from maestro.pr_remediation_state import Marker
from maestro.remediation_tracker import (
    MERGE_BLOCK_AWAITING_APPROVAL,
    MERGE_BLOCK_AWAITING_PUSH,
    MERGE_BLOCK_CI_FAILING,
    classify_lap_delivery,
)
from maestro.types import TaskOutcome


@pytest.mark.parametrize("candidate", ["success", "partial"])
def test_landing_confirmed_yields_success(candidate: TaskOutcome) -> None:
    """A landing-relevant record whose branch git shows landed → ``success`` —
    including a ``partial`` candidate the confirmed landing upgrades."""
    facts = OutcomeFacts(landing_relevant=True, landing_confirmed=True)
    assert decide_outcome(candidate, facts) == "success"


@pytest.mark.parametrize("candidate", ["success", "partial"])
def test_landing_unconfirmed_yields_partial(candidate: TaskOutcome) -> None:
    """A landing-relevant record git cannot confirm landed → ``partial`` —
    including a ``success`` candidate the missing landing downgrades."""
    facts = OutcomeFacts(landing_relevant=True, landing_confirmed=False)
    assert decide_outcome(candidate, facts) == "partial"


@pytest.mark.parametrize("candidate", ["success", "partial"])
@pytest.mark.parametrize("confirmed", [True, False])
def test_not_landing_relevant_returns_candidate_unchanged(
    candidate: TaskOutcome, confirmed: bool,
) -> None:
    """With no PR-landing dimension there is nothing to ground against, so the
    candidate is returned unchanged regardless of the (irrelevant) landing bit."""
    facts = OutcomeFacts(landing_relevant=False, landing_confirmed=confirmed)
    assert decide_outcome(candidate, facts) == candidate


@pytest.mark.parametrize("relevant", [True, False])
@pytest.mark.parametrize("confirmed", [True, False])
def test_failure_candidate_never_rescued(
    relevant: bool, confirmed: bool,
) -> None:
    """A ``"failure"`` candidate is returned unchanged for every fact
    combination — a landing check never rescues or manufactures a failure."""
    facts = OutcomeFacts(landing_relevant=relevant, landing_confirmed=confirmed)
    assert decide_outcome("failure", facts) == "failure"


def test_prose_invariance_no_text_parameter() -> None:
    """The function cannot read narration: its only parameters are the candidate
    outcome and the git-truth facts — no summary/findings/text parameter — and
    :class:`OutcomeFacts` carries only boolean git-truth fields."""
    params = list(inspect.signature(decide_outcome).parameters)
    assert params == ["candidate_outcome", "facts"]

    field_names = {f.name for f in dataclasses.fields(OutcomeFacts)}
    assert field_names == {
        "landing_relevant", "landing_confirmed", "lap_delivering",
    }
    for f in dataclasses.fields(OutcomeFacts):
        assert f.type in ("bool", bool)
    # No field name hints at prose the authority might read.
    for name in field_names:
        assert not any(
            token in name for token in ("summary", "findings", "text", "prose")
        )


def test_prose_invariance_identical_facts_identical_verdict() -> None:
    """Identical facts always produce the identical verdict — the function is a
    pure function of its two arguments, with no hidden text channel."""
    facts = OutcomeFacts(landing_relevant=True, landing_confirmed=False)
    first = decide_outcome("success", facts)
    second = decide_outcome("success", facts)
    assert first == second == "partial"


def test_outcome_facts_is_frozen() -> None:
    """``OutcomeFacts`` is immutable — the injected git-truth cannot be mutated
    after construction."""
    facts = OutcomeFacts(landing_relevant=True, landing_confirmed=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        facts.landing_confirmed = False  # type: ignore[misc]


# ──────────────────────────────────────────────────────────────────────
# partial_provenance — delivery provenance beside the verdict (proposal 116)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("candidate", ["success", "failure"])
@pytest.mark.parametrize("lap_delivering", [True, False])
def test_partial_provenance_none_for_non_partial(
    candidate: TaskOutcome, lap_delivering: bool,
) -> None:
    """Only a ``partial`` verdict carries delivery provenance — a ``success`` or
    ``failure`` landing verdict returns ``None`` regardless of the delivery
    bit."""
    facts = OutcomeFacts(
        landing_relevant=True,
        landing_confirmed=candidate == "success",
        lap_delivering=lap_delivering,
    )
    assert partial_provenance(candidate, facts) is None


def test_partial_provenance_delivering_note() -> None:
    """A landing-relevant ``partial`` whose lap advanced its owed head → the
    ``[delivering]`` note."""
    facts = OutcomeFacts(
        landing_relevant=True, landing_confirmed=False, lap_delivering=True,
    )
    assert partial_provenance("partial", facts) == _LAP_DELIVERING_NOTE


def test_partial_provenance_null_lap_note() -> None:
    """A landing-relevant ``partial`` whose lap advanced no owed head → the
    ``[null-lap]`` note."""
    facts = OutcomeFacts(
        landing_relevant=True, landing_confirmed=False, lap_delivering=False,
    )
    assert partial_provenance("partial", facts) == _NULL_LAP_NOTE


@pytest.mark.parametrize("lap_delivering", [True, False])
def test_partial_provenance_none_when_not_landing_relevant(
    lap_delivering: bool,
) -> None:
    """A ``partial`` with no PR-landing dimension has no owed head to describe,
    so no provenance note is produced even when the delivery bit is set."""
    facts = OutcomeFacts(
        landing_relevant=False,
        landing_confirmed=False,
        lap_delivering=lap_delivering,
    )
    assert partial_provenance("partial", facts) is None


def test_provenance_notes_are_bracket_marked_and_marker_safe() -> None:
    """The notes carry their unique bracket marker and none of the push-family
    marker strings a downstream ``key_findings`` scanner reads — so appending a
    note to ``key_findings`` can never falsely trip the conflict-cooldown bypass
    or the chosen-quiet classifier."""
    assert _LAP_DELIVERING_NOTE.startswith("[delivering]")
    assert _NULL_LAP_NOTE.startswith("[null-lap]")
    for note in (_LAP_DELIVERING_NOTE, _NULL_LAP_NOTE):
        lowered = note.lower()
        assert "awaiting tend push" not in lowered
        assert "delivered to pr, awaiting review/merge" not in lowered
        assert "[owned-push-available]" not in lowered
        assert "[push-unconfirmed]" not in lowered
        assert "[stood-down]" not in lowered


@pytest.mark.parametrize("candidate", ["success", "partial", "failure"])
@pytest.mark.parametrize("landing_confirmed", [True, False])
@pytest.mark.parametrize("landing_relevant", [True, False])
@pytest.mark.parametrize("lap_delivering", [True, False])
def test_decide_outcome_ignores_lap_delivering(
    candidate: TaskOutcome,
    landing_confirmed: bool,
    landing_relevant: bool,
    lap_delivering: bool,
) -> None:
    """The new ``lap_delivering`` field never enters the landing verdict: for
    every fact combination the verdict is identical with the bit set either way,
    so ``decide_outcome``'s three-value output is byte-for-byte unchanged."""
    base = OutcomeFacts(
        landing_relevant=landing_relevant, landing_confirmed=landing_confirmed,
    )
    flipped = OutcomeFacts(
        landing_relevant=landing_relevant,
        landing_confirmed=landing_confirmed,
        lap_delivering=lap_delivering,
    )
    assert decide_outcome(candidate, base) == decide_outcome(candidate, flipped)


# ──────────────────────────────────────────────────────────────────────
# Delivery clause bound to push-truth — forbid "delivered to pr" under
# AWAITING_PUSH (proposal: push-truth summary bind)
# ──────────────────────────────────────────────────────────────────────

_DELIVERED_SUBSTR = "delivered to pr"
_LLM_DELIVERED_SUMMARY = (
    f"Resolved on branch, {Marker.DELIVERED_AWAITING_MERGE} — addressed the "
    "review comments."
)


def test_compose_clause_awaiting_push_is_push_owed() -> None:
    """An ``AWAITING_PUSH`` blocker composes the push-owed clause — no
    ``delivered to pr`` substring, and it names HEAD ahead of origin (criteria
    1 + 2)."""
    clause = compose_partial_delivery_clause(MERGE_BLOCK_AWAITING_PUSH)
    assert clause == _PUSH_OWED_CLAUSE
    assert _DELIVERED_SUBSTR not in clause.lower()
    assert "HEAD ahead of origin" in clause


@pytest.mark.parametrize(
    "blocker", [MERGE_BLOCK_AWAITING_APPROVAL, MERGE_BLOCK_CI_FAILING, "", None],
)
def test_compose_clause_external_retains_delivered(blocker: str | None) -> None:
    """A non-``AWAITING_PUSH`` blocker (external review/CI, or absent) composes
    the retained delivered clause (criterion 3)."""
    clause = compose_partial_delivery_clause(blocker)
    assert clause == _DELIVERED_CLAUSE
    assert _DELIVERED_SUBSTR in clause.lower()


@pytest.mark.parametrize("blocker", ["awaiting_push", "  AWAITING_PUSH  "])
def test_compose_clause_awaiting_push_case_and_space_insensitive(
    blocker: str,
) -> None:
    """The blocker match is case- and whitespace-insensitive, mirroring the
    committed-unpushed actuator's ``strip().upper()`` compare."""
    assert compose_partial_delivery_clause(blocker) == _PUSH_OWED_CLAUSE


def test_compose_clause_awaiting_push_never_attempted_is_neutral() -> None:
    """An ``AWAITING_PUSH`` blocker whose push was *never attempted*
    (``push_delivered is None``) composes the neutral push-owed clause: it owns
    the push (``[push-owed]``, no ``delivered to pr``) but does NOT assert the
    unverified "HEAD ahead of origin" git fact — the branch may already be
    up-to-date on origin (criterion 1 preserved, over-assertion removed)."""
    clause = compose_partial_delivery_clause(
        MERGE_BLOCK_AWAITING_PUSH, push_delivered=None,
    )
    assert clause == _PUSH_OWED_UNVERIFIED_CLAUSE
    assert clause.startswith("[push-owed]")
    assert _DELIVERED_SUBSTR not in clause.lower()
    assert "HEAD ahead of origin" not in clause
    assert classify_lap_delivery(clause) == "NULL"


def test_neutral_push_owed_clause_does_not_assert_unverified_owed_fact() -> None:
    """The ``push_delivered is None`` (never-attempted) clause must NOT positively
    assert that a push *is* owed. Per the review (PR #3586, comment 3868416285),
    "a push is owed" is true iff local commits have not reached ``origin/<branch>``
    — i.e. iff HEAD is ahead of origin — the *exact same* unverified git fact the
    literal string "HEAD ahead of origin" names, merely reworded. In the reachable
    checkout-fallback state (``branch_name != pr_branch`` → ``push_delivered=None``)
    the branch may already be up-to-date on origin from a prior lap, so a settled
    "push still owed" / "not closed" claim is git-ungrounded. The clause must
    express only *non-confirmation* — that closure was not *verified* — not a
    verified owed-push fact."""
    clause = compose_partial_delivery_clause(
        MERGE_BLOCK_AWAITING_PUSH, push_delivered=None,
    )
    lowered = clause.lower()
    # A settled positive assertion that a push IS owed / the transaction IS not
    # closed is the reworded unverified git fact — forbidden for the None arm.
    assert "still owed" not in lowered
    assert "not closed" not in lowered
    # It must instead hedge to non-confirmation (git never verified closure).
    assert ("not confirmed" in lowered) or ("not verified" in lowered)


def test_compose_clause_awaiting_push_confirmed_failed_names_head_ahead() -> None:
    """An ``AWAITING_PUSH`` blocker whose push was *attempted and confirmed-failed*
    (``push_delivered is False``) still names HEAD ahead of origin — the local
    commits provably did not reach origin, so the git fact holds (criterion 2)."""
    clause = compose_partial_delivery_clause(
        MERGE_BLOCK_AWAITING_PUSH, push_delivered=False,
    )
    assert clause == _PUSH_OWED_CLAUSE
    assert "HEAD ahead of origin" in clause


def test_compose_awaiting_push_arms_do_not_collapse() -> None:
    """The two AWAITING_PUSH arms must stay *distinct*: this is the exact
    regression the review flagged — collapsing ``None`` (never attempted) into
    ``False`` (confirmed-failed) is what made the record over-assert "HEAD ahead
    of origin" on a possibly up-to-date branch. Only the confirmed-failed arm may
    name that git fact; both remain push-owed and carry no ``delivered to pr``."""
    never_attempted = compose_partial_delivery_clause(
        MERGE_BLOCK_AWAITING_PUSH, push_delivered=None,
    )
    confirmed_failed = compose_partial_delivery_clause(
        MERGE_BLOCK_AWAITING_PUSH, push_delivered=False,
    )
    assert never_attempted != confirmed_failed
    assert "HEAD ahead of origin" in confirmed_failed
    assert "HEAD ahead of origin" not in never_attempted
    for clause in (never_attempted, confirmed_failed):
        assert clause.startswith("[push-owed]")
        assert _DELIVERED_SUBSTR not in clause.lower()


def test_bind_awaiting_push_scrubs_delivered_claim() -> None:
    """A push-owed lap (AWAITING_PUSH, push not delivered) whose summary carries
    an agent-authored delivery claim is scrubbed: the composed summary loses the
    ``delivered to pr`` substring and gains the push-owed marker naming HEAD
    ahead of origin (criteria 1 + 2)."""
    bound = bind_delivery_claim_to_push_truth(
        _LLM_DELIVERED_SUMMARY,
        MERGE_BLOCK_AWAITING_PUSH,
        push_delivered=False,
    )
    assert bound is not None
    assert _DELIVERED_SUBSTR not in bound.lower()
    assert "HEAD ahead of origin" in bound
    # The downstream delivery sensor must no longer misread it as DELIVERING.
    assert classify_lap_delivery(bound) == "NULL"


def test_bind_awaiting_push_never_attempted_scrubs_to_neutral_clause() -> None:
    """A push-owed lap whose push was *never attempted* (``push_delivered is
    None``) — reachable when a checkout fallback left ``branch_name != pr_branch``
    or the outcome was non-deliverable, yet the prestamp gate still labelled the
    blocker AWAITING_PUSH — scrubs the ``delivered to pr`` claim to the *neutral*
    push-owed clause. The record must not assert "HEAD ahead of origin" because
    git never confirmed it (the branch may be up-to-date on origin from a prior
    lap), but the claim must still lose delivery phrasing and gain a push-owed
    marker."""
    bound = bind_delivery_claim_to_push_truth(
        _LLM_DELIVERED_SUMMARY,
        MERGE_BLOCK_AWAITING_PUSH,
        push_delivered=None,
    )
    assert bound is not None
    assert _DELIVERED_SUBSTR not in bound.lower()
    assert _PUSH_OWED_UNVERIFIED_CLAUSE in bound
    assert "HEAD ahead of origin" not in bound
    # The downstream delivery sensor must no longer misread it as DELIVERING.
    assert classify_lap_delivery(bound) == "NULL"


def test_bind_awaiting_push_scrubs_mixed_case_claim() -> None:
    """A mixed-case delivery claim — which ``classify_lap_delivery`` would still
    catch via its own lower-casing — is scrubbed too, so no case escapes the
    bind."""
    text = "RESOLVED, DELIVERED TO PR, AWAITING REVIEW/MERGE."
    bound = bind_delivery_claim_to_push_truth(
        text, MERGE_BLOCK_AWAITING_PUSH, push_delivered=False,
    )
    assert bound is not None
    assert _DELIVERED_SUBSTR not in bound.lower()
    assert classify_lap_delivery(bound) == "NULL"


@pytest.mark.parametrize(
    "blocker", [MERGE_BLOCK_AWAITING_APPROVAL, MERGE_BLOCK_CI_FAILING],
)
def test_bind_external_blocker_retains_delivered_claim(blocker: str) -> None:
    """An external-only blocker (review/CI) retains the ``delivered to pr``
    phrasing — the resolution is on the branch and only a human act remains
    (criterion 3)."""
    bound = bind_delivery_claim_to_push_truth(
        _LLM_DELIVERED_SUMMARY, blocker, push_delivered=False,
    )
    assert bound == _LLM_DELIVERED_SUMMARY
    assert _DELIVERED_SUBSTR in (bound or "").lower()


def test_bind_confirmed_push_retains_delivered_claim() -> None:
    """A confirmed push (``push_delivered is True``) keeps the honest delivery
    claim even when the blocker field still reads the stale pre-push
    AWAITING_PUSH — the mechanical marker swap already stamped a real
    delivery."""
    bound = bind_delivery_claim_to_push_truth(
        _LLM_DELIVERED_SUMMARY,
        MERGE_BLOCK_AWAITING_PUSH,
        push_delivered=True,
    )
    assert bound == _LLM_DELIVERED_SUMMARY
    assert _DELIVERED_SUBSTR in (bound or "").lower()


@pytest.mark.parametrize("push_delivered", [False, None])
def test_bind_none_and_empty_text_pass_through(
    push_delivered: bool | None,
) -> None:
    """Falsy text (``None`` findings or an empty summary) is returned unchanged
    regardless of blocker or push-truth — nothing to scrub."""
    assert bind_delivery_claim_to_push_truth(
        None, MERGE_BLOCK_AWAITING_PUSH, push_delivered=push_delivered,
    ) is None
    assert bind_delivery_claim_to_push_truth(
        "", MERGE_BLOCK_AWAITING_PUSH, push_delivered=push_delivered,
    ) == ""


def test_bind_awaiting_push_no_claim_is_noop() -> None:
    """A push-owed summary that never claimed delivery is returned unchanged —
    the bind only rewrites the delivery phrasing, nothing else."""
    text = "Resolved on branch; push still owed."
    assert bind_delivery_claim_to_push_truth(
        text, MERGE_BLOCK_AWAITING_PUSH, push_delivered=False,
    ) == text


def test_push_owed_clause_carries_no_foreign_protocol_marker() -> None:
    """The push-owed clause carries its own ``[push-owed]`` bracket and none of
    the push-family protocol markers a sensor keys on, so appending it can never
    falsely trip a delivery/forced-quiet classifier."""
    assert _PUSH_OWED_CLAUSE.startswith("[push-owed]")
    lowered = _PUSH_OWED_CLAUSE.lower()
    assert "delivered to pr, awaiting review/merge" not in lowered
    assert str(Marker.OWNED_PUSH_AVAILABLE) not in lowered
    assert str(Marker.AWAITING_TEND_PUSH) not in lowered
    assert str(Marker.PUSH_UNCONFIRMED) not in lowered
    assert classify_lap_delivery(_PUSH_OWED_CLAUSE) == "NULL"


def test_neutral_push_owed_clause_carries_no_foreign_protocol_marker() -> None:
    """The neutral never-attempted clause carries its own ``[push-owed]`` bracket
    and none of the push-family protocol markers a sensor keys on, so appending it
    can never falsely trip a delivery/forced-quiet classifier — and, unlike the
    assertive clause, it names no unverified "HEAD ahead of origin" git fact."""
    assert _PUSH_OWED_UNVERIFIED_CLAUSE.startswith("[push-owed]")
    lowered = _PUSH_OWED_UNVERIFIED_CLAUSE.lower()
    assert "delivered to pr, awaiting review/merge" not in lowered
    assert "head ahead of origin" not in lowered
    assert str(Marker.OWNED_PUSH_AVAILABLE) not in lowered
    assert str(Marker.AWAITING_TEND_PUSH) not in lowered
    assert str(Marker.PUSH_UNCONFIRMED) not in lowered
    assert classify_lap_delivery(_PUSH_OWED_UNVERIFIED_CLAUSE) == "NULL"


def test_verdict_identical_across_blocker_classes_only_summary_differs() -> None:
    """Criterion 4: the landing verdict is identical for both blocker classes —
    it is a pure function of the git facts and never reads the blocker — while
    only the composed summary text diverges."""
    facts = OutcomeFacts(landing_relevant=True, landing_confirmed=False)
    push_verdict = decide_outcome("success", facts)
    external_verdict = decide_outcome("success", facts)
    assert push_verdict == external_verdict == "partial"

    push_summary = bind_delivery_claim_to_push_truth(
        _LLM_DELIVERED_SUMMARY, MERGE_BLOCK_AWAITING_PUSH, push_delivered=False,
    )
    external_summary = bind_delivery_claim_to_push_truth(
        _LLM_DELIVERED_SUMMARY,
        MERGE_BLOCK_AWAITING_APPROVAL,
        push_delivered=False,
    )
    # Same verdict, divergent summary: the change touches only narration.
    assert push_summary != external_summary
    assert _DELIVERED_SUBSTR not in (push_summary or "").lower()
    assert _DELIVERED_SUBSTR in (external_summary or "").lower()
