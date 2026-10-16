"""Tests for the extracted
``maestro.loop.record_binds.apply_remediation_narration_framing``.

This is ``run``'s ``# 5b-ii.`` + ``# 5c.`` pair lifted into one named,
independently-testable stage. ``# 5b-ii.`` scrubs an agent-authored "delivered
to pr, awaiting review/merge" claim down to a push-owed clause when this lap's
mechanical truth is push-owed; ``# 5c.`` then prepends the loop-authored host
verdict so downstream context injection reports "awaiting tend push" rather than
"delivered". Their order is load-bearing — the scrub must run *first* so the
already-honest ``remediation_note`` is never rewritten by it — which is why the
two are one function rather than two.

The extraction is behaviour-preserving, so these tests pin all four arms of the
function's own branch matrix — the AWAITING_PUSH scrub taken or skipped, crossed
with the note framing taken or skipped — plus the input variations each arm
turns on (claim present or not, ``push_delivered`` ``False`` / ``None`` /
``True``, findings present or ``None``) and the ordering invariant, the way
``tests/test_apply_mergeable_flip_actuator.py`` pins its sibling. No
``agent.run``, no SDK patching — the function is called directly on a bare
``make_agent()`` agent with ``_last_remediation_merge_block`` assigned per test,
which is what the extraction bought.
"""

from __future__ import annotations

import pytest

from maestro.loop.record_binds import apply_remediation_narration_framing
from maestro.outcome_authority import compose_partial_delivery_clause
from maestro.pr_remediation_state import Marker
from maestro.remediation_tracker import (
    MERGE_BLOCK_AWAITING_APPROVAL,
    MERGE_BLOCK_AWAITING_PUSH,
)

#: The agent-authored false claim the scrub exists to bind to push-truth.
_CLAIM = str(Marker.DELIVERED_AWAITING_MERGE)
#: The loop-authored host verdict ``# 5c.`` prepends — already honest.
_NOTE = (
    f"Resolved on branch, {Marker.AWAITING_TEND_PUSH} — PR #2647 not yet "
    f"landed (merge blocker: {MERGE_BLOCK_AWAITING_PUSH})."
)
#: The assertive clause a *confirmed-failed* push (``push_delivered=False``)
#: substitutes; HEAD is genuinely ahead of origin.
_OWED = compose_partial_delivery_clause(
    MERGE_BLOCK_AWAITING_PUSH, push_delivered=False,
)
#: The neutral clause a *never-attempted* push (``push_delivered=None``)
#: substitutes; non-confirmation only, never an ungrounded "HEAD ahead" claim.
_UNVERIFIED = compose_partial_delivery_clause(
    MERGE_BLOCK_AWAITING_PUSH, push_delivered=None,
)


