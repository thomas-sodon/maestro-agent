"""Verdict-agnostic costume-signature gate tests.

These exercise ``_gate_pr_remediation_branch_resolved`` (and its pure counter
``count_branch_resolved_partials``) for the specific failure the gate exists to
close: a PR re-lapped as ``partial`` across a *shifting* Tend-clearable blocker
label (CONFLICTING → AWAITING_PUSH → BEHIND_MAIN). Earlier, the count keyed on a
single on-branch wording under-counted when the label changed between laps, so
the wheel kept turning. The gate now accumulates every undelivered signature
by ``#NNNN`` lineage, so verdict-shifting cannot dodge it.

Distinct from ``tests/test_agent_pr_gates.py``, which covers the single-label
(``awaiting tend push``) path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.loop.pr_remediation import (
    _BUG_COMMENTS_PROBLEM_MARKER,
    _HUMAN_COMMENTS_PROBLEM_MARKER,
    _NON_BUG_COMMENTS_PROBLEM_MARKER,
    _QUALITY_GATES_PROBLEM_MARKER,
    _REVIEW_FEEDBACK_PROBLEM_MARKER,
)
from maestro.memory import TaskRecord
from maestro.pr_remediation_state import PrRemediationState
from maestro.remediation_tracker import (
    _UNDELIVERED_PARTIAL_MARKERS,
    MERGE_BLOCK_AWAITING_PUSH,
    MERGE_BLOCK_BEHIND_MAIN,
    MERGE_BLOCK_CONFLICTING,
    MERGE_BLOCK_NONE,
    count_branch_resolved_partials,
    find_stand_down,
    record_stand_down,
)

# Realistic ``result_summary`` strings for each Tend-clearable blocker label —
# the exact wording each lap records in production: the on-branch resolution
# (CONFLICTING resolved on-branch), and the two mechanical ``[merge-block]``
# skips (AWAITING_PUSH / BEHIND_MAIN).
_AWAITING_PUSH_RESOLVED = (
    "Resolved on branch, awaiting tend push — PR #{n} not yet landed "
    "(merge blocker: CONFLICTING)."
)
_MERGE_BLOCK_AWAITING_PUSH = (
    "[merge-block] PR #{n} remediation skipped at zero SDK cost on branch "
    "maestro/evolve/ep{n} — blocker AWAITING_PUSH is cleared by Tend PR, "
    "not the LLM"
)
_MERGE_BLOCK_BEHIND_MAIN = (
    "[merge-block] PR #{n} remediation skipped at zero SDK cost on branch "
    "maestro/evolve/ep{n} — blocker BEHIND_MAIN is cleared by Tend PR, "
    "not the LLM"
)


def _lap(
    pr_number: int,
    summary: str,
    idx: int,
    *,
    outcome: str = "partial",
    pr_head_sha: str | None = None,
) -> TaskRecord:
    """Build one ``Remediate PR #<n>`` history lap with the given *summary*."""
    return TaskRecord(
        task_id=f"task{pr_number}-{idx}",
        goal=f"Remediate PR #{pr_number}: fix CI",
        plan=[],
        outcome=outcome,
        result_summary=summary.format(n=pr_number),
        errors=[],
        duration_secs=0.0,
        timestamp=datetime.now(UTC).isoformat(),
        source="external",
        key_findings=summary.format(n=pr_number),
        pr_head_sha=pr_head_sha,
    )


def _seed_mixed_label(agent, pr_number: int) -> None:  # type: ignore[no-untyped-def]
    """Seed two partials for *pr_number* with DIFFERENT blocker labels.

    One on-branch ``awaiting tend push`` lap and one ``[merge-block]``
    BEHIND_MAIN skip — exactly the verdict-shift the old single-label counter
    would have under-counted to 1 (below the default threshold of 2).
    """
    agent.memory.record_task(_lap(pr_number, _AWAITING_PUSH_RESOLVED, 0))
    agent.memory.record_task(_lap(pr_number, _MERGE_BLOCK_BEHIND_MAIN, 1))


