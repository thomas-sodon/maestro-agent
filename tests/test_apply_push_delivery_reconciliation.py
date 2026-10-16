"""Parity/behaviour test for
``loop.task_delivery.apply_push_delivery_reconciliation``.

The stage is the verbatim lift of ``run``'s ``# 5-push.`` block (proposal 106):
the idempotent backstop push *behind* the agent's own delivery, then the
reconciliation that maps this lap's delivery truth (``push_delivered`` /
``push_error``) onto the honest marker inside ``remediation_note`` and fires the
fire-or-close directive on an owned-but-undelivered push. These tests call the
method directly with keyword args and pin each delivery-truth arm — every
external (``_vcs.push`` / ``.fetch`` / ``.commits_not_on_remote``,
``_fetch_origin_for_merge_truth``, and the mem_fs-backed consciousness) is
mocked, so nothing shells out.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from maestro.loop.pr_remediation import _OWNED_PUSH_FIRE_OR_CLOSE_DIRECTIVE
from maestro.loop.task_delivery import apply_push_delivery_reconciliation
from maestro.pr_remediation_state import Marker
from maestro.vcs import VCSError

_PR_BRANCH = "maestro/evolve/ep700"


def test_branch_mismatch_guard_skips_push_note_unchanged(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Guard arm: ``branch_name != pr_branch`` never attempts the push, so
    ``push_delivered`` stays ``None``; with the branch reading EXTERNAL the note
    passes through byte-identical."""
    agent = make_agent()
    agent._fetch_origin_for_merge_truth = MagicMock()  # type: ignore[method-assign]
    agent._vcs.commits_not_on_remote.return_value = 0  # EXTERNAL
    note = f"Resolved — {Marker.AWAITING_TEND_PUSH} on PR #700."

    push_delivered, push_error, out_note = (
        apply_push_delivery_reconciliation(
            agent,
            branch_name="maestro/task/other",
            pr_branch=_PR_BRANCH,
            outcome="success",
            remediation_note=note,
            remediation_pr_num=700,
        )
    )

    assert push_delivered is None
    assert push_error is None
    assert out_note == note
    agent._vcs.push.assert_not_called()


