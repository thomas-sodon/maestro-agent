"""Ahead-of-origin push actuator — the guard pushes, it no longer only re-labels.

The chosen-stand-down ahead-of-origin guard historically *detected* an owned
branch ahead of origin (un-pushed commits by Maestro's own hand) and merely
re-labeled the verdict ``[owned-push-available]`` plus a ``Finish PR #N``
directive routed to a future LLM turn — which narrated "awaiting" again. These
tests pin the converted behavior: the detector is now an *actuator* that
mechanically runs ``git push`` BEFORE any verdict string is composed, gated
through the owned-branches oracle so a non-owned branch is never pushed. Every
external is mocked (``mem_fs``, subprocess via ``_vcs``, ``is_pr_settled``
patched), so nothing shells out.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from maestro.loop.pr_remediation import _OwnedPushResult
from maestro.memory import TaskRecord
from maestro.pr_facts import MergeTruth
from maestro.pr_remediation_state import Marker
from maestro.remediation_tracker import MERGE_BLOCK_AWAITING_PUSH, find_stand_down
from maestro.vcs import VCSError


def _stood_down_record(pr_num: int) -> TaskRecord:
    """A chosen ``[stood-down]`` remediation lap for PR #pr_num (no commit)."""
    return TaskRecord(
        task_id=f"sd{pr_num}",
        goal=f"Remediate PR #{pr_num}: address comments",
        plan=[],
        outcome="partial",
        result_summary=f"[stood-down] awaiting external approval on PR #{pr_num}.",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-07-18T00:00:00+00:00",
        source="external",
        key_findings="[stood-down] chose silence",
    )


def _merge_truth(pr_num: int, *, settled: bool) -> MergeTruth:
    """A provenance-stamped merge-state fact for the re-derivation seam."""
    return MergeTruth(
        pr_number=pr_num,
        settled=settled,
        state="MERGED" if settled else "OPEN",
        branch_landed=settled,
        source="host_api",
        observed_at=0.0,
    )


def _arm_owned_ahead_guard(agent: object, pr_num: int, branch: str) -> None:
    """Configure *agent* so the OWNED_AHEAD + external-blocker guard fires.

    The other two chosen-stand-down vetoes (user-request, salvage) are disabled
    so the ahead-of-origin guard is the sole decision, and the fetch-first
    rev-list count reports the branch ahead of origin.
    """
    agent.config.user_request_blocks_standdown_enabled = False  # type: ignore[attr-defined]
    agent.config.salvage_before_disengage_enabled = False  # type: ignore[attr-defined]
    agent._fetch_origin_for_merge_truth = MagicMock()  # type: ignore[attr-defined]
    agent._vcs.commits_not_on_remote.return_value = 3  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_owned_ahead_pushes_before_composing_verdict(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """AC1: an OWNED_AHEAD classification invokes ``_push_owned_branch`` BEFORE
    any verdict string is composed. The push side effect asserts the record's
    summary is still the untouched stood-down text at push time; after the call
    the delivered marker is present — proving the push preceded verdict
    composition."""
    agent = make_agent()
    branch = "maestro/evolve/ep300"
    _arm_owned_ahead_guard(agent, 300, branch)
    record = _stood_down_record(300)
    original_summary = record.result_summary

    def _assert_verdict_not_yet_composed(_branch: str) -> _OwnedPushResult:
        # No verdict marker has been stamped onto the record yet.
        assert record.result_summary == original_summary
        assert Marker.OWNED_PUSH_AVAILABLE not in (record.result_summary or "")
        assert Marker.DELIVERED_AWAITING_MERGE not in (record.result_summary or "")
        return _OwnedPushResult(pushed=True)

    agent._push_owned_branch = MagicMock(  # type: ignore[method-assign]
        side_effect=_assert_verdict_not_yet_composed,
    )
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_merge_truth(300, settled=False),
    )

    await agent._record_remediation_outcome(
        goal="Remediate PR #300: address comments",
        pr_branch=branch,
        record=record,
        head_before="abc",
    )

    agent._push_owned_branch.assert_called_once_with(branch)
    # The verdict was composed only after the push returned.
    assert Marker.DELIVERED_AWAITING_MERGE in record.result_summary