class TestCounterVerdictAgnostic:
    """``count_branch_resolved_partials`` counts every undelivered label."""

    def test_merge_block_skip_lap_counts(self) -> None:
        # A ``[merge-block]`` AWAITING_PUSH / BEHIND_MAIN skip is a non-event
        # the same as an on-branch resolution — it must count.
        tasks = [
            {
                "goal": "Remediate PR #2538: fix CI",
                "outcome": "partial",
                "result_summary": _MERGE_BLOCK_BEHIND_MAIN.format(n=2538),
            },
        ]
        assert count_branch_resolved_partials(tasks, 2538) == 1

    def test_mixed_labels_accumulate_across_lineage(self) -> None:
        # CONFLICTING-resolved + AWAITING_PUSH skip + BEHIND_MAIN skip — three
        # different labels, all the same PR, all counted.
        tasks = [
            {
                "goal": "Remediate PR #2538: fix CI",
                "outcome": "partial",
                "result_summary": _AWAITING_PUSH_RESOLVED.format(n=2538),
            },
            {
                "goal": "Remediate PR #2538: fix CI",
                "outcome": "partial",
                "result_summary": _MERGE_BLOCK_AWAITING_PUSH.format(n=2538),
            },
            {
                "goal": "Remediate PR #2538: fix CI",
                "outcome": "partial",
                "result_summary": _MERGE_BLOCK_BEHIND_MAIN.format(n=2538),
            },
        ]
        assert count_branch_resolved_partials(tasks, 2538) == 3

    def test_owned_push_available_lap_counts(self) -> None:
        # Proposal 098: an AWAITING_PUSH lineage is owned, so it stops accruing
        # the two Tend-clearable markers — the merge-block gate declines to skip,
        # so no ``[merge-block]`` stamp, and the preamble steers off the
        # ``awaiting tend push`` wording. A lap whose own push failed stamps
        # ``[owned-push-available]``; it must count, or the cheap costume brake
        # never latches for that lineage and a failing push burns full SDK laps
        # to the escalation-bridge budget.
        tasks = [
            {
                "goal": "Remediate PR #2538: fix CI",
                "outcome": "partial",
                "result_summary": (
                    "[owned-push-available] PR #2538 branch is ahead of "
                    "origin; push failed."
                ),
            },
        ]
        assert count_branch_resolved_partials(tasks, 2538) == 1

    def test_delivered_lap_is_not_counted(self) -> None:
        # The affirmative complement: a lap that actually pushed stamps
        # ``DELIVERED_AWAITING_MERGE`` — delivery succeeded, so it is not a
        # costume and must not feed the brake.
        tasks = [
            {
                "goal": "Remediate PR #2538: fix CI",
                "outcome": "partial",
                "result_summary": (
                    "Delivered to PR, awaiting review/merge — PR #2538."
                ),
            },
        ]
        assert count_branch_resolved_partials(tasks, 2538) == 0

    def test_honest_partial_without_signature_excluded(self) -> None:
        # A genuine partial carrying no undelivered signature is an honest
        # miss, not a costume — it is not counted, so a first real attempt is
        # never pre-empted.
        tasks = [
            {
                "goal": "Remediate PR #2538: fix CI",
                "outcome": "partial",
                "result_summary": "Made progress on the failing test; not done.",
            },
        ]
        assert count_branch_resolved_partials(tasks, 2538) == 0