def test_clean_deliver_swaps_to_delivered_marker(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Clean deliver arm: a first-try push success stamps ``push_delivered`` True
    and swaps the ``AWAITING_TEND_PUSH`` costume for ``DELIVERED_AWAITING_MERGE``
    — no fetch/retry, no fire-or-close directive."""
    agent = make_agent()
    note = f"Resolved — {Marker.AWAITING_TEND_PUSH} on PR #700."

    push_delivered, push_error, out_note = (
        apply_push_delivery_reconciliation(
            agent,
            branch_name=_PR_BRANCH,
            pr_branch=_PR_BRANCH,
            outcome="success",
            remediation_note=note,
            remediation_pr_num=700,
        )
    )

    assert push_delivered is True
    assert push_error is None
    agent._vcs.push.assert_called_once_with(_PR_BRANCH)
    agent._vcs.fetch.assert_not_called()
    assert Marker.DELIVERED_AWAITING_MERGE in out_note
    assert Marker.AWAITING_TEND_PUSH not in out_note
    assert not agent.consciousness.load_self_model().next_task_directive


def test_first_push_fails_then_retry_succeeds(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Retry arm: the first push raises, a fetch runs, and the single retry
    succeeds — ``push_delivered`` is True with the delivered marker."""
    agent = make_agent()
    agent._vcs.push.side_effect = [VCSError("stale"), None]
    note = f"Resolved — {Marker.AWAITING_TEND_PUSH}."

    push_delivered, push_error, out_note = (
        apply_push_delivery_reconciliation(
            agent,
            branch_name=_PR_BRANCH,
            pr_branch=_PR_BRANCH,
            outcome="partial",
            remediation_note=note,
            remediation_pr_num=700,
        )
    )

    assert push_delivered is True
    assert push_error is None
    agent._vcs.fetch.assert_called_once_with("origin")
    assert agent._vcs.push.call_count == 2
    assert Marker.DELIVERED_AWAITING_MERGE in out_note


def test_both_pushes_fail_owned_marker_suffix_and_directive(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Both-fail arm: both attempts raise, so ``push_delivered`` is False and
    ``push_error`` is carried; the marker becomes ``OWNED_PUSH_AVAILABLE`` with a
    PUSH FAILED suffix and the fire-or-close directive fills the empty slot."""
    agent = make_agent()
    agent._vcs.push.side_effect = VCSError("remote rejected")
    note = f"Resolved — {Marker.AWAITING_TEND_PUSH}."

    push_delivered, push_error, out_note = (
        apply_push_delivery_reconciliation(
            agent,
            branch_name=_PR_BRANCH,
            pr_branch=_PR_BRANCH,
            outcome="success",
            remediation_note=note,
            remediation_pr_num=700,
        )
    )

    assert push_delivered is False
    assert push_error is not None
    assert "remote rejected" in push_error
    assert agent._vcs.push.call_count == 2
    assert Marker.OWNED_PUSH_AVAILABLE in out_note
    assert f"PUSH FAILED: {push_error}" in out_note
    assert Marker.AWAITING_TEND_PUSH not in out_note
    model = agent.consciousness.load_self_model()
    assert model.next_task_directive is not None
    assert "#700" in model.next_task_directive
    assert _PR_BRANCH in model.next_task_directive


def test_fetch_failure_during_retry_still_retries_push(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Fetch-swallow arm: the pre-retry fetch itself raises; it logs and falls
    through so the retry push still runs and succeeds."""
    agent = make_agent()
    agent._vcs.push.side_effect = [VCSError("stale"), None]
    agent._vcs.fetch.side_effect = VCSError("network down")
    note = f"Resolved — {Marker.AWAITING_TEND_PUSH}."

    push_delivered, push_error, out_note = (
        apply_push_delivery_reconciliation(
            agent,
            branch_name=_PR_BRANCH,
            pr_branch=_PR_BRANCH,
            outcome="success",
            remediation_note=note,
            remediation_pr_num=700,
        )
    )

    assert push_delivered is True
    assert push_error is None
    agent._vcs.fetch.assert_called_once_with("origin")
    assert agent._vcs.push.call_count == 2
    assert Marker.DELIVERED_AWAITING_MERGE in out_note


def test_never_attempted_owned_ahead_appends_marker_and_fires_directive(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Never-attempted-OWNED_AHEAD arm: the push is never attempted (branch
    mismatch) but git proves the branch is ahead of origin, so the reachability
    probe resolves ``OWNED_PUSH_AVAILABLE``; since the note carries no
    ``AWAITING_TEND_PUSH`` to swap, the owned marker is *appended* and the
    directive fires into the empty slot — with no PUSH FAILED suffix."""
    agent = make_agent()
    agent._fetch_origin_for_merge_truth = MagicMock()  # type: ignore[method-assign]
    agent._vcs.commits_not_on_remote.return_value = 2  # OWNED_AHEAD
    note = "Resolved partially — completeness-gate note without a marker."

    push_delivered, push_error, out_note = (
        apply_push_delivery_reconciliation(
            agent,
            branch_name="maestro/task/other",
            pr_branch=_PR_BRANCH,
            outcome="success",
            remediation_note=note,
            remediation_pr_num=700,
        )
    )

    assert push_delivered is None
    assert push_error is None
    agent._vcs.push.assert_not_called()
    assert out_note.startswith(note)
    assert Marker.OWNED_PUSH_AVAILABLE in out_note
    assert "PUSH FAILED" not in out_note
    model = agent.consciousness.load_self_model()
    assert model.next_task_directive is not None
    assert "#700" in model.next_task_directive


def test_owned_push_leaves_occupied_directive_slot_untouched(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Occupied-slot arm: on a failed owned push, a pre-set
    ``next_task_directive`` (a higher-precedence bridge that already spoke) is
    preserved — the fire-or-close directive fills only an empty slot."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.next_task_directive = "PRE-SET: higher-precedence bridge already spoke"
    agent.consciousness.save_self_model(model)
    agent._vcs.push.side_effect = VCSError("remote rejected")
    note = f"Resolved — {Marker.AWAITING_TEND_PUSH}."

    push_delivered, _push_error, out_note = (
        apply_push_delivery_reconciliation(
            agent,
            branch_name=_PR_BRANCH,
            pr_branch=_PR_BRANCH,
            outcome="success",
            remediation_note=note,
            remediation_pr_num=700,
        )
    )

    assert push_delivered is False
    assert Marker.OWNED_PUSH_AVAILABLE in out_note
    reloaded = agent.consciousness.load_self_model()
    assert (
        reloaded.next_task_directive
        == "PRE-SET: higher-precedence bridge already spoke"
    )


def test_owned_push_without_pr_number_skips_directive(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """No-PR-number arm: a failed owned push with ``remediation_pr_num=None``
    still records ``OWNED_PUSH_AVAILABLE`` (marker + PUSH FAILED suffix) and logs
    loudly, but the ``if remediation_pr_num is not None and pr_branch:`` guard's
    false branch fires no fire-or-close directive — the empty slot stays empty."""
    agent = make_agent()
    agent._vcs.push.side_effect = VCSError("remote rejected")
    note = f"Resolved — {Marker.AWAITING_TEND_PUSH}."

    push_delivered, push_error, out_note = (
        apply_push_delivery_reconciliation(
            agent,
            branch_name=_PR_BRANCH,
            pr_branch=_PR_BRANCH,
            outcome="success",
            remediation_note=note,
            remediation_pr_num=None,
        )
    )

    assert push_delivered is False
    assert push_error is not None
    assert Marker.OWNED_PUSH_AVAILABLE in out_note
    assert f"PUSH FAILED: {push_error}" in out_note
    assert not agent.consciousness.load_self_model().next_task_directive


def test_none_remediation_note_skips_reconciliation(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """No-note delivered arm: the backstop push runs on a deliverable outcome
    and lands, so a note-less lap has nothing to reconcile — the return note
    stays None and no directive is fired."""
    agent = make_agent()

    push_delivered, push_error, out_note = (
        apply_push_delivery_reconciliation(
            agent,
            branch_name=_PR_BRANCH,
            pr_branch=_PR_BRANCH,
            outcome="success",
            remediation_note=None,
            remediation_pr_num=700,
        )
    )

    assert push_delivered is True
    assert push_error is None
    assert out_note is None
    agent._vcs.push.assert_called_once_with(_PR_BRANCH)
    assert not agent.consciousness.load_self_model().next_task_directive


def _settled(agent, settled: bool) -> MagicMock:  # type: ignore[no-untyped-def]
    reader = MagicMock(return_value=MagicMock(settled=settled))
    agent.is_pr_settled = reader
    return reader


def test_none_note_failed_push_mints_owned_marker_and_directive(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """#4161: a note-less remediation lap (no §4c/§4d note) whose push fails
    both attempts on an unsettled PR still records the owned marker, the PUSH
    FAILED suffix, and the fire-or-close directive."""
    agent = make_agent()
    agent._vcs.push.side_effect = VCSError("remote rejected")
    reader = _settled(agent, False)

    push_delivered, push_error, out_note = (
        apply_push_delivery_reconciliation(
            agent,
            branch_name=_PR_BRANCH,
            pr_branch=_PR_BRANCH,
            outcome="partial",
            remediation_note=None,
            remediation_pr_num=700,
        )
    )

    assert push_delivered is False
    assert push_error == "remote rejected"
    assert out_note is not None
    assert out_note.startswith("Resolved partially — ")
    assert Marker.OWNED_PUSH_AVAILABLE in out_note
    assert "PUSH FAILED: remote rejected" in out_note
    reader.assert_called_once_with(700)
    expected = _OWNED_PUSH_FIRE_OR_CLOSE_DIRECTIVE.format(
        pr=700, branch=_PR_BRANCH,
    )
    assert agent.consciousness.load_self_model().next_task_directive == expected


def test_none_note_never_attempted_owned_ahead_mints_owned_marker(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """#4161: a note-less remediation lap whose push was never attempted (a
    non-deliverable outcome) on a branch git proves ahead of origin records the
    owned marker — without a PUSH FAILED suffix."""
    agent = make_agent()
    agent._branch_ahead_of_origin_count = MagicMock(return_value=2)  # type: ignore[method-assign]
    _settled(agent, False)

    push_delivered, push_error, out_note = (
        apply_push_delivery_reconciliation(
            agent,
            branch_name="maestro/task/fallback",
            pr_branch=_PR_BRANCH,
            outcome="partial",
            remediation_note=None,
            remediation_pr_num=700,
        )
    )

    assert push_delivered is None
    assert push_error is None
    agent._vcs.push.assert_not_called()
    agent._branch_ahead_of_origin_count.assert_called_once_with(_PR_BRANCH)
    assert out_note is not None
    assert Marker.OWNED_PUSH_AVAILABLE in out_note
    assert "PUSH FAILED" not in out_note


def test_none_note_never_attempted_nothing_local_stays_none(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A note-less never-attempted lap with nothing local to push resolves a
    non-owned marker, so no note is minted and merge truth is never read."""
    agent = make_agent()
    agent._branch_ahead_of_origin_count = MagicMock(return_value=0)  # type: ignore[method-assign]
    reader = _settled(agent, False)

    _pd, _pe, out_note = apply_push_delivery_reconciliation(
        agent,
        branch_name="maestro/task/fallback",
        pr_branch=_PR_BRANCH,
        outcome="partial",
        remediation_note=None,
        remediation_pr_num=700,
    )

    assert out_note is None
    reader.assert_not_called()
    assert not agent.consciousness.load_self_model().next_task_directive


def test_none_note_settled_pr_stays_none(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A failed push on a PR that merge truth reads as settled must not receive
    a false owned stamp: the note stays None and no directive fires."""
    agent = make_agent()
    agent._vcs.push.side_effect = VCSError("remote rejected")
    _settled(agent, True)

    push_delivered, _pe, out_note = apply_push_delivery_reconciliation(
        agent,
        branch_name=_PR_BRANCH,
        pr_branch=_PR_BRANCH,
        outcome="partial",
        remediation_note=None,
        remediation_pr_num=700,
    )

    assert push_delivered is False
    assert out_note is None
    assert not agent.consciousness.load_self_model().next_task_directive


def test_none_note_non_remediation_lap_stays_none(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An ordinary (non-remediation) lap — ``remediation_pr_num`` None — is
    untouched even when its push fails: no note, no merge-truth read."""
    agent = make_agent()
    agent._vcs.push.side_effect = VCSError("remote rejected")
    reader = _settled(agent, False)

    push_delivered, _pe, out_note = apply_push_delivery_reconciliation(
        agent,
        branch_name=_PR_BRANCH,
        pr_branch=_PR_BRANCH,
        outcome="partial",
        remediation_note=None,
        remediation_pr_num=None,
    )

    assert push_delivered is False
    assert out_note is None
    reader.assert_not_called()
    assert not agent.consciousness.load_self_model().next_task_directive


def test_none_note_failed_outcome_stays_none(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A note-less ``failed`` lap deliberately skips the push, so even on an
    ahead branch it must not mint a "Resolved partially" note or a
    fire-or-close directive — nor pay the ahead probe or merge-truth read."""
    agent = make_agent()
    agent._branch_ahead_of_origin_count = MagicMock(return_value=2)  # type: ignore[method-assign]
    reader = _settled(agent, False)

    push_delivered, _pe, out_note = apply_push_delivery_reconciliation(
        agent,
        branch_name=_PR_BRANCH,
        pr_branch=_PR_BRANCH,
        outcome="failed",
        remediation_note=None,
        remediation_pr_num=700,
    )

    assert push_delivered is None
    assert out_note is None
    agent._vcs.push.assert_not_called()
    agent._branch_ahead_of_origin_count.assert_not_called()
    reader.assert_not_called()
    assert not agent.consciousness.load_self_model().next_task_directive