@pytest.mark.asyncio
async def test_successful_push_reobserves_and_drops_awaiting_label(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """AC2: after a successful mechanical push, ``is_pr_settled`` is re-derived
    fetch-first (the per-iteration memo is dropped) and the resulting verdict is
    the honest delivered label — never the ``[owned-push-available]`` / awaiting
    costume. No fire-or-close directive and no stand-down verdict are written."""
    agent = make_agent()
    branch = "maestro/evolve/ep301"
    _arm_owned_ahead_guard(agent, 301, branch)
    # Seed a stale settled memo; the guard must drop it so the re-read re-fetches.
    agent._merge_truth_memo[301] = _merge_truth(301, settled=True)
    agent._push_owned_branch = MagicMock(  # type: ignore[method-assign]
        return_value=_OwnedPushResult(pushed=True),
    )
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_merge_truth(301, settled=False),
    )
    record = _stood_down_record(301)

    await agent._record_remediation_outcome(
        goal="Remediate PR #301: address comments",
        pr_branch=branch,
        record=record,
        head_before="abc",
    )

    # Fetch-first re-derivation: the stale memo was dropped and is_pr_settled
    # re-observed for this PR.
    assert 301 not in agent._merge_truth_memo
    agent.is_pr_settled.assert_any_call(301)
    # The verdict is the honest delivered label, not the awaiting costume.
    assert Marker.OWNED_PUSH_AVAILABLE not in record.result_summary
    assert Marker.OWNED_PUSH_AVAILABLE not in (record.key_findings or "")
    assert Marker.DELIVERED_AWAITING_MERGE in record.result_summary
    assert record.outcome == "partial"
    # No fire-or-close directive — the fire already happened.
    model = agent.consciousness.load_self_model()
    assert not model.next_task_directive
    # No stand-down verdict was minted.
    assert str(agent.config.memory_dir / "stand_down_verdicts.jsonl") not in mem_fs


@pytest.mark.asyncio
async def test_successful_push_settled_head_stamps_landed(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """AC2 companion: a successful push whose re-derived head git shows settled
    coerces to the landed marker, never the awaiting costume."""
    agent = make_agent()
    branch = "maestro/evolve/ep302"
    _arm_owned_ahead_guard(agent, 302, branch)
    agent._push_owned_branch = MagicMock(  # type: ignore[method-assign]
        return_value=_OwnedPushResult(pushed=True),
    )
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_merge_truth(302, settled=True),
    )
    record = _stood_down_record(302)

    await agent._record_remediation_outcome(
        goal="Remediate PR #302: address comments",
        pr_branch=branch,
        record=record,
        head_before="abc",
    )

    assert Marker.LANDED in record.result_summary
    assert Marker.OWNED_PUSH_AVAILABLE not in record.result_summary