class TestBranchResolvedGateVerdictAgnostic:
    """The gate fires on mixed-label laps and skips a first-time conflict."""

    @pytest.mark.asyncio
    async def test_mixed_label_partials_trip_gate_at_threshold(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        # (a) Two partials with DIFFERENT blocker labels (one awaiting-push,
        # one merge-block BEHIND_MAIN) reach the default threshold of 2 and trip
        # the gate — the verdict shift no longer dodges the count.
        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
        _seed_mixed_label(agent, 2538)

        with patch("maestro.agent.query_on_session") as query:
            gated = await agent._gate_pr_remediation_branch_resolved(
                "Remediate PR #2538: fix CI", "maestro/evolve/ep2538",
                "external",
            )

        assert gated is True
        # No SDK session opened — zero-cost skip.
        query.assert_not_called()
        skip = agent.memory.get_recent_tasks(10)[-1]
        assert skip.outcome == "partial"
        assert skip.duration_secs == 0.0
        assert "[branch-resolved]" in (skip.key_findings or "")

    @pytest.mark.asyncio
    async def test_first_time_conflicting_lap_not_skipped(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        # (b) A PR surfacing for the FIRST time (no prior partials) must route to
        # remediation — a blanket CONFLICTING skip would strand real content
        # conflicts, the over-reach this plan explicitly rejects.
        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]

        gated = await agent._gate_pr_remediation_branch_resolved(
            "Remediate PR #2538: resolve merge conflict",
            "maestro/evolve/ep2538", "external",
        )

        assert gated is False
        # Nothing recorded, nothing escalated — the normal path proceeds.
        agent._notify_best_effort.assert_not_awaited()
        assert agent.memory.get_recent_tasks(10) == []

    @pytest.mark.asyncio
    async def test_escalation_recorded_exactly_once(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        # (c) When the gate fires on mixed-label laps it escalates exactly once
        # and records exactly one ``[branch-resolved]`` no-op.
        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
        _seed_mixed_label(agent, 2538)

        gated = await agent._gate_pr_remediation_branch_resolved(
            "Remediate PR #2538: fix CI", "maestro/evolve/ep2538", "external",
        )

        assert gated is True
        agent._notify_best_effort.assert_awaited_once()
        message = agent._notify_best_effort.await_args.args[0]
        assert "PR #2538" in message
        # Exactly one new branch-resolved skip record was appended (2 seeded
        # laps + 1 skip == 3 total).
        history = agent.memory.get_recent_tasks(10)
        branch_resolved = [
            t for t in history if "[branch-resolved]" in (t.key_findings or "")
        ]
        assert len(branch_resolved) == 1


class TestMergeBlockGateRecordsPartial:
    """``_gate_pr_remediation_merge_block`` stamps ``partial`` — never ``success``.

    git-merge-truth shows a BEHIND_MAIN PR unlanded, so the ``[merge-block]``
    zero-cost skip must record ``partial`` (which escalates), never a false
    ``success`` that ``measure_false_success_recurrence`` counts and that
    silently drains the unlanded-remediation sensor. The agent-side convergence
    already landed (``outcome="partial"``); this test locks the contract for the
    sole Tend-clearable verdict (BEHIND_MAIN — AWAITING_PUSH is owned, proposal
    098) so no future edit can re-introduce a ``success``-stamped drain from this
    gate path.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("reason", "vcs_setup"),
        [
            # BEHIND_MAIN: branch trails origin/main → commits_behind > 0. The
            # sole Tend-clearable blocker (AWAITING_PUSH is owned, proposal 098).
            (
                MERGE_BLOCK_BEHIND_MAIN,
                lambda vcs: setattr(
                    vcs.commits_ahead_behind, "return_value", (0, 4),
                ),
            ),
        ],
    )
    async def test_merge_block_skip_records_partial_for_all_tend_clearable_blockers(
        self, make_agent, reason, vcs_setup,  # type: ignore[no-untyped-def]
    ) -> None:
        # For every Tend-clearable verdict the gate fires zero-cost, records
        # ``partial`` (never ``success``), and escalates the named blocker once.
        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
        vcs_setup(agent._vcs)

        with patch("maestro.agent.query_on_session") as query:
            gated = await agent._gate_pr_remediation_merge_block(
                "Remediate PR #2538: fix CI", "maestro/evolve/ep2538",
                "external",
            )

        assert gated is True
        # Zero SDK cost: no session opened.
        query.assert_not_called()
        tasks = agent.memory.get_recent_tasks(5)
        assert len(tasks) == 1
        skip = tasks[0]
        # The honest stamp: the PR is unlanded, so the skip is ``partial`` —
        # and ``success`` is unreachable from this gate path.
        assert skip.outcome == "partial"
        assert skip.outcome != "success"
        assert skip.duration_secs == 0.0
        assert "[merge-block]" in (skip.key_findings or "")
        assert reason in (skip.result_summary or "")
        # The escalation side-effect fires exactly once, naming the blocker.
        agent._notify_best_effort.assert_awaited_once()
        message = agent._notify_best_effort.await_args.args[0]
        assert "PR #2538" in message
        assert reason in message
        # The skip is counted in the separate merge-block class, NOT the
        # ``should_reroute`` outcome list, so it never exiles the PR's
        # still-unattempted comment/overflow work (#2796).
        assert agent.remediation_tracker.merge_block_skip_count(2538) == 1
        assert "2538" not in agent.remediation_tracker._data

    @pytest.mark.asyncio
    async def test_awaiting_push_stays_runnable(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        """AWAITING_PUSH is owned (proposal 098), so it is not in the
        Tend-clearable set and the gate declines to skip — no zero-cost no-op, no
        escalation. BEHIND_MAIN is unaffected (it stays Tend's mechanical catch-up
        merge), which the sibling case below pins."""
        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
        # origin/<branch> ref absent → the probe refines to AWAITING_PUSH.
        agent._vcs.rev_parse.return_value = None

        gated = await agent._gate_pr_remediation_merge_block(
            "Remediate PR #2538: fix CI", "maestro/evolve/ep2538", "external",
        )

        assert gated is False
        assert agent.memory.get_recent_tasks(5) == []
        agent._notify_best_effort.assert_not_awaited()
        assert agent.remediation_tracker.merge_block_skip_count(2538) == 0

    @pytest.mark.asyncio
    async def test_still_skips_behind_main(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        """Only AWAITING_PUSH is owned. A behind-but-clean branch is still Tend's
        mechanical catch-up merge, so the gate keeps skipping it."""
        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
        agent._vcs.commits_ahead_behind.return_value = (0, 4)

        gated = await agent._gate_pr_remediation_merge_block(
            "Remediate PR #2538: fix CI", "maestro/evolve/ep2538", "external",
        )

        assert gated is True
        assert agent.remediation_tracker.merge_block_skip_count(2538) == 1


class TestMergeBlockGateExemptsCommentGoals:
    """Comment/review-driven goals must NOT be merge-block-skipped (#2738).

    Applying or declining an AI bug/suggestion/nit finding, answering a human
    thread, and posting the ``<!-- maestro:overflow-ack -->`` acknowledgement
    are LLM-only work that Tend PR's mechanical forward-merge never does. Because
    CLAUDE.md is touched by nearly every merge, an open PR branch is almost
    always BEHIND_MAIN — so without this exemption a routed comment/overflow
    finding was dequeued as a zero-cost ``partial`` no-op, the ack was never
    posted, and Tend re-routed the same finding every pass (a silent livelock).
    The gate now mirrors ``_is_pure_quality_gates_directive``: any
    reviewer/comment marker in the goal yields to the normal remediation path.
    """

    _PR = 2736
    _BRANCH = "maestro/evolve/ep2736"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "goal",
        [
            pytest.param(
                f"Remediate PR #2736: PR #2736 has 1 "
                f"{_NON_BUG_COMMENTS_PROBLEM_MARKER}suggestion/nit comment(s) "
                "flagged by the code reviewer.",
                id="non-bug-overflow",
            ),
            pytest.param(
                f"Remediate PR #2736: PR #2736 has 1 "
                f"{_BUG_COMMENTS_PROBLEM_MARKER}comment(s) flagged by the code "
                "reviewer.",
                id="bug",
            ),
            pytest.param(
                f"Remediate PR #2736: PR #2736 has 1 "
                f"{_HUMAN_COMMENTS_PROBLEM_MARKER}thread(s) to address.",
                id="human",
            ),
            pytest.param(
                f"Remediate PR #2736: {_REVIEW_FEEDBACK_PROBLEM_MARKER}2736. "
                "Address each blocking thread.",
                id="review-feedback",
            ),
        ],
    )
    async def test_comment_goal_not_skipped_while_behind_main(
        self, make_agent, goal,  # type: ignore[no-untyped-def]
    ) -> None:
        # Even with the probe configured to report the Tend-clearable
        # BEHIND_MAIN verdict, a goal carrying any reviewer/comment marker must
        # yield to the normal remediation path — and never even consult the
        # probe (the exemption sits before it).
        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
        agent._probe_pr_merge_block = AsyncMock(  # type: ignore[method-assign]
            return_value=(False, MERGE_BLOCK_BEHIND_MAIN),
        )

        with patch("maestro.agent.query_on_session") as query:
            gated = await agent._gate_pr_remediation_merge_block(
                goal, self._BRANCH, "external",
            )

        assert gated is False
        # The marker check short-circuits before the git probe.
        agent._probe_pr_merge_block.assert_not_awaited()
        query.assert_not_called()
        # No zero-cost ``partial`` skip is recorded: no TaskRecord, no
        # escalation, no no-op count for the PR.
        assert len(agent.memory.get_recent_tasks(5)) == 0
        agent._notify_best_effort.assert_not_awaited()
        assert str(self._PR) not in agent.remediation_tracker._data

    @pytest.mark.asyncio
    async def test_mixed_quality_gate_and_comment_goal_not_skipped(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        # A goal aggregating a quality-gates problem AND a comment finding must
        # still proceed — skipping it would silently drop the LLM-only comment
        # work, matching ``_is_pure_quality_gates_directive``'s "any reviewer
        # marker ⇒ not pure" contract.
        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
        agent._probe_pr_merge_block = AsyncMock(  # type: ignore[method-assign]
            return_value=(False, MERGE_BLOCK_BEHIND_MAIN),
        )
        goal = (
            f"Remediate PR #2736: After merging origin/main into PR #2736"
            f"{_QUALITY_GATES_PROBLEM_MARKER} ruff. Also PR #2736 has 1 "
            f"{_NON_BUG_COMMENTS_PROBLEM_MARKER}suggestion/nit comment(s)."
        )

        gated = await agent._gate_pr_remediation_merge_block(
            goal, self._BRANCH, "external",
        )

        assert gated is False
        agent._probe_pr_merge_block.assert_not_awaited()
        assert len(agent.memory.get_recent_tasks(5)) == 0
        agent._notify_best_effort.assert_not_awaited()
        assert str(self._PR) not in agent.remediation_tracker._data


class TestBranchResolvedFindingWording:
    """Gate's ``finding`` must not embed any ``_UNDELIVERED_PARTIAL_MARKERS``.

    If the finding string contains a marker substring it would satisfy its own
    counting predicate on the next invocation, re-arming the self-latch (#2595).
    """

    @pytest.mark.asyncio
    async def test_finding_contains_no_tend_clearable_marker(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
        _seed_mixed_label(agent, 2564)

        await agent._gate_pr_remediation_branch_resolved(
            "Remediate PR #2564: fix CI", "maestro/evolve/ep2564", "external",
        )

        skip = agent.memory.get_recent_tasks(5)[-1]
        finding_lower = (skip.result_summary or "").lower()
        for marker in _UNDELIVERED_PARTIAL_MARKERS:
            assert marker not in finding_lower, (
                f"gate finding embeds marker {marker!r}; "
                f"self-latch re-arms on next invocation"
            )


class TestBranchResolvedGateIntegration:
    """Progress-aware gate: delivered laps discounted; undelivered laps count."""

    @pytest.mark.asyncio
    async def test_one_delivered_one_undelivered_at_threshold_does_not_fire(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        # Threshold == 2; one lap is delivered (Tend pushed), one is not.
        # Undelivered count == 1 < 2 → gate must NOT fire.
        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]

        sha_old = "a" * 40
        sha_current = "b" * 40

        # Seed two laps with different head SHAs; current origin head == sha_current.
        agent.memory.record_task(_lap(2564, _AWAITING_PUSH_RESOLVED, 0, pr_head_sha=sha_old))
        agent.memory.record_task(_lap(2564, _AWAITING_PUSH_RESOLVED, 1, pr_head_sha=sha_current))
        # Configure VCS: rev_parse returns current head; is_ancestor(sha_old, current) True.
        agent._vcs.rev_parse.return_value = sha_current
        agent._vcs.is_ancestor.side_effect = (
            lambda candidate, descendant: candidate == sha_old
        )

        gated = await agent._gate_pr_remediation_branch_resolved(
            "Remediate PR #2564: fix CI", "maestro/evolve/ep2564", "external",
        )

        assert gated is False
        agent._notify_best_effort.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_two_undelivered_laps_fires(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        # Both laps at the current head → neither delivered → count == 2 → fires.
        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]

        sha_current = "b" * 40
        agent.memory.record_task(_lap(2564, _AWAITING_PUSH_RESOLVED, 0, pr_head_sha=sha_current))
        agent.memory.record_task(_lap(2564, _AWAITING_PUSH_RESOLVED, 1, pr_head_sha=sha_current))
        agent._vcs.rev_parse.return_value = sha_current
        agent._vcs.is_ancestor.return_value = False  # never a proper ancestor

        gated = await agent._gate_pr_remediation_branch_resolved(
            "Remediate PR #2564: fix CI", "maestro/evolve/ep2564", "external",
        )

        assert gated is True

    @pytest.mark.asyncio
    async def test_gate_stamp_does_not_raise_count_on_second_invocation(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        # Gate fires on 2 undelivered laps, writes its own stamp.
        # A 3rd invocation (gate re-enters) must still see count == 2, not 3.
        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]

        sha_current = "b" * 40
        agent.memory.record_task(_lap(2564, _AWAITING_PUSH_RESOLVED, 0, pr_head_sha=sha_current))
        agent.memory.record_task(_lap(2564, _AWAITING_PUSH_RESOLVED, 1, pr_head_sha=sha_current))
        agent._vcs.rev_parse.return_value = sha_current
        agent._vcs.is_ancestor.return_value = False

        # First invocation — fires and writes the stamp.
        gated = await agent._gate_pr_remediation_branch_resolved(
            "Remediate PR #2564: fix CI", "maestro/evolve/ep2564", "external",
        )
        assert gated is True

        # Check the stamp is excluded from the raw count.
        from dataclasses import asdict
        dicts = [asdict(t) for t in agent.memory.get_recent_tasks(50)]
        count = count_branch_resolved_partials(dicts, 2564)
        assert count == 2  # stamp excluded; real laps still count

    @pytest.mark.asyncio
    async def test_pr_branch_none_falls_back_to_unfiltered(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        # When pr_branch is None, no VCS call is made and unfiltered count is used.
        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
        _seed_mixed_label(agent, 2564)

        gated = await agent._gate_pr_remediation_branch_resolved(
            "Remediate PR #2564: fix CI", None, "external",
        )

        # Two laps, threshold == 2, no branch → gate fires (unfiltered count == 2).
        assert gated is True
        agent._vcs.is_ancestor.assert_not_called()

    @pytest.mark.asyncio
    async def test_unresolvable_ref_falls_back_to_unfiltered(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        # rev_parse returns None (branch pruned) → no predicate built → unfiltered.
        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
        _seed_mixed_label(agent, 2564)
        agent._vcs.rev_parse.return_value = None

        gated = await agent._gate_pr_remediation_branch_resolved(
            "Remediate PR #2564: fix CI", "maestro/evolve/ep2564", "external",
        )

        assert gated is True
        agent._vcs.is_ancestor.assert_not_called()


class TestBranchResolvedPrHeadShaStamp:
    """Gate stamps ``pr_head_sha`` on its skip record."""

    @pytest.mark.asyncio
    async def test_skip_record_has_pr_head_sha(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
        sha_current = "c" * 40
        _seed_mixed_label(agent, 2564)
        agent._vcs.rev_parse.return_value = sha_current
        agent._vcs.is_ancestor.return_value = False

        await agent._gate_pr_remediation_branch_resolved(
            "Remediate PR #2564: fix CI", "maestro/evolve/ep2564", "external",
        )

        skip = agent.memory.get_recent_tasks(10)[-1]
        assert skip.pr_head_sha == sha_current


class TestMergeBlockPrHeadShaStamp:
    """``[merge-block]`` laps honour the #2595 progress-aware contract (#4218).

    A SHA-less merge-block row always counted toward the branch-resolved
    threshold, so two of them evicted the next remediation even after Tend's
    catch-up merge had advanced the head.
    """

    @pytest.mark.asyncio
    async def test_skip_record_has_pr_head_sha(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
        agent._vcs.commits_ahead_behind.return_value = (0, 4)
        agent._vcs.rev_parse.return_value = "a" * 40

        gated = await agent._gate_pr_remediation_merge_block(
            "Remediate PR #2538: fix CI", "maestro/evolve/ep2538", "external",
        )

        assert gated is True
        skip = agent.memory.get_recent_tasks(5)[-1]
        assert skip.pr_head_sha == "a" * 40

    @pytest.mark.asyncio
    async def test_skip_record_without_branch_has_no_sha(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
        # The real probe cannot classify BEHIND_MAIN without a branch; mock it
        # to pin the defensive no-branch arm (mirrors ``gate_stood_down``).
        agent._probe_pr_merge_block = AsyncMock(  # type: ignore[method-assign]
            return_value=(False, MERGE_BLOCK_BEHIND_MAIN),
        )

        gated = await agent._gate_pr_remediation_merge_block(
            "Remediate PR #2538: fix CI", None, "external",
        )

        assert gated is True
        skip = agent.memory.get_recent_tasks(5)[-1]
        assert skip.pr_head_sha is None

    @pytest.mark.asyncio
    async def test_empty_rev_parse_stamps_none(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
        agent._probe_pr_merge_block = AsyncMock(  # type: ignore[method-assign]
            return_value=(False, MERGE_BLOCK_BEHIND_MAIN),
        )
        agent._vcs.rev_parse.return_value = ""

        await agent._gate_pr_remediation_merge_block(
            "Remediate PR #2538: fix CI", "maestro/evolve/ep2538", "external",
        )

        skip = agent.memory.get_recent_tasks(5)[-1]
        assert skip.pr_head_sha is None

    @pytest.mark.asyncio
    async def test_tend_advanced_merge_block_laps_are_discounted(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
        for i in range(2):
            agent.memory.record_task(
                _lap(2538, _MERGE_BLOCK_BEHIND_MAIN, i, pr_head_sha="o" * 40),
            )
        agent._vcs.rev_parse.return_value = "n" * 40
        agent._vcs.is_ancestor.return_value = True

        gated = await agent._gate_pr_remediation_branch_resolved(
            "Remediate PR #2538: fix CI", "maestro/evolve/ep2538", "external",
        )

        assert gated is False
        agent._notify_best_effort.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stalled_merge_block_laps_still_evict(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
        for i in range(2):
            agent.memory.record_task(
                _lap(2538, _MERGE_BLOCK_BEHIND_MAIN, i, pr_head_sha="s" * 40),
            )
        agent._vcs.rev_parse.return_value = "s" * 40
        agent._vcs.is_ancestor.return_value = False

        gated = await agent._gate_pr_remediation_branch_resolved(
            "Remediate PR #2538: fix CI", "maestro/evolve/ep2538", "external",
        )

        assert gated is True


class TestStoodDownGate:
    """``_gate_pr_remediation_stood_down`` — the read side of the chosen brake.

    Wires the durable ``(pr, head_sha)`` stand-down verdict to the dequeue: a
    verdict I authored is honored (request evicted at zero SDK cost) only while
    git merge-truth still shows the head unlanded with Tend PR the sole
    remaining pusher. A new commit (different head SHA) or a landed/non-Tend
    blocker releases the brake and falls through to normal remediation.
    """

    _GOAL = "Remediate PR #2638: fix CLAUDE.md conflict"
    _BRANCH = "maestro/evolve/ep2638"
    _SHA = "f6ad1ca3"

    @pytest.mark.asyncio
    async def test_eviction_on_matching_verdict_unlanded_sole_pusher(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        # A verdict keyed to the current head + an unlanded Tend-clearable
        # merge-truth state (BEHIND_MAIN, the sole such state — AWAITING_PUSH is
        # owned, proposal 098) evicts at zero SDK cost, recording ``[stood-down]``.
        agent = make_agent()
        agent._vcs.rev_parse.return_value = self._SHA
        record_stand_down(2638, self._SHA, agent.stand_down_verdicts)
        agent._probe_pr_merge_block = AsyncMock(  # type: ignore[method-assign]
            return_value=(False, MERGE_BLOCK_BEHIND_MAIN),
        )

        with patch("maestro.agent.query_on_session") as query:
            gated = await agent._gate_pr_remediation_stood_down(
                self._GOAL, self._BRANCH, "external",
            )

        assert gated is True
        query.assert_not_called()  # zero SDK cost
        tasks = agent.memory.get_recent_tasks(5)
        assert len(tasks) == 1
        skip = tasks[0]
        assert skip.outcome == "partial"
        assert skip.outcome != "success"
        assert skip.duration_secs == 0.0
        assert "[stood-down]" in (skip.result_summary or "")
        assert skip.pr_head_sha == self._SHA

    @pytest.mark.asyncio
    async def test_no_eviction_on_awaiting_push(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        """AWAITING_PUSH is owned (proposal 098): a prior "Tend delivers this"
        verdict is no longer honest, so the stand-down brake releases and the lap
        runs rather than being evicted."""
        agent = make_agent()
        agent._vcs.rev_parse.return_value = self._SHA
        record_stand_down(2638, self._SHA, agent.stand_down_verdicts)
        agent._probe_pr_merge_block = AsyncMock(  # type: ignore[method-assign]
            return_value=(False, MERGE_BLOCK_AWAITING_PUSH),
        )

        gated = await agent._gate_pr_remediation_stood_down(
            self._GOAL, self._BRANCH, "external",
        )

        assert gated is False
        assert agent.memory.get_recent_tasks(5) == []

    @pytest.mark.asyncio
    async def test_no_skip_when_head_sha_differs(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        # The verdict is keyed to a prior head; a new commit moved the head, so
        # the lookup misses and the brake auto-releases (probe never consulted).
        agent = make_agent()
        record_stand_down(2638, self._SHA, agent.stand_down_verdicts)
        agent._vcs.rev_parse.return_value = "newsha99"  # head advanced
        agent._probe_pr_merge_block = AsyncMock(  # type: ignore[method-assign]
            return_value=(False, MERGE_BLOCK_AWAITING_PUSH),
        )

        gated = await agent._gate_pr_remediation_stood_down(
            self._GOAL, self._BRANCH, "external",
        )

        assert gated is False
        assert agent.memory.get_recent_tasks(5) == []
        # Auto-release short-circuits before any merge-truth probe.
        agent._probe_pr_merge_block.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_skip_when_git_shows_head_landed(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        # The verdict matches the head, but merge-truth now shows it landed —
        # release the brake rather than mask a fork that actually moved.
        agent = make_agent()
        agent._vcs.rev_parse.return_value = self._SHA
        record_stand_down(2638, self._SHA, agent.stand_down_verdicts)
        agent._probe_pr_merge_block = AsyncMock(  # type: ignore[method-assign]
            return_value=(True, MERGE_BLOCK_NONE),
        )

        gated = await agent._gate_pr_remediation_stood_down(
            self._GOAL, self._BRANCH, "external",
        )

        assert gated is False
        assert agent.memory.get_recent_tasks(5) == []

    @pytest.mark.asyncio
    async def test_no_skip_for_non_tend_clearable_blocker(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        # A conflict needs LLM resolution — not a Tend-clearable sole-pusher
        # state — so the verdict is not honored even though the head matches.
        agent = make_agent()
        agent._vcs.rev_parse.return_value = self._SHA
        record_stand_down(2638, self._SHA, agent.stand_down_verdicts)
        agent._probe_pr_merge_block = AsyncMock(  # type: ignore[method-assign]
            return_value=(False, MERGE_BLOCK_CONFLICTING),
        )

        gated = await agent._gate_pr_remediation_stood_down(
            self._GOAL, self._BRANCH, "external",
        )

        assert gated is False
        assert agent.memory.get_recent_tasks(5) == []

    @pytest.mark.asyncio
    async def test_no_skip_without_verdict(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        # No verdict authored for this (pr, head) — normal remediation proceeds.
        agent = make_agent()
        agent._vcs.rev_parse.return_value = self._SHA
        agent._probe_pr_merge_block = AsyncMock(  # type: ignore[method-assign]
            return_value=(False, MERGE_BLOCK_AWAITING_PUSH),
        )

        gated = await agent._gate_pr_remediation_stood_down(
            self._GOAL, self._BRANCH, "external",
        )

        assert gated is False
        agent._probe_pr_merge_block.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_skip_on_unparseable_goal(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        # A directive without a ``Remediate PR #<n>:`` header has nothing to key
        # on — fall through without touching git.
        agent = make_agent()
        gated = await agent._gate_pr_remediation_stood_down(
            "Investigate something", self._BRANCH, "external",
        )
        assert gated is False
        agent._vcs.rev_parse.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_skip_on_unresolvable_branch_head(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        # An unresolvable origin head SHA can never match a verdict.
        agent = make_agent()
        record_stand_down(2638, self._SHA, agent.stand_down_verdicts)
        agent._vcs.rev_parse.return_value = None
        agent._probe_pr_merge_block = AsyncMock(  # type: ignore[method-assign]
            return_value=(False, MERGE_BLOCK_AWAITING_PUSH),
        )

        gated = await agent._gate_pr_remediation_stood_down(
            self._GOAL, None, "external",
        )

        assert gated is False
        agent._probe_pr_merge_block.assert_not_awaited()


class TestStoodDownWriteSide:
    """Write side: a remediation closing ``[stood-down]`` records a verdict."""

    @pytest.mark.asyncio
    async def test_stood_down_outcome_records_verdict(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        # A remediation task whose summary carries ``[stood-down]`` persists a
        # durable (pr, head_sha) verdict keyed to the origin head the gate reads.
        agent = make_agent()
        agent._vcs.rev_parse.return_value = "originsha42"
        # Branch not ahead of origin (count 0 via conftest default) → the
        # ahead-of-origin guard classifies EXTERNAL and the verdict is written as
        # before; mock the fetch-first seam so no subprocess runs.
        agent._fetch_origin_for_merge_truth = MagicMock()  # type: ignore[method-assign]
        record = TaskRecord(
            task_id="t1",
            goal="Remediate PR #2638: fix CI",
            plan=[],
            outcome="partial",
            result_summary=(
                "[stood-down] Git-confirmed #2638 head unlanded + Tend sole "
                "pusher; chose silence at the fork rather than re-cure."
            ),
            errors=[],
            duration_secs=0.0,
            timestamp=datetime.now(UTC).isoformat(),
            source="external",
            key_findings="[stood-down] chose silence",
        )

        await agent._record_remediation_outcome(
            goal="Remediate PR #2638: fix CI",
            pr_branch="maestro/evolve/ep2638",
            record=record,
            head_before="originsha42",  # no commit → genuine stand-down
        )

        verdict = find_stand_down(2638, "originsha42", agent.stand_down_verdicts)
        assert verdict is not None
        assert verdict.pr == 2638
        assert verdict.head_sha == "originsha42"

    @pytest.mark.asyncio
    async def test_stood_down_outcome_records_state(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        # The stand-down write and the typed observability field are driven by
        # one classified state: a [stood-down] outcome persists the verdict and
        # records ``_last_remediation_state`` as STOOD_DOWN, asserted via the
        # enum rather than any marker substring (proposal 070).
        agent = make_agent()
        agent._vcs.rev_parse.return_value = "originsha99"
        # Branch not ahead of origin (count 0) → EXTERNAL; mock the fetch seam.
        agent._fetch_origin_for_merge_truth = MagicMock()  # type: ignore[method-assign]
        record = TaskRecord(
            task_id="t3",
            goal="Remediate PR #2700: fix CI",
            plan=[],
            outcome="partial",
            result_summary=(
                "[stood-down] Git-confirmed #2700 head unlanded + Tend sole "
                "pusher; chose silence at the fork rather than re-cure."
            ),
            errors=[],
            duration_secs=0.0,
            timestamp=datetime.now(UTC).isoformat(),
            source="external",
            key_findings="[stood-down] chose silence",
        )

        await agent._record_remediation_outcome(
            goal="Remediate PR #2700: fix CI",
            pr_branch="maestro/evolve/ep2700",
            record=record,
            head_before="originsha99",  # no commit → genuine stand-down
        )

        assert agent._last_remediation_state is PrRemediationState.STOOD_DOWN
        verdict = find_stand_down(2700, "originsha99", agent.stand_down_verdicts)
        assert verdict is not None
        assert verdict.pr == 2700

    @pytest.mark.asyncio
    async def test_non_stood_down_outcome_records_no_verdict(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        # A normal remediation outcome writes no stand-down verdict.
        agent = make_agent()
        agent._vcs.rev_parse.return_value = "originsha42"
        record = TaskRecord(
            task_id="t2",
            goal="Remediate PR #2638: fix CI",
            plan=[],
            outcome="success",
            result_summary="Resolved the conflict and committed on-branch.",
            errors=[],
            duration_secs=1.0,
            timestamp=datetime.now(UTC).isoformat(),
            source="external",
            key_findings="committed the fix",
        )

        await agent._record_remediation_outcome(
            goal="Remediate PR #2638: fix CI",
            pr_branch="maestro/evolve/ep2638",
            record=record,
            head_before="oldsha",
        )

        assert find_stand_down(
            2638, "originsha42", agent.stand_down_verdicts,
        ) is None
