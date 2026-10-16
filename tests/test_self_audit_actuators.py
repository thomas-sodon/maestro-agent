"""Self-audit actuators - directive fill, orphaned-cure retire, phantom
escalation, handoff and refutation delivery (``maestro/loop/self_audit.py``);
split out of ``tests/test_agent_lifecycle.py``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from maestro.memory import EvolutionPlan, TaskRecord, VersionRecord
from maestro.pr_facts import MergeTruth
from maestro.vcs import PrMergeState
from tests.test_agent_lifecycle import (
    _make_plan,
)

# ──────────────────────────────────────────────────────────────────────
# Audit→directive bridge
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_finding_sets_directive_when_empty(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When _audit_self_model produces findings and next_task_directive is
    empty, the first finding is auto-routed into the directive."""
    from maestro.audit_checks import AuditFinding

    agent = make_agent()
    # Ensure directive starts empty.
    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    finding = AuditFinding(
        kind="undeclared_limitation",
        statement="Error pattern 'timeout' recurred 4 times.",
        cites=["t1", "t2"],
        rule="repeated_error_without_limitation",
    )

    # Patch run_all_checks to return our controlled finding.
    with patch(
        "maestro.audit_checks.run_all_checks", return_value=[finding],
    ):
        await agent._audit_self_model(iteration=1)

    updated = agent.consciousness.load_self_model()
    assert updated.next_task_directive is not None
    assert updated.next_task_directive.startswith(
        "Address audit finding [undeclared_limitation]: ",
    )
    assert len(updated.next_task_directive) <= 200
    # Actuation telemetry (090): the bridge logs a ``directive_set`` row whose
    # detail is the directive text it wrote into the empty slot.
    acts = _narration_activation_rows(
        agent, mem_fs, gate="audit_directive_bridge",
    )
    assert len(acts) == 1
    assert acts[0]["action"] == "directive_set"
    assert acts[0]["detail"] == updated.next_task_directive