@pytest.mark.asyncio
async def test_failed_push_retains_owned_push_partial_and_directive(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """AC3: a failed mechanical push retains the pre-existing loud
    ``[owned-push-available]`` partial plus a fire-or-close directive naming the
    branch — no stand-down verdict is minted."""
    agent = make_agent()
    branch = "maestro/evolve/ep303"
    _arm_owned_ahead_guard(agent, 303, branch)
    agent._push_owned_branch = MagicMock(  # type: ignore[method-assign]
        return_value=_OwnedPushResult(pushed=False, error="push rejected"),
    )
    record = _stood_down_record(303)

    await agent._record_remediation_outcome(
        goal="Remediate PR #303: address comments",
        pr_branch=branch,
        record=record,
        head_before="abc",
    )

    assert record.outcome == "partial"
    assert Marker.OWNED_PUSH_AVAILABLE in record.result_summary
    assert Marker.OWNED_PUSH_AVAILABLE in (record.key_findings or "")
    assert Marker.DELIVERED_AWAITING_MERGE not in record.result_summary
    model = agent.consciousness.load_self_model()
    assert model.next_task_directive is not None
    assert branch in model.next_task_directive
    assert "#303" in model.next_task_directive
    # No stand-down verdict was minted.
    assert find_stand_down(303, "0" * 40, agent.stand_down_verdicts) is None


@pytest.mark.asyncio
async def test_awaiting_push_blocker_still_actuates_when_owned_push_on(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """The activation delivers rather than defers (proposal 098).

    The chosen-stand-down guard reads ``classify_blocker_ownership`` *without*
    ``own_push`` on purpose: an ``AWAITING_PUSH`` blocker must keep classifying
    external here so it still satisfies the guard's external-blocker condition
    and routes into ``_push_owned_branch``. Threading ``own_push=True`` through
    would make the blocker owned, fail that condition, and silently divert the
    branch into a ``[stood-down]`` verdict — the exact quiet the activation
    removes.
    """
    agent = make_agent()
    branch = "maestro/evolve/ep304"
    _arm_owned_ahead_guard(agent, 304, branch)
    # The lap's merge-block probe named the unpushed on-branch resolution.
    agent._last_remediation_merge_block = MERGE_BLOCK_AWAITING_PUSH
    agent._push_owned_branch = MagicMock(  # type: ignore[method-assign]
        return_value=_OwnedPushResult(pushed=True),
    )
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_merge_truth(304, settled=False),
    )
    record = _stood_down_record(304)

    await agent._record_remediation_outcome(
        goal="Remediate PR #304: address comments",
        pr_branch=branch,
        record=record,
        head_before="abc",
    )

    # The actuator fired: the branch was pushed, not stood down.
    agent._push_owned_branch.assert_called_once_with(branch)
    assert Marker.DELIVERED_AWAITING_MERGE in record.result_summary
    assert Marker.OWNED_PUSH_AVAILABLE not in record.result_summary
    # No stand-down verdict was minted for the pushed branch.
    assert find_stand_down(304, "0" * 40, agent.stand_down_verdicts) is None


def test_push_owned_branch_refuses_non_owned_branch(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """AC4: ``_push_owned_branch`` never pushes a branch the owned-branches
    oracle rejects — no ``git push`` subprocess call occurs and the result is
    marked refused."""
    agent = make_agent()
    # Default fixture: no open evolution PRs and an empty ledger → nothing owned.
    agent.evolution._host.get_open_evolution_pr_states.return_value = {}

    result = agent._push_owned_branch("maestro/evolve/not-owned")

    assert result.pushed is False
    assert result.refused is True
    agent._vcs.push.assert_not_called()


def test_push_owned_branch_pushes_confirmed_owned_branch(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Positive complement of AC4: a ``maestro/evolve/*`` head confirmed by BOTH
    the live open-PR set and the persistent PR ledger is pushed exactly once."""
    agent = make_agent()
    branch = "maestro/evolve/ep400"
    agent.evolution._host.get_open_evolution_pr_states.return_value = {branch: object()}
    agent.pr_ledger.observe_head(400, branch, "deadbeef")

    result = agent._push_owned_branch(branch)

    assert result.pushed is True
    assert result.refused is False
    agent._vcs.push.assert_called_once_with(branch)


def test_record_owned_push_delivered_none_record_tolerated(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A ``None`` record is tolerated and no fire-or-close directive is written —
    unlike the failure path, the fire (push) already happened."""
    agent = make_agent()
    agent._record_owned_push_delivered(
        500, "maestro/evolve/ep500", None, _merge_truth(500, settled=False),
    )
    model = agent.consciousness.load_self_model()
    assert not model.next_task_directive


def test_record_owned_push_delivered_populates_empty_fields(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When the record's summary/findings are empty, the delivered note replaces
    them outright (the ``else`` arm of the append ternaries)."""
    agent = make_agent()
    record = TaskRecord(
        task_id="empty",
        goal="Remediate PR #501: address comments",
        plan=[],
        outcome="partial",
        result_summary="",
        errors=[],
        duration_secs=0.0,
        timestamp="2026-07-18T00:00:00+00:00",
        source="external",
        key_findings=None,
    )
    agent._record_owned_push_delivered(
        501, "maestro/evolve/ep501", record, _merge_truth(501, settled=False),
    )
    assert record.result_summary.startswith(str(Marker.DELIVERED_AWAITING_MERGE))
    assert (record.key_findings or "").startswith(
        str(Marker.DELIVERED_AWAITING_MERGE),
    )


def test_push_owned_branch_returns_failure_on_git_error(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A genuine ``git push`` failure on an owned branch is returned, not
    swallowed (fail-loud): ``pushed`` is False with the error carried."""
    agent = make_agent()
    branch = "maestro/evolve/ep401"
    agent.evolution._host.get_open_evolution_pr_states.return_value = {branch: object()}
    agent.pr_ledger.observe_head(401, branch, "deadbeef")
    agent._vcs.push.side_effect = RuntimeError("remote rejected")

    result = agent._push_owned_branch(branch)

    assert result.pushed is False
    assert result.refused is False
    assert result.error is not None
    assert "remote rejected" in result.error


# ---------------------------------------------------------------------------
# Wiring: the owned-branch push helper stamps 'pushed'/'delivered' from the
# verifier boolean, never the push subprocess's raw return; a no-op push cannot
# be stamped delivered and instead yields a ``[push-unconfirmed]`` partial that
# fills only an *empty* ``next_task_directive``. Relocated unchanged from the
# retired ``tests/test_owned_branch_oracle.py``.
# ---------------------------------------------------------------------------

_SHA_A = "a" * 40
_SHA_B = "b" * 40


def _own_branch(agent: object, pr_num: int, branch: str) -> None:
    """Make *branch* a confirmed owned-writable evolution head for *agent*."""
    agent.evolution._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        branch: object(),
    }
    agent.pr_ledger.observe_head(pr_num, branch, "deadbeef")  # type: ignore[attr-defined]


def test_push_helper_stamps_pushed_on_confirmed_advance(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(a) advanced head → pushed=True: the remote head read back equals the
    pushed local head SHA, so 'delivered' binds to the verifier boolean."""
    agent = make_agent()
    branch = "maestro/evolve/ep410"
    _own_branch(agent, 410, branch)
    agent._vcs.rev_parse.return_value = _SHA_A  # local head == origin head

    result = agent._push_owned_branch(branch)

    assert result.pushed is True
    assert result.unconfirmed is False
    agent._vcs.push.assert_called_once_with(branch)


def test_push_helper_noop_push_is_unconfirmed_not_delivered(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(b) unchanged head no-op push → unconfirmed=True: the push exits 0 but
    ``origin/<branch>`` did not advance to the pushed SHA, so 'pushed' (delivered)
    is never stamped — binding proves a no-op push cannot claim delivery."""
    agent = make_agent()
    branch = "maestro/evolve/ep411"
    _own_branch(agent, 411, branch)

    def _rev(ref: str) -> str:
        return _SHA_B if ref.startswith("origin/") else _SHA_A

    agent._vcs.rev_parse.side_effect = _rev

    result = agent._push_owned_branch(branch)

    assert result.pushed is False
    assert result.unconfirmed is True
    agent._vcs.push.assert_called_once_with(branch)


def test_push_helper_fetch_failure_is_unconfirmed(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(c) fetch failure → unconfirmed: a ``VCSError`` on the verifier's fetch
    seam maps to False, so the push is recorded unconfirmed, never delivered."""
    agent = make_agent()
    branch = "maestro/evolve/ep412"
    _own_branch(agent, 412, branch)
    agent._vcs.fetch.side_effect = VCSError("network down")

    result = agent._push_owned_branch(branch)

    assert result.pushed is False
    assert result.unconfirmed is True


def test_fetch_remote_branch_ok_true_on_clean_fetch(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The injected fetch seam returns True when the fetch does not raise."""
    agent = make_agent()
    assert agent._fetch_remote_branch_ok("maestro/evolve/ep413") is True


def test_fetch_remote_branch_ok_false_on_vcs_error(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The injected fetch seam returns False (never raises) on a ``VCSError``."""
    agent = make_agent()
    agent._vcs.fetch.side_effect = VCSError("boom")
    assert agent._fetch_remote_branch_ok("maestro/evolve/ep413") is False


def test_record_push_unconfirmed_stamps_partial_marker_and_fills_empty_slot(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(b/d) an unconfirmed push stamps a ``[push-unconfirmed]`` partial and fills
    the *empty* directive slot with a fire-or-close directive naming the PR."""
    agent = make_agent()
    record = TaskRecord(
        task_id="u1",
        goal="Remediate PR #420: address comments",
        plan=[],
        outcome="partial",
        result_summary="",
        errors=[],
        duration_secs=0.0,
        timestamp="2026-07-21T00:00:00+00:00",
        source="external",
        key_findings=None,
    )

    agent._record_push_unconfirmed(420, "maestro/evolve/ep420", record)

    assert record.outcome == "partial"
    assert record.result_summary.startswith(str(Marker.PUSH_UNCONFIRMED))
    assert (record.key_findings or "").startswith(str(Marker.PUSH_UNCONFIRMED))
    model = agent.consciousness.load_self_model()
    assert model.next_task_directive is not None
    assert "#420" in model.next_task_directive
    assert "maestro/evolve/ep420" in model.next_task_directive


def test_record_push_unconfirmed_does_not_overwrite_preset_directive(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(d) a pre-set ``next_task_directive`` is never overwritten by the
    unconfirmed-push handler — it fills only an empty slot."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.next_task_directive = "PRE-SET: do the important thing first"
    agent.consciousness.save_self_model(model)
    record = TaskRecord(
        task_id="u2",
        goal="Remediate PR #421: address comments",
        plan=[],
        outcome="partial",
        result_summary="prior text",
        errors=[],
        duration_secs=0.0,
        timestamp="2026-07-21T00:00:00+00:00",
        source="external",
        key_findings="prior findings",
    )

    agent._record_push_unconfirmed(421, "maestro/evolve/ep421", record)

    # The pre-set directive survives; the note appends to non-empty fields.
    reloaded = agent.consciousness.load_self_model()
    assert reloaded.next_task_directive == "PRE-SET: do the important thing first"
    assert record.result_summary.startswith("prior text ")
    assert str(Marker.PUSH_UNCONFIRMED) in record.result_summary
    assert str(Marker.PUSH_UNCONFIRMED) in (record.key_findings or "")


def test_record_push_unconfirmed_tolerates_none_record(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A ``None`` record is tolerated; the empty directive slot is still filled."""
    agent = make_agent()
    agent._record_push_unconfirmed(422, "maestro/evolve/ep422", None)
    model = agent.consciousness.load_self_model()
    assert model.next_task_directive is not None
    assert "#422" in model.next_task_directive


@pytest.mark.asyncio
async def test_unconfirmed_push_routes_to_push_unconfirmed_handler(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The ``_record_remediation_outcome`` guard routes an unconfirmed push into
    the ``[push-unconfirmed]`` handler — not the delivered stamp, nor the
    owned-push-available refusal — proving the caller branch on ``unconfirmed``."""
    agent = make_agent()
    branch = "maestro/evolve/ep423"
    agent.config.user_request_blocks_standdown_enabled = False
    agent.config.salvage_before_disengage_enabled = False
    agent._fetch_origin_for_merge_truth = MagicMock()
    agent._vcs.commits_not_on_remote.return_value = 3
    agent._push_owned_branch = MagicMock(
        return_value=_OwnedPushResult(pushed=False, unconfirmed=True),
    )
    record = TaskRecord(
        task_id="sd423",
        goal="Remediate PR #423: address comments",
        plan=[],
        outcome="partial",
        result_summary="[stood-down] awaiting external approval on PR #423.",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-07-21T00:00:00+00:00",
        source="external",
        key_findings="[stood-down] chose silence",
    )

    await agent._record_remediation_outcome(
        goal="Remediate PR #423: address comments",
        pr_branch=branch,
        record=record,
        head_before="abc",
    )

    agent._push_owned_branch.assert_called_once_with(branch)
    assert Marker.PUSH_UNCONFIRMED in record.result_summary
    assert Marker.DELIVERED_AWAITING_MERGE not in record.result_summary
    assert Marker.OWNED_PUSH_AVAILABLE not in record.result_summary
    assert record.outcome == "partial"