def test_push_owed_blocker_scrubs_the_false_claim_from_both_fields(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Blocker AWAITING_PUSH + failed push → claim bound to the push-owed clause.

    Both the summary and the distilled findings carry the claim, and both must
    come back with it replaced — a half-transaction narrated as whole is exactly
    what ``classify_lap_delivery`` would otherwise misread as DELIVERING.
    """
    agent = make_agent()
    agent._last_remediation_merge_block = MERGE_BLOCK_AWAITING_PUSH

    summary, key_findings = apply_remediation_narration_framing(
        agent,
        summary=f"Fixed the gate. {_CLAIM}.",
        key_findings=f"Threads cleared; {_CLAIM}.",
        remediation_note=None,
        push_delivered=False,
    )

    assert summary == f"Fixed the gate. {_OWED}."
    assert key_findings == f"Threads cleared; {_OWED}."
    assert _CLAIM not in summary
    assert _CLAIM not in key_findings


def test_never_attempted_push_substitutes_the_neutral_clause(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """``push_delivered=None`` asserts only non-confirmation, not "HEAD ahead"."""
    agent = make_agent()
    agent._last_remediation_merge_block = MERGE_BLOCK_AWAITING_PUSH

    summary, key_findings = apply_remediation_narration_framing(
        agent,
        summary=f"{_CLAIM} — see branch.",
        key_findings=None,
        remediation_note=None,
        push_delivered=None,
    )

    assert summary == f"{_UNVERIFIED} — see branch."
    assert key_findings is None


def test_claimless_summary_survives_the_scrub_byte_identical(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A push-owed lap that never claimed delivery is left exactly as written."""
    agent = make_agent()
    agent._last_remediation_merge_block = MERGE_BLOCK_AWAITING_PUSH
    original = "Rewrote the dequeue gate; commits are local only."

    summary, key_findings = apply_remediation_narration_framing(
        agent,
        summary=original,
        key_findings="One file touched.",
        remediation_note=None,
        push_delivered=False,
    )

    assert summary == original
    assert key_findings == "One file touched."


def test_empty_summary_passes_through_unchanged(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An empty summary comes back empty — the bind passes falsy text through.

    ``bind_delivery_claim_to_push_truth`` returns a falsy *text* unchanged, so
    the result is ``""`` whether or not the ``or summary`` tail is present: that
    tail is a typing device (it keeps the bind's ``str | None`` off the caller's
    ``str`` summary, which the ``TaskRecord`` requires), not a runtime fallback
    this test could distinguish. What is pinned here is the pass-through — an
    empty summary is never turned into ``None``.
    """
    agent = make_agent()
    agent._last_remediation_merge_block = MERGE_BLOCK_AWAITING_PUSH

    summary, key_findings = apply_remediation_narration_framing(
        agent,
        summary="",
        key_findings=None,
        remediation_note=None,
        push_delivered=False,
    )

    assert summary == ""
    assert key_findings is None


def test_confirmed_push_leaves_a_genuine_delivery_claim_untouched(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """``push_delivered=True`` → the blocker field is a stale pre-push read.

    The mechanical marker swap already stamped an honest delivery, so the claim
    stands even though the recorded blocker still reads AWAITING_PUSH.
    """
    agent = make_agent()
    agent._last_remediation_merge_block = MERGE_BLOCK_AWAITING_PUSH
    original = f"Pushed the fix. {_CLAIM}."

    summary, key_findings = apply_remediation_narration_framing(
        agent,
        summary=original,
        key_findings=_CLAIM,
        remediation_note=None,
        push_delivered=True,
    )

    assert summary == original
    assert key_findings == _CLAIM


@pytest.mark.parametrize(
    "blocker", [None, MERGE_BLOCK_AWAITING_APPROVAL],
    ids=["no_blocker_recorded", "external_review_gate"],
)
def test_non_matching_blocker_skips_both_binds(
    make_agent, blocker,  # type: ignore[no-untyped-def]
) -> None:
    """Only the owned AWAITING_PUSH blocker scrubs; every other lap is honest.

    An external review/CI gate means the resolution *is* on the PR branch and
    only a human act remains, so the delivery phrasing is true and stands.
    """
    agent = make_agent()
    agent._last_remediation_merge_block = blocker

    summary, key_findings = apply_remediation_narration_framing(
        agent,
        summary=f"Done. {_CLAIM}.",
        key_findings=f"Findings: {_CLAIM}.",
        remediation_note=None,
        push_delivered=False,
    )

    assert summary == f"Done. {_CLAIM}."
    assert key_findings == f"Findings: {_CLAIM}."


def test_remediation_note_frames_summary_and_findings(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A downgraded remediation leads with the host verdict, not "delivered"."""
    agent = make_agent()
    agent._last_remediation_merge_block = None

    summary, key_findings = apply_remediation_narration_framing(
        agent,
        summary="Reconciled the branch.",
        key_findings="Two threads replied.",
        remediation_note=_NOTE,
        push_delivered=None,
    )

    assert summary == f"{_NOTE} Reconciled the branch."
    assert key_findings == f"{_NOTE} Two threads replied."


def test_framing_honours_the_500_char_cap(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The framed summary keeps ``_frame_remediation_note``'s 500-char budget.

    Only the summary carries that budget. The findings are left uncapped, so a
    framed record's ``key_findings`` can run past the 1000-char ceiling
    ``_distill_findings`` applies to its own output — a known, pre-existing
    overflow this behaviour-preserving extraction deliberately does not change.
    The assertion below records that overflow's current size; it is not an
    invariant blessing unbounded findings.
    """
    agent = make_agent()
    agent._last_remediation_merge_block = None
    long_summary = "x" * 900

    summary, key_findings = apply_remediation_narration_framing(
        agent,
        summary=long_summary,
        key_findings="y" * 900,
        remediation_note=_NOTE,
        push_delivered=None,
    )

    assert len(summary) == 500
    assert summary.startswith(f"{_NOTE} ")
    assert len(key_findings or "") == len(_NOTE) + 1 + 900


def test_absent_note_skips_the_framing(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """``remediation_note=None`` → no prepend; the narration passes through."""
    agent = make_agent()
    agent._last_remediation_merge_block = None

    summary, key_findings = apply_remediation_narration_framing(
        agent,
        summary="Plain task summary.",
        key_findings="Plain findings.",
        remediation_note=None,
        push_delivered=None,
    )

    assert summary == "Plain task summary."
    assert key_findings == "Plain findings."


def test_none_findings_become_the_note_alone_when_framed(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """``_distill_findings`` may return ``None``; the note then *is* the findings."""
    agent = make_agent()
    agent._last_remediation_merge_block = None

    summary, key_findings = apply_remediation_narration_framing(
        agent,
        summary="Branch pushed.",
        key_findings=None,
        remediation_note=_NOTE,
        push_delivered=None,
    )

    assert summary == f"{_NOTE} Branch pushed."
    assert key_findings == _NOTE


def test_scrub_runs_before_framing_so_the_note_survives_unrewritten(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The ordering invariant: scrub first, then frame.

    The loop-authored note is already honest, so the scrub must never see it.
    The probe is a note that *quotes* the very claim it is correcting: under the
    correct order the quote survives verbatim as the summary's prefix, while a
    reordering (frame, then scrub over the combined text) would rewrite the
    quote inside the note too. The cap's half of the same pin — framing last is
    what keeps the *lengthened* narration inside 500 chars — needs a near-cap
    summary to discriminate, so it lives in the dedicated test below.
    """
    agent = make_agent()
    agent._last_remediation_merge_block = MERGE_BLOCK_AWAITING_PUSH
    quoting_note = f"{Marker.AWAITING_TEND_PUSH} — the step claimed '{_CLAIM}'."

    summary, key_findings = apply_remediation_narration_framing(
        agent,
        summary=f"All gates green. {_CLAIM}.",
        key_findings=f"{_CLAIM} on the branch.",
        remediation_note=quoting_note,
        push_delivered=False,
    )

    # The note reaches the record intact — its quoted claim is not scrubbed.
    assert summary.startswith(f"{quoting_note} ")
    assert key_findings == f"{quoting_note} {_OWED} on the branch."
    # ...while the agent's *own* claim, after the note, is bound to push-truth.
    assert summary == f"{quoting_note} All gates green. {_OWED}."


def test_framing_last_keeps_the_lengthened_summary_inside_the_cap(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The ordering pin's budget half, with a summary sized to discriminate.

    The push-owed clause is ~60 chars longer than the claim it replaces, so the
    order decides whether the cap sees the grown text. The summary is padded so
    the *framed, un-scrubbed* narration lands exactly on 500 with the claim
    intact: scrubbing first and framing last caps the grown text at 500 (what is
    asserted), while framing first would cap at 500 and only then substitute,
    landing 60 chars over budget.
    """
    agent = make_agent()
    agent._last_remediation_merge_block = MERGE_BLOCK_AWAITING_PUSH
    padding = "x" * (500 - len(_NOTE) - len(_CLAIM) - 2)

    summary, _ = apply_remediation_narration_framing(
        agent,
        summary=f"{padding} {_CLAIM}",
        key_findings=None,
        remediation_note=_NOTE,
        push_delivered=False,
    )

    assert len(summary) == 500
    assert summary.startswith(f"{_NOTE} {padding}")