@pytest.mark.asyncio
async def test_audit_finding_preserves_existing_directive(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When next_task_directive is already set, audit findings do NOT
    overwrite it."""
    from maestro.audit_checks import AuditFinding

    agent = make_agent()
    existing_directive = "Focus on improving test coverage"
    model = agent.consciousness.load_self_model()
    model.next_task_directive = existing_directive
    agent.consciousness.save_self_model(model)

    finding = AuditFinding(
        kind="stale_entry",
        statement="capabilities_known: stale capability claim",
        cites=["capabilities_known"],
        rule="stale_entry",
    )

    with patch(
        "maestro.audit_checks.run_all_checks", return_value=[finding],
    ):
        await agent._audit_self_model(iteration=1)

    updated = agent.consciousness.load_self_model()
    assert updated.next_task_directive == existing_directive


@pytest.mark.asyncio
async def test_no_findings_leaves_directive_unchanged(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When _audit_self_model produces zero findings, next_task_directive
    is left untouched."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    with patch(
        "maestro.audit_checks.run_all_checks", return_value=[],
    ):
        await agent._audit_self_model(iteration=1)

    updated = agent.consciousness.load_self_model()
    assert updated.next_task_directive is None


@pytest.mark.asyncio
async def test_audit_computes_settled_prs_once_for_both_consumers(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """``_audit_self_model`` computes ``settled_prs`` once and the same set
    reaches both ``run_all_checks`` and a phantom rule (criterion 4)."""
    agent = make_agent()
    agent.config.settlement_reconciler_enabled = True
    agent.config.unconfirmed_landing_enabled = True

    sentinel = frozenset({3971, 3972})
    reconcile = MagicMock(return_value=sentinel)

    run_all_seen: dict[str, object] = {}

    def _capture_run_all(*args: object, **kwargs: object) -> list[object]:
        run_all_seen["settled_prs"] = kwargs.get("settled_prs")
        return []

    phantom_seen: dict[str, object] = {}

    def _capture_phantom(*args: object, **kwargs: object) -> list[object]:
        phantom_seen["settled_prs"] = kwargs.get("settled_prs")
        return []

    with (
        patch(
            "maestro.audit_checks_settlement.reconcile_settled_verdicts", reconcile,
        ),
        patch(
            "maestro.audit_checks.run_all_checks", _capture_run_all,
        ),
        patch(
            "maestro.audit_checks_phantom.check_unconfirmed_own_landing",
            _capture_phantom,
        ),
    ):
        await agent._audit_self_model(iteration=5)

    # Computed once, before run_all_checks.
    reconcile.assert_called_once()
    assert run_all_seen["settled_prs"] == sentinel
    # The very same set reaches the phantom rule.
    assert phantom_seen["settled_prs"] == sentinel
    assert run_all_seen["settled_prs"] is phantom_seen["settled_prs"]


@pytest.mark.asyncio
async def test_audit_absent_ledger_passes_empty_settled_prs(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With the reconciler off, ``run_all_checks`` receives an empty
    ``settled_prs`` so the finding set is unchanged (criterion 4)."""
    agent = make_agent()
    agent.config.settlement_reconciler_enabled = False

    seen: dict[str, object] = {}

    def _capture_run_all(*args: object, **kwargs: object) -> list[object]:
        seen["settled_prs"] = kwargs.get("settled_prs")
        return []

    with patch("maestro.audit_checks.run_all_checks", _capture_run_all):
        await agent._audit_self_model(iteration=5)

    assert seen["settled_prs"] == frozenset()


def _overdue_version(
    version_id: str = "v_overdue",
    commit_sha: str = "deadbeefcafe0000",
    quality_gate_pass: bool = True,
) -> VersionRecord:
    """A VersionRecord merged long ago — past any impact-review cutoff."""
    return VersionRecord(
        version_id=version_id,
        plan_id="ep_old",
        plan_title="Ancient evolution",
        commit_sha=commit_sha,
        merged_at="2020-01-01T00:00:00+00:00",
        window_tasks_total=0,
        window_tasks_succeeded=0,
        window_tasks_failed=0,
        window_sdk_cost_usd=0.0,
        window_sdk_calls=0,
        window_cache_hit_rate=0.0,
        window_rate_limit_hits=0,
        window_error_count=0,
        quality_gate_pass=quality_gate_pass,
    )


@pytest.mark.asyncio
async def test_audit_self_model_surfaces_overdue_impact_review(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """An overdue, never-revisited VersionRecord surfaces as an
    ``unreviewed_evolution`` finding in the journal + open questions and
    feeds the audit→directive bridge."""
    agent = make_agent()
    agent.memory.record_version(_overdue_version())

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    # No legacy-rule findings — isolate the impact-review rule.
    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        await agent._audit_self_model(iteration=1)

    updated = agent.consciousness.load_self_model()
    assert any(
        q.startswith("[audit] unreviewed_evolution:")
        for q in updated.open_questions
    )
    assert updated.next_task_directive is not None
    assert updated.next_task_directive.startswith(
        "Address audit finding [unreviewed_evolution]: ",
    )

    entries = agent.journal.read_recent_events(20)
    assert any(
        isinstance(e.content, str)
        and "[unreviewed_evolution]" in e.content
        and "v_overdue" in e.content
        for e in entries
    )


@pytest.mark.asyncio
async def test_audit_self_model_clipped_impact_review_stays_unrecorded(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Regression #4099: a rule-9 finding clipped off the cycle cap by later
    prepend rules must not be marked surfaced — only the surviving version is
    recorded, and the clipped one re-surfaces next cycle."""
    from maestro.audit_checks import MAX_FINDINGS_PER_RULE, AuditFinding

    agent = make_agent()
    agent.config.orphaned_cure_enabled = True
    agent.config.phantom_escalation_enabled = True
    agent.memory.record_version(_overdue_version("v_a", commit_sha="aaaa0000"))
    agent.memory.record_version(_overdue_version("v_b", commit_sha="bbbb0000"))

    orphaned = [
        AuditFinding(
            kind="unmerged_work",
            statement=f"Cure plan ep_{i} unmerged.",
            cites=[f"ep_{i}"],
            rule="orphaned_cure",
        )
        for i in range(MAX_FINDINGS_PER_RULE)
    ]
    phantom = [AuditFinding(
        kind="phantom_escalation",
        statement="PR #1 escalated only in prose.",
        cites=["t_1"],
        rule="phantom_escalation",
    )]

    with patch("maestro.audit_checks.run_all_checks", return_value=[]), \
            patch("maestro.audit_checks_phantom.check_orphaned_cure",
                  return_value=orphaned), \
            patch("maestro.audit_checks_phantom.check_phantom_escalation",
                  return_value=phantom):
        await agent._audit_self_model(iteration=1)

    first = agent.memory.get_recent_audit_results(1)[-1]
    first_impact = [
        f for f in first.findings if f.kind == "unreviewed_evolution"
    ]
    assert len(first_impact) == 1
    survivor = first_impact[0].cites[0]
    assert agent.impact_review_surfaced.load() == {survivor}
    (clipped,) = {"v_a", "v_b"} - {survivor}

    with patch("maestro.audit_checks.run_all_checks", return_value=[]), \
            patch("maestro.audit_checks_phantom.check_orphaned_cure", return_value=[]), \
            patch("maestro.audit_checks_phantom.check_phantom_escalation",
                  return_value=[]):
        await agent._audit_self_model(iteration=2)

    second = agent.memory.get_recent_audit_results(1)[-1]
    impact = [f for f in second.findings if f.kind == "unreviewed_evolution"]
    assert len(impact) == 1
    assert impact[0].cites == [clipped]
    updated = agent.consciousness.load_self_model()
    assert any(
        q.startswith("[audit] unreviewed_evolution:") and clipped in q
        for q in updated.open_questions
    )
    recorded = agent.impact_review_surfaced.load()
    assert recorded == {"v_a", "v_b"}
    persisted_cites = {
        c
        for r in agent.memory.get_recent_audit_results(10)
        for f in r.findings
        if f.kind == "unreviewed_evolution"
        for c in f.cites
    }
    assert recorded <= persisted_cites


@pytest.mark.asyncio
async def test_audit_self_model_impact_review_disabled(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """With ``audit_impact_review_enabled=False`` the rule never runs even
    when an overdue version exists."""
    agent = make_agent()
    agent.config.audit_impact_review_enabled = False
    agent.memory.record_version(_overdue_version())

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        await agent._audit_self_model(iteration=1)

    updated = agent.consciousness.load_self_model()
    assert not any(
        q.startswith("[audit] unreviewed_evolution:")
        for q in updated.open_questions
    )
    assert updated.next_task_directive is None


def _phantom_remediation_task(
    pr_number: int = 2376, task_id: str = "t_phantom",
) -> TaskRecord:
    """A remediation stamped not-landed (the stamp-gate downgrade note)."""
    return TaskRecord(
        task_id=task_id,
        goal=f"Remediate PR #{pr_number}: Fix the widget",
        plan=[],
        outcome="partial",  # type: ignore[arg-type]
        result_summary=(
            f"Resolved on branch, awaiting tend push — PR #{pr_number} "
            f"not yet landed (merge blocker: BEHIND_MAIN)."
        ),
        errors=[],
        duration_secs=1.0,
        timestamp="2026-06-01T00:00:00Z",
    )


def _implemented_cure_plan(plan_id: str = "ep_cure") -> EvolutionPlan:
    """An ``implemented``-but-unmerged evolution plan (a built, opened cure)."""
    return EvolutionPlan(
        plan_id=plan_id,
        title="Add the cure",
        category="capability",
        rationale="r",
        proposed_changes=["x"],
        files_affected=["maestro/x.py"],
        expected_impact="none",
        status="implemented",
    )


@pytest.mark.asyncio
async def test_audit_self_model_surfaces_orphaned_cure(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """An aged ``implemented``-but-unmerged plan coexisting with remediation
    laps surfaces an ``unmerged_work`` finding and feeds the directive bridge."""
    agent = make_agent()
    agent.memory.save_evolution_plan(_implemented_cure_plan())
    agent.memory.record_task(_phantom_remediation_task())
    # Pre-seed the first-seen sidecar so the plan reads as aged (age = 5 - 0).
    mem_fs[str(agent.config.memory_dir / "orphaned_cure_first_seen.json")] = (
        json.dumps({"ep_cure": 0})
    )

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    # Isolate rule 11: silence the legacy combiner and rule 9.
    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        agent.config.audit_impact_review_enabled = False
        await agent._audit_self_model(iteration=5)

    updated = agent.consciousness.load_self_model()
    assert any(
        q.startswith("[audit] unmerged_work:")
        for q in updated.open_questions
    )
    assert updated.next_task_directive is not None
    assert updated.next_task_directive.startswith(
        "Address audit finding [unmerged_work]: ",
    )
    entries = agent.journal.read_recent_events(20)
    assert any(
        isinstance(e.content, str)
        and "[unmerged_work]" in e.content
        and "ep_cure" in e.content
        for e in entries
    )


@pytest.mark.asyncio
async def test_audit_self_model_orphaned_cure_disabled(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """With ``orphaned_cure_enabled=False`` the rule never runs even when an
    aged implemented cure coexists with remediation laps."""
    agent = make_agent()
    agent.config.orphaned_cure_enabled = False
    # Silence the other git-touching audit rules so the phantom task we use as
    # a remediation lap does not reach the real git-merge-truth subprocess.
    agent.config.audit_impact_review_enabled = False
    agent.memory.save_evolution_plan(_implemented_cure_plan())
    agent.memory.record_task(_phantom_remediation_task())
    mem_fs[str(agent.config.memory_dir / "orphaned_cure_first_seen.json")] = (
        json.dumps({"ep_cure": 0})
    )

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        await agent._audit_self_model(iteration=5)

    updated = agent.consciousness.load_self_model()
    assert not any(
        q.startswith("[audit] unmerged_work:")
        for q in updated.open_questions
    )
    assert updated.next_task_directive is None


def _seed_conflicted_cure(agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """Seed an aged implemented cure with tend-confirmed conflict evidence."""
    from maestro.messaging import MessageStore

    agent.memory.save_evolution_plan(_implemented_cure_plan())
    agent.memory.update_plan_status(
        "ep_cure",
        "implemented",
        pr_url="https://github.com/o/r/pull/1",
        last_conflict_resolution_attempt_at="2026-01-01T00:00:00+00:00",
    )
    # Pre-seed the first-seen sidecar so the cure reads as far past the
    # default retire ceiling of 30 cycles (age = 40 - 0).
    mem_fs[str(agent.config.memory_dir / "orphaned_cure_first_seen.json")] = (
        json.dumps({"ep_cure": 0})
    )
    # Proposal 091: the sweep now gates retirement on a delivered disposition,
    # which needs a real message store and a PR-comment poster. Wire both so the
    # sweep can retire; ``make_agent`` leaves ``message_store`` None and the real
    # ``comment_issue`` would shell out to ``gh``. The failure test overrides
    # ``comment_issue`` to a failing post.
    agent.message_store = MessageStore(agent.config.memory_dir)
    agent.evolution._host.comment_issue = MagicMock(return_value=True)


@pytest.mark.asyncio
async def test_audit_self_model_retires_orphaned_cure(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """With both rule-11 flags on, an aged conflict-evidenced implemented cure
    is retired at the audit dispatch after its disposition is delivered — a PR
    comment, a message to Thomas, and a standdown — and the retirement is
    journaled."""
    from maestro.escalation_bridge import has_standdown

    agent = make_agent()
    _seed_conflicted_cure(agent, mem_fs)

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        agent.config.audit_impact_review_enabled = False
        await agent._audit_self_model(iteration=40)

    reloaded = agent.memory.get_plan("ep_cure")
    assert reloaded is not None
    assert reloaded.status == "obsolete"
    # The disposition was delivered before retirement: a PR comment posted, a
    # close-or-keep message reached Thomas, and a standdown was recorded.
    agent.evolution._host.comment_issue.assert_called_once()
    assert agent.evolution._host.comment_issue.call_args.args[0] == 1
    disposition = [
        m for m in agent.message_store.get_recent()
        if m.message_id == "disposition-1"
    ]
    assert len(disposition) == 1
    assert has_standdown(agent.escalation_standdowns, 1) is True
    entries = agent.journal.read_recent_events(20)
    assert any(
        e.triggered_by == "orphaned_cure_retire"
        and isinstance(e.content, str)
        and "ep_cure" in e.content
        for e in entries
    )


@pytest.mark.asyncio
async def test_audit_self_model_leaves_cure_when_disposition_fails(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """When the PR-comment post fails, delivery is fail-closed: the cure is left
    ``implemented`` (tend keeps servicing it), no message or standdown lands, and
    nothing is journaled."""
    from maestro.escalation_bridge import has_standdown

    agent = make_agent()
    _seed_conflicted_cure(agent, mem_fs)
    # The host cannot post the disposition comment → deliver_disposition returns
    # False → the sweep refuses to retire.
    agent.evolution._host.comment_issue = MagicMock(return_value=False)

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        agent.config.audit_impact_review_enabled = False
        await agent._audit_self_model(iteration=40)

    reloaded = agent.memory.get_plan("ep_cure")
    assert reloaded is not None
    assert reloaded.status == "implemented"
    # Fail-closed: nothing durable was written and nothing journaled.
    assert agent.message_store.get_recent() == []
    assert has_standdown(agent.escalation_standdowns, 1) is False
    entries = agent.journal.read_recent_events(20)
    assert not any(
        e.triggered_by == "orphaned_cure_retire" for e in entries
    )


@pytest.mark.asyncio
async def test_audit_self_model_skips_retire_when_pr_url_missing(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A retire candidate with no PR URL has no delivery path: the closure
    refuses (returns False) and the plan is left implemented."""
    from maestro.messaging import MessageStore

    agent = make_agent()
    agent.memory.save_evolution_plan(_implemented_cure_plan())
    # Aged and conflict-evidenced, but no ``pr_url`` — the delivery closure
    # short-circuits on ``not plan.pr_url``.
    agent.memory.update_plan_status(
        "ep_cure",
        "implemented",
        last_conflict_resolution_attempt_at="2026-01-01T00:00:00+00:00",
    )
    mem_fs[str(agent.config.memory_dir / "orphaned_cure_first_seen.json")] = (
        json.dumps({"ep_cure": 0})
    )
    agent.message_store = MessageStore(agent.config.memory_dir)

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        agent.config.audit_impact_review_enabled = False
        await agent._audit_self_model(iteration=40)

    reloaded = agent.memory.get_plan("ep_cure")
    assert reloaded is not None
    assert reloaded.status == "implemented"
    assert agent.message_store.get_recent() == []


@pytest.mark.asyncio
async def test_audit_self_model_skips_retire_when_pr_url_unparseable(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A retire candidate whose PR URL cannot be parsed refuses retirement: the
    closure catches the ValueError and returns False, leaving the plan
    implemented and never posting a comment."""
    from maestro.messaging import MessageStore

    agent = make_agent()
    agent.memory.save_evolution_plan(_implemented_cure_plan())
    agent.memory.update_plan_status(
        "ep_cure",
        "implemented",
        pr_url="https://github.com/o/r/pull/not-a-number",
        last_conflict_resolution_attempt_at="2026-01-01T00:00:00+00:00",
    )
    mem_fs[str(agent.config.memory_dir / "orphaned_cure_first_seen.json")] = (
        json.dumps({"ep_cure": 0})
    )
    agent.message_store = MessageStore(agent.config.memory_dir)
    agent.evolution._host.comment_issue = MagicMock(return_value=True)

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        agent.config.audit_impact_review_enabled = False
        await agent._audit_self_model(iteration=40)

    reloaded = agent.memory.get_plan("ep_cure")
    assert reloaded is not None
    assert reloaded.status == "implemented"
    # Unparseable URL → no delivery attempted, so no comment posted.
    agent.evolution._host.comment_issue.assert_not_called()


@pytest.mark.asyncio
async def test_audit_self_model_retire_flag_off_leaves_cure(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """With ``orphaned_cure_retire_enabled=False`` the dispose never fires."""
    agent = make_agent()
    agent.config.orphaned_cure_retire_enabled = False
    _seed_conflicted_cure(agent, mem_fs)

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        agent.config.audit_impact_review_enabled = False
        await agent._audit_self_model(iteration=40)

    reloaded = agent.memory.get_plan("ep_cure")
    assert reloaded is not None
    assert reloaded.status == "implemented"
    entries = agent.journal.read_recent_events(20)
    assert not any(
        e.triggered_by == "orphaned_cure_retire" for e in entries
    )


@pytest.mark.asyncio
async def test_audit_self_model_retire_requires_rule_master_switch(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """With ``orphaned_cure_enabled=False`` the dispose never fires even when
    the retire flag itself is on — the actuator lives inside rule 11's block."""
    agent = make_agent()
    agent.config.orphaned_cure_enabled = False
    agent.config.orphaned_cure_retire_enabled = True
    _seed_conflicted_cure(agent, mem_fs)

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        agent.config.audit_impact_review_enabled = False
        await agent._audit_self_model(iteration=40)

    reloaded = agent.memory.get_plan("ep_cure")
    assert reloaded is not None
    assert reloaded.status == "implemented"
    entries = agent.journal.read_recent_events(20)
    assert not any(
        e.triggered_by == "orphaned_cure_retire" for e in entries
    )


def _phantom_escalation_task(
    pr_number: int = 2579, task_id: str = "t_escal",
) -> TaskRecord:
    """A ``partial`` remediation lap whose summary asserts 'escalating'."""
    return TaskRecord(
        task_id=task_id,
        goal=f"Remediate PR #{pr_number}: fix the gate",
        plan=[],
        outcome="partial",  # type: ignore[arg-type]
        result_summary=(
            f"Branch resolved, escalating PR #{pr_number} once; standing down."
        ),
        errors=[],
        duration_secs=1.0,
        timestamp="2026-06-01T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_audit_self_model_surfaces_phantom_escalation(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A ``partial`` lap asserting 'escalating' for a PR with no outbound
    message referencing it surfaces a ``phantom_escalation`` finding and feeds
    the directive bridge."""
    agent = make_agent()
    agent.memory.record_task(_phantom_escalation_task())

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    # Isolate rule 12: silence the legacy combiner and the other git-touching
    # rules so no real subprocess runs and only phantom_escalation can fire.
    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        agent.config.audit_impact_review_enabled = False
        agent.config.orphaned_cure_enabled = False
        await agent._audit_self_model(iteration=1)

    updated = agent.consciousness.load_self_model()
    assert any(
        q.startswith("[audit] phantom_escalation:")
        for q in updated.open_questions
    )
    assert updated.next_task_directive is not None
    assert updated.next_task_directive.startswith(
        "Address audit finding [phantom_escalation]: ",
    )
    entries = agent.journal.read_recent_events(20)
    assert any(
        isinstance(e.content, str)
        and "[phantom_escalation]" in e.content
        and "#2579" in e.content
        for e in entries
    )


@pytest.mark.asyncio
async def test_audit_self_model_phantom_escalation_disabled(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """With ``phantom_escalation_enabled=False`` the rule never runs even when
    a phantom escalation lap exists."""
    agent = make_agent()
    agent.config.phantom_escalation_enabled = False
    agent.config.audit_impact_review_enabled = False
    agent.config.orphaned_cure_enabled = False
    agent.memory.record_task(_phantom_escalation_task())

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        await agent._audit_self_model(iteration=1)

    updated = agent.consciousness.load_self_model()
    assert not any(
        q.startswith("[audit] phantom_escalation:")
        for q in updated.open_questions
    )
    assert updated.next_task_directive is None


def _claimed_escalation_messages(agent, issue_number):  # type: ignore[no-untyped-def]
    """Claimed-escalation messages delivered for *issue_number* (proposal 090)."""
    from maestro.messaging import MessageStore

    # The id folds the delivery timestamp in as an epoch tag
    # (``claimed-escalation-<issue>-<timestamp>``), so match on the
    # trailing-hyphen prefix rather than an exact id.
    prefix = f"claimed-escalation-{issue_number}-"
    return [
        m for m in MessageStore(agent.config.memory_dir).get_recent()
        if m.message_id.startswith(prefix)
    ]


@pytest.mark.asyncio
async def test_phantom_escalation_delivery_completes_claim(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """With the delivery flag on, two 'escalating #N' laps (the ≥2-lap
    threshold) with no referencing message deliver exactly one merge-or-close
    message and record one standdown; a second cycle is a no-op because the
    delivered message now references #N (rule 12 self-extinguishes)."""
    from maestro.escalation_bridge import has_standdown

    agent = make_agent()
    _silence_git_audit_rules(agent)
    agent.config.phantom_escalation_enabled = True
    agent.config.phantom_escalation_delivery_enabled = True
    agent.memory.record_task(
        _phantom_escalation_task(pr_number=2579, task_id="t_escal_a"),
    )
    agent.memory.record_task(
        _phantom_escalation_task(pr_number=2579, task_id="t_escal_b"),
    )

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        await agent._audit_self_model(iteration=1)

    assert has_standdown(agent.escalation_standdowns, 2579) is True
    delivered = _claimed_escalation_messages(agent, 2579)
    assert len(delivered) == 1
    assert "#2579" in delivered[0].content

    # Actuation telemetry (090): one ``handoff`` row grounded on the issue.
    acts = _narration_activation_rows(
        agent, mem_fs, gate="phantom_escalation_delivery",
    )
    assert len(acts) == 1
    assert acts[0]["action"] == "handoff"
    assert acts[0]["detail"] == "pr=#2579"

    # Second cycle: the delivered message now references #2579, so rule 12 finds
    # no phantom escalation and the actuator is a no-op — still exactly one.
    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        await agent._audit_self_model(iteration=2)
    assert len(_claimed_escalation_messages(agent, 2579)) == 1
    # Still exactly one telemetry row — the no-op second cycle added none.
    assert len(
        _narration_activation_rows(
            agent, mem_fs, gate="phantom_escalation_delivery",
        )
    ) == 1


@pytest.mark.asyncio
async def test_phantom_escalation_delivery_single_lap_suppressed(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A single 'escalating #N' lap sits below the ≥2-lap delivery threshold —
    the actuator never fires (no message, no standdown), even flag-on."""
    from maestro.escalation_bridge import has_standdown

    agent = make_agent()
    _silence_git_audit_rules(agent)
    agent.config.phantom_escalation_enabled = True
    agent.config.phantom_escalation_delivery_enabled = True
    agent.memory.record_task(
        _phantom_escalation_task(pr_number=2579, task_id="t_escal_solo"),
    )

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        await agent._audit_self_model(iteration=1)

    assert has_standdown(agent.escalation_standdowns, 2579) is False
    assert _claimed_escalation_messages(agent, 2579) == []


@pytest.mark.asyncio
async def test_phantom_escalation_delivery_disabled_delivers_nothing(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """With the delivery flag off (default), two 'escalating #N' laps still
    surface the detection finding but the actuator never fires."""
    from maestro.escalation_bridge import has_standdown

    agent = make_agent()
    _silence_git_audit_rules(agent)
    agent.config.phantom_escalation_enabled = True
    agent.config.phantom_escalation_delivery_enabled = False
    agent.memory.record_task(
        _phantom_escalation_task(pr_number=2579, task_id="t_escal_a"),
    )
    agent.memory.record_task(
        _phantom_escalation_task(pr_number=2579, task_id="t_escal_b"),
    )

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        await agent._audit_self_model(iteration=1)

    assert has_standdown(agent.escalation_standdowns, 2579) is False
    assert _claimed_escalation_messages(agent, 2579) == []
    # No delivery → no actuation telemetry row (delivery flag off).
    assert _narration_activation_rows(
        agent, mem_fs, gate="phantom_escalation_delivery",
    ) == []


def _evolve_pr_state(number: int, head_ref: str) -> PrMergeState:
    """A minimal open ``maestro/evolve/*`` PR state for the readiness tests."""
    return PrMergeState(
        number=number, head_ref=head_ref, title="t", body="b", state="OPEN",
        mergeable="MERGEABLE", merge_state_status="CLEAN",
        review_decision="", created_at="", updated_at="", url="",
    )


def _handoff_messages(agent, pr_number):  # type: ignore[no-untyped-def]
    """Merge-or-close handoff messages delivered for *pr_number*."""
    from maestro.messaging import MessageStore

    prefix = f"handoff-{pr_number}-"
    return [
        m for m in MessageStore(agent.config.memory_dir).get_recent()
        if m.message_id.startswith(prefix)
    ]


@pytest.mark.asyncio
async def test_green_ready_handoff_delivers_when_flag_on(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Criterion 1/4 — with ``green_ready_handoff_enabled`` on, an owned,
    git-unlanded, merge-ready PR (``observe_pr_readiness`` reads
    ``ready_and_waiting``) with no standdown records a ``pr_number``-keyed
    standdown and delivers exactly one merge-or-close message via the existing
    ``deliver_handoff`` actuator — the earliest handoff path.

    Delivery-truth bind: the arm's 'delivered' truth binds to a re-read of the
    ``pr_number``-keyed ``stand_down_verdicts.jsonl`` row. ``deliver_handoff``
    records only the escalation-ledger standdown, never that verdict row, so the
    bind backfills it directly and stamps ``handoff_row_persisted`` (not
    ``handoff``) — the honest signal that the actuator left no artifact."""
    from maestro.audit_checks_phantom import (
        _pr_keyed_stand_down_row_count,
    )
    from maestro.escalation_bridge import has_standdown

    agent = make_agent()
    _silence_git_audit_rules(agent)
    agent.config.green_ready_handoff_enabled = True
    host = MagicMock()
    host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/p31": _evolve_pr_state(3181, "maestro/evolve/p31"),
    }
    agent.evolution._host = host  # type: ignore[attr-defined]
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(ready_and_waiting=True),
    )

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        await agent._audit_self_model(iteration=1)

    assert has_standdown(agent.escalation_standdowns, 3181) is True
    delivered = _handoff_messages(agent, 3181)
    assert len(delivered) == 1
    assert "#3181" in delivered[0].content
    # Delivery-truth bind: a pr_number-keyed stand_down_verdicts.jsonl row is on
    # disk when the rule returns — the durable artifact narration cannot author.
    assert _pr_keyed_stand_down_row_count(3181, agent.stand_down_verdicts) == 1
    # Actuation telemetry: the bind backfilled the row deliver_handoff left out,
    # so the row is ``handoff_row_persisted`` rather than ``handoff``.
    acts = _narration_activation_rows(
        agent, mem_fs, gate="green_ready_handoff",
    )
    assert len(acts) == 1
    assert acts[0]["action"] == "handoff_row_persisted"
    assert acts[0]["detail"] == "pr=#3181"
    # Deed ledger: the fired forced arm records exactly one HANDOFF_DELIVERED
    # deed keyed to the handed-off PR.
    from maestro import deed_ledger

    deeds = deed_ledger.recent_deeds(agent.config.memory_dir, n=10)
    assert len(deeds) == 1
    assert deeds[0].deed_type == deed_ledger.DeedType.HANDOFF_DELIVERED.value
    assert deeds[0].pr_number == 3181


@pytest.mark.asyncio
async def test_green_ready_handoff_skipped_when_flag_off(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Criterion 4 — with the flag off, ``_audit_self_model`` never dispatches
    rule 22: no standdown, no message, and the readiness chokepoint is never
    consulted (even one that WOULD hand off must not be read)."""
    from maestro.escalation_bridge import has_standdown

    agent = make_agent()
    _silence_git_audit_rules(agent)
    # _silence_git_audit_rules already sets this False; restate the precondition.
    agent.config.green_ready_handoff_enabled = False
    host = MagicMock()
    host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/p31": _evolve_pr_state(3181, "maestro/evolve/p31"),
    }
    agent.evolution._host = host  # type: ignore[attr-defined]
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(ready_and_waiting=True),
    )

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        await agent._audit_self_model(iteration=1)

    assert has_standdown(agent.escalation_standdowns, 3181) is False
    assert _handoff_messages(agent, 3181) == []
    agent.observe_pr_readiness.assert_not_called()
    # Flag off is the no-op branch: no handoff fired, so no deed recorded.
    from maestro import deed_ledger

    assert deed_ledger.count_deeds(agent.config.memory_dir) == 0


@pytest.mark.asyncio
async def test_green_ready_handoff_ci_pending_no_delivery(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Criterion 3 — a CI-pending/failing owned PR reads not-``ready_and_waiting``,
    so rule 22 hands nothing off even with the flag on: no message, no
    escalation-ledger standdown, and no ``stand_down_verdicts.jsonl`` row."""
    from maestro.audit_checks_phantom import (
        _pr_keyed_stand_down_row_count,
    )
    from maestro.escalation_bridge import has_standdown

    agent = make_agent()
    _silence_git_audit_rules(agent)
    agent.config.green_ready_handoff_enabled = True
    host = MagicMock()
    host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/p31": _evolve_pr_state(3181, "maestro/evolve/p31"),
    }
    agent.evolution._host = host  # type: ignore[attr-defined]
    # Not merge-ready — CI is still pending / failing.
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(ready_and_waiting=False),
    )

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        await agent._audit_self_model(iteration=1)

    assert has_standdown(agent.escalation_standdowns, 3181) is False
    assert _handoff_messages(agent, 3181) == []
    assert _pr_keyed_stand_down_row_count(3181, agent.stand_down_verdicts) == 0
    # Not-merge-ready is the skip branch: no handoff fired, so no deed recorded.
    from maestro import deed_ledger

    assert deed_ledger.count_deeds(agent.config.memory_dir) == 0


def _unconfirmed_landing_task(
    pr_number: int = 2638,
    sha: str = "ac48a769",
    task_id: str = "t_landing",
) -> TaskRecord:
    """A ``partial`` handoff: a SHA I committed on-branch, never confirmed landed."""
    return TaskRecord(
        task_id=task_id,
        goal=f"Remediate PR #{pr_number}: fix the gate",
        plan=[],
        outcome="partial",  # type: ignore[arg-type]
        result_summary=(
            f"Resolved on branch, awaiting tend push for PR #{pr_number}; "
            f"committed {sha}."
        ),
        errors=[],
        duration_secs=1.0,
        timestamp="2026-06-01T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_audit_self_model_surfaces_unconfirmed_landing(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A ``partial`` handoff whose committed SHA git shows is still not an
    ancestor of origin/main past the grace age surfaces an
    ``unconfirmed_landing`` finding and feeds the directive bridge."""
    agent = make_agent()
    agent.memory.record_task(_unconfirmed_landing_task())
    # Pre-seed the first-seen sidecar so the handoff reads as aged (age = 5 - 0).
    mem_fs[str(agent.config.memory_dir / "unconfirmed_landing_first_seen.json")] = (
        json.dumps({"ac48a769": 0})
    )
    # Force the agent's merge-truth oracle to report the PR still unmerged.
    agent._host_or_git_pr_merged = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    # Isolate rule 14: silence the legacy combiner and the other git-touching
    # rules, and force the SHA-ancestor oracle to report "not an ancestor".
    with (
        patch("maestro.audit_checks.run_all_checks", return_value=[]),
        patch(
            "maestro.audit_checks_phantom.is_branch_merged_into_main",
            return_value=False,
        ),
    ):
        agent.config.audit_impact_review_enabled = False
        agent.config.orphaned_cure_enabled = False
        agent.config.phantom_escalation_enabled = False
        await agent._audit_self_model(iteration=5)

    updated = agent.consciousness.load_self_model()
    assert any(
        q.startswith("[audit] unconfirmed_landing:")
        for q in updated.open_questions
    )
    assert updated.next_task_directive is not None
    assert updated.next_task_directive.startswith(
        "Address audit finding [unconfirmed_landing]: ",
    )
    entries = agent.journal.read_recent_events(20)
    assert any(
        isinstance(e.content, str)
        and "[unconfirmed_landing]" in e.content
        and "#2638" in e.content
        for e in entries
    )


@pytest.mark.asyncio
async def test_audit_self_model_unconfirmed_landing_disabled(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """With ``unconfirmed_landing_enabled=False`` the rule never runs even when
    an aged-unlanded handoff exists — the oracle is untouched."""
    agent = make_agent()
    agent.config.unconfirmed_landing_enabled = False
    agent.config.audit_impact_review_enabled = False
    agent.config.orphaned_cure_enabled = False
    agent.config.phantom_escalation_enabled = False
    agent.memory.record_task(_unconfirmed_landing_task())
    mem_fs[str(agent.config.memory_dir / "unconfirmed_landing_first_seen.json")] = (
        json.dumps({"ac48a769": 0})
    )
    agent._host_or_git_pr_merged = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    with (
        patch("maestro.audit_checks.run_all_checks", return_value=[]),
        patch(
            "maestro.audit_checks_phantom.is_branch_merged_into_main",
            return_value=False,
        ) as mock_ancestor,
    ):
        await agent._audit_self_model(iteration=5)

    updated = agent.consciousness.load_self_model()
    assert not any(
        q.startswith("[audit] unconfirmed_landing:")
        for q in updated.open_questions
    )
    assert updated.next_task_directive is None
    mock_ancestor.assert_not_called()


def _silence_unconfirmed_landing_siblings(agent) -> None:  # type: ignore[no-untyped-def]
    """Silence every audit rule but rule 14 for the forced-arm tests.

    The rule-14 forced arm is nested under its own master switch and reads the
    graced journal findings, so ``unconfirmed_landing_enabled`` must stay on —
    ``_silence_git_audit_rules`` would turn it off. Disable only the siblings.
    """
    agent.config.audit_impact_review_enabled = False
    agent.config.orphaned_cure_enabled = False
    agent.config.phantom_escalation_enabled = False


@pytest.mark.asyncio
async def test_unconfirmed_landing_forces_handoff(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """With ``unconfirmed_landing_forced_enabled`` on, a past-grace rule-14
    finding records a ``pr_number``-keyed standdown and delivers exactly one
    merge-or-close message referencing the cited PR."""
    from maestro.escalation_bridge import has_standdown

    agent = make_agent()
    agent.config.unconfirmed_landing_forced_enabled = True
    _silence_unconfirmed_landing_siblings(agent)
    agent.memory.record_task(_unconfirmed_landing_task())
    # Pre-seed the first-seen sidecar so the handoff reads as aged (5 - 0 >= 3).
    mem_fs[
        str(agent.config.memory_dir / "unconfirmed_landing_first_seen.json")
    ] = json.dumps({"ac48a769": 0})
    agent._host_or_git_pr_merged = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )

    with (
        patch("maestro.audit_checks.run_all_checks", return_value=[]),
        patch(
            "maestro.audit_checks_phantom.is_branch_merged_into_main",
            return_value=False,
        ),
    ):
        await agent._audit_self_model(iteration=5)

    assert has_standdown(agent.escalation_standdowns, 2638) is True
    delivered = _handoff_messages(agent, 2638)
    assert len(delivered) == 1
    assert "#2638" in delivered[0].content
    # Actuation telemetry (090): one ``handoff`` row grounded on the PR.
    acts = _narration_activation_rows(
        agent, mem_fs, gate="unconfirmed_landing_forced",
    )
    assert len(acts) == 1
    assert acts[0]["action"] == "handoff"
    assert acts[0]["detail"] == "pr=#2638"


@pytest.mark.asyncio
async def test_unconfirmed_landing_forced_disabled_writes_no_standdown(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """With the forcing flag off (default), an aged-unlanded handoff records no
    standdown and delivers no message — the journal arm still surfaces the
    finding as an ``[audit]`` question (unchanged)."""
    from maestro.escalation_bridge import has_standdown

    agent = make_agent()
    agent.config.unconfirmed_landing_forced_enabled = False
    _silence_unconfirmed_landing_siblings(agent)
    agent.memory.record_task(_unconfirmed_landing_task())
    mem_fs[
        str(agent.config.memory_dir / "unconfirmed_landing_first_seen.json")
    ] = json.dumps({"ac48a769": 0})
    agent._host_or_git_pr_merged = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )

    with (
        patch("maestro.audit_checks.run_all_checks", return_value=[]),
        patch(
            "maestro.audit_checks_phantom.is_branch_merged_into_main",
            return_value=False,
        ),
    ):
        await agent._audit_self_model(iteration=5)

    assert has_standdown(agent.escalation_standdowns, 2638) is False
    assert _handoff_messages(agent, 2638) == []
    # Journal arm unchanged: the finding still surfaced as an [audit] question.
    updated = agent.consciousness.load_self_model()
    assert any(
        q.startswith("[audit] unconfirmed_landing:")
        for q in updated.open_questions
    )
    # No delivery → no actuation telemetry row (flag off).
    assert _narration_activation_rows(
        agent, mem_fs, gate="unconfirmed_landing_forced",
    ) == []


@pytest.mark.asyncio
async def test_unconfirmed_landing_forced_idempotent(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A second audit cycle re-delivers nothing — the standdown is durable per
    PR, so ``deliver_handoff`` is a no-op on the still-unlanded PR."""
    agent = make_agent()
    agent.config.unconfirmed_landing_forced_enabled = True
    _silence_unconfirmed_landing_siblings(agent)
    agent.memory.record_task(_unconfirmed_landing_task())
    mem_fs[
        str(agent.config.memory_dir / "unconfirmed_landing_first_seen.json")
    ] = json.dumps({"ac48a769": 0})
    agent._host_or_git_pr_merged = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )

    with (
        patch("maestro.audit_checks.run_all_checks", return_value=[]),
        patch(
            "maestro.audit_checks_phantom.is_branch_merged_into_main",
            return_value=False,
        ),
    ):
        await agent._audit_self_model(iteration=5)
        await agent._audit_self_model(iteration=6)

    assert len(_handoff_messages(agent, 2638)) == 1
    # Idempotent: exactly one ``handoff`` telemetry row across both cycles —
    # the second delivery is a no-op (``deliver_handoff`` returned ``False``),
    # which must not add a row.
    acts = _narration_activation_rows(
        agent, mem_fs, gate="unconfirmed_landing_forced",
    )
    assert len(acts) == 1
    assert acts[0]["action"] == "handoff"


@pytest.mark.asyncio
async def test_unconfirmed_landing_forced_within_grace_no_delivery(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """The forced arm reads the graced journal findings, so a within-grace
    handoff (SHA first-seen this cycle, age 0 < grace) produces no finding and
    therefore no delivery."""
    from maestro.escalation_bridge import has_standdown

    agent = make_agent()
    agent.config.unconfirmed_landing_forced_enabled = True
    _silence_unconfirmed_landing_siblings(agent)
    agent.memory.record_task(_unconfirmed_landing_task())
    # No sidecar pre-seed: the SHA is first-seen this cycle → age 0 < grace (3).
    agent._host_or_git_pr_merged = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )

    with (
        patch("maestro.audit_checks.run_all_checks", return_value=[]),
        patch(
            "maestro.audit_checks_phantom.is_branch_merged_into_main",
            return_value=False,
        ),
    ):
        await agent._audit_self_model(iteration=1)

    assert has_standdown(agent.escalation_standdowns, 2638) is False
    assert _handoff_messages(agent, 2638) == []


def _sha_only_handoff_task(
    sha: str = "ac48a769",
    task_id: str = "t_sha_only",
) -> TaskRecord:
    """A PR-less ``partial`` handoff: an own committed SHA handed off on-branch,
    with no ``#NNNN`` anywhere — a non-remediation goal and a summary citing no
    PR. The detector admits it PR-less; the forced arm routes it to the
    fire-or-close directive rather than ``deliver_handoff``."""
    return TaskRecord(
        task_id=task_id,
        goal="Investigate the flaky gate",
        plan=[],
        outcome="partial",  # type: ignore[arg-type]
        result_summary=f"Resolved on branch; committed {sha}.",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-06-01T00:00:00Z",
    )


def _all_handoff_messages(agent):  # type: ignore[no-untyped-def]
    """Every merge-or-close handoff message the agent delivered, any PR."""
    from maestro.messaging import MessageStore

    return [
        m for m in MessageStore(agent.config.memory_dir).get_recent()
        if m.message_id.startswith("handoff-")
    ]


@pytest.mark.asyncio
async def test_unconfirmed_landing_forces_prless_directive(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """With the forcing flag on, a past-grace PR-less finding (no #NNNN) writes a
    fire-or-close ``next_task_directive`` naming the committed SHA into an empty
    slot — pre-empting the generic audit→directive bridge — and delivers no
    standdown and no handoff message (the PR-less arm never calls
    ``deliver_handoff``)."""
    from maestro.outcome_grounding import _SELF_COMMITTED_FIRE_OR_CLOSE_DIRECTIVE

    agent = make_agent()
    agent.config.unconfirmed_landing_forced_enabled = True
    _silence_unconfirmed_landing_siblings(agent)
    agent.memory.record_task(_sha_only_handoff_task())
    # Pre-seed the first-seen sidecar so the handoff reads as aged (5 - 0 >= 3).
    mem_fs[
        str(agent.config.memory_dir / "unconfirmed_landing_first_seen.json")
    ] = json.dumps({"ac48a769": 0})
    agent._host_or_git_pr_merged = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    with (
        patch("maestro.audit_checks.run_all_checks", return_value=[]),
        patch(
            "maestro.audit_checks_phantom.is_branch_merged_into_main",
            return_value=False,
        ),
    ):
        await agent._audit_self_model(iteration=5)

    updated = agent.consciousness.load_self_model()
    assert updated.next_task_directive == (
        _SELF_COMMITTED_FIRE_OR_CLOSE_DIRECTIVE.format(sha="ac48a769")
    )
    assert "ac48a769" in updated.next_task_directive
    # PR-less arm delivers nothing: no handoff message, no standdown ledger.
    assert _all_handoff_messages(agent) == []
    assert (
        str(agent.config.memory_dir / "escalation_handoffs.json") not in mem_fs
    )
    # Actuation telemetry (090): one ``directive_set`` row (empty slot filled),
    # grounded on the committed SHA.
    acts = _narration_activation_rows(agent, mem_fs, gate="rule_14_pr_less")
    assert len(acts) == 1
    assert acts[0]["action"] == "directive_set"
    assert acts[0]["detail"] == "sha=ac48a769"


# ──────────────────────────────────────────────────────────────────────
# Dropped fire-or-close directive ledger (proposal 090) — the rule-14
# PR-less write-time writer routes through ``directive_ledger.offer_directive``.
# ──────────────────────────────────────────────────────────────────────


def _dropped_rows(agent, mem_fs):  # type: ignore[no-untyped-def]
    """The dropped-directive ledger rows currently persisted for *agent*."""
    raw = mem_fs.get(
        str(agent.config.memory_dir / "dropped_directives.jsonl"),
    )
    if not raw:
        return []
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _narration_activation_rows(agent, mem_fs, gate=None):  # type: ignore[no-untyped-def]
    """Narration→action gate-activation rows persisted for *agent* (proposal 090).

    When *gate* is given, only rows whose ``gate`` matches are returned.
    """
    raw = mem_fs.get(
        str(agent.config.memory_dir / "gate_activations.jsonl"),
    )
    if not raw:
        return []
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if gate is not None:
        rows = [r for r in rows if r.get("gate") == gate]
    return rows


@pytest.mark.asyncio
async def test_audit_prless_finding_ledgers_dropped_directive(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Rule-14 PR-less arm: with the forcing flag on, an occupied directive slot
    and the replay flag on, the fire-or-close directive is ledgered (grounded on
    the finding's SHA) instead of being silently discarded."""
    agent = make_agent()
    agent.config.unconfirmed_landing_forced_enabled = True
    agent.config.dropped_directive_replay_enabled = True
    _silence_unconfirmed_landing_siblings(agent)
    agent.memory.record_task(_sha_only_handoff_task())
    mem_fs[
        str(agent.config.memory_dir / "unconfirmed_landing_first_seen.json")
    ] = json.dumps({"ac48a769": 0})
    agent._host_or_git_pr_merged = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )

    model = agent.consciousness.load_self_model()
    model.next_task_directive = "already set"
    agent.consciousness.save_self_model(model)

    with (
        patch("maestro.audit_checks.run_all_checks", return_value=[]),
        patch(
            "maestro.audit_checks_phantom.is_branch_merged_into_main",
            return_value=False,
        ),
    ):
        await agent._audit_self_model(iteration=5)

    updated = agent.consciousness.load_self_model()
    assert updated.next_task_directive == "already set"
    rows = _dropped_rows(agent, mem_fs)
    assert len(rows) == 1
    assert rows[0]["source"] == "rule_14_pr_less"
    assert rows[0]["shas"] == ["ac48a769"]
    assert rows[0]["pr_numbers"] == []
    # Actuation telemetry (090): one ``dropped`` row grounded on the SHA.
    acts = _narration_activation_rows(agent, mem_fs, gate="rule_14_pr_less")
    assert len(acts) == 1
    assert acts[0]["action"] == "dropped"
    assert acts[0]["detail"] == "sha=ac48a769"


@pytest.mark.asyncio
async def test_audit_prless_finding_flag_off_drops_silently(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Rule-14 PR-less arm with the replay flag off: the directive is discarded
    into the occupied slot exactly as today — nothing ledgered."""
    agent = make_agent()
    agent.config.unconfirmed_landing_forced_enabled = True
    agent.config.dropped_directive_replay_enabled = False
    _silence_unconfirmed_landing_siblings(agent)
    agent.memory.record_task(_sha_only_handoff_task())
    mem_fs[
        str(agent.config.memory_dir / "unconfirmed_landing_first_seen.json")
    ] = json.dumps({"ac48a769": 0})
    agent._host_or_git_pr_merged = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )

    model = agent.consciousness.load_self_model()
    model.next_task_directive = "already set"
    agent.consciousness.save_self_model(model)

    with (
        patch("maestro.audit_checks.run_all_checks", return_value=[]),
        patch(
            "maestro.audit_checks_phantom.is_branch_merged_into_main",
            return_value=False,
        ),
    ):
        await agent._audit_self_model(iteration=5)

    updated = agent.consciousness.load_self_model()
    assert updated.next_task_directive == "already set"
    assert _dropped_rows(agent, mem_fs) == []


@pytest.mark.asyncio
async def test_unconfirmed_landing_prless_directive_respects_occupied_slot(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """With the one-shot directive slot already occupied, the PR-less forced arm
    leaves it untouched — fill-if-empty semantics, the same as the write-time
    self-committed bind."""
    agent = make_agent()
    agent.config.unconfirmed_landing_forced_enabled = True
    _silence_unconfirmed_landing_siblings(agent)
    agent.memory.record_task(_sha_only_handoff_task())
    mem_fs[
        str(agent.config.memory_dir / "unconfirmed_landing_first_seen.json")
    ] = json.dumps({"ac48a769": 0})
    agent._host_or_git_pr_merged = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )

    model = agent.consciousness.load_self_model()
    model.next_task_directive = "Existing directive — do not clobber."
    agent.consciousness.save_self_model(model)

    with (
        patch("maestro.audit_checks.run_all_checks", return_value=[]),
        patch(
            "maestro.audit_checks_phantom.is_branch_merged_into_main",
            return_value=False,
        ),
    ):
        await agent._audit_self_model(iteration=5)

    updated = agent.consciousness.load_self_model()
    assert updated.next_task_directive == "Existing directive — do not clobber."
    assert _all_handoff_messages(agent) == []


@pytest.mark.asyncio
async def test_unconfirmed_landing_prless_forced_disabled_no_directive(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """With the forcing flag off (default), a PR-less finding writes no
    fire-or-close directive — but the always-on journal arm still surfaces it as
    an ``[audit]`` question, and the generic audit→directive bridge (not the
    forced arm) fills the empty slot."""
    agent = make_agent()
    agent.config.unconfirmed_landing_forced_enabled = False
    _silence_unconfirmed_landing_siblings(agent)
    agent.memory.record_task(_sha_only_handoff_task())
    mem_fs[
        str(agent.config.memory_dir / "unconfirmed_landing_first_seen.json")
    ] = json.dumps({"ac48a769": 0})
    agent._host_or_git_pr_merged = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    with (
        patch("maestro.audit_checks.run_all_checks", return_value=[]),
        patch(
            "maestro.audit_checks_phantom.is_branch_merged_into_main",
            return_value=False,
        ),
    ):
        await agent._audit_self_model(iteration=5)

    updated = agent.consciousness.load_self_model()
    assert any(
        q.startswith("[audit] unconfirmed_landing:")
        for q in updated.open_questions
    )
    # No forced fire-or-close directive; the generic bridge fills the slot.
    directive = updated.next_task_directive or ""
    assert not directive.startswith("Fire: own commit")
    assert directive.startswith("Address audit finding [unconfirmed_landing]:")


# ──────────────────────────────────────────────────────────────────────
# Ungrounded-question-closure rule 16 (proposal 088 Phase 1)
# ──────────────────────────────────────────────────────────────────────


def _ungrounded_closure_resolution():  # type: ignore[no-untyped-def]
    """A removed open question whose only probe failed (ungrounded closure)."""
    from maestro.memory import QuestionResolution

    return QuestionResolution(
        resolution_id="r-ungrounded",
        iteration=1,
        timestamp="2026-06-21T00:00:00+00:00",
        question="Can I land a cure unaided?",
        task_id="t_fail",
        outcome="failure",  # type: ignore[arg-type]
        disposition="removed",  # type: ignore[arg-type]
        finding="probe failed",
    )


def _silence_git_audit_rules(agent) -> None:  # type: ignore[no-untyped-def]
    """Disable the git-keyed audit rules dispatched alongside the rules under test.

    The default-off *forced* disposition (``unconfirmed_landing_forced_enabled``)
    is intentionally left untouched: it ships dark, and the forced-path tests
    enable it explicitly *after* calling this helper.
    """
    agent.config.audit_impact_review_enabled = False
    agent.config.orphaned_cure_enabled = False
    agent.config.phantom_escalation_enabled = False
    agent.config.unconfirmed_landing_enabled = False
    # Rule 22 (green_ready_handoff) ships default-*on* (its veto-proof handoff
    # lever must not depend on intention), so — unlike the default-off rules
    # above — it is silenced here explicitly so the rules-under-test do not also
    # trip an unrelated merge-ready-PR handoff.
    agent.config.green_ready_handoff_enabled = False


@pytest.mark.asyncio
async def test_audit_self_model_surfaces_ungrounded_closure(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """With the flag on, a removed-question-on-failed-probe row surfaces a
    ``contradicted_claim`` finding and feeds the directive bridge."""
    agent = make_agent()
    agent.config.audit_ungrounded_closure_enabled = True
    _silence_git_audit_rules(agent)
    agent.memory.append_question_resolution(_ungrounded_closure_resolution())

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        await agent._audit_self_model(iteration=2)

    updated = agent.consciousness.load_self_model()
    assert any(
        q.startswith("[audit] contradicted_claim:")
        and "was closed (removed)" in q
        for q in updated.open_questions
    )
    assert updated.next_task_directive is not None


@pytest.mark.asyncio
async def test_audit_self_model_ungrounded_closure_disabled(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """With the flag off, the rule never fires even on an ungrounded removal."""
    agent = make_agent()
    agent.config.audit_ungrounded_closure_enabled = False
    _silence_git_audit_rules(agent)
    agent.memory.append_question_resolution(_ungrounded_closure_resolution())

    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        await agent._audit_self_model(iteration=2)

    updated = agent.consciousness.load_self_model()
    assert not any("was closed (removed)" in q for q in updated.open_questions)
    assert updated.next_task_directive is None


# ──────────────────────────────────────────────────────────────────────
# Reality-refutation actuator (proposal 088 Phase 2)
# ──────────────────────────────────────────────────────────────────────


def _refutation_messages(agent):  # type: ignore[no-untyped-def]
    """Reality-refutation messages delivered under the agent's data dir."""
    from maestro.messaging import MessageStore

    return [
        m for m in MessageStore(agent.config.memory_dir).get_recent()
        if m.message_id == "reality-refutation-4242"
    ]


@pytest.mark.asyncio
async def test_audit_self_model_reality_refutation_delivers(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Crossing the threshold with a live ``[audit refuted]`` entry and a
    git-unmerged PR delivers exactly one message and records one standdown."""
    from maestro.escalation_bridge import has_standdown

    agent = make_agent()
    agent.config.audit_reality_refutation_enabled = True
    agent.config.audit_reality_refutation_threshold = 3
    _silence_git_audit_rules(agent)
    # The actuator reads merge state through the ``is_pr_settled`` chokepoint
    # (proposal 087): an unmerged PR is not ``branch_landed``, so it escalates.
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=4242, settled=False, state="OPEN",
            branch_landed=False, source="host_api", observed_at=0.0,
        ),
    )

    model = agent.consciousness.load_self_model()
    model.open_questions = [
        "[audit refuted] unconfirmed_own_landing: PR #4242 is resolved",
    ]
    agent.consciousness.save_self_model(model)

    with (
        patch("maestro.audit_checks.run_all_checks", return_value=[]),
        patch(
            "maestro.loop.self_audit.measure_reality_refutation",
            return_value={("unconfirmed_own_landing", 4242): 3},
        ),
    ):
        await agent._audit_self_model(iteration=5)

    delivered = _refutation_messages(agent)
    assert len(delivered) == 1
    assert "#4242" in delivered[0].content
    assert has_standdown(agent.escalation_standdowns, 4242) is True


@pytest.mark.asyncio
async def test_audit_self_model_reality_refutation_idempotent(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A second cycle re-delivers nothing — the standdown is durable per PR."""
    agent = make_agent()
    agent.config.audit_reality_refutation_enabled = True
    agent.config.audit_reality_refutation_threshold = 3
    _silence_git_audit_rules(agent)
    # Unmerged (not ``branch_landed``) through the ``is_pr_settled`` chokepoint.
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=4242, settled=False, state="OPEN",
            branch_landed=False, source="host_api", observed_at=0.0,
        ),
    )

    model = agent.consciousness.load_self_model()
    model.open_questions = [
        "[audit refuted] unconfirmed_own_landing: PR #4242 is resolved",
    ]
    agent.consciousness.save_self_model(model)

    with (
        patch("maestro.audit_checks.run_all_checks", return_value=[]),
        patch(
            "maestro.loop.self_audit.measure_reality_refutation",
            return_value={("unconfirmed_own_landing", 4242): 3},
        ),
    ):
        await agent._audit_self_model(iteration=5)
        await agent._audit_self_model(iteration=6)

    assert len(_refutation_messages(agent)) == 1


@pytest.mark.asyncio
async def test_audit_self_model_reality_refutation_skips_when_merged(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A git result showing the PR merged suppresses the escalation."""
    agent = make_agent()
    agent.config.audit_reality_refutation_enabled = True
    agent.config.audit_reality_refutation_threshold = 3
    _silence_git_audit_rules(agent)
    # Merged (``branch_landed``) through the ``is_pr_settled`` chokepoint
    # (proposal 087) suppresses the escalation.
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=4242, settled=True, state="MERGED",
            branch_landed=True, source="host_api", observed_at=0.0,
        ),
    )

    model = agent.consciousness.load_self_model()
    model.open_questions = [
        "[audit refuted] unconfirmed_own_landing: PR #4242 is resolved",
    ]
    agent.consciousness.save_self_model(model)

    with (
        patch("maestro.audit_checks.run_all_checks", return_value=[]),
        patch(
            "maestro.loop.self_audit.measure_reality_refutation",
            return_value={("unconfirmed_own_landing", 4242): 3},
        ),
    ):
        await agent._audit_self_model(iteration=5)

    assert _refutation_messages(agent) == []


@pytest.mark.asyncio
async def test_audit_self_model_reality_refutation_disabled(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """With the flag off the sensor is never consulted and nothing is delivered."""
    agent = make_agent()
    agent.config.audit_reality_refutation_enabled = False
    _silence_git_audit_rules(agent)

    model = agent.consciousness.load_self_model()
    model.open_questions = [
        "[audit refuted] unconfirmed_own_landing: PR #4242 is resolved",
    ]
    agent.consciousness.save_self_model(model)

    measure = MagicMock(return_value={("unconfirmed_own_landing", 4242): 9})
    with (
        patch("maestro.audit_checks.run_all_checks", return_value=[]),
        patch("maestro.loop.self_audit.measure_reality_refutation", measure),
    ):
        await agent._audit_self_model(iteration=5)

    measure.assert_not_called()
    assert _refutation_messages(agent) == []


# ──────────────────────────────────────────────────────────────────────
# Refutation-accountability actuator (proposal 129 Phase C)
# ──────────────────────────────────────────────────────────────────────

_RA_REFUTED = "[audit refuted] unconfirmed_landing: PR #4242 actually landed"
_RA_CANONICAL = (
    "[audit] unconfirmed_landing: Own PR #4242 is not an ancestor of origin/main."
)


@pytest.mark.asyncio
async def test_audit_self_model_refutation_accountability_disabled(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Flag off: the sensor is never consulted, open_questions and the gate
    trace are untouched (byte-identical off path)."""
    from maestro.audit_checks import measure_gate_triggers

    agent = make_agent()
    agent.config.audit_refutation_accountability_enabled = False
    _silence_git_audit_rules(agent)

    model = agent.consciousness.load_self_model()
    model.open_questions = [_RA_REFUTED]
    agent.consciousness.save_self_model(model)

    sensor = MagicMock(return_value=[])
    with (
        patch("maestro.audit_checks.run_all_checks", return_value=[]),
        patch("maestro.audit_checks.measure_refutation_rejections", sensor),
    ):
        await agent._audit_self_model(iteration=5)

    sensor.assert_not_called()
    after = agent.consciousness.load_self_model().open_questions
    assert after == [_RA_REFUTED]
    assert "refutation_accountability" not in measure_gate_triggers(
        agent.gate_activations,
    )


@pytest.mark.asyncio
async def test_audit_self_model_refutation_accountability_drops_duplicate(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on: a restore whose canonical string is already present this cycle
    drops the refuted entry (no duplicate burns an audit slot), saves the model,
    and writes one journal entry plus one gate row."""
    from maestro.audit_checks import RefutationRejection, measure_gate_triggers

    agent = make_agent()
    agent.config.audit_refutation_accountability_enabled = True
    agent.config.audit_refutation_rejection_cycles = 3
    _silence_git_audit_rules(agent)

    model = agent.consciousness.load_self_model()
    # The re-firing finding appended the canonical string earlier this cycle;
    # the stale refutation from a prior cycle still sits beside it.
    model.open_questions = [_RA_REFUTED, _RA_CANONICAL]
    agent.consciousness.save_self_model(model)

    rejection = RefutationRejection(
        question=_RA_REFUTED,
        rule="unconfirmed_own_landing",
        target="#4242",
        cycles=3,
        restored_question=_RA_CANONICAL,
    )
    with (
        patch("maestro.audit_checks.run_all_checks", return_value=[]),
        patch(
            "maestro.audit_checks.measure_refutation_rejections",
            return_value=[rejection],
        ),
    ):
        await agent._audit_self_model(iteration=5)

    after = agent.consciousness.load_self_model().open_questions
    assert _RA_REFUTED not in after
    assert after.count(_RA_CANONICAL) == 1
    triggers = measure_gate_triggers(agent.gate_activations)
    assert triggers.get("refutation_accountability") == 1
    thoughts = [e.content for e in agent.journal.read_recent_events(10)]
    assert any(
        "unconfirmed_own_landing" in t and "#4242" in t for t in thoughts
    )


@pytest.mark.asyncio
async def test_audit_self_model_refutation_accountability_drops_long_duplicate(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on: a canonical string past ``MAX_ITEM_CHARS`` still matches the
    truncated entry the upsert stored, so no second copy burns an audit slot."""
    from maestro.audit_checks import RefutationRejection
    from maestro.consciousness import Consciousness

    agent = make_agent()
    agent.config.audit_refutation_accountability_enabled = True
    agent.config.audit_refutation_rejection_cycles = 3
    _silence_git_audit_rules(agent)

    long_canonical = (
        "[audit] banked_owned_act_reperformance_extra: " + "x" * 160
    )
    stored = Consciousness._truncate_item(
        long_canonical, Consciousness.MAX_ITEM_CHARS,
    )
    assert stored != long_canonical
    model = agent.consciousness.load_self_model()
    model.open_questions = [_RA_REFUTED, stored]
    agent.consciousness.save_self_model(model)

    rejection = RefutationRejection(
        question=_RA_REFUTED,
        rule="unconfirmed_own_landing",
        target="#4242",
        cycles=3,
        restored_question=long_canonical,
    )
    with (
        patch("maestro.audit_checks.run_all_checks", return_value=[]),
        patch(
            "maestro.audit_checks.measure_refutation_rejections",
            return_value=[rejection],
        ),
    ):
        await agent._audit_self_model(iteration=5)

    after = agent.consciousness.load_self_model().open_questions
    assert _RA_REFUTED not in after
    assert after.count(stored) == 1


@pytest.mark.asyncio
async def test_audit_self_model_refutation_accountability_swaps_when_absent(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on: when the canonical string is absent (e.g. evicted under the audit
    sub-cap), the refuted entry is swapped for the restored canonical form."""
    from maestro.audit_checks import RefutationRejection

    agent = make_agent()
    agent.config.audit_refutation_accountability_enabled = True
    _silence_git_audit_rules(agent)

    model = agent.consciousness.load_self_model()
    model.open_questions = [_RA_REFUTED]  # canonical NOT present
    agent.consciousness.save_self_model(model)

    rejection = RefutationRejection(
        question=_RA_REFUTED,
        rule="unconfirmed_own_landing",
        target="#4242",
        cycles=4,
        restored_question=_RA_CANONICAL,
    )
    with (
        patch("maestro.audit_checks.run_all_checks", return_value=[]),
        patch(
            "maestro.audit_checks.measure_refutation_rejections",
            return_value=[rejection],
        ),
    ):
        await agent._audit_self_model(iteration=5)

    after = agent.consciousness.load_self_model().open_questions
    assert _RA_REFUTED not in after
    assert _RA_CANONICAL in after


@pytest.mark.asyncio
async def test_audit_self_model_refutation_accountability_runs_alongside_088(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Both flags on: the 129 restore does not preempt the 088 escalation.

    Regression — the 129 actuator rebinds ``open_questions`` (dropping the
    ``[audit refuted]`` entry) before the 088 reality-refutation actuator reads
    it. Keying 088 off a pre-restore snapshot keeps the two additive: for a
    mechanical_fact PR both flags cover, 088 still delivers its merge-or-close
    message and standdown *alongside* the 129 restore, not in place of it. Were
    088 to read the mutated list it would see no refuted entry for #4242 and
    stay silent.
    """
    from maestro.audit_checks import RefutationRejection
    from maestro.escalation_bridge import has_standdown

    agent = make_agent()
    agent.config.audit_refutation_accountability_enabled = True
    agent.config.audit_refutation_rejection_cycles = 3
    agent.config.audit_reality_refutation_enabled = True
    agent.config.audit_reality_refutation_threshold = 3
    _silence_git_audit_rules(agent)
    # Unmerged (not ``branch_landed``) through the ``is_pr_settled`` chokepoint,
    # so the 088 actuator escalates.
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=4242, settled=False, state="OPEN",
            branch_landed=False, source="host_api", observed_at=0.0,
        ),
    )

    model = agent.consciousness.load_self_model()
    model.open_questions = [_RA_REFUTED]
    agent.consciousness.save_self_model(model)

    rejection = RefutationRejection(
        question=_RA_REFUTED,
        rule="unconfirmed_own_landing",
        target="#4242",
        cycles=3,
        restored_question=_RA_CANONICAL,
    )
    with (
        patch("maestro.audit_checks.run_all_checks", return_value=[]),
        patch(
            "maestro.audit_checks.measure_refutation_rejections",
            return_value=[rejection],
        ),
        patch(
            "maestro.loop.self_audit.measure_reality_refutation",
            return_value={("unconfirmed_own_landing", 4242): 3},
        ),
    ):
        await agent._audit_self_model(iteration=5)

    # 088 fired alongside the 129 restore — not preempted by it.
    assert len(_refutation_messages(agent)) == 1
    assert has_standdown(agent.escalation_standdowns, 4242) is True
    # And the 129 restore still landed: the refuted entry became canonical.
    after = agent.consciousness.load_self_model().open_questions
    assert _RA_REFUTED not in after
    assert _RA_CANONICAL in after


# ---------------------------------------------------------------------------
# Obsolete-directive actuator wiring in _audit_self_model
# ---------------------------------------------------------------------------

_DIRECTIVE_PID = "46e575f898de"


def _arm_obsolete_directive(agent, status: str, directive: str) -> None:  # type: ignore[no-untyped-def]
    import dataclasses

    agent.memory.save_evolution_plan(
        dataclasses.replace(_make_plan(_DIRECTIVE_PID), status=status),
    )

    def _set(live) -> None:  # type: ignore[no-untyped-def]
        live.next_task_directive = directive

    agent.consciousness.mutate_self_model(_set)


@pytest.mark.asyncio
async def test_audit_self_model_obsolete_directive_actuator_fires(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on + a directive naming one pending plan retires it, clears the
    directive, and records one OWNED_PRUNE deed and one gate activation."""
    from maestro.deed_ledger import DeedType

    agent = make_agent()
    agent.config.loop_obsolete_directive_actuator_enabled = True
    _arm_obsolete_directive(
        agent, "pending_review",
        f"Obsolete pending plan {_DIRECTIVE_PID} via mark_plan_obsolete.",
    )

    await agent._audit_self_model(iteration=1)

    plan = agent.memory.get_plan(_DIRECTIVE_PID)
    assert plan is not None
    assert plan.status == "obsolete"
    assert agent.consciousness.load_self_model().next_task_directive is None
    deeds = agent.deed_ledger.recent(10)
    assert len(deeds) == 1
    assert deeds[0].deed_type == DeedType.OWNED_PRUNE.value
    assert deeds[0].pr_number is None
    gates = [
        r for r in agent.gate_activations.read_all()
        if r.gate == "obsolete_directive_actuator"
    ]
    assert len(gates) == 1
    assert gates[0].detail == _DIRECTIVE_PID


@pytest.mark.asyncio
async def test_audit_self_model_obsolete_directive_actuator_clears_stale(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A single named plan already off pending_review just clears the slot."""
    agent = make_agent()
    agent.config.loop_obsolete_directive_actuator_enabled = True
    _arm_obsolete_directive(
        agent, "rejected", f"mark_plan_obsolete {_DIRECTIVE_PID}.",
    )

    await agent._audit_self_model(iteration=1)

    assert agent.consciousness.load_self_model().next_task_directive is None
    assert agent.deed_ledger.recent(10) == []
    plan = agent.memory.get_plan(_DIRECTIVE_PID)
    assert plan is not None
    assert plan.status == "rejected"


@pytest.mark.asyncio
async def test_audit_self_model_obsolete_directive_actuator_flag_off(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Flag off leaves the directive and the plan untouched."""
    agent = make_agent()
    agent.config.loop_obsolete_directive_actuator_enabled = False
    directive = f"mark_plan_obsolete {_DIRECTIVE_PID}."
    _arm_obsolete_directive(agent, "pending_review", directive)

    await agent._audit_self_model(iteration=1)

    assert agent.consciousness.load_self_model().next_task_directive == directive
    plan = agent.memory.get_plan(_DIRECTIVE_PID)
    assert plan is not None
    assert plan.status == "pending_review"
    assert agent.deed_ledger.recent(10) == []


@pytest.mark.asyncio
async def test_audit_self_model_obsolete_directive_actuator_noop_paths(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on with an empty slot, or a directive not naming the lever, is inert."""
    agent = make_agent()
    agent.config.loop_obsolete_directive_actuator_enabled = True

    await agent._audit_self_model(iteration=1)
    assert agent.consciousness.load_self_model().next_task_directive is None

    directive = f"Review plan {_DIRECTIVE_PID} before approving."
    _arm_obsolete_directive(agent, "pending_review", directive)
    await agent._audit_self_model(iteration=2)

    assert agent.consciousness.load_self_model().next_task_directive == directive
    plan = agent.memory.get_plan(_DIRECTIVE_PID)
    assert plan is not None
    assert plan.status == "pending_review"


@pytest.mark.asyncio
async def test_audit_self_model_obsolete_directive_actuator_keeps_rewrite(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A directive rewritten concurrently during the fire is preserved."""
    from maestro import lever_capture

    agent = make_agent()
    agent.config.loop_obsolete_directive_actuator_enabled = True
    _arm_obsolete_directive(
        agent, "pending_review", f"mark_plan_obsolete {_DIRECTIVE_PID}.",
    )
    real_fire = lever_capture.fire_directive_named_obsolete

    def _fire_then_rewrite(memory, backend, directive):  # type: ignore[no-untyped-def]
        outcome = real_fire(memory, backend, directive)

        def _rewrite(live) -> None:  # type: ignore[no-untyped-def]
            live.next_task_directive = "Land PR #42 next."

        agent.consciousness.mutate_self_model(_rewrite)
        return outcome

    with patch(
        "maestro.loop.obsolete_directive.fire_directive_named_obsolete",
        side_effect=_fire_then_rewrite,
    ):
        await agent._audit_self_model(iteration=1)

    assert (
        agent.consciousness.load_self_model().next_task_directive
        == "Land PR #42 next."
    )
    plan = agent.memory.get_plan(_DIRECTIVE_PID)
    assert plan is not None
    assert plan.status == "obsolete"
